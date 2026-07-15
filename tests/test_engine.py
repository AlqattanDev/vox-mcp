import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from voxmcp.audio import CaptureResult, CaptureStopReason
from voxmcp.config import VoxConfig
from voxmcp.engine import VoxEngine
from voxmcp.errors import BusyError
from voxmcp.eventlog import JsonlEventLogger
from voxmcp.lease import LeaseManager, OperationGate
from voxmcp.speech import SpeechResult
from voxmcp.state import VoiceStateMachine
from voxmcp.storage import AudioStore


class FakeRecorder:
    def __init__(self, path: Path, *, speech: bool = True):
        self.path = path
        self.speech = speech

    def capture(self, *, device=None, control=None):
        if control and control.cancelled:
            reason = CaptureStopReason.CANCELLED
            speech = False
        else:
            reason = CaptureStopReason.TRAILING_SILENCE if self.speech else CaptureStopReason.ONSET_TIMEOUT
            speech = self.speech
        if speech:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(b"RIFF" + b"0" * 64)
        return CaptureResult(
            samples=np.ones(160, dtype=np.float32) if speech else np.array([], dtype=np.float32),
            sample_rate=16000,
            reason=reason,
            speech_detected=speech,
            elapsed_s=1,
            audio_duration_s=1 if speech else 0,
            speech_duration_s=0.5 if speech else 0,
            trailing_silence_s=1.2 if speech else 0,
            noise_floor_dbfs=-60.0,
            latest_wav_path=self.path if speech else None,
        )


class FakeHandle:
    running = False
    def wait(self): return 0
    def cancel(self): return None


class FakePlayer:
    def play_file(self, *args, **kwargs): return FakeHandle()
    def cancel_all(self): return 0


class FakeSpeech:
    async def synthesize(self, text, destination, **kwargs):
        destination.write_bytes(b"RIFF" + b"1" * 64)
        return SpeechResult("kokoro", 10, path=str(destination))

    async def transcribe(self, path, **kwargs):
        return SpeechResult("whisper.cpp", 20, text="hello world")


class FakeSupervisor:
    async def all_statuses(self): return {}


class FakeCompat:
    pass


def make_engine(tmp_path: Path, *, speech=True):
    config = VoxConfig(state_dir=tmp_path / "state", idle_timeout_seconds=600)
    store = AudioStore(tmp_path / "audio")
    return VoxEngine(
        config=config,
        home=tmp_path,
        state=VoiceStateMachine(snapshot_path=config.snapshot_path, idle_timeout_seconds=600),
        recorder=FakeRecorder(store.latest_stt, speech=speech),
        player=FakePlayer(),
        speech=FakeSpeech(),
        supervisor=FakeSupervisor(),
        store=store,
        logger=JsonlEventLogger(config.event_log_path),
        compatibility=FakeCompat(),
        lease=LeaseManager(ttl_seconds=600),
        gate=OperationGate(),
    )


@pytest.mark.asyncio
async def test_converse_speaks_then_listens_and_returns_idle(tmp_path: Path):
    engine = make_engine(tmp_path)
    result = await engine.converse("claude", "Hi")
    assert result["spoken"]["status"] == "completed"
    assert result["heard"]["transcript"] == "hello world"
    assert result["session"]["state"] == "idle"
    assert result["session"]["microphone_open"] is False


@pytest.mark.asyncio
async def test_no_speech_is_bounded_and_keeps_session(tmp_path: Path):
    engine = make_engine(tmp_path, speech=False)
    result = await engine.listen("claude")
    assert result["status"] == "no_speech"
    assert result["session"]["state"] == "idle"


@pytest.mark.asyncio
async def test_competing_client_gets_busy_without_waiting(tmp_path: Path):
    engine = make_engine(tmp_path)
    await engine.session("claude", "start")
    with pytest.raises(Exception, match="belongs|owned|Audio"):
        await engine.listen("codex")


@pytest.mark.asyncio
async def test_global_stop_releases_a_stale_owner(tmp_path: Path):
    engine = make_engine(tmp_path)
    await engine.session("claude", "start")

    stopped = await engine.session("codex", "stop")

    assert stopped["state"] == "off"
    assert (await engine.lease.status()) == {"owned": False}
    restarted = await engine.session("codex", "start")
    assert restarted["lease"]["owner_id"] == "codex"


@pytest.mark.asyncio
async def test_survey_playback_failure_returns_partial_and_cleans_state(tmp_path: Path):
    class FailingPlayer:
        def play_file(self, *args, **kwargs):
            raise RuntimeError("speaker disappeared")

        def cancel_all(self):
            return 0

    engine = make_engine(tmp_path)
    engine.player = FailingPlayer()

    result = await engine.survey("claude", [{"message": "hello"}])

    assert result["status"] == "partial"
    assert result["completed"] == 0
    assert "speaker disappeared" in result["results"][0]["error"]
    assert (await engine.status())["state"] == "idle"


@pytest.mark.asyncio
async def test_handoff_alias_is_canonical_and_claimable_after_reconnect(tmp_path: Path):
    engine = make_engine(tmp_path)
    await engine.session("mcp:host:claude-code", "start")

    pending = await engine.session(
        "mcp:host:claude-code",
        "handoff",
        target_client_id="codex",
    )

    assert pending["handoff"]["reserved_for"] == "mcp:host:codex"
    claimed = await engine.session("mcp:host:codex", "start")
    assert claimed["lease"]["owner_id"] == "mcp:host:codex"


@pytest.mark.asyncio
async def test_ignored_cancel_does_not_mislabel_noninterruptible_speech(tmp_path: Path):
    class BlockingHandle:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.cancel_calls = 0

        @property
        def running(self):
            return not self.release.is_set()

        def wait(self):
            self.started.set()
            assert self.release.wait(timeout=2)
            return 0

        def cancel(self):
            self.cancel_calls += 1
            self.release.set()

    class BlockingPlayer:
        def __init__(self, handle):
            self.handle = handle

        def play_file(self, *args, **kwargs):
            return self.handle

        def cancel_all(self):
            return 0

    engine = make_engine(tmp_path)
    handle = BlockingHandle()
    engine.player = BlockingPlayer(handle)
    speaking = asyncio.create_task(
        engine.speak("claude", "finish this", interruptible=False)
    )
    assert await asyncio.to_thread(handle.started.wait, 1)

    ignored = await engine.control("claude", "cancel")
    assert ignored["signalled"] is False
    assert handle.cancel_calls == 0
    handle.release.set()

    result = await speaking
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_host_cancellation_keeps_gate_until_recorder_confirms_closed(tmp_path: Path):
    class WedgedRecorder:
        def __init__(self, path: Path):
            self.path = path
            self.entered = threading.Event()
            self.release = threading.Event()
            self.entered_count = 0

        def capture(self, *, device=None, control=None):
            self.entered_count += 1
            self.entered.set()
            assert self.release.wait(timeout=5)
            return CaptureResult(
                samples=np.array([], dtype=np.float32),
                sample_rate=48_000,
                reason=CaptureStopReason.CANCELLED,
                speech_detected=False,
                elapsed_s=1,
                audio_duration_s=0,
                speech_duration_s=0,
                trailing_silence_s=0,
                noise_floor_dbfs=-60,
                latest_wav_path=None,
            )

    engine = make_engine(tmp_path)
    recorder = WedgedRecorder(engine.store.latest_stt)
    engine.recorder = recorder
    listening = asyncio.create_task(engine.listen("claude"))
    assert await asyncio.to_thread(recorder.entered.wait, 1)
    listening.cancel()
    await asyncio.sleep(0.05)

    status = await engine.status()
    assert status["session"]["microphone_open"] is True
    assert "still closing" in status["detail"].lower()
    assert status["operation"]["busy"] is True

    # The queue is what protects the device now: a competing listen waits for
    # the wedged recorder instead of opening the microphone underneath it.
    queued = asyncio.create_task(engine.listen("claude"))
    await asyncio.sleep(0.05)
    assert not queued.done()
    assert recorder.entered_count == 1  # the second turn never reached capture
    assert (await engine.status())["operation"]["queue_depth"] == 1

    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued

    recorder.release.set()
    with pytest.raises(asyncio.CancelledError):
        await listening
    assert (await engine.status())["session"]["microphone_open"] is False


class HoldingHandle:
    """A playback handle that keeps the speakers busy until released."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    @property
    def running(self):
        return not self.release.is_set()

    def wait(self):
        self.started.set()
        self.release.wait(timeout=5)
        return 0

    def cancel(self):
        self.release.set()


class HoldingPlayer:
    def __init__(self, handle):
        self.handle = handle

    def play_file(self, *args, **kwargs):
        return self.handle

    def cancel_all(self):
        return 0


async def _hold_the_microphone(engine):
    """Start a real speak turn and return once it owns the gate."""

    handle = HoldingHandle()
    engine.player = HoldingPlayer(handle)
    task = asyncio.create_task(engine.speak("claude", "holding the mic"))
    assert await asyncio.to_thread(handle.started.wait, 2)
    return task, handle


@pytest.mark.asyncio
async def test_queued_converse_does_not_open_the_microphone(tmp_path: Path):
    engine = make_engine(tmp_path)
    holder, handle = await _hold_the_microphone(engine)

    queued = asyncio.create_task(engine.converse("claude", "second", agent="mobilescape"))
    await asyncio.sleep(0.05)

    status = await engine.status()
    assert status["operation"]["queue_depth"] == 1
    # The whole point: waiting in line must not hold the device open.
    assert status["session"]["microphone_open"] is False
    assert engine.microphone_open is False

    handle.release.set()
    await asyncio.wait_for(holder, timeout=5)
    await asyncio.wait_for(queued, timeout=5)
    assert (await engine.status())["session"]["microphone_open"] is False


@pytest.mark.asyncio
async def test_cancel_drains_queued_turns(tmp_path: Path):
    engine = make_engine(tmp_path)
    holder, handle = await _hold_the_microphone(engine)

    queued = [
        asyncio.create_task(engine.converse("claude", "one", agent="infra")),
        asyncio.create_task(engine.converse("claude", "two", agent="mobilescape")),
    ]
    await asyncio.sleep(0.05)
    assert (await engine.status())["operation"]["queue_depth"] == 2

    result = await engine.control("claude", "cancel")
    assert result["queue_drained"] == 2

    # Neither queued turn is left waiting for a mic that will never come.
    for task in queued:
        with pytest.raises(BusyError):
            await asyncio.wait_for(task, timeout=2)

    handle.release.set()
    await asyncio.gather(holder, return_exceptions=True)  # cancel ends the active turn
    status = await engine.status()
    assert status["operation"]["queue_depth"] == 0
    assert status["operation"]["busy"] is False


@pytest.mark.asyncio
async def test_session_stop_drains_queued_turns(tmp_path: Path):
    engine = make_engine(tmp_path)
    holder, handle = await _hold_the_microphone(engine)

    queued = asyncio.create_task(engine.converse("claude", "queued", agent="infra"))
    await asyncio.sleep(0.05)
    assert (await engine.status())["operation"]["queue_depth"] == 1

    await engine.session("claude", "stop")

    with pytest.raises(BusyError):
        await asyncio.wait_for(queued, timeout=2)

    handle.release.set()
    await asyncio.gather(holder, return_exceptions=True)  # stop ends the active turn
    status = await engine.status()
    assert status["operation"]["queue_depth"] == 0
    assert status["operation"]["busy"] is False
