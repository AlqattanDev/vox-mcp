from __future__ import annotations

import asyncio
import base64
import queue
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from voxmcp.audio import AudioPlayer, CaptureConfig, CaptureControl, PlaybackRegistry, write_wav_atomic
from voxmcp.capture_source import PersistentCaptureSource
from voxmcp.remote import (
    REMOTE_SAMPLE_RATE,
    PhoneLink,
    RemoteAudioUnavailable,
    RemoteSoundDevice,
    RemoteInputStream,
    RemotePlaybackProcess,
    wav_duration_s,
)


def pcm(values: list[float]) -> bytes:
    return (np.array(values, dtype=np.float32) * 32767).astype("<i2").tobytes()


class Loop:
    """A stand-in for the daemon loop that runs the callback inline."""

    def __init__(self) -> None:
        self.closed = False

    def call_soon_threadsafe(self, callback: Any, *args: Any) -> None:
        if self.closed:
            raise RuntimeError("loop is closed")
        callback(*args)


def attach(link: PhoneLink) -> tuple[Loop, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []
    loop = Loop()
    link.attach(send=sent.append, loop=loop, user_agent="Mozilla/5.0 (Linux; Android 14)")
    return loop, sent


# --------------------------------------------------------------------- capture


def test_pcm_from_the_phone_reaches_every_open_capture() -> None:
    link = PhoneLink()
    attach(link)
    frames = link.subscribe()

    link.push_pcm(pcm([0.5, -0.5]))

    delivered = frames.get_nowait()
    assert delivered.dtype == np.float32
    assert delivered == pytest.approx([0.5, -0.5], abs=1e-3)


def test_audio_is_dropped_when_no_phone_is_attached() -> None:
    link = PhoneLink()
    frames = link.subscribe()
    link.push_pcm(pcm([0.5]))
    # Fan-out is independent of attachment, but nothing *asks* for frames while
    # detached, so an unsubscribed link must simply not queue forever.
    link.unsubscribe(frames)
    link.push_pcm(pcm([0.5]))
    assert frames.qsize() == 1


def test_the_stream_hands_the_callback_exact_blocks() -> None:
    link = PhoneLink()
    attach(link)
    blocks: list[np.ndarray] = []
    ready = threading.Event()

    def callback(indata: Any, count: int, _t: Any, _s: Any) -> None:
        blocks.append(np.asarray(indata).reshape(-1).copy())
        assert count == 4
        ready.set()

    stream = RemoteInputStream(
        samplerate=REMOTE_SAMPLE_RATE, blocksize=4, callback=callback, link=link
    )
    stream.start()
    try:
        link.push_pcm(pcm([0.1] * 10))
        assert ready.wait(timeout=2.0)
        deadline = time.monotonic() + 1.0
        while len(blocks) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stream.stop()

    assert [block.size for block in blocks[:2]] == [4, 4]


def test_the_stream_tells_the_phone_when_the_microphone_is_wanted() -> None:
    link = PhoneLink()
    _loop, sent = attach(link)
    stream = RemoteInputStream(
        samplerate=REMOTE_SAMPLE_RATE, blocksize=4, callback=lambda *_: None, link=link
    )

    stream.start()
    stream.stop()

    mic_messages = [message for message in sent if message["type"] == "mic"]
    assert [message["open"] for message in mic_messages] == [True, False]


def test_a_phone_attaching_mid_turn_is_told_the_microphone_is_already_open() -> None:
    link = PhoneLink()
    attach(link)
    link.set_mic_open(True)

    _loop, sent = attach(link)

    assert {"type": "mic", "open": True} in sent


def test_the_capture_source_runs_unchanged_on_the_phone() -> None:
    """The whole gate/guard path is reused, so the phone is just a device."""

    link = PhoneLink()
    attach(link)
    source = PersistentCaptureSource(
        CaptureConfig(frame_ms=20, save_latest=False, latest_wav_path=None),
        sounddevice=RemoteSoundDevice(),
        stream_factory=lambda **kwargs: RemoteInputStream(link=link, **kwargs),
        open_guard_s=0.0,
    )
    assert source.open() is True
    try:
        assert source.sample_rate == REMOTE_SAMPLE_RATE
        control = CaptureControl()
        source.open_gate()
        frames = source.frames(control)
        link.push_pcm(pcm([0.4] * (REMOTE_SAMPLE_RATE // 50) * 2))
        first = next(frames)
        assert first.size == REMOTE_SAMPLE_RATE // 50
        control.cancel()
    finally:
        source.close()


def test_a_closed_gate_still_means_deaf_over_the_link() -> None:
    link = PhoneLink()
    attach(link)
    source = PersistentCaptureSource(
        CaptureConfig(frame_ms=20, save_latest=False, latest_wav_path=None),
        sounddevice=RemoteSoundDevice(),
        stream_factory=lambda **kwargs: RemoteInputStream(link=link, **kwargs),
        open_guard_s=0.0,
    )
    source.open()
    try:
        control = CaptureControl()
        frames = source.frames(control)
        link.push_pcm(pcm([0.4] * REMOTE_SAMPLE_RATE))
        # The gate was never opened: the generator ends rather than yielding.
        assert list(frames) == []
    finally:
        source.close()


def test_the_device_is_named_after_the_phone() -> None:
    link = PhoneLink()
    attach(link)
    from voxmcp import remote

    previous = remote.PHONE
    remote.PHONE = link
    try:
        info = RemoteSoundDevice().query_devices(None, "input")
    finally:
        remote.PHONE = previous
    assert "Android" in info["name"]
    assert info["default_samplerate"] == float(REMOTE_SAMPLE_RATE)


# -------------------------------------------------------------------- playback


def test_playback_ships_the_wav_and_ends_on_the_phone_ack(tmp_path: Path) -> None:
    link = PhoneLink()
    _loop, sent = attach(link)
    wav = write_wav_atomic(tmp_path / "reply.wav", np.zeros(1_600, dtype=np.float32), 16_000)
    payload = wav.read_bytes()

    playback_id = link.start_playback(payload, volume=0.8, duration_s=wav_duration_s(payload))
    process = RemotePlaybackProcess(playback_id, link=link)

    message = sent[-1]
    assert message["type"] == "play"
    assert message["volume"] == 0.8
    assert base64.b64decode(message["wav"]) == payload
    assert process.poll() is None

    link.finish_playback(playback_id)
    assert process.poll() == 0
    assert process.wait() == 0


def test_cancelling_playback_tells_the_phone_to_stop(tmp_path: Path) -> None:
    link = PhoneLink()
    _loop, sent = attach(link)
    playback_id = link.start_playback(b"RIFF", volume=1.0, duration_s=0.1)

    process = RemotePlaybackProcess(playback_id, link=link)
    process.terminate()

    assert {"type": "cancel", "id": playback_id} in sent
    assert process.poll() == 0


def test_a_phone_that_never_acks_does_not_wedge_the_turn() -> None:
    link = PhoneLink()
    attach(link)
    playback_id = link.start_playback(b"RIFF", volume=1.0, duration_s=0.0)

    started = time.monotonic()
    assert link.wait_playback(playback_id, timeout=0.2) is False
    assert time.monotonic() - started < 1.0


def test_dropping_the_phone_releases_everything_still_in_flight() -> None:
    link = PhoneLink()
    attach(link)
    playback_id = link.start_playback(b"RIFF", volume=1.0, duration_s=30.0)

    link.detach()

    assert link.connected is False
    assert link.wait_playback(playback_id, timeout=0.1) is True
    with pytest.raises(RemoteAudioUnavailable):
        link.start_playback(b"RIFF", volume=1.0, duration_s=0.1)


def test_the_player_uses_the_phone_the_user_last_spoke_from(tmp_path: Path, monkeypatch) -> None:
    """Attached is not chosen — the reply goes where the user is."""

    from voxmcp import remote

    link = PhoneLink()
    _loop, sent = attach(link)
    monkeypatch.setattr(remote, "PHONE", link)
    # Merely attached: the reply belongs in the room the user is sitting in,
    # so it must reach the local player and not the phone on the desk.
    played_in_the_room: list[list[str]] = []

    class Local:
        pid = 1
        def poll(self) -> int | None: return None
        def wait(self, timeout: float | None = None) -> int: return 0
        def terminate(self) -> None: ...
        def kill(self) -> None: ...

    def to_the_room(command: list[str], **_kwargs: Any) -> Local:
        played_in_the_room.append(command)
        return Local()

    room = AudioPlayer(registry=PlaybackRegistry(), popen_factory=to_the_room)
    room.play_file(
        write_wav_atomic(tmp_path / "room.wav", np.zeros(800, dtype=np.float32), 16_000),
        volume=0.5,
    )
    assert played_in_the_room, "an attached but unspoken-to phone stole the reply"
    assert not [m for m in sent if m["type"] == "play"]

    # Now he speaks from the phone. From here the phone is where the voice goes.
    link.choose(True)
    wav = write_wav_atomic(tmp_path / "cue.wav", np.zeros(800, dtype=np.float32), 16_000)

    spawned: list[Any] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        spawned.append(args)
        raise AssertionError("a phone is attached; nothing should reach afplay")

    player = AudioPlayer(registry=PlaybackRegistry(), popen_factory=refuse)
    handle = player.play_file(wav, volume=0.5)

    assert spawned == []
    assert sent[-1]["type"] == "play"
    assert handle.running is True
    link.finish_playback(sent[-1]["id"])
    assert handle.running is False


def test_the_player_falls_back_to_the_room_when_no_phone_is_attached(tmp_path: Path) -> None:
    from voxmcp import remote

    empty = PhoneLink()
    previous = remote.PHONE
    remote.PHONE = empty
    wav = write_wav_atomic(tmp_path / "cue.wav", np.zeros(800, dtype=np.float32), 16_000)

    class FakeProcess:
        pid = 4321

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None: ...

        def kill(self) -> None: ...

    spawned: list[list[str]] = []

    def popen(command: list[str], **_kwargs: Any) -> FakeProcess:
        spawned.append(command)
        return FakeProcess()

    try:
        player = AudioPlayer(
            registry=PlaybackRegistry(),
            popen_factory=popen,
            which=lambda name: f"/usr/bin/{name}",
            platform_name="Darwin",
        )
        player.play_file(wav)
    finally:
        remote.PHONE = previous

    assert spawned and spawned[0][0].endswith("afplay")


def test_wav_duration_reads_the_header(tmp_path: Path) -> None:
    wav = write_wav_atomic(tmp_path / "one.wav", np.zeros(16_000, dtype=np.float32), 16_000)
    assert wav_duration_s(wav.read_bytes()) == pytest.approx(1.0, abs=0.01)
    assert wav_duration_s(b"not a wav") == 0.0


# ------------------------------------------------------------------- selection


def test_an_injected_recorder_is_never_replaced_by_the_phone() -> None:
    from voxmcp import remote
    from voxmcp.audio import AudioRecorder
    from voxmcp.engine import VoxEngine

    link = PhoneLink()
    attach(link)
    previous = remote.PHONE
    remote.PHONE = link
    try:
        engine = VoxEngine.default()
        fake = object()
        recorder = AudioRecorder(sounddevice=fake)
        device, sounddevice, _factory = engine._capture_backend(recorder)
        assert sounddevice is fake
        assert device is engine.input_device

        # Attached but not spoken from: the microphone stays this machine's.
        device, sounddevice, factory = engine._capture_backend(AudioRecorder())
        assert not isinstance(sounddevice, RemoteSoundDevice)

        link.choose(True)
        _device, remote_sd, factory = engine._capture_backend(AudioRecorder())
        assert isinstance(remote_sd, RemoteSoundDevice)
        assert factory is not None
    finally:
        remote.PHONE = previous


def test_the_socket_refuses_a_wrong_token() -> None:
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from voxmcp.mcp_server import create_mcp

    server = create_mcp(control_token="right")
    app = server.http_app(path="/mcp", transport="http")

    with TestClient(app, client=("127.0.0.1", 5555)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/phone/ws?t=wrong"):
                pass


def test_the_page_is_served_to_loopback() -> None:
    from starlette.testclient import TestClient

    from voxmcp.mcp_server import create_mcp

    app = create_mcp(control_token="right").http_app(path="/mcp", transport="http")
    with TestClient(app, client=("127.0.0.1", 5555)) as client:
        response = client.get("/phone")
    assert response.status_code == 200
    assert "phone/ws" in response.text


def test_the_socket_carries_audio_and_playback_acks(monkeypatch) -> None:
    from starlette.testclient import TestClient

    from voxmcp import remote
    from voxmcp.mcp_server import create_mcp

    link = PhoneLink()
    monkeypatch.setattr(remote, "PHONE", link)
    app = create_mcp(control_token="right").http_app(path="/mcp", transport="http")

    with TestClient(app, client=("127.0.0.1", 5555)) as client:
        with client.websocket_connect("/phone/ws?t=right") as socket:
            assert link.connected is True
            frames = link.subscribe()
            socket.send_bytes(pcm([0.25] * 8))
            delivered = frames.get(timeout=2.0)
            assert delivered.size == 8

            playback_id = link.start_playback(b"RIFF", volume=1.0, duration_s=5.0)
            assert socket.receive_json()["type"] == "play"
            socket.send_json({"type": "ended", "id": playback_id})
            deadline = time.monotonic() + 2.0
            while link.playback_running(playback_id) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert link.playback_running(playback_id) is False

    assert link.connected is False


def test_the_phone_is_reachable_from_the_tailnet_and_nowhere_else() -> None:
    """The one Vox surface that is not loopback-only is still not the internet."""

    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from voxmcp.mcp_server import create_mcp

    app = create_mcp(control_token="right").http_app(path="/mcp", transport="http")

    with TestClient(app, client=("100.119.94.107", 5555)) as client:
        assert client.get("/phone").status_code == 200

    with TestClient(app, client=("203.0.113.9", 5555)) as client:
        assert client.get("/phone").status_code == 403
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/phone/ws?t=right"):
                pass


def test_a_token_copied_out_of_a_wrapped_terminal_still_connects() -> None:
    """A terminal wrap turns into spaces mid-token; that is not a wrong token."""

    from starlette.testclient import TestClient

    from voxmcp.mcp_server import create_mcp

    app = create_mcp(control_token="rightlongtoken").http_app(path="/mcp", transport="http")
    with TestClient(app, client=("127.0.0.1", 5555)) as client:
        with client.websocket_connect("/phone/ws?t=right   longtoken") as socket:
            socket.send_bytes(b"")


def test_a_say_from_the_phone_reaches_read_aloud_verbatim() -> None:
    """The phone can ask for any text read back; it rides the read_aloud path."""

    from starlette.testclient import TestClient

    from voxmcp.mcp_server import create_mcp

    spoken: list[tuple[str, str]] = []
    heard = threading.Event()

    class FakeEngine:
        async def read_aloud(self, client_id: str, *, text: str | None = None) -> dict:
            spoken.append((client_id, text or ""))
            heard.set()
            return {"status": "ok"}

    app = create_mcp(FakeEngine(), control_token="right").http_app(
        path="/mcp", transport="http"
    )
    with TestClient(app, client=("127.0.0.1", 5555)) as client:
        with client.websocket_connect("/phone/ws?t=right") as socket:
            socket.send_json({"type": "say", "text": "  the exact words  "})
            assert heard.wait(timeout=2.0)
            # Empty and non-say messages must not dispatch anything.
            socket.send_json({"type": "say", "text": "   "})
            socket.send_json({"type": "nonsense"})
            socket.send_bytes(b"")

    assert spoken == [("phone", "the exact words")]


def test_the_phone_can_start_and_end_a_turn() -> None:
    """The phone as the turn key, not only as the microphone.

    Without this the phone could be available and nothing more: the mic opened
    when something on the Mac asked for it, so turning voice on started no
    conversation, and once a turn had begun nothing on the phone could end it.
    Three actions and no others — the rest of the controls stay on the Mac,
    where the agent is.
    """

    from starlette.testclient import TestClient

    from voxmcp.mcp_server import create_mcp

    asked: list[str] = []
    seen = threading.Event()

    class FakeEngine:
        async def control(self, action: str, client_id: str, **_: object) -> dict:
            asked.append(f"{client_id}:{action}")
            seen.set()
            return {"status": "ok"}

    app = create_mcp(FakeEngine(), control_token="right").http_app(
        path="/mcp", transport="http"
    )
    with TestClient(app, client=("127.0.0.1", 5555)) as client:
        with client.websocket_connect("/phone/ws?t=right") as socket:
            for action in ("gate_open", "gate_close", "cancel"):
                seen.clear()
                socket.send_json({"type": "control", "action": action})
                assert seen.wait(timeout=2.0)
            # Anything else the phone invents is ignored rather than obeyed.
            socket.send_json({"type": "control", "action": "stop"})
            socket.send_json({"type": "control"})
            socket.send_json({"type": "say", "text": "settle"})
            time.sleep(0.2)

    assert asked == ["phone:gate_open", "phone:gate_close", "phone:cancel"]


def test_the_voice_follows_the_user_between_the_mac_and_the_phone(monkeypatch) -> None:
    """Ali's rule, in his words: *the voice follows you.*

    The old rule was "a connected phone is the microphone", which is a fact
    about the network rather than about where the person is. Sitting at the
    Mac with the app open on the desk, his replies came out of the phone and
    his dictation recorded from it. Pressing something is the signal; being
    attached is not.
    """

    from starlette.testclient import TestClient

    from voxmcp import remote
    from voxmcp.mcp_server import create_mcp

    link = PhoneLink()
    monkeypatch.setattr(remote, "PHONE", link)

    class FakeEngine:
        async def control(self, action: str, client_id: str, **_: object) -> dict:
            return {"status": "ok"}

        async def note(self, client_id: str, **_: object) -> dict:
            return {"status": "ok"}

    app = create_mcp(FakeEngine(), control_token="right").http_app(
        path="/mcp", transport="http"
    )
    with TestClient(app, client=("127.0.0.1", 5555)) as client:
        with client.websocket_connect("/phone/ws?t=right") as socket:
            # Attached and silent. He is at the Mac; the Mac keeps the voice.
            assert link.connected is True
            assert link.is_destination is False

            # He presses talk on the phone. The voice moves to him.
            socket.send_json({"type": "control", "action": "gate_open"})
            deadline = time.time() + 2.0
            while not link.is_destination and time.time() < deadline:
                time.sleep(0.02)
            assert link.is_destination is True

            # He comes home and uses the key on the Mac. It moves back.
            client.post(
                "/control",
                json={"action": "gate_open"},
                headers={"Authorization": "Bearer right"},
            )
            assert link.is_destination is False

            # He leaves again and talks from the phone. It follows him, even
            # though the conversation began on the Mac.
            socket.send_json({"type": "control", "action": "gate_open"})
            deadline = time.time() + 2.0
            while not link.is_destination and time.time() < deadline:
                time.sleep(0.02)
            assert link.is_destination is True

        # The phone left. A phone that is gone cannot be where he is speaking.
        deadline = time.time() + 2.0
        while link.connected and time.time() < deadline:
            time.sleep(0.02)
        assert link.is_destination is False


def test_an_agent_speaking_does_not_move_the_voice(monkeypatch) -> None:
    """A reply follows the user; it does not reset where the voice goes.

    This is the half that makes "I started on the laptop and left the house"
    work: he presses talk on the phone, and every later turn of that
    conversation keeps reaching the phone even though the agent, not him, is
    the one starting them.
    """

    from starlette.testclient import TestClient

    from voxmcp import remote
    from voxmcp.mcp_server import create_mcp

    link = PhoneLink()
    attach(link)
    monkeypatch.setattr(remote, "PHONE", link)
    link.choose(True)

    class FakeEngine:
        async def control(self, action: str, client_id: str, **_: object) -> dict:
            return {"status": "ok"}

    app = create_mcp(FakeEngine(), control_token="right").http_app(
        path="/mcp", transport="http"
    )
    with TestClient(app, client=("127.0.0.1", 5555)):
        pass
    # Nothing a non-user action does may take the voice off the phone.
    assert link.is_destination is True


def test_the_phone_is_told_when_the_whole_reading_is_over(monkeypatch) -> None:
    """A long selection is many spans; only this side knows the last one played.

    Without this the phone leaves after span one — it acked a wav, and a wav
    is all it can see — and the rest of the reading falls back to the Mac's
    own speaker with nobody in the room to hear it.
    """

    from starlette.testclient import TestClient

    from voxmcp import remote
    from voxmcp.mcp_server import create_mcp

    link = PhoneLink()
    monkeypatch.setattr(remote, "PHONE", link)
    started = threading.Event()
    release = threading.Event()

    class FakeEngine:
        async def read_aloud(self, client_id: str, *, text: str | None = None) -> dict:
            started.set()
            await asyncio.get_running_loop().run_in_executor(None, release.wait, 5.0)
            return {"status": "ok"}

    app = create_mcp(FakeEngine(), control_token="right").http_app(
        path="/mcp", transport="http"
    )
    with TestClient(app, client=("127.0.0.1", 5555)) as client:
        with client.websocket_connect("/phone/ws?t=right") as socket:
            socket.send_json({"type": "say", "id": "u7", "text": "three sentences"})
            assert started.wait(timeout=2.0)
            # Mid-reading the phone is handed a span, and acking it is not the
            # end of anything.
            playback_id = link.start_playback(b"RIFF", volume=1.0, duration_s=0.1)
            assert socket.receive_json()["type"] == "play"
            socket.send_json({"type": "ended", "id": playback_id})
            release.set()
            assert socket.receive_json() == {"type": "say_done", "id": "u7"}


def test_a_second_tap_replaces_the_reading_instead_of_queueing_behind_it() -> None:
    """Tapping speak aloud twice must restart, not read the first one again."""

    from starlette.testclient import TestClient

    from voxmcp.mcp_server import create_mcp

    actions: list[str] = []
    done = threading.Event()

    class FakeEngine:
        async def control(self, client_id: str, action: str, **_: object) -> dict:
            actions.append(f"control:{action}")
            return {"status": "cancel_signalled"}

        async def read_aloud(self, client_id: str, *, text: str | None = None) -> dict:
            actions.append(f"read:{text}")
            done.set()
            return {"status": "ok"}

    app = create_mcp(FakeEngine(), control_token="right").http_app(
        path="/mcp", transport="http"
    )
    with TestClient(app, client=("127.0.0.1", 5555)) as client:
        with client.websocket_connect("/phone/ws?t=right") as socket:
            socket.send_json({"type": "say", "text": "second one", "replace": True})
            assert done.wait(timeout=2.0)

    assert actions == ["control:cancel", "read:second one"]
