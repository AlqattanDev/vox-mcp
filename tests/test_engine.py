import asyncio
from dataclasses import replace
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from voxmcp.audio import AudioRecorder, CaptureConfig, CaptureResult, CaptureStopReason
from voxmcp.config import VoxConfig
from voxmcp.engine import VoxEngine
from voxmcp.errors import BusyError, VoxError
from voxmcp.eventlog import JsonlEventLogger
from voxmcp.lease import LeaseManager, OperationGate
from voxmcp.speech import SpeechResult
from voxmcp.state import VoiceStateMachine
from voxmcp.storage import AudioStore


class FakeRecorder:
    def __init__(self, path: Path, *, speech: bool = True):
        self.path = path
        self.speech = speech

    def capture(self, *, device=None, control=None, on_speech_started=None):
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


class CountingSpeech(FakeSpeech):
    def __init__(self) -> None:
        self.spans: list[str] = []

    async def synthesize(self, text, destination, **kwargs):
        self.spans.append(text)
        return await super().synthesize(text, destination, **kwargs)


class BargingRecorder(FakeRecorder):
    """A microphone that hears the user start talking over the agent.

    Onset is announced on the caller's thread exactly as the real recorder
    announces it from its worker thread, then the capture keeps running and
    returns the utterance — the words that triggered it included, which in the
    real recorder is what the pre-roll buffer preserves.
    """

    def __init__(self, path: Path, *, transcript_speech: bool = True, fire: bool = True):
        super().__init__(path, speech=transcript_speech)
        self.fire = fire
        self.captures = 0
        self.fired = threading.Event()

    def capture(self, *, device=None, control=None, on_speech_started=None):
        self.captures += 1
        if self.fire and on_speech_started is not None:
            on_speech_started()
            self.fired.set()
        return super().capture(device=device, control=control)


class ArmedHoldingRecorder(FakeRecorder):
    """An armed capture that stays open for the whole reply, like the real one.

    BargingRecorder returns its capture immediately, which completes the future
    and clears ``_microphone_active`` before a cancel can possibly land — so it
    cannot reproduce the ordering that wedges the session. The real device holds
    the microphone until the control is cancelled, and that is precisely what
    makes the mid-speech return-to-idle illegal.
    """

    def __init__(self, path: Path):
        super().__init__(path, speech=False)
        self.armed = threading.Event()

    def capture(self, *, device=None, control=None, on_speech_started=None):
        self.armed.set()
        while control is None or not control.cancelled:
            if control is None:
                break
            threading.Event().wait(0.005)
        return super().capture(device=device, control=control)


class BlockingHandle:
    """Playback that actually has to be cancelled to stop."""

    def __init__(self):
        self._done = threading.Event()
        self.cancelled = False
        self.cancel_grace = None
        self.running = True

    def wait(self, timeout=None):
        self._done.wait(timeout if timeout is not None else 5.0)
        return -15 if self.cancelled else 0

    def cancel(self, grace_s: float = 0.2):
        self.cancelled = True
        self.cancel_grace = grace_s
        self.running = False
        self._done.set()


class BlockingPlayer:
    def __init__(self):
        self.handles: list[BlockingHandle] = []
        self.volumes: list[float] = []

    def play_file(self, *args, volume=1.0, **kwargs):
        self.volumes.append(volume)
        handle = BlockingHandle()
        self.handles.append(handle)
        return handle

    def cancel_all(self):
        return 0


def make_engine(tmp_path: Path, *, speech=True, recorder=None, player=None, speech_client=None,
                barge_in=False):
    config = VoxConfig(
        state_dir=tmp_path / "state", idle_timeout_seconds=600, barge_in_enabled=barge_in
    )
    store = AudioStore(tmp_path / "audio")
    engine = VoxEngine(
        config=config,
        home=tmp_path,
        state=VoiceStateMachine(snapshot_path=config.snapshot_path, idle_timeout_seconds=600),
        recorder=recorder or FakeRecorder(store.latest_stt, speech=speech),
        player=player or FakePlayer(),
        speech=speech_client or FakeSpeech(),
        supervisor=FakeSupervisor(),
        store=store,
        logger=JsonlEventLogger(config.event_log_path),
        lease=LeaseManager(ttl_seconds=600),
        gate=OperationGate(),
    )
    if barge_in:
        # These fixtures exercise the barge-in *mechanism*. Whether it is
        # allowed to arm on this machine's speakers is policy, tested on its
        # own above, and must not depend on what is plugged in while the suite
        # runs.
        engine._barge_in_require_headphones = False
    return engine


@pytest.mark.asyncio
async def test_converse_speaks_then_listens_and_returns_idle(tmp_path: Path):
    engine = make_engine(tmp_path)
    result = await engine.converse("claude", "Hi")
    assert result["spoken"]["status"] == "completed"
    assert result["heard"]["transcript"] == "hello world"
    assert result["session"]["state"] == "idle"
    assert result["session"]["microphone_open"] is False


@pytest.mark.asyncio
async def test_health_reports_zero_mic_level_when_closed(tmp_path: Path):
    # The menu-bar waveform reads mic_level; with the mic closed it must be a
    # hard 0 so a stale capture level can never make the meter look live.
    engine = make_engine(tmp_path)
    health = await engine.health()
    assert health["mic_level"] == 0.0
    assert health["microphone_open"] is False


@pytest.mark.asyncio
async def test_no_speech_is_bounded_and_keeps_session(tmp_path: Path):
    engine = make_engine(tmp_path, speech=False)
    result = await engine.listen("claude")
    assert result["status"] == "no_speech"
    assert result["session"]["state"] == "idle"


@pytest.mark.asyncio
async def test_competing_client_shares_session_and_queues(tmp_path: Path):
    """Two hosts share one session; the second waits on the gate, not BusyError."""

    engine = make_engine(tmp_path)
    await engine.session("claude", "start")
    result = await engine.listen("codex")
    assert result["status"] in {"completed", "no_speech"}
    assert (await engine.status())["lease"]["shared"] is True


@pytest.mark.asyncio
async def test_global_stop_from_any_client(tmp_path: Path):
    engine = make_engine(tmp_path)
    await engine.session("claude", "start")

    stopped = await engine.session("codex", "stop")

    assert stopped["state"] == "off"
    lease = await engine.lease.status()
    assert lease.get("owned") is False
    assert lease.get("shared") is True
    restarted = await engine.session("codex", "start")
    assert restarted["lease"]["last_actor"] == "codex"


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
async def test_handoff_is_shared_and_does_not_exclude(tmp_path: Path):
    engine = make_engine(tmp_path)
    await engine.session("mcp:host:claude-code", "start")

    pending = await engine.session(
        "mcp:host:claude-code",
        "handoff",
        target_client_id="codex",
    )

    assert pending["status"] == "shared"
    assert pending["handoff"]["shared"] is True
    # Both hosts can still use the session without takeover.
    spoken = await engine.speak("mcp:host:claude-code", "still here")
    assert spoken["status"] == "completed"
    spoken2 = await engine.speak("mcp:host:codex", "me too")
    assert spoken2["status"] == "completed"


@pytest.mark.asyncio
async def test_two_hosts_converse_in_queue_order(tmp_path: Path):
    engine = make_engine(tmp_path)
    holder, handle = await _hold_the_microphone(engine)

    second = asyncio.create_task(engine.speak("codex", "after you", agent="mobilescape"))
    await asyncio.sleep(0.05)
    status = await engine.status()
    assert status["operation"]["queue_depth"] == 1

    handle.release.set()
    await asyncio.wait_for(holder, timeout=5)
    result = await asyncio.wait_for(second, timeout=5)
    assert result["status"] == "completed"
    assert (await engine.status())["operation"]["busy"] is False


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
    assert status["operation"]["queue"][0]["agent"] == "mobilescape"
    # The whole point: waiting in line must not hold the device open.
    assert status["session"]["microphone_open"] is False
    assert engine.microphone_open is False

    handle.release.set()
    await asyncio.wait_for(holder, timeout=5)
    await asyncio.wait_for(queued, timeout=5)
    assert (await engine.status())["session"]["microphone_open"] is False


@pytest.mark.asyncio
async def test_cancelling_speech_answers_the_caller(tmp_path: Path):
    """A cancelled speak used to hang its caller forever with no response."""

    engine = make_engine(tmp_path)
    speaking, handle = await _hold_the_microphone(engine)

    signalled = await engine.control("claude", "cancel")
    assert signalled["signalled"] is True

    result = await asyncio.wait_for(speaking, timeout=5)
    assert result["status"] == "cancelled"
    assert result["action"] == "speak"
    assert (await engine.status())["operation"]["busy"] is False


@pytest.mark.asyncio
async def test_host_cancellation_still_propagates(tmp_path: Path):
    """Only a deliberate cancel is an answer; a dropped request is not."""

    engine = make_engine(tmp_path)
    speaking, handle = await _hold_the_microphone(engine)

    speaking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await speaking

    handle.release.set()
    await asyncio.sleep(0.05)
    assert (await engine.status())["operation"]["busy"] is False


@pytest.mark.asyncio
async def test_queue_transitions_are_logged(tmp_path: Path):
    engine = make_engine(tmp_path)
    holder, handle = await _hold_the_microphone(engine)

    queued = asyncio.create_task(engine.converse("claude", "second", agent="infra"))
    await asyncio.sleep(0.05)
    handle.release.set()
    await asyncio.wait_for(holder, timeout=5)
    await asyncio.wait_for(queued, timeout=5)

    events = [
        json.loads(line)
        for line in Path(engine.config.event_log_path).read_text().splitlines()
        if line.strip()
    ]
    by_event = {event["event"]: event for event in events}
    assert "queue.enter" in by_event
    assert "queue.exit" in by_event
    assert by_event["queue.enter"]["data"]["agent"] == "infra"
    assert by_event["queue.exit"]["data"]["waited_s"] >= 0


@pytest.mark.asyncio
async def test_drained_queue_is_logged(tmp_path: Path):
    engine = make_engine(tmp_path)
    holder, handle = await _hold_the_microphone(engine)

    queued = asyncio.create_task(engine.converse("claude", "second", agent="infra"))
    await asyncio.sleep(0.05)
    await engine.control("claude", "cancel")
    with pytest.raises(BusyError):
        await asyncio.wait_for(queued, timeout=2)
    handle.release.set()
    await asyncio.gather(holder, return_exceptions=True)

    events = [
        json.loads(line)
        for line in Path(engine.config.event_log_path).read_text().splitlines()
        if line.strip()
    ]
    drained = next(event for event in events if event["event"] == "queue.drained")
    assert drained["data"]["agent"] == "infra"
    assert drained["data"]["reason"] == "cancelled"


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


class TextAwaitingRecorder:
    """Holds the microphone open until text is delivered, like the real device.

    A listen only blocks the host's turn because the capture genuinely runs for
    as long as the user might speak. Reproducing that is the whole point: the
    typed text has to end a capture that is already in progress.
    """

    def __init__(self) -> None:
        self.started = threading.Event()

    def capture(self, *, device=None, control=None, on_speech_started=None):
        self.started.set()
        while control is not None and not control.text_delivered:
            if control.cancelled:
                break
            threading.Event().wait(0.005)
        return CaptureResult(
            samples=np.array([], dtype=np.float32),
            sample_rate=16000,
            reason=CaptureStopReason.DELIVERED_TEXT,
            speech_detected=False,
            elapsed_s=1,
            audio_duration_s=0,
            speech_duration_s=0,
            trailing_silence_s=0,
            noise_floor_dbfs=-60.0,
            latest_wav_path=None,
        )


class RefusingSpeech(FakeSpeech):
    """Fails the test if anything tries to transcribe."""

    def __init__(self) -> None:
        self.transcribe_calls = 0

    async def transcribe(self, path, **kwargs):
        self.transcribe_calls += 1
        raise AssertionError("delivered text must never reach Whisper")


class HoldingRecorder:
    """Blocks inside capture until cancelled or released — models a stuck listen."""

    def __init__(self, path: Path):
        self.path = path
        self.started = threading.Event()
        self.release = threading.Event()
        self.saw_cancel = False

    def capture(self, *, device=None, control=None):
        self.started.set()
        while not self.release.wait(timeout=0.02):
            if control is not None and control.cancelled:
                self.saw_cancel = True
                return CaptureResult(
                    samples=np.array([], dtype=np.float32),
                    sample_rate=16000,
                    reason=CaptureStopReason.CANCELLED,
                    speech_detected=False,
                    elapsed_s=1,
                    audio_duration_s=0,
                    speech_duration_s=0,
                    trailing_silence_s=0,
                    noise_floor_dbfs=-60.0,
                    latest_wav_path=None,
                )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"RIFF" + b"0" * 64)
        return CaptureResult(
            samples=np.ones(160, dtype=np.float32),
            sample_rate=16000,
            reason=CaptureStopReason.TRAILING_SILENCE,
            speech_detected=True,
            elapsed_s=1,
            audio_duration_s=1,
            speech_duration_s=0.5,
            trailing_silence_s=1.2,
            noise_floor_dbfs=-60.0,
            latest_wav_path=self.path,
        )


@pytest.mark.asyncio
async def test_takeover_cancels_stuck_listen_and_unblocks_next_turn(tmp_path: Path):
    """Takeover cancels whoever holds the mic and drains the queue (shared session)."""

    engine = make_engine(tmp_path)
    holding = HoldingRecorder(engine.store.latest_stt)
    engine.recorder = holding

    listening = asyncio.create_task(engine.listen("claude"))
    assert await asyncio.to_thread(holding.started.wait, 2)

    status = await engine.session("codex", "takeover")
    assert status["state"] == "idle"
    assert status["session"]["microphone_open"] is False
    assert holding.saw_cancel is True

    result = await asyncio.wait_for(listening, timeout=2)
    assert result["status"] == "cancelled"
    assert result["reason"] == "cancelled"

    engine.recorder = FakeRecorder(engine.store.latest_stt)

    assert (await engine.status())["operation"]["busy"] is False
    follow_up = await asyncio.wait_for(engine.converse("codex", "hi again"), timeout=2)
    assert follow_up["spoken"]["status"] == "completed"
    assert follow_up["session"]["state"] == "idle"


@pytest.mark.asyncio
async def test_io_modes_narrate_and_dictate(tmp_path: Path):
    engine = make_engine(tmp_path)
    await engine.session("claude", "set_mode", mode="narrate")
    narrate = await engine.converse("claude", "story only")
    assert narrate["spoken"]["status"] == "completed"
    assert "heard" not in narrate or narrate.get("heard") is None
    assert narrate["io_mode"] == "narrate"

    await engine.session("claude", "set_mode", mode="dictate")
    spoken = await engine.speak("claude", "should skip")
    assert spoken["status"] == "skipped"
    dictated = await engine.converse("claude", "ignored tts")
    assert dictated["spoken"]["status"] == "skipped"
    assert dictated["heard"]["status"] == "completed"
    assert dictated["heard"]["transcript"] == "hello world"

    await engine.session("claude", "set_mode", mode="talk")


@pytest.mark.asyncio
async def test_last_heard_persists_and_claim_undelivered(tmp_path: Path):
    engine = make_engine(tmp_path)
    result = await engine.listen("claude")
    assert result["status"] == "completed"
    # Normal delivery marks delivered.
    assert engine.last_heard.undelivered() is None
    assert engine.last_heard.read() is not None
    assert engine.last_heard.read().delivered is True

    # Simulate undelivered: write fresh record.
    engine.last_heard.write(
        transcript="lost words",
        reason="trailing_silence",
        session_id="s1",
        client_id="claude",
        agent="default",
        turn_id="t1",
        delivered=False,
    )
    status = await engine.status()
    assert status["undelivered_heard"]["present"] is True
    claimed = await engine.session("claude", "claim_undelivered")
    assert claimed["claimed_heard"]["transcript"] == "lost words"
    assert engine.last_heard.undelivered() is None


@pytest.mark.asyncio
async def test_host_cancel_after_stt_returns_transcript(tmp_path: Path):
    """Host CancelledError after STT still returns the finished transcript."""

    engine = make_engine(tmp_path)

    async def operation():
        heard = {
            "status": "completed",
            "transcript": "recovered speech",
            "turn_id": "abc",
        }
        engine.last_heard.write(
            transcript="recovered speech",
            reason="trailing_silence",
            session_id="s",
            client_id="claude",
            agent="default",
            turn_id="abc",
            delivered=False,
        )
        engine._set_pending_heard(heard, "abc")
        raise asyncio.CancelledError()

    result = await engine._run_operation("claude", "listen", operation)
    assert result["status"] == "completed"
    assert result["delivered_via"] == "cancel_recovery"
    assert result["transcript"] == "recovered speech"
    assert engine.last_heard.undelivered() is None


def test_split_for_tts_streams_sentences_but_keeps_short_and_runon_whole() -> None:
    from voxmcp.engine import split_for_tts

    # Short replies stay whole — nothing to stream.
    assert split_for_tts("Done.") == ["Done."]

    # A long multi-sentence reply splits on sentence boundaries.
    long_reply = (
        "The endpointing is fixed now and the mic no longer cuts you off. "
        "I also added the earcons you asked for so you hear the window open. "
        "The status bar shows a red mic only when it is truly listening."
    )
    chunks = split_for_tts(long_reply)
    assert len(chunks) == 3
    assert chunks[0].startswith("The endpointing")
    assert "".join(chunks).replace(" ", "") == long_reply.replace(" ", "")

    # A single long run-on sentence is never split mid-sentence.
    runon = "yeah " * 60
    assert split_for_tts(runon) == [runon.strip()]


@pytest.mark.asyncio
async def test_speak_streams_each_sentence_and_completes(tmp_path: Path):
    engine = make_engine(tmp_path)
    counting = CountingSpeech()
    engine.speech = counting
    long_reply = (
        "The endpointing is fixed now and the mic no longer cuts you off. "
        "I also added the earcons you asked for so you hear the window open. "
        "The status bar shows a red mic only when it is truly listening."
    )
    result = await engine.speak("claude", long_reply)
    assert result["status"] == "completed"
    assert result["audio_path"] is not None
    # Each sentence was synthesized and played as its own streamed span.
    assert len(counting.spans) == 3


@pytest.mark.asyncio
async def test_note_addresses_one_agent_and_only_it_claims(tmp_path: Path):
    engine = make_engine(tmp_path)
    await engine.session("claude", "start")
    result = await engine.note("http-control", target_agent="mobilescape")
    assert result["status"] == "noted"
    assert result["transcript"] == "hello world"
    assert result["target_agent"] == "mobilescape"

    # A different agent neither sees nor claims a note addressed to mobilescape.
    other_status = await engine._status_for_agent("infra")
    assert other_status["undelivered_heard"]["present"] is False
    other = await engine.session("infra", "claim_undelivered", agent="infra")
    assert other["claimed_heard"] is None

    # The addressed agent sees it and claims it once.
    addressed = await engine._status_for_agent("mobilescape")
    assert addressed["undelivered_heard"]["present"] is True
    assert addressed["undelivered_heard"]["kind"] == "note"
    claimed = await engine.session("mobilescape", "claim_undelivered", agent="mobilescape")
    assert claimed["claimed_heard"]["transcript"] == "hello world"
    assert engine.notes.get("mobilescape") is None


@pytest.mark.asyncio
async def test_broadcast_note_is_claimable_by_any_agent(tmp_path: Path):
    engine = make_engine(tmp_path)
    await engine.session("claude", "start")
    await engine.note("http-control")  # no target_agent → broadcast
    claimed = await engine.session("whoever", "claim_undelivered", agent="whoever")
    assert claimed["claimed_heard"]["transcript"] == "hello world"


@pytest.mark.asyncio
async def test_note_no_speech_stores_nothing(tmp_path: Path):
    engine = make_engine(tmp_path, speech=False)
    await engine.session("claude", "start")
    result = await engine.note("claude")
    assert result["status"] == "no_speech"
    assert engine.last_heard.undelivered() is None


@pytest.mark.asyncio
async def test_reply_addresses_the_last_agent_that_spoke(tmp_path: Path):
    # A user-initiated reply must go back to whoever just spoke — no picker.
    engine = make_engine(tmp_path)
    await engine.session("claude", "start")
    await engine.speak("mobilescape", "here is your update", agent="mobilescape")
    assert engine._last_spoken_agent == "mobilescape"

    result = await engine.reply("http-control")
    assert result["status"] == "noted"
    assert result["target_agent"] == "mobilescape"

    # Only the last speaker sees and claims it; an unrelated agent does not.
    other = await engine.session("infra", "claim_undelivered", agent="infra")
    assert other["claimed_heard"] is None
    claimed = await engine.session("mobilescape", "claim_undelivered", agent="mobilescape")
    assert claimed["claimed_heard"]["transcript"] == "hello world"


@pytest.mark.asyncio
async def test_converse_onset_timeout_flows_into_capture(tmp_path: Path):
    # The reply window is a short onset_timeout; it must reach the recorder so a
    # declined reply closes fast instead of holding the mic for the full onset.
    engine = make_engine(tmp_path)
    captured: dict[str, float] = {}
    original = engine._listen_locked

    async def spy(client_id, **kwargs):
        captured["onset_timeout"] = kwargs.get("onset_timeout")
        return await original(client_id, **kwargs)

    engine._listen_locked = spy  # type: ignore[assignment]
    await engine.converse("claude", "hi", onset_timeout=3.0)
    assert captured["onset_timeout"] == 3.0


@pytest.mark.asyncio
async def test_speak_streaming_disabled_synthesizes_once(tmp_path: Path):
    engine = make_engine(tmp_path)
    engine._stream_tts = False
    counting = CountingSpeech()
    engine.speech = counting
    long_reply = (
        "The endpointing is fixed now and the mic no longer cuts you off. "
        "I also added the earcons you asked for so you hear the window open. "
        "The status bar shows a red mic only when it is truly listening."
    )
    result = await engine.speak("claude", long_reply)
    assert result["status"] == "completed"
    assert len(counting.spans) == 1


@pytest.mark.asyncio
async def test_barge_in_stops_playback_and_keeps_the_interrupting_words(tmp_path: Path):
    # Talking over the agent must not cost the user their turn: the capture
    # that noticed them is the capture that answers.
    store = AudioStore(tmp_path / "audio")
    recorder = BargingRecorder(store.latest_stt)
    player = BlockingPlayer()
    engine = make_engine(tmp_path, recorder=recorder, player=player, barge_in=True)

    result = await engine.converse("claude", "Here is a long explanation.")

    assert result["spoken"]["status"] == "barge_in"
    assert result["barge_in"] is True
    assert result["heard"]["transcript"] == "hello world"
    assert result["status"] == "completed"
    assert player.handles[0].cancelled is True
    # Cancelled hard, not with the polite grace a normal stop uses.
    assert player.handles[0].cancel_grace == pytest.approx(0.05)
    # One capture served both the detection and the reply.
    assert recorder.captures == 1
    assert engine.microphone_open is False


@pytest.mark.asyncio
async def test_barge_in_aborts_the_rest_of_a_streamed_reply(tmp_path: Path):
    # Sentence four must never arrive once the user is talking over sentence
    # one, including the span already being synthesized ahead.
    store = AudioStore(tmp_path / "audio")
    counting = CountingSpeech()
    engine = make_engine(
        tmp_path,
        recorder=BargingRecorder(store.latest_stt),
        player=BlockingPlayer(),
        speech_client=counting,
        barge_in=True,
    )

    result = await engine.converse(
        "claude",
        "The endpointing is fixed now and the mic no longer cuts you off. "
        "I also added the earcons you asked for so you hear the window open. "
        "The status bar shows a red mic only when it is truly listening.",
    )

    assert result["spoken"]["status"] == "barge_in"
    # Two spans at most: the one playing plus the one already in flight.
    assert len(counting.spans) <= 2


@pytest.mark.asyncio
async def test_armed_but_unused_barge_in_releases_the_microphone(tmp_path: Path):
    # The regression that matters most: a turn nobody interrupted must not
    # leave its armed capture holding the device while the listen that
    # follows opens a second stream on it.
    store = AudioStore(tmp_path / "audio")
    recorder = BargingRecorder(store.latest_stt, fire=False)
    engine = make_engine(tmp_path, recorder=recorder, barge_in=True)

    result = await engine.converse("claude", "Nobody interrupts this one.")

    assert result["spoken"]["status"] == "completed"
    assert result.get("barge_in") is None
    assert result["heard"]["transcript"] == "hello world"
    # One armed capture, then one real listen — never both at once.
    assert recorder.captures == 2
    assert engine.barge_in_armed is False
    assert engine.microphone_open is False
    assert result["session"]["state"] == "idle"


@pytest.mark.asyncio
async def test_typed_text_ends_a_listen_and_becomes_the_turn(tmp_path: Path):
    # Ali's complaint, verbatim: "I need to wait for you to listen to everything
    # I said, then you will get the message I sent — which just wastes a turn."
    recorder = TextAwaitingRecorder()
    speech = RefusingSpeech()
    engine = make_engine(tmp_path, recorder=recorder, speech_client=speech)

    async def deliver_when_listening():
        for _ in range(400):
            if recorder.started.is_set() and engine.microphone_open:
                break
            await asyncio.sleep(0.005)
        return await engine.control("claude", "deliver_text", text="typed instead of spoken")

    deliverer = asyncio.create_task(deliver_when_listening())
    heard = await engine.listen("claude")
    signalled = await deliverer

    assert signalled["delivered"] is True
    assert heard["status"] == "completed"
    assert heard["transcript"] == "typed instead of spoken"
    assert heard["backend"] == "delivered_text"
    # No audio was transcribed, and the session is free for the next turn.
    assert speech.transcribe_calls == 0
    assert heard["session"]["state"] == "idle"
    assert engine.microphone_open is False


@pytest.mark.asyncio
async def test_typed_text_is_classified_for_intent_like_speech(tmp_path: Path):
    # Delivered text runs the same path as a transcript, so typing a control
    # command works exactly as saying it does.
    recorder = TextAwaitingRecorder()
    engine = make_engine(tmp_path, recorder=recorder, speech_client=RefusingSpeech())

    async def deliver_when_listening():
        for _ in range(400):
            if recorder.started.is_set() and engine.microphone_open:
                break
            await asyncio.sleep(0.005)
        await engine.control("claude", "deliver_text", text="stop")

    deliverer = asyncio.create_task(deliver_when_listening())
    heard = await engine.listen("claude")
    await deliverer

    assert heard["transcript"] == "stop"
    assert heard["control"] == {"action": "stop", "session_ended": True}


@pytest.mark.asyncio
async def test_delivering_text_with_no_listen_running_is_a_no_op(tmp_path: Path):
    # Nothing is blocking the turn, so there is nothing to shortcut — the caller
    # should just send the message normally rather than get a false success.
    engine = make_engine(tmp_path)

    result = await engine.control("claude", "deliver_text", text="nobody is listening")

    assert result == {"status": "no_listen_active", "delivered": False}


@pytest.mark.asyncio
async def test_delivering_empty_text_is_rejected(tmp_path: Path):
    engine = make_engine(tmp_path)

    with pytest.raises(VoxError):
        await engine.control("claude", "deliver_text", text="   ")


def test_armed_capture_stays_open_for_the_whole_reply(tmp_path: Path):
    # Measured before this was pinned: the armed microphone closed itself at
    # 15.1 s of a 72 s reply, so the back 79% could not be interrupted and the
    # only visible symptom was the menu-bar mic badge quietly disappearing.
    # A real AudioRecorder, because the armed config is derived from the
    # recorder's own CaptureConfig. Constructing one opens no device.
    engine = make_engine(tmp_path, recorder=AudioRecorder(CaptureConfig()), barge_in=True)
    armed = engine.armed_capture_config()

    assert armed.onset_timeout_s is None
    # The ordinary listen keeps its timeout: an unattended mic must still close.
    assert engine.recorder.config.onset_timeout_s is not None


@pytest.mark.asyncio
async def test_cancel_while_armed_releases_playback_and_the_microphone(tmp_path: Path):
    # A cancel arriving mid-speech must reach the armed capture too, or it
    # keeps the microphone with nothing left to consume its result.
    store = AudioStore(tmp_path / "audio")
    engine = make_engine(
        tmp_path,
        recorder=ArmedHoldingRecorder(store.latest_stt),
        player=BlockingPlayer(),
        barge_in=True,
    )

    async def cancel_soon():
        for _ in range(200):
            if engine.barge_in_armed:
                break
            await asyncio.sleep(0.005)
        engine._signal_cancel(manual_end=False, cancel_task=False, force=True)

    canceller = asyncio.create_task(cancel_soon())
    result = await engine.converse("claude", "This turn gets cancelled.")
    await canceller

    assert engine.barge_in_armed is False
    assert engine.microphone_open is False
    assert result["spoken"]["status"] in {"cancelled", "barge_in", "completed"}
    # The mic closing is not enough. Cancel tries to return to idle *before*
    # barge-in disarms, and that attempt is refused while the microphone is
    # still open — so the session sat in SPEAKING forever and every later turn
    # failed, with the microphone correctly closed the whole time. Checking the
    # device but not the state machine is why this shipped: assert both.
    assert engine.state.snapshot().to_dict()["state"] == "idle"
    assert result["session"]["state"] == "idle"


@pytest.mark.asyncio
async def test_barge_in_that_transcribes_to_nothing_is_treated_as_silence(tmp_path: Path):
    # Audio loud enough to trip the gate but empty after STT is almost
    # certainly our own voice returning through the speakers. Handing that
    # back as a user utterance would be worse than the interruption.
    store = AudioStore(tmp_path / "audio")

    class EchoSpeech(FakeSpeech):
        async def transcribe(self, path, **kwargs):
            return SpeechResult("whisper.cpp", 20, text="   ")

    engine = make_engine(
        tmp_path,
        recorder=BargingRecorder(store.latest_stt),
        player=BlockingPlayer(),
        speech_client=EchoSpeech(),
        barge_in=True,
    )

    result = await engine.converse("claude", "Here is a long explanation.")

    assert result["spoken"]["status"] == "barge_in"
    assert result["heard"]["status"] == "no_speech"
    assert "transcript" not in result["heard"]
    events = (tmp_path / "state" / "events.jsonl").read_text()
    assert "barge_in.echo_suspected" in events


@pytest.mark.asyncio
async def test_barge_in_stays_off_unless_enabled(tmp_path: Path):
    # Default config must never open the mic during playback.
    store = AudioStore(tmp_path / "audio")
    recorder = BargingRecorder(store.latest_stt)
    engine = make_engine(tmp_path, recorder=recorder)

    result = await engine.converse("claude", "Hi")

    assert result["spoken"]["status"] == "completed"
    assert result.get("barge_in") is None
    assert recorder.captures == 1  # the listen only; nothing was armed


@pytest.mark.asyncio
async def test_armed_microphone_is_reported_while_the_agent_is_speaking(tmp_path: Path):
    # The panel reads microphone_open independently of state, so an armed
    # window must say the mic is hot even though state is still speaking.
    store = AudioStore(tmp_path / "audio")
    engine = make_engine(
        tmp_path,
        recorder=BargingRecorder(store.latest_stt, fire=False),
        player=BlockingPlayer(),
        barge_in=True,
    )
    seen: list[dict] = []

    async def watch():
        for _ in range(200):
            if engine.barge_in_armed:
                seen.append(await engine.health())
                engine._signal_cancel(manual_end=False, cancel_task=False, force=True)
                return
            await asyncio.sleep(0.005)

    watcher = asyncio.create_task(watch())
    await engine.converse("claude", "Listening while I talk.")
    await watcher

    assert seen, "never observed the armed window"
    health = seen[0]
    assert health["microphone_open"] is True
    assert health["mic_armed_for_barge_in"] is True
    assert health["barge_in_enabled"] is True
    assert "interrupt" in health["detail"]


@pytest.mark.asyncio
async def test_companion_is_off_unless_enabled(tmp_path: Path):
    # Nothing reaches the network by default, not even indirectly.
    engine = make_engine(tmp_path)
    result = await engine.companion("claude", brief="Working on it.")
    assert result["status"] == "disabled"
    assert result["turns"] == []


@pytest.mark.asyncio
async def test_companion_answers_small_talk_and_escalates_the_work(tmp_path: Path, monkeypatch):
    # The whole point: chat is handled locally-ish and fast, anything about the
    # project comes straight back to the agent that knows it.
    from voxmcp import engine as engine_module
    from voxmcp.companion import CompanionReply

    heard = iter(["how's it going", "why did engine.py crash"])
    asked: list[str] = []

    class ScriptedSpeech(FakeSpeech):
        async def transcribe(self, path, **kwargs):
            return SpeechResult("whisper.cpp", 20, text=next(heard, "bye"))

    async def fake_ask(prompt, **kwargs):
        asked.append(prompt)
        return CompanionReply(True, "Doing great, still here.", "ok", 900)

    monkeypatch.setattr(engine_module, "ask_companion", fake_ask)
    engine = make_engine(tmp_path, speech_client=ScriptedSpeech())
    engine.config = VoxConfig(
        state_dir=tmp_path / "state", idle_timeout_seconds=600, companion_enabled=True
    )

    result = await engine.companion("claude", brief="Reading the audio path.", budget_turns=5)

    assert result["status"] == "escalated"
    assert result["reason"] == "out_of_scope"
    # Answered the pleasantry, refused the code question.
    assert asked == ["how's it going"]
    assert result["turns"][0]["said"] == "Doing great, still here."
    assert result["turns"][1]["escalated"] is True
    # The agent gets everything that was said, so the user never repeats themselves.
    assert result["transcript"] == ["how's it going", "why did engine.py crash"]


@pytest.mark.asyncio
async def test_companion_backend_failure_escalates_rather_than_stalling(tmp_path: Path, monkeypatch):
    # A dead backend must hand the conversation back, not leave the user
    # talking to a microphone that never answers.
    from voxmcp import engine as engine_module
    from voxmcp.companion import CompanionReply

    async def dead_backend(prompt, **kwargs):
        return CompanionReply(False, "", "llm-spending-limit", 40)

    monkeypatch.setattr(engine_module, "ask_companion", dead_backend)
    engine = make_engine(tmp_path)
    engine.config = VoxConfig(
        state_dir=tmp_path / "state", idle_timeout_seconds=600, companion_enabled=True
    )

    result = await engine.companion("claude", brief="One sec.", budget_turns=3)

    assert result["status"] == "escalated"
    assert result["reason"] == "llm-spending-limit"


@pytest.mark.asyncio
async def test_companion_speaks_in_its_own_voice(tmp_path: Path, monkeypatch):
    # A companion that sounds like the agent reads as the agent going vague
    # about its own work.
    from voxmcp import engine as engine_module
    from voxmcp.companion import CompanionReply

    async def fake_ask(prompt, **kwargs):
        return CompanionReply(True, "Still here.", "ok", 10)

    monkeypatch.setattr(engine_module, "ask_companion", fake_ask)
    engine = make_engine(tmp_path)
    engine.config = VoxConfig(
        state_dir=tmp_path / "state", idle_timeout_seconds=600, companion_enabled=True
    )
    engine._voice_pool = ["af_sky", "bf_emma", "am_adam", "bm_george"]

    result = await engine.companion("claude", brief="Give me a minute.", budget_turns=1)

    assert result["agent"] == "companion"
    assert engine.agent_voices.resolve("companion") != engine.default_voice


@pytest.mark.asyncio
async def test_the_armed_gate_is_stricter_where_it_actually_decides(tmp_path: Path):
    # Tightening only speech_margin_db looks like an echo gate and is not one:
    # that knob feeds the energy path, which only decides when webrtcvad is
    # absent. Wherever webrtcvad is installed the VAD path rules, so the armed
    # config has to be stricter *there* or barge-in listens through its own
    # playback exactly as sensitively as it listens to the user.
    from voxmcp.audio import AdaptiveCaptureState

    from voxmcp.audio import AudioRecorder, CaptureConfig

    engine = make_engine(tmp_path, barge_in=True)
    normal = CaptureConfig(save_latest=False, latest_wav_path=None)
    engine.recorder = AudioRecorder(normal)
    armed = engine.armed_capture_config()

    always_speech = lambda _samples, _sr: True  # noqa: E731 - WebRTC on room tone

    def required_rise(config) -> float:
        state = AdaptiveCaptureState(16_000, config, always_speech)
        return state._vad_required_rise_db()

    assert required_rise(armed) > required_rise(normal)
    assert armed.max_vad_margin_db > normal.max_vad_margin_db
    # Onset still needs sustained speech, and the room is re-read faster so our
    # own bleed becomes the floor within a sentence.
    assert armed.speech_start_s > normal.speech_start_s
    assert armed.noise_window_s < normal.noise_window_s


@pytest.mark.asyncio
async def test_barge_in_declines_when_playback_reaches_the_microphone(tmp_path: Path, monkeypatch):
    # Measured on this MacBook: Kokoro through the built-in speakers reads
    # -22 dBFS p90 while Ali's own voice peaks at -29.8. He is quieter than his
    # own echo, so no threshold separates them. Arming anyway would mean the
    # agent interrupting itself; it declines and says why instead.
    from voxmcp import engine as engine_module

    monkeypatch.setattr(engine_module, "default_output_name", lambda: "MacBook Pro Speakers")
    engine = make_engine(tmp_path, barge_in=True)
    engine._barge_in_require_headphones = True  # the shipped default

    availability = engine.barge_in_availability()

    assert availability["available"] is False
    assert availability["reason"] == "shared_output"
    assert "headphones" in availability["detail"].lower()


@pytest.mark.asyncio
async def test_barge_in_arms_itself_on_headphones(tmp_path: Path, monkeypatch):
    # Nothing to configure: plug in AirPods and it becomes available.
    from voxmcp import engine as engine_module

    monkeypatch.setattr(engine_module, "default_output_name", lambda: "Ali's AirPods Pro")
    engine = make_engine(tmp_path, barge_in=True)
    engine._barge_in_require_headphones = True  # the shipped default

    assert engine.barge_in_availability()["available"] is True


@pytest.mark.asyncio
async def test_speakers_can_be_overridden_deliberately(tmp_path: Path, monkeypatch):
    from voxmcp import engine as engine_module

    monkeypatch.setattr(engine_module, "default_output_name", lambda: "MacBook Pro Speakers")
    engine = make_engine(tmp_path, barge_in=True)
    engine._barge_in_require_headphones = False

    assert engine.barge_in_availability()["available"] is True


# ---------------------------------------------------------------------------
# Persistent capture: one stream per turn, a gate in front of it.
# ---------------------------------------------------------------------------


class ScriptedMic:
    """A fake device that keeps delivering frames on its own thread.

    Speech and silence alternate forever, so any number of turns can run
    against one stream — which is the whole point of what is being tested.
    """

    default = SimpleNamespace(device=(1, 2))

    SAMPLE_RATE = 1_000
    SPEECH_FRAMES = 10
    SILENT_FRAMES = 10

    def __init__(self, *, silent_frames: int | None = None) -> None:
        # silent_frames=0 is "someone who has not stopped talking" — the only
        # way to test a control that ends a turn, since otherwise trailing
        # silence ends it first and there is nothing left to signal.
        if silent_frames is not None:
            self.SILENT_FRAMES = silent_frames
        self.opens = 0
        self.closes = 0
        self._running = threading.Event()
        self._counter = 0
        self._lock = threading.Lock()

    def query_devices(self, device, kind):
        assert kind == "input"
        return {
            "name": "Scripted mic",
            "default_samplerate": float(self.SAMPLE_RATE),
            "max_input_channels": 1,
        }

    def InputStream(self, **kwargs):  # noqa: N802 - sounddevice API
        owner = self
        callback = kwargs["callback"]
        block = kwargs["blocksize"]

        class Stream:
            def start(self):
                owner.opens += 1
                owner._running.set()

                def pump():
                    period = owner.SPEECH_FRAMES + owner.SILENT_FRAMES
                    while owner._running.is_set():
                        with owner._lock:
                            index = owner._counter
                            owner._counter += 1
                        amplitude = 0.4 if index % period < owner.SPEECH_FRAMES else 0.0
                        frame = np.full(block, amplitude, dtype=np.float32)
                        callback(frame.reshape(-1, 1), block, None, None)
                        # Still four times faster than the 20 ms frames it is
                        # pretending to deliver. Any tighter and several of
                        # these threads running at once starve the executor
                        # the captures they feed are running on.
                        time.sleep(0.005)

                threading.Thread(target=pump, daemon=True).start()

            def stop(self):
                owner._running.clear()

            def close(self):
                owner.closes += 1

            # The open-per-listen fallback still uses the stream as a context
            # manager, so the same fake has to serve both paths.
            def __enter__(self):
                self.start()
                return self

            def __exit__(self, *_exc):
                self.stop()
                self.close()
                return None

        return Stream()


def make_live_engine(tmp_path: Path, *, silent_frames: int | None = None):
    """An engine whose recorder is the real one, over a scripted device."""

    mic = ScriptedMic(silent_frames=silent_frames)
    store = AudioStore(tmp_path / "audio")
    recorder = AudioRecorder(
        CaptureConfig(
            onset_timeout_s=5.0,
            trailing_silence_s=0.12,
            short_trailing_silence_s=0.12,
            min_duration_s=0.0,
            max_duration_s=3.0,
            pre_roll_s=0.0,
            speech_start_s=0.06,
            frame_ms=20,
            save_latest=True,
            latest_wav_path=store.latest_stt,
        ),
        sounddevice=mic,
    )
    engine = make_engine(tmp_path, recorder=recorder)
    engine._stream_open_guard_s = 0.0
    return engine, mic


async def wait_for(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_back_to_back_turns_share_one_stream(tmp_path: Path):
    """The churn is the bug: two opens per turn is what fires the HFP pop.

    Consecutive turns inside the idle-release linger reuse the device rather
    than reopening it, which is the whole point of the linger.
    """

    engine, mic = make_live_engine(tmp_path)
    try:
        first = await engine.listen("claude")
        second = await engine.listen("claude")
        assert first["transcript"] == "hello world"
        assert second["transcript"] == "hello world"
        assert mic.opens == 1
        assert mic.closes == 0
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_the_device_is_released_when_the_turn_ends(tmp_path: Path):
    """macOS lights its microphone indicator for an open *device*.

    It knows nothing about our software gate, so a stream that outlives the turn
    is an indicator burning with no honest meaning — which is exactly what Ali
    saw and called uncomfortable. Shutting the gate is not enough; the device
    has to go.
    """

    engine, mic = make_live_engine(tmp_path)
    engine._stream_idle_release_s = 0.05
    try:
        await engine.listen("claude")
        # Wait on the *device*, not on stream_open: _close_source drops its
        # reference before handing the actual close to a thread, so the flag
        # flips first and the hardware follows.
        assert await wait_for(lambda: mic.closes == 1)
        assert engine.stream_open is False
        assert engine.gate_open is False
        assert engine.microphone_open is False

        health = await engine.health()
        assert health["stream_open"] is False
        assert health["gate_open"] is False
        assert health["microphone_open"] is False
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_the_device_stays_up_while_a_capture_still_needs_it(tmp_path: Path):
    """The linger must never close the device out from under a live turn.

    Asserted as the invariant rather than by sleeping past the linger, because
    when the turn ends is up to the endpointer: an open microphone with no device
    behind it is the failure, whenever it happens.
    """

    engine, _ = make_live_engine(tmp_path, silent_frames=0)
    # Shorter than any turn, so a linger that ignored the outstanding hold would
    # fire in the middle of one.
    engine._stream_idle_release_s = 0.01
    try:
        await engine.control("http-control", "gate_open")
        assert await wait_for(lambda: engine.microphone_open)

        deaf_while_open = 0
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if engine.microphone_open and not engine.stream_open:
                deaf_while_open += 1
            await asyncio.sleep(0.01)
        assert deaf_while_open == 0
        # And once nothing needs it, it does go.
        assert await wait_for(lambda: not engine.stream_open)
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_stopping_the_session_tears_the_stream_down(tmp_path: Path):
    """Pause and stop keep their meaning: the device is genuinely released."""

    engine, mic = make_live_engine(tmp_path)
    await engine.listen("claude")
    assert mic.opens == 1

    await engine.session("http-control", "stop")
    assert mic.closes == 1
    assert engine.stream_open is False
    assert engine.gate_open is False


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_pausing_releases_the_device_and_resuming_reopens_it(tmp_path: Path):
    engine, mic = make_live_engine(tmp_path)
    try:
        await engine.listen("claude")
        await engine.session("http-control", "pause")
        assert mic.closes == 1
        assert engine.stream_open is False

        await engine.session("http-control", "resume")
        await engine.listen("claude")
        assert mic.opens == 2  # a new session, a new stream
        assert engine.stream_open is True
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_the_turn_key_opens_a_turn_and_closing_it_submits(tmp_path: Path):
    """Tap to talk, tap to send — the second tap must reach a live capture."""

    engine, mic = make_live_engine(tmp_path)
    try:
        opened = await engine.control("http-control", "gate_open")
        assert opened["status"] == "gate_opened"
        assert opened["gate_open"] is True
        assert opened["listening"] is False

        assert await wait_for(lambda: engine.microphone_open)
        assert engine.gate_open is True

        # Promptly, while the capture is certainly still running. The mic
        # cannot be made to talk forever: the adaptive floor learns any
        # unvarying sound as the room, which is exactly what it is for.
        closed = await engine.control("http-control", "gate_close")
        assert closed["status"] == "gate_closed"
        assert closed["signalled"] is True
        assert closed["manual_end"] is True

        assert await wait_for(lambda: not engine.microphone_open)
        assert engine.gate_open is False
        assert mic.opens == 1
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_turn_opened_by_the_key_is_submitted_not_discarded(tmp_path: Path):
    """What was said reaches an agent, addressed to whoever last spoke."""

    engine, _ = make_live_engine(tmp_path)
    try:
        await engine.control("http-control", "gate_open")
        # Trailing silence stays on as the fallback end — the key is the
        # primary signal, not the only one.
        assert await wait_for(lambda: bool(engine.notes.pending_targets()))
        note = engine.notes.claim("*")
        assert note is not None
        assert note["transcript"] == "hello world"
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_the_turn_key_attaches_to_a_listen_that_is_already_waiting(tmp_path: Path):
    """When converse already holds the mic, the key must not start a second turn."""

    engine, mic = make_live_engine(tmp_path)
    try:
        listening = asyncio.create_task(engine.listen("claude"))
        assert await wait_for(lambda: engine.microphone_open)

        opened = await engine.control("http-control", "gate_open")
        assert opened["listening"] is True
        assert opened["opened"] is False  # _capture_once had already opened it

        result = await listening
        assert result["transcript"] == "hello world"
        assert mic.opens == 1
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_gate_open_turn_does_not_hang_up_while_you_are_thinking(tmp_path: Path):
    """The key said "I'm talking"; the runtime must not time out the onset."""

    engine, _ = make_live_engine(tmp_path)
    recorder = engine._open_ended_recorder()
    assert recorder.config.onset_timeout_s is None
    assert engine.recorder.config.onset_timeout_s == 5.0  # the default is untouched


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_persistent_capture_can_be_switched_off(tmp_path: Path):
    """A kill switch, because this owns the microphone for the whole session."""

    engine, mic = make_live_engine(tmp_path)
    engine._persistent_capture = False
    try:
        await engine.listen("claude")
        assert engine.stream_open is False
        assert mic.opens == 1  # the old open-per-listen path still works
        await engine.listen("claude")
        assert mic.opens == 2
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_closing_the_turn_during_warm_up_is_not_swallowed(tmp_path: Path):
    """Tap, tap — fast. The second tap must not land on nothing.

    A turn spends its first second warming up: waiting out the stream-open
    guard, then playing the cue. Found live: the capture control used to be
    published only after that, so an impatient second tap signalled nothing
    and the microphone stayed open with no way to close it.
    """

    engine, _ = make_live_engine(tmp_path)
    engine._stream_open_guard_s = 1.0
    try:
        await engine.control("http-control", "gate_open")
        # Well inside the warm-up window, before any capture exists.
        await asyncio.sleep(0.1)
        assert engine.microphone_open is False
        closed = await engine.control("http-control", "gate_close")
        assert closed["signalled"] is True

        assert await wait_for(lambda: engine.state.state.value == "idle")
        assert engine.microphone_open is False
        assert engine.gate_open is False
        # Nothing was said, so nothing was submitted.
        assert engine.notes.pending_targets() == []
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_the_start_cue_is_never_played_into_an_open_gate(tmp_path: Path):
    """The cue goes to the same headset the microphone is in.

    It has to finish before the endpointer is listening, or Vox opens turns on
    the sound of its own blip.
    """

    engine, _ = make_live_engine(tmp_path)
    engine._stream_open_guard_s = 0.0
    gate_states: list[bool] = []

    async def watched_cue() -> None:
        gate_states.append(engine.gate_open)

    engine._play_listen_start_cue = watched_cue  # type: ignore[method-assign]
    try:
        await engine.listen("claude")
        assert gate_states == [False]
    finally:
        await engine.session("http-control", "stop")


# ---------------------------------------------------------------------------
# Hold-to-talk dictation: no agent, no TTS, no MCP session.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_dictation_captures_everything_between_press_and_release(tmp_path: Path):
    """No VAD, no endpointing: the key defines the utterance, not the silence."""

    engine, mic = make_live_engine(tmp_path)
    try:
        started = await engine.dictate("http-control", "dictate_start")
        assert started == {"status": "dictating"}
        assert await wait_for(lambda: engine.microphone_open)

        # Long enough to span the scripted mic's silent half — which would have
        # endpointed an ordinary listen, and must not end a dictation.
        await asyncio.sleep(0.6)

        result = await engine.dictate("http-control", "dictate_end")
        assert result["status"] == "dictated"
        assert result["text"] == "Hello world."
        assert await wait_for(lambda: not engine.microphone_open)
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_dictation_never_speaks_and_never_takes_an_agent_turn(tmp_path: Path):
    engine, _ = make_live_engine(tmp_path)
    log = Path(engine.config.event_log_path)
    try:
        await engine.dictate("http-control", "dictate_start")
        await asyncio.sleep(0.3)
        await engine.dictate("http-control", "dictate_end")
    finally:
        await engine.session("http-control", "stop")

    events = [json.loads(line)["event"] for line in log.read_text().splitlines() if line.strip()]
    assert not [event for event in events if event.startswith("tts.")]
    assert "note.captured" not in events
    assert "dictation.completed" in events
    # Nothing was addressed to an agent: dictation goes to the cursor, not Vox.
    assert engine.notes.pending_targets() == []


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_dictation_gives_the_device_back_when_the_key_comes_up(tmp_path: Path):
    """The regression test for a microphone indicator that never went out.

    Dictation used to hand a *shared* stream back still open — it closed only
    the one it had opened itself — so a single hold-to-talk press left the
    device live until the next pause or stop. Observed on Ali's machine as an
    orange dot burning with no session running and the gate shut, straight after
    the one dictation press the setup instructions asked for.
    """

    engine, mic = make_live_engine(tmp_path)
    engine._stream_idle_release_s = 0.05
    try:
        await engine.dictate("http-control", "dictate_start")
        assert await wait_for(lambda: engine.microphone_open)
        assert engine.stream_open is True
        await asyncio.sleep(0.2)
        await engine.dictate("http-control", "dictate_end")

        # The device itself, not just the flag: _close_source drops its reference
        # before the actual close runs on a thread.
        assert await wait_for(lambda: mic.closes == 1)
        assert engine.stream_open is False
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_the_waveform_gets_levels_during_a_dictation(tmp_path: Path):
    """`/health` used to read the level from the listen and barge-in controls only.

    A dictation publishes through its own control, so `mic_level` was hard-zeroed
    for the whole hold no matter what the capture measured — the waveform sat dead
    flat in the one mode that runs from other apps. Levels arrive as a *burst* of
    everything measured since the last poll, because frames are 20 ms and the app
    polls at 12.5 Hz.
    """

    engine, _ = make_live_engine(tmp_path)
    try:
        await engine.dictate("http-control", "dictate_start")
        assert await wait_for(lambda: engine.microphone_open)

        # Poll the way the status app does: hand back the cursor from last time so
        # each sample is drawn exactly once.
        levels: list[float] = []
        bursts: list[int] = []
        seq = 0
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(levels) < 5:
            health = await engine.health(levels_since=seq)
            seq = health["mic_levels_seq"]
            levels.extend(health["mic_levels"])
            bursts.append(len(health["mic_levels"]))
            await asyncio.sleep(0.08)

        assert len(levels) >= 5, "the waveform received nothing to draw"
        assert max(levels) > 0, "every sample was silence"
        # More than one sample per poll, which is the whole point of a burst.
        assert max(bursts) > 1
    finally:
        await engine.dictate("http-control", "dictate_end")
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_second_health_reader_cannot_starve_the_waveform(tmp_path: Path):
    """`/health` has more callers than the status app.

    `vox doctor`, `vox status` and any agent's health probe all hit it. A
    destructive read let whichever polled first take the samples, so running the
    doctor froze the pill's waveform mid-dictation. Both readers must see the
    whole signal.
    """

    engine, _ = make_live_engine(tmp_path)
    try:
        await engine.dictate("http-control", "dictate_start")
        assert await wait_for(lambda: engine.microphone_open)
        await asyncio.sleep(0.3)

        # A poll from something that is not the status app, first.
        doctor = await engine.health()
        app = await engine.health()
        assert doctor["mic_levels"], "nothing was measured to compare"
        # Not depleted: the second reader still sees everything the first did. The
        # mic is live, so it may also have picked up frames that arrived in
        # between — hence a prefix rather than equality.
        assert len(app["mic_levels"]) >= len(doctor["mic_levels"])
        assert app["mic_levels"][: len(doctor["mic_levels"])] == doctor["mic_levels"]
        assert app["mic_levels_seq"] >= doctor["mic_levels_seq"]
    finally:
        await engine.dictate("http-control", "dictate_end")
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_the_waveform_is_empty_when_the_microphone_is_shut(tmp_path: Path):
    """A stale level must never make the meter look like it is hearing you."""

    engine, _ = make_live_engine(tmp_path)
    try:
        await engine.listen("claude")
        health = await engine.health()
        assert health["microphone_open"] is False
        assert health["mic_level"] == 0.0
        assert health["mic_levels"] == []
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_dictation_does_not_wait_out_the_stream_open_guard(tmp_path: Path):
    """A hold-to-talk key is already being spoken into.

    The guard stops the Bluetooth open transient from satisfying speech onset,
    and the raw dictation path has no onset detection to fool — the pop just
    reaches Whisper as a click. Waiting it out here would instead eat the first
    second of every single dictation.
    """

    engine, _ = make_live_engine(tmp_path)
    engine._stream_open_guard_s = 5.0
    try:
        started = time.monotonic()
        await engine.dictate("http-control", "dictate_start")
        # The gate is open immediately, not five seconds from now.
        assert engine.gate_open is True
        assert time.monotonic() - started < 1.0
        await engine.dictate("http-control", "dictate_end")
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_reading_aloud_never_opens_the_microphone(tmp_path: Path):
    """Text-to-speech is not a reason to listen to the room.

    Structural, not a promise: read_aloud does not pass barge_in, so nothing on
    the path can arm a capture. Asserted because it is a privacy contract and
    nothing should be free to break it silently.
    """

    engine, mic = make_live_engine(tmp_path)
    log = Path(engine.config.event_log_path)
    # With barge-in *enabled*, so the assertion is about read_aloud not arming
    # rather than about barge-in being off.
    engine.config = replace(engine.config, barge_in_enabled=True)
    armed: list[str] = []

    async def record_arm(client_id: str) -> None:
        armed.append(client_id)

    engine._arm_barge_in = record_arm  # type: ignore[method-assign]
    try:
        result = await engine.read_aloud("http-control", text="Version 3.11.4, exactly.")
        assert result["status"] != "skipped"
        assert armed == []
        assert engine.stream_open is False
        assert engine.microphone_open is False
        assert mic.opens == 0
    finally:
        await engine.session("http-control", "stop")

    events = [json.loads(line)["event"] for line in log.read_text().splitlines() if line.strip()]
    assert "capture.stream_opened" not in events
    assert "listening.started" not in events


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_what_was_said_is_left_on_the_clipboard(tmp_path: Path):
    """So a turn that landed nowhere is still recoverable with ⌘V."""

    engine, _ = make_live_engine(tmp_path)
    copied: list[bytes] = []
    engine._clipboard_runner = lambda argv, payload: copied.append(payload)
    try:
        result = await engine.listen("claude")
        assert result["transcript"] == "hello world"
        assert copied == [b"hello world"]
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_typed_text_does_not_clobber_the_clipboard(tmp_path: Path):
    """Text the user typed is already theirs; taking their clipboard for it is rude."""

    engine, _ = make_live_engine(tmp_path)
    copied: list[bytes] = []
    engine._clipboard_runner = lambda argv, payload: copied.append(payload)
    try:
        listening = asyncio.create_task(engine.listen("claude"))
        assert await wait_for(lambda: engine.microphone_open)
        await engine.control("http-control", "deliver_text", text="typed instead")
        result = await listening
        assert result["transcript"] == "typed instead"
        assert result["backend"] == "delivered_text"
        assert copied == []
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_the_clipboard_hand_off_can_be_switched_off(tmp_path: Path):
    engine, _ = make_live_engine(tmp_path)
    engine._clipboard_transcript = False
    copied: list[bytes] = []
    engine._clipboard_runner = lambda argv, payload: copied.append(payload)
    try:
        await engine.listen("claude")
        assert copied == []
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_the_dictated_words_are_never_written_to_the_event_log(tmp_path: Path):
    """They are going to the user's own cursor; the log has no business with them."""

    engine, _ = make_live_engine(tmp_path)
    log = Path(engine.config.event_log_path)
    try:
        await engine.dictate("http-control", "dictate_start")
        await asyncio.sleep(0.3)
        result = await engine.dictate("http-control", "dictate_end")
    finally:
        await engine.session("http-control", "stop")

    assert result["text"] == "Hello world."
    assert "Hello world" not in log.read_text()
    assert "hello world" not in log.read_text()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_dictation_does_not_clobber_the_stt_recovery_recording(tmp_path: Path):
    """latest.wav is one rolling file an interrupted turn is recovered from."""

    engine, _ = make_live_engine(tmp_path)
    try:
        await engine.listen("claude")
        recovery = engine.store.latest_stt
        assert recovery.is_file()
        before = recovery.read_bytes()

        await engine.dictate("http-control", "dictate_start")
        await asyncio.sleep(0.3)
        await engine.dictate("http-control", "dictate_end")

        assert recovery.read_bytes() == before
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_dictation_wins_over_a_turn_that_is_already_listening(tmp_path: Path):
    """An explicit physical act outranks whatever Vox was doing."""

    engine, mic = make_live_engine(tmp_path)
    try:
        listening = asyncio.create_task(engine.listen("claude"))
        assert await wait_for(lambda: engine.microphone_open)

        await engine.dictate("http-control", "dictate_start")
        # The interrupted turn ended the honest way — manual end, not cancel —
        # so whatever had already been said is kept rather than discarded. How
        # much that is depends on when the key landed, so the assertion is on
        # the reason, not on the words.
        heard = await listening
        assert heard["capture"]["reason"] == "manual_end"

        await asyncio.sleep(0.3)
        result = await engine.dictate("http-control", "dictate_end")
        assert result["status"] == "dictated"
        assert mic.opens == 1  # still one stream for the whole session
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_ending_a_dictation_nobody_started_is_harmless(tmp_path: Path):
    engine, _ = make_live_engine(tmp_path)
    assert await engine.dictate("http-control", "dictate_end") == {
        "status": "not_dictating",
        "text": "",
    }


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_second_press_while_dictating_does_not_start_a_second_capture(tmp_path: Path):
    engine, _ = make_live_engine(tmp_path)
    try:
        assert (await engine.dictate("http-control", "dictate_start"))["status"] == "dictating"
        assert (await engine.dictate("http-control", "dictate_start"))["status"] == (
            "already_dictating"
        )
        await engine.dictate("http-control", "dictate_end")
    finally:
        await engine.session("http-control", "stop")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_an_unknown_dictation_action_is_refused(tmp_path: Path):
    engine, _ = make_live_engine(tmp_path)
    with pytest.raises(VoxError):
        await engine.dictate("http-control", "dictate_sideways")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_pausing_closes_the_microphone_even_mid_dictation(tmp_path: Path):
    """Pause is a privacy stop and must be able to stop everything.

    A dictation holds the microphone through its own control, so leaving it
    out of the cancel path made pause and stop wait three seconds for a
    capture nothing had signalled, then report the runtime wedged — with the
    microphone still open the whole time.
    """

    engine, mic = make_live_engine(tmp_path)
    await engine.dictate("http-control", "dictate_start")
    assert await wait_for(lambda: engine.microphone_open)

    await engine.session("http-control", "pause")

    assert engine.microphone_open is False
    assert engine.stream_open is False
    assert mic.closes == 1

    # The audio is discarded, not typed somewhere the user stopped looking.
    result = await engine.dictate("http-control", "dictate_end")
    assert result["text"] == ""


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_read_aloud_speaks_the_selection_exactly_as_given(tmp_path: Path):
    """No model on this path: what is selected is what is spoken."""

    speech = CountingSpeech()
    engine = make_engine(tmp_path, speech_client=speech)
    selection = "Deploy 3 replicas to eu-west-1 at 07:45, not 0745."

    result = await engine.read_aloud("http-control", text=selection)

    assert result["status"] == "completed"
    assert "".join(speech.spans) == selection


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_read_aloud_refuses_an_empty_selection_without_speaking(tmp_path: Path):
    speech = CountingSpeech()
    engine = make_engine(tmp_path, speech_client=speech)

    for empty in (None, "", "   \n\t "):
        result = await engine.read_aloud("http-control", text=empty)
        assert result == {"status": "skipped", "reason": "empty_selection"}
    assert speech.spans == []


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_read_aloud_queues_behind_an_agent_instead_of_talking_over_it(tmp_path: Path):
    """Ali's call: the agent finishes its sentence, then the selection is read."""

    speech = CountingSpeech()
    engine = make_engine(tmp_path, speech_client=speech)

    agent_turn = asyncio.create_task(engine.speak("claude", "The agent was already talking."))
    reading = asyncio.create_task(engine.read_aloud("http-control", text="And then the selection."))
    await asyncio.gather(agent_turn, reading)

    assert speech.spans == ["The agent was already talking.", "And then the selection."]


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_read_aloud_speaks_even_in_dictate_mode(tmp_path: Path):
    """io_mode governs what agents may do with the speaker, not the user."""

    speech = CountingSpeech()
    engine = make_engine(tmp_path, speech_client=speech)
    engine._save_io_mode("dictate")

    assert (await engine.speak("claude", "agents stay quiet"))["status"] == "skipped"
    assert (await engine.read_aloud("http-control", text="but this is read"))["status"] == (
        "completed"
    )
    assert speech.spans == ["but this is read"]
