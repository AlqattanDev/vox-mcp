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


def test_the_player_uses_the_phone_when_one_is_attached(tmp_path: Path, monkeypatch) -> None:
    from voxmcp import remote

    link = PhoneLink()
    _loop, sent = attach(link)
    monkeypatch.setattr(remote, "PHONE", link)
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
