import asyncio
import json
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


class CountingSpeech(FakeSpeech):
    def __init__(self) -> None:
        self.spans: list[str] = []

    async def synthesize(self, text, destination, **kwargs):
        self.spans.append(text)
        return await super().synthesize(text, destination, **kwargs)


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
async def test_note_captures_and_leaves_undelivered_for_next_turn(tmp_path: Path):
    engine = make_engine(tmp_path)
    await engine.session("claude", "start")
    result = await engine.note("claude")
    assert result["status"] == "noted"
    assert result["transcript"] == "hello world"
    # A note is NOT delivered in its own call — it waits, undelivered, for the
    # agent to claim on its next turn.
    undelivered = engine.last_heard.undelivered()
    assert undelivered is not None
    assert undelivered.public()["present"] is True
    claimed = await engine.session("claude", "claim_undelivered")
    assert claimed["claimed_heard"]["transcript"] == "hello world"
    assert engine.last_heard.undelivered() is None


@pytest.mark.asyncio
async def test_note_no_speech_stores_nothing(tmp_path: Path):
    engine = make_engine(tmp_path, speech=False)
    await engine.session("claude", "start")
    result = await engine.note("claude")
    assert result["status"] == "no_speech"
    assert engine.last_heard.undelivered() is None


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
