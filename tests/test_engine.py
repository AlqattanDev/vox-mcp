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


class FakeCompat:
    pass


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
    return VoxEngine(
        config=config,
        home=tmp_path,
        state=VoiceStateMachine(snapshot_path=config.snapshot_path, idle_timeout_seconds=600),
        recorder=recorder or FakeRecorder(store.latest_stt, speech=speech),
        player=player or FakePlayer(),
        speech=speech_client or FakeSpeech(),
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
async def test_cancel_while_armed_releases_playback_and_the_microphone(tmp_path: Path):
    # A cancel arriving mid-speech must reach the armed capture too, or it
    # keeps the microphone with nothing left to consume its result.
    store = AudioStore(tmp_path / "audio")
    engine = make_engine(
        tmp_path,
        recorder=BargingRecorder(store.latest_stt, fire=False),
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
