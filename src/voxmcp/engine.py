"""Persistent, host-neutral conversation engine owned by the Vox runtime."""

from __future__ import annotations

import asyncio
import csv
from datetime import date, timedelta
import io
import json
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .audio import (
    default_output_name,
    output_is_isolated,
    AudioError,
    AudioPlayer,
    AudioRecorder,
    CaptureConfig,
    CaptureControl,
    CaptureResult,
    CaptureStopReason,
    PlaybackHandle,
    PlaybackLevels,
)
from .capture_source import PersistentCaptureSource
from .dictation import Runner as ClipboardRunner
from .dictation import clean_dictation, copy_to_clipboard
from .companion import COMPANION_AGENT, ask_companion
from .config import VoxConfig
from .diagnostics import static_diagnostics
from .earcons import earcons_enabled, ensure_earcons
from .errors import BusyError, PrivacyError, ServiceUnavailableError, VoxError
from .eventlog import JsonlEventLogger, read_events
from .intents import (
    classify_spoken_intent,
    companion_may_answer,
    companion_should_stop,
    is_non_speech_transcript,
)
from .agents import AgentVoices, FALLBACK_VOICES
from .last_heard import LastHeardStore
from .notes import NotesStore
from .lease import DEFAULT_AGENT, LeaseManager, OperationGate
from .models import SessionState, SpokenIntent, StopReason
from .services import ServiceSupervisor
from .speech import LocalSpeechClient, SpeechResult, synthesize_with_macos_say
from .state import IllegalStateTransition, VoiceStateMachine
from .storage import AudioStore

IO_MODES = ("talk", "narrate", "dictate")

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+")


def split_for_tts(text: str, *, min_chunk: int = 60, min_total: int = 140) -> list[str]:
    """Split a reply into sentence spans for streamed speech.

    Short replies stay whole (nothing to gain, and one clip is cleaner). Longer
    replies split only on real sentence boundaries — never mid-sentence — and
    fragments shorter than ``min_chunk`` fold into the previous span so the
    audio never sounds choppy. A single long run-on sentence returns as one span.
    """

    stripped = text.strip()
    if len(stripped) < min_total:
        return [stripped]
    chunks: list[str] = []
    for part in _SENTENCE_BOUNDARY.split(stripped):
        part = part.strip()
        if not part:
            continue
        if chunks and len(chunks[-1]) < min_chunk:
            chunks[-1] = f"{chunks[-1]} {part}"
        else:
            chunks.append(part)
    return chunks or [stripped]


def _default_home() -> Path:
    return Path(os.environ.get("VOX_HOME", "~/.vox")).expanduser()


class VoxEngine:
    """Coordinates state, hardware, and local speech services."""

    def __init__(
        self,
        *,
        config: VoxConfig,
        home: Path,
        state: VoiceStateMachine,
        recorder: AudioRecorder,
        player: AudioPlayer,
        speech: LocalSpeechClient,
        supervisor: ServiceSupervisor,
        store: AudioStore,
        logger: JsonlEventLogger,
        lease: LeaseManager | None = None,
        gate: OperationGate | None = None,
    ) -> None:
        self.config = config
        self.home = home
        self.state = state
        self.recorder = recorder
        self.player = player
        self.speech = speech
        self.supervisor = supervisor
        self.store = store
        self.logger = logger
        self.lease = lease or LeaseManager(ttl_seconds=max(30.0, config.idle_timeout_seconds))
        self.gate = gate or OperationGate()
        self.gate.on_event = self._log_queue_event
        # The speaking waveform's source of truth: the envelope of the span
        # being played, published as the clock plays it.
        self.playback_levels = PlaybackLevels()
        self.last_heard = LastHeardStore(Path(config.state_dir) / "last_heard.json")
        self.notes = NotesStore(Path(config.state_dir) / "notes.json")
        self._io_mode_path = Path(config.state_dir) / "io_mode"
        self._io_mode = self._load_io_mode()

        self.default_voice = os.environ.get("VOX_VOICE", "af_sky").lower()
        self.agent_voices = AgentVoices(home / "agents.json", default_voice=self.default_voice)
        self._voice_pool: list[str] | None = None
        self.default_language = os.environ.get("VOX_LANGUAGE", "en")
        self.default_wait_seconds = float(os.environ.get("VOX_WAIT_SECONDS", "60"))
        self.input_device: int | str | None = os.environ.get("VOX_INPUT_DEVICE")
        self.volume = min(1.0, max(0.0, float(os.environ.get("VOX_VOLUME", "1"))))
        self._earcons_enabled = earcons_enabled()
        self._earcon_paths: tuple[Path, Path, Path] | None = None
        self._stream_tts = os.environ.get("VOX_STREAM_TTS", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

        # Barge-in echo gate. There is no acoustic echo cancellation anywhere in
        # the stack — playback is an opaque afplay subprocess, so no reference
        # signal exists to subtract. On laptop speakers the microphone hears
        # Kokoro, so an armed capture runs a deliberately deaf configuration:
        # the noise floor is allowed to rise fast enough to swallow the steady
        # speaker bleed, and only speech well above that floor, sustained rather
        # than transient, can trigger onset. Tune these from measurements taken
        # by `vox barge-in calibrate`, not by feel.
        self._barge_in_speech_start_s = float(
            os.environ.get("VOX_BARGE_IN_SPEECH_START_SECONDS", "0.30")
        )
        self._barge_in_speech_margin_db = float(
            os.environ.get("VOX_BARGE_IN_SPEECH_MARGIN_DB", "18.0")
        )
        # WebRTC votes speech on our own playback bleed, so tightening only the
        # energy path leaves the gate wide open on any machine where webrtcvad
        # is installed — which is every machine that has it as a dependency.
        # These are the knobs the VAD path actually reads.
        self._barge_in_noise_spread_k = float(
            os.environ.get("VOX_BARGE_IN_NOISE_SPREAD_K", "6.0")
        )
        self._barge_in_max_vad_margin_db = float(
            os.environ.get("VOX_BARGE_IN_MAX_VAD_MARGIN_DB", "24.0")
        )
        # The *floor* of the required rise, and the only knob that can guarantee
        # a separation rather than merely permit one: the rise is
        # clamp(k * spread, vad_margin, max_vad_margin), so raising the ceiling
        # allows a big requirement while raising this one enforces it. Default is
        # the ordinary floor, so barge-in behaves exactly as before until
        # calibration says otherwise.
        self._barge_in_vad_margin_db = float(
            os.environ.get("VOX_BARGE_IN_VAD_MARGIN_DB", "0") or "0"
        )
        # A short window so the floor re-reads the room quickly once our own
        # playback becomes part of it: the speaker bleed has to count as the
        # new silence within a sentence, not within a paragraph.
        self._barge_in_noise_window_s = float(
            os.environ.get("VOX_BARGE_IN_NOISE_WINDOW_SECONDS", "0.8")
        )
        self._barge_in_duck_volume = min(
            1.0, max(0.0, float(os.environ.get("VOX_BARGE_IN_DUCK_VOLUME", "0.85")))
        )
        self._barge_in_cancel_grace_s = max(
            0.0, float(os.environ.get("VOX_BARGE_IN_CANCEL_GRACE_S", "0.05"))
        )
        self._barge_in_require_headphones = os.environ.get(
            "VOX_BARGE_IN_REQUIRE_HEADPHONES", "1"
        ).strip().lower() not in {"0", "false", "no", "off"}

        # One capture stream per *turn*, shared by everything inside that turn,
        # instead of one per listen. Opening a stream is what produces the
        # Bluetooth HFP transient that used to open turns nobody started, so the
        # fix is to stop doing it between the armed capture and the listen that
        # follows it — and to drop frames at the callback whenever no capture is
        # attached.
        self._persistent_capture = os.environ.get(
            "VOX_PERSISTENT_CAPTURE", "1"
        ).strip().lower() not in {"0", "false", "no", "off"}
        # The transient arrives ~240 ms after the stream engages and decays over
        # ~350 ms. Half a second was measured to be too short — onset still
        # fired 513 ms after open on the FreeClip — and this is waited out
        # before the cue that says the microphone is live.
        self._stream_open_guard_s = max(
            0.0, float(os.environ.get("VOX_STREAM_OPEN_GUARD_SECONDS", "1.0"))
        )
        # Set for the listen that immediately follows our own TTS in converse.
        # The 1s stream-open guard and start chime exist for a cold listen
        # (Bluetooth pop, "I can hear you now"). After we just spoke they
        # only add a dead gap. Bare listen() is unchanged.
        self._hot_listen_after_tts = False
        # How long the device stays open after the last capture lets go.
        #
        # This is not a grace period for idling: macOS lights its microphone
        # indicator for as long as an input device is open and knows nothing
        # about our software gate, so a stream that outlives the turn is a
        # permanently lit dot with no honest meaning. The linger exists only so
        # the hand-off from an armed barge-in capture to the listen that follows
        # it inside one turn does not close and reopen the device in between.
        self._stream_idle_release_s = max(
            0.0, float(os.environ.get("VOX_STREAM_IDLE_RELEASE_SECONDS", "2.0"))
        )
        # Leave every spoken transcript on the clipboard, so a dictation that
        # landed in no text field at all is still recoverable with ⌘V.
        self._clipboard_transcript = os.environ.get(
            "VOX_CLIPBOARD_TRANSCRIPT", "1"
        ).strip().lower() not in {"0", "false", "no", "off"}
        # Overridden by tests so the suite never writes to the real clipboard.
        self._clipboard_runner: ClipboardRunner | None = None
        # Hold-to-talk dictation. The cap is a backstop for a key that never
        # came up, not a limit anyone should reach by talking.
        self._dictation_max_s = min(
            300.0, max(1.0, float(os.environ.get("VOX_DICTATION_MAX_SECONDS", "120")))
        )
        self._dictation_cleanup = os.environ.get("VOX_DICTATION_CLEANUP", "rules").strip().lower()

        self._active_lock = threading.RLock()
        self._active_task: asyncio.Task[Any] | None = None
        self._active_client: str | None = None
        self._capture_control: CaptureControl | None = None
        self._playback: PlaybackHandle | None = None
        self._playback_interruptible = True
        # A capture that runs *during* playback so the user can interrupt by
        # talking.  Kept separate from _capture_control because for the length
        # of one turn both can be live: the armed one listening through the
        # speech, the normal one owning the reply that follows.
        self._barge_in_control: CaptureControl | None = None
        self._barge_in_future: asyncio.Future[Any] | None = None
        self._barge_in_fired = False
        self._barge_in_loop: asyncio.AbstractEventLoop | None = None
        self._cancel_requested = False
        self._microphone_active = False
        self._microphone_closing = False
        self._pending_heard: dict[str, Any] | None = None
        self._pending_turn_id: str | None = None
        self._pending_agent: str | None = None
        # The agent whose voice last spoke, so a user-initiated "reply" can be
        # addressed back to it without an agent picker.
        self._last_spoken_agent: str | None = None
        self._audio_tempdir: tempfile.TemporaryDirectory[str] | None = None
        self._source: PersistentCaptureSource | None = None
        # How many captures currently need the device. Zero means the device is
        # released, which is the only state in which macOS stops showing the
        # microphone indicator.
        self._source_holds = 0
        # Which machine the open capture stream reaches, so a stream held over
        # from the previous turn is not reused after the user moved.
        self._source_is_phone = False
        self._source_release_task: asyncio.Task[Any] | None = None
        self._dictation_control: CaptureControl | None = None
        self._dictation_future: asyncio.Future[Any] | None = None
        # Only set when dictation had to open a stream of its own, which it
        # then owns and must close.
        self._dictation_source: PersistentCaptureSource | None = None
        # Turns started by the hotkey rather than by an agent. Held so the tasks
        # are not garbage collected mid-capture.
        self._detached_turns: set[asyncio.Task[Any]] = set()

    @classmethod
    def default(cls) -> "VoxEngine":
        home = _default_home()
        # state holds events + last_heard; do not create empty logs/* theater
        # (launchd stdout/stderr are /dev/null by design).
        for directory in (home, home / "state"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                directory.chmod(0o700)
            except OSError:
                pass

        env = dict(os.environ)
        env.setdefault("VOX_STATE_DIR", str(home / "state"))
        config = VoxConfig.from_env(env)
        audio_tempdir: tempfile.TemporaryDirectory[str] | None = None
        if config.persist_audio:
            audio_root = home / "audio"
            audio_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        else:
            audio_tempdir = tempfile.TemporaryDirectory(prefix="vox-audio-")
            audio_root = Path(audio_tempdir.name)
        store = AudioStore(audio_root, replay_items=8, ttl_hours=24)
        capture = CaptureConfig(
            onset_timeout_s=float(os.environ.get("VOX_ONSET_TIMEOUT_SECONDS", "5")),
            trailing_silence_s=float(os.environ.get("VOX_TRAILING_SILENCE_SECONDS", "1.6")),
            short_trailing_silence_s=float(
                os.environ.get("VOX_SHORT_TRAILING_SILENCE_SECONDS", "0.6")
            ),
            min_speech_s=float(os.environ.get("VOX_MIN_SPEECH_SECONDS", "0.5")),
            false_onset_speech_s=float(
                os.environ.get("VOX_FALSE_ONSET_SPEECH_SECONDS", "0.2")
            ),
            short_utterance_speech_s=float(
                os.environ.get("VOX_SHORT_UTTERANCE_SPEECH_SECONDS", "1.5")
            ),
            long_utterance_speech_s=float(
                os.environ.get("VOX_LONG_UTTERANCE_SPEECH_SECONDS", "3.0")
            ),
            min_duration_s=float(os.environ.get("VOX_MIN_UTTERANCE_SECONDS", "0.5")),
            max_duration_s=float(os.environ.get("VOX_MAX_UTTERANCE_SECONDS", "75")),
            pre_roll_s=float(os.environ.get("VOX_PRE_ROLL_SECONDS", "0.3")),
            speech_hold_s=float(os.environ.get("VOX_SPEECH_HOLD_SECONDS", "0.06")),
            # The absolute floor below which nothing is speech, whatever the
            # adaptive noise floor says. It has to clear the room's own tone:
            # a quiet room that still sits above this value gets classified as
            # continuous speech, which inflates speech_duration_s and stops a
            # turn from ever endpointing. `vox calibrate` measures the room and
            # prints the value to use.
            minimum_speech_dbfs=float(os.environ.get("VOX_MINIMUM_SPEECH_DBFS", "-48")),
            speech_margin_db=float(os.environ.get("VOX_SPEECH_MARGIN_DB", "9")),
            latest_wav_path=store.latest_stt,
            save_latest=True,
        )
        supervisor = ServiceSupervisor()

        async def ensure(name: str) -> object:
            return await supervisor.ensure_ready(name)

        whisper_root = Path.home() / ".voicemode" / "services" / "whisper"
        speech = LocalSpeechClient(
            tts_base_url=config.tts_url,
            stt_base_url=config.stt_url,
            ensure_service=ensure,
            whisper_cli=whisper_root / "build" / "bin" / "whisper-cli",
            whisper_model=whisper_root / "models" / "ggml-large-v3-turbo.bin",
        )
        engine = cls(
            config=config,
            home=home,
            state=VoiceStateMachine(
                snapshot_path=config.snapshot_path,
                idle_timeout_seconds=config.idle_timeout_seconds,
                privacy_enabled=config.privacy_enabled,
            ),
            recorder=AudioRecorder(capture),
            player=AudioPlayer(),
            speech=speech,
            supervisor=supervisor,
            store=store,
            logger=JsonlEventLogger(
                config.event_log_path,
                include_transcripts=config.persist_transcripts,
            ),
        )
        engine._audio_tempdir = audio_tempdir
        return engine

    @property
    def control_token_path(self) -> Path:
        return self.home / "control.token"

    def ensure_control_token(self) -> str:
        try:
            token = self.control_token_path.read_text().strip()
            if len(token) >= 32:
                return token
        except OSError:
            pass
        token = secrets.token_urlsafe(32)
        temporary = self.control_token_path.with_suffix(".tmp")
        temporary.write_text(token + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, self.control_token_path)
        self.control_token_path.chmod(0o600)
        return token

    def shutdown(self) -> None:
        """Synchronously quiesce local audio before the daemon process exits."""

        self._signal_cancel(manual_end=False, cancel_task=False, force=True)
        try:
            self.player.cancel_all()
        except Exception:
            pass

    async def _claim(self, client_id: str, *, start: bool = True) -> None:
        """Join the shared session. Never excludes another host.

        Audio exclusivity is the OperationGate FIFO only. owner_id / lease
        last_actor are diagnostics (who spoke last), not permission gates.
        """

        await self.lease.claim(client_id)
        snapshot = self.state.snapshot()
        if start and snapshot.state is SessionState.OFF:
            self.state.start(client_id)
            self._log("session.started", client_id=client_id)
        elif snapshot.state is SessionState.ERROR:
            try:
                self.state.recover(client_id=client_id)
            except Exception:
                self.state.stop(StopReason.ERROR, client_id=None)
                self.state.start(client_id)

    def _log_queue_event(self, event: str, **data: Any) -> None:
        """Record queue transitions alongside every other voice event."""

        self._log(event, **data)

    def _log(self, event: str, *, transcript: str | None = None, **data: Any) -> None:
        snapshot = self.state.snapshot()
        self.logger.log(
            event,
            session_id=snapshot.session_id,
            state=snapshot.state,
            data=data,
            transcript=transcript,
        )

    def _set_active(self, client_id: str) -> None:
        with self._active_lock:
            self._active_task = asyncio.current_task()
            self._active_client = client_id
            self._cancel_requested = False

    def _clear_active(self) -> None:
        with self._active_lock:
            self._active_task = None
            self._active_client = None
            self._capture_control = None
            self._playback = None
            self._barge_in_control = None
            self._barge_in_future = None
            self._barge_in_fired = False
            self._cancel_requested = False

    def _load_io_mode(self) -> str:
        try:
            value = self._io_mode_path.read_text().strip().lower()
        except OSError:
            return "talk"
        return value if value in IO_MODES else "talk"

    def _save_io_mode(self, mode: str) -> str:
        if mode not in IO_MODES:
            raise VoxError(f"Unknown io mode: {mode}")
        self._io_mode_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._io_mode_path.with_suffix(".tmp")
        temporary.write_text(mode + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, self._io_mode_path)
        self._io_mode = mode
        return mode

    def _set_pending_heard(self, heard: dict[str, Any], turn_id: str) -> None:
        with self._active_lock:
            self._pending_heard = heard
            self._pending_turn_id = turn_id

    def _take_pending_heard(self) -> tuple[dict[str, Any] | None, str | None]:
        with self._active_lock:
            heard = self._pending_heard
            turn_id = self._pending_turn_id
            self._pending_heard = None
            self._pending_turn_id = None
            return heard, turn_id

    async def _run_operation(
        self,
        client_id: str,
        action: str,
        operation: Callable[[], Awaitable[Any]],
        *,
        agent: str | None = None,
    ) -> Any:
        await self._claim(client_id)
        if self.state.state is SessionState.PAUSED:
            raise PrivacyError("Voice session is paused; resume it before using audio")
        async with self.gate.operation(client_id, action, agent=agent or DEFAULT_AGENT):
            self._set_active(client_id)
            with self._active_lock:
                self._pending_agent = agent or DEFAULT_AGENT
            try:
                result = await operation()
                _, turn_id = self._take_pending_heard()
                if turn_id:
                    self.last_heard.mark_delivered(turn_id)
                return result
            except asyncio.CancelledError:
                # Set by control(cancel) before it cancelled us, so it tells a
                # deliberate cancel apart from the host dropping the request.
                with self._active_lock:
                    user_requested = self._cancel_requested
                pending_heard, turn_id = self._take_pending_heard()
                self._signal_cancel(manual_end=False, cancel_task=False)
                self._return_idle_if_active()
                self._log("turn.cancelled", client_id=client_id, action=action)
                if pending_heard is not None and not user_requested:
                    # Host dropped the tool after STT finished. Return the
                    # transcript so the agent still hears the user.
                    if turn_id:
                        self.last_heard.mark_delivered(turn_id)
                    payload: dict[str, Any] = {
                        "status": "completed",
                        "delivered_via": "cancel_recovery",
                        "action": action,
                        "session": self.state.snapshot().to_dict(),
                    }
                    if action == "converse":
                        payload["heard"] = pending_heard
                        payload["spoken"] = {"status": "completed"}
                    else:
                        payload.update(pending_heard)
                    self._log(
                        "turn.recovered",
                        client_id=client_id,
                        action=action,
                        turn_id=turn_id,
                    )
                    return payload
                if user_requested:
                    # Cancelling mid-speech used to leave the caller with no
                    # response at all: the tool call never completed and the
                    # agent hung. A cancel is an answer, so report it as one.
                    # last_heard stays undelivered for explicit recovery.
                    return {
                        "status": "cancelled",
                        "action": action,
                        "session": self.state.snapshot().to_dict(),
                        "undelivered_heard": (
                            self.last_heard.undelivered().public()
                            if self.last_heard.undelivered() is not None
                            else {"present": False}
                        ),
                    }
                raise
            except Exception as exc:
                self._take_pending_heard()
                self._signal_cancel(manual_end=False, cancel_task=False)
                self._mark_error(exc)
                self._log(
                    "turn.failed",
                    client_id=client_id,
                    action=action,
                    error_type=type(exc).__name__,
                )
                raise
            finally:
                self._clear_active()
                with self._active_lock:
                    self._pending_agent = None

    def _return_idle_if_active(self) -> None:
        with self._active_lock:
            if self._microphone_active:
                return
        state = self.state.state
        if state in {
            SessionState.LISTENING,
            SessionState.PROCESSING,
            SessionState.SPEAKING,
            SessionState.PAUSED,
        }:
            try:
                self.state.cancel_turn(client_id=None)
            except IllegalStateTransition:
                pass

    async def _wait_for_microphone_closed(self, timeout: float = 3.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            with self._active_lock:
                active = self._microphone_active
            if not active:
                return True
            if asyncio.get_running_loop().time() >= deadline:
                self._mark_error(
                    RuntimeError(f"microphone did not confirm closure within {timeout:g} seconds")
                )
                return False
            await asyncio.sleep(0.02)

    def _mark_error(self, exc: Exception) -> None:
        if self.state.state is SessionState.OFF:
            return
        try:
            self.state.mark_error(f"{type(exc).__name__}: {exc}", client_id=None)
        except IllegalStateTransition:
            pass

    def _deliver_text(self, text: str | None) -> dict[str, Any]:
        """Hand typed text to the listen that is currently blocking the turn.

        A running listen holds the host's turn open, so anything typed while the
        microphone is live waits for the mic to time out before it is read —
        the user sits through a listen they have already decided not to use.
        This ends that capture immediately and the text becomes the turn.

        A no-op when no listen is active: there is nothing to shortcut, and the
        caller should just send the message normally.
        """

        value = (text or "").strip()
        if not value:
            raise VoxError("deliver_text requires non-empty text")
        with self._active_lock:
            control = self._capture_control
            microphone_open = self._microphone_active
        if control is None or not microphone_open:
            return {"status": "no_listen_active", "delivered": False}
        control.deliver_text(value)
        return {"status": "delivered", "delivered": True, "chars": len(value)}

    def _signal_cancel(
        self,
        *,
        manual_end: bool,
        cancel_task: bool = True,
        force: bool = False,
    ) -> dict[str, Any]:
        with self._active_lock:
            task = self._active_task
            control = self._capture_control
            barge_control = self._barge_in_control
            dictation = self._dictation_control
            playback = self._playback
            interruptible = self._playback_interruptible
            client = self._active_client
        cancel_control = control is not None
        # An armed capture owns the microphone even though the session still
        # reads SPEAKING. Leaving it running here would strand a live
        # InputStream with nothing left to await its result.
        cancel_barge_in = barge_control is not None
        cancel_playback = (
            playback is not None
            and playback.running
            and not manual_end
            and (force or interruptible)
        )
        cancel_async_task = (
            cancel_task
            and not manual_end
            and control is None
            and barge_control is None
            and (playback is None or force or interruptible)
            and task is not None
            and task is not asyncio.current_task()
        )
        # A dictation holds the microphone through its own control, not
        # _capture_control. Leaving it out made pause and stop unable to close
        # the mic at all — they waited three seconds for a capture nothing had
        # signalled and then reported the runtime wedged. Pause and stop are
        # privacy stops, so a dictation is *cancelled*: the audio is discarded
        # rather than typed somewhere the user is no longer looking.
        cancel_dictation = dictation is not None
        signalled = (
            cancel_control
            or cancel_barge_in
            or cancel_dictation
            or cancel_playback
            or cancel_async_task
        )
        if signalled and not manual_end:
            with self._active_lock:
                self._cancel_requested = True
        if dictation is not None:
            with self._active_lock:
                self._microphone_closing = True
            dictation.cancel()
        if control is not None:
            with self._active_lock:
                self._microphone_closing = True
            control.end_utterance() if manual_end else control.cancel()
        if barge_control is not None:
            with self._active_lock:
                self._microphone_closing = True
            barge_control.end_utterance() if manual_end else barge_control.cancel()
        if cancel_playback and playback is not None:
            playback.cancel()
        # A capture runs in a worker thread.  Cancelling its asyncio wrapper
        # would release the operation gate before the microphone confirms it
        # has closed.  Signal CaptureControl and let the turn unwind normally.
        if cancel_async_task and task is not None:
            task.cancel()
        return {"signalled": signalled, "client_id": client, "manual_end": manual_end}

    @property
    def barge_in_armed(self) -> bool:
        with self._active_lock:
            return self._barge_in_control is not None

    def barge_in_availability(self) -> dict[str, Any]:
        """Whether barge-in can honestly run right now, and why not if it cannot.

        Measured on this MacBook: Kokoro coming back through the built-in
        speakers reads at −22 dBFS p90 while the user's own voice peaks at
        −29.8. The user is quieter than the echo, so no threshold separates
        them — on speakers this is not a tuning problem, it is arithmetic.
        Rather than let it arm and interrupt itself, it declines and says so.
        """

        if not self.config.barge_in_enabled:
            return {"available": False, "reason": "disabled", "output_device": None}
        name = default_output_name()
        isolated = output_is_isolated(name)
        if not isolated and self._barge_in_require_headphones:
            return {
                "available": False,
                "reason": "shared_output",
                "output_device": name or "unknown",
                "detail": (
                    "Playback goes to a device the microphone can hear, so talking over "
                    "Vox cannot be told apart from Vox. Use headphones, or run one "
                    "session with VOX_BARGE_IN_REQUIRE_HEADPHONES=0 voxd to try anyway "
                    "— it cannot be persisted in settings.json."
                ),
            }
        return {"available": True, "reason": "ok", "output_device": name or "unknown"}

    def _on_barge_in_onset(self) -> None:
        """Kill playback the instant the user starts talking.

        Runs on the recorder's worker thread, so it touches nothing that
        belongs to the event loop directly: the subprocess kill is thread-safe
        and the state transition is handed back over call_soon_threadsafe.
        """

        with self._active_lock:
            if self._barge_in_fired:
                return
            self._barge_in_fired = True
            playback = self._playback
            loop = self._barge_in_loop
        if playback is not None:
            playback.cancel(grace_s=self._barge_in_cancel_grace_s)
        if loop is not None:
            loop.call_soon_threadsafe(self._on_barge_in_onset_loop)

    def _on_barge_in_onset_loop(self) -> None:
        """The event-loop half of barge-in: make the session state honest."""

        try:
            self.state.begin_listening(client_id=self._active_client)
        except IllegalStateTransition:
            # The turn already moved on (cancel, stop, error). The capture
            # still unwinds normally; only the label would have been wrong.
            pass
        self._log("barge_in.detected", client_id=self._active_client)

    def phone_status(self) -> dict[str, Any]:
        """Whether a phone is currently standing in for the local devices."""

        from .remote import PHONE

        return PHONE.status()

    def _capture_backend(
        self, recorder: AudioRecorder
    ) -> tuple[Any, Any, Callable[..., Any] | None]:
        """Pick the device this capture runs on: the phone if one is attached.

        An injected recorder always wins. A test that hands Vox a fake stream
        must not have its frames quietly replaced by a phone that happens to be
        connected, and the same rule keeps the real microphone reachable when
        someone deliberately pins a device.
        """

        if (
            recorder.sounddevice is not None
            or recorder.stream_factory is not None
            or self.input_device is not None
        ):
            return self.input_device, recorder.sounddevice, recorder.stream_factory
        from .remote import PHONE, RemoteSoundDevice, remote_stream_factory

        # Attached is not chosen. A phone left connected on the desk used to
        # take the microphone from the Mac its owner was sitting at.
        if not PHONE.is_destination:
            return self.input_device, recorder.sounddevice, recorder.stream_factory
        return None, RemoteSoundDevice(), remote_stream_factory

    async def _ensure_source(self) -> PersistentCaptureSource | None:
        """Open the capture stream and take a hold on it, or return None.

        **Every non-None return is a hold the caller owns and must hand back
        with `_release_source`**, exactly like a lock. That is what keeps the
        device's lifetime tied to the turn instead of the session: while holds
        are outstanding the stream stays up, so an armed barge-in capture and
        the listen that follows it share one open; when the last one lets go the
        device is released and macOS stops showing the microphone indicator.

        A stream that will not open must never take voice down with it: the
        caller falls back to the old open-per-listen path, which still works,
        just with the transient it always had.
        """

        if not self._persistent_capture:
            return None
        if not isinstance(self.recorder, AudioRecorder):
            # An injected fake recorder owns its own frames; leave it alone.
            return None
        # Taken before the open so a release scheduled by the previous turn
        # cannot close the device out from under this one mid-open.
        self._hold_source()
        with self._active_lock:
            source = self._source
        device, sounddevice, stream_factory = self._capture_backend(self.recorder)
        from .remote import RemoteSoundDevice

        wants_phone = isinstance(sounddevice, RemoteSoundDevice)
        if source is not None and source.stream_open:
            if wants_phone == self._source_is_phone:
                return source
            # The user moved: this stream reaches the machine they were at a
            # moment ago, not the one they just spoke from. Reusing it would
            # record the wrong room — the device is deliberately held open
            # between turns, so without this the first turn after a switch
            # always went to the old microphone.
            self._log(
                "capture.backend_switched",
                to="phone" if wants_phone else "mac",
            )
            await asyncio.to_thread(source.close)
            with self._active_lock:
                self._source = None
        source = PersistentCaptureSource(
            self.recorder.config,
            device=device,
            # The stream has to reach the same hardware the recorder was built
            # against, injected fakes included — otherwise a test recorder ends
            # up driving the real microphone.
            sounddevice=sounddevice,
            stream_factory=stream_factory,
            open_guard_s=self._stream_open_guard_s,
            on_event=self._log,
        )
        try:
            await asyncio.to_thread(source.open)
        except AudioError as exc:
            self._log("capture.stream_failed", error=str(exc))
            self._release_source()
            return None
        with self._active_lock:
            self._source = source
            self._source_is_phone = wants_phone
        return source

    def _hold_source(self) -> None:
        """Claim the device for one capture, cancelling any pending release."""

        with self._active_lock:
            self._source_holds += 1
            pending = self._source_release_task
            self._source_release_task = None
        if pending is not None:
            pending.cancel()

    def _release_source(self) -> None:
        """Hand the device back. The last release schedules the close.

        Safe to call from a future's done-callback, which is where every capture
        actually finishes.
        """

        with self._active_lock:
            if self._source_holds > 0:
                self._source_holds -= 1
            remaining = self._source_holds
            if remaining > 0 or self._source is None:
                return
            if self._source_release_task is not None:
                return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop to schedule on (a worker thread at shutdown). The stream
            # is closed by pause/stop or by the next release that has one.
            return
        task = loop.create_task(self._release_source_after_linger())
        with self._active_lock:
            self._source_release_task = task

    async def _release_source_after_linger(self) -> None:
        try:
            if self._stream_idle_release_s > 0:
                await asyncio.sleep(self._stream_idle_release_s)
        except asyncio.CancelledError:
            return
        with self._active_lock:
            self._source_release_task = None
            if self._source_holds > 0:
                # Someone claimed the device while we were waiting.
                return
        await self._close_source()

    async def _close_source(self) -> None:
        """Tear the capture stream down — the privacy stop, not the gate."""

        with self._active_lock:
            source = self._source
            self._source = None
            self._source_holds = 0
            pending = self._source_release_task
            self._source_release_task = None
        if pending is not None:
            pending.cancel()
        if source is None:
            return
        await asyncio.to_thread(source.close)

    @property
    def gate_open(self) -> bool:
        source = self._source
        return bool(source is not None and source.gate_open)

    @property
    def stream_open(self) -> bool:
        source = self._source
        return bool(source is not None and source.stream_open)

    async def _arm_barge_in(self, client_id: str) -> None:
        """Open the microphone alongside playback, gated against our own voice."""

        recorder = self.recorder
        armed: Any = recorder
        if isinstance(recorder, AudioRecorder):
            armed = AudioRecorder(self.armed_capture_config())
        control = CaptureControl()
        loop = asyncio.get_running_loop()
        # Resolved before the mic is marked active: opening a stream can block,
        # and nothing may observe an open microphone that has not been submitted.
        source = await self._ensure_source()
        # Published before the capture is submitted, never after: the worker
        # can reach speech onset before this coroutine is scheduled again, and
        # initialising _barge_in_fired afterwards would erase that detection.
        with self._active_lock:
            self._barge_in_control = control
            self._barge_in_fired = False
            self._barge_in_loop = loop
            # The microphone is genuinely open while the session state still
            # says SPEAKING. status()/health() read this flag directly, so
            # setting it here is what keeps the panel honest during the window.
            self._microphone_active = True
            self._microphone_closing = False
        if source is not None:
            # Arming attaches to the live stream instead of opening a second
            # one, which is what used to collapse the headset into call audio
            # again at disarm. It listens past the closed gate deliberately:
            # arming is its own consent act, with its own hardened thresholds.
            future = loop.run_in_executor(
                None,
                lambda: armed.capture_from_frames(
                    source.frames(control, respect_gate=False),
                    source.sample_rate,
                    control=control,
                    on_speech_started=self._on_barge_in_onset,
                ),
            )
        else:
            future = loop.run_in_executor(
                None,
                lambda: armed.capture(
                    device=self.input_device,
                    control=control,
                    on_speech_started=self._on_barge_in_onset,
                ),
            )
        with self._active_lock:
            self._barge_in_future = future

        def microphone_closed(_future: asyncio.Future[Any]) -> None:
            if source is not None:
                self._release_source()
            with self._active_lock:
                self._microphone_active = False
                self._microphone_closing = False
                fired = self._barge_in_fired
            if fired:
                # Only cue the close when the user knew the window was open.
                self._play_listen_stop_cue()

        future.add_done_callback(microphone_closed)
        self._log("barge_in.armed", client_id=client_id)

    async def _disarm_barge_in(self) -> None:
        """Tear down an armed capture that never fired.

        Skipping this would leave a live InputStream behind while the listen
        that follows opens a second one on the same device.
        """

        with self._active_lock:
            control = self._barge_in_control
            future = self._barge_in_future
            fired = self._barge_in_fired
        if control is None or fired:
            return
        control.cancel()
        if future is not None:
            try:
                await asyncio.wait_for(asyncio.shield(future), timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                pass
        with self._active_lock:
            self._barge_in_control = None
            self._barge_in_future = None
            self._microphone_active = False
        # A cancel that lands while barge-in is armed tries to return to idle
        # *before* this runs, and _return_idle_if_active refuses while the
        # microphone is still open — so that attempt silently does nothing and
        # the session used to stay SPEAKING forever, failing every later turn.
        # Disarming is what makes the transition legal, so it retries here.
        # On the completed path complete_turn already reached IDLE and this is a
        # no-op; on the fired path we returned above and the capture is the reply.
        self._return_idle_if_active()

    async def _harvest_barge_in(self, client_id: str, *, language: str | None) -> dict[str, Any]:
        """Finish the capture that interrupted playback and treat it as the reply."""

        with self._active_lock:
            future = self._barge_in_future
        if future is None:  # pragma: no cover - arming always sets it
            return {"status": "no_speech", "reason": "barge_in_lost"}
        try:
            capture = await asyncio.shield(future)
        finally:
            with self._active_lock:
                self._barge_in_control = None
                self._barge_in_future = None
                self._barge_in_fired = False

        self._log(
            "listening.stopped",
            client_id=client_id,
            reason=capture.reason.value,
            duration_s=round(capture.audio_duration_s, 3),
            speech=capture.speech_detected,
            barge_in=True,
        )
        if (
            capture.reason is CaptureStopReason.CANCELLED
            or not capture.speech_detected
            or capture.latest_wav_path is None
        ):
            self._return_idle_if_active()
            return self._silent_heard_result(capture)

        self.state.begin_processing(client_id=client_id)
        transcription = await self.speech.transcribe(
            capture.latest_wav_path,
            language=language or self.default_language,
        )
        if is_non_speech_transcript(transcription.text):
            # Nothing intelligible came back from audio loud enough to trip the
            # gate — the likely source is Kokoro's own voice returning through
            # the speakers. Reporting that to the agent as a user utterance
            # would be worse than the interruption itself, so it becomes
            # silence and leaves a trail for the calibration to be re-run.
            self._log(
                "barge_in.echo_suspected",
                client_id=client_id,
                speech_seconds=round(capture.speech_duration_s, 3),
            )
            self._return_idle_if_active()
            return self._silent_heard_result(capture)

        self._log(
            "stt.completed",
            client_id=client_id,
            backend=transcription.backend,
            elapsed_ms=transcription.elapsed_ms,
            transcript=transcription.text,
        )
        transcript, turn_id, intent = self._record_heard(client_id, capture, transcription)
        return await self._heard_result(
            client_id, capture, transcription, transcript, turn_id, intent
        )

    async def _speak_locked(
        self,
        client_id: str,
        message: str,
        *,
        voice: str | None,
        speed: float,
        instructions: str | None,
        interruptible: bool = True,
        barge_in: bool = False,
    ) -> dict[str, Any]:
        if not message.strip():
            return {"status": "skipped", "reason": "empty_message"}
        self.state.begin_speaking(client_id=client_id)
        with self._active_lock:
            self._last_spoken_agent = self._pending_agent
        self._log("tts.started", client_id=client_id, chars=len(message))

        if barge_in:
            await self._arm_barge_in(client_id)
        try:
            chunks = split_for_tts(message) if self._stream_tts else [message]
            if len(chunks) <= 1:
                spoken = await self._speak_single(
                    client_id, message, voice=voice, speed=speed,
                    instructions=instructions, interruptible=interruptible,
                )
            else:
                spoken = await self._speak_streaming(
                    client_id, chunks, voice=voice, speed=speed,
                    instructions=instructions, interruptible=interruptible,
                )
        except BaseException:
            if barge_in:
                await self._disarm_barge_in()
            raise
        if barge_in:
            # A fired barge-in keeps its capture running: it is the reply.
            # Anything else must release the device before the next listen
            # opens a second stream on it.
            await self._disarm_barge_in()
        return spoken

    async def _render_tts(
        self,
        text: str,
        *,
        voice: str | None,
        speed: float,
        instructions: str | None,
    ) -> tuple[Path, SpeechResult]:
        """Synthesize one span, fall back to the OS voice, and commit it."""

        destination = self.store.new_work_path("tts")
        try:
            result = await self.speech.synthesize(
                text,
                destination,
                voice=(voice or self.default_voice).lower(),
                speed=speed,
                instructions=instructions,
            )
        except ServiceUnavailableError:
            result = await synthesize_with_macos_say(text, destination)
        replay_path = self.store.commit_tts(Path(result.path or destination))
        return replay_path, result

    async def _play_and_wait(self, replay_path: Path, *, interruptible: bool) -> bool:
        """Play one committed clip; return True if it finished, False if cancelled."""

        # afplay takes its volume as a launch argument, so ducking cannot react
        # to a threat that has already been detected. When barge-in is armed
        # the whole turn plays slightly quieter instead, widening the gap the
        # echo gate has to work with for the price of a barely audible drop.
        volume = self.volume * self._barge_in_duck_volume if self.barge_in_armed else self.volume
        handle = self.player.play_file(replay_path, volume=volume, blocking=False)
        # Published from the file's actual samples, aligned to the moment the
        # player launched — the true waveform of this span, not an animation.
        self.playback_levels.begin(replay_path)
        with self._active_lock:
            self._playback = handle
            self._playback_interruptible = interruptible
            # The user can start talking during synthesis, before any playback
            # exists to cancel. Onset then had nothing to kill, so this span
            # has to arrive already dead rather than talk over them.
            already_barged = self._barge_in_fired
        if already_barged:
            handle.cancel(grace_s=self._barge_in_cancel_grace_s)
        try:
            return_code = await asyncio.to_thread(handle.wait)
        finally:
            self.playback_levels.end()
        with self._active_lock:
            cancelled = self._cancel_requested
            self._playback = None
            self._playback_interruptible = True
        return not (cancelled or return_code < 0)

    async def _speak_single(
        self,
        client_id: str,
        message: str,
        *,
        voice: str | None,
        speed: float,
        instructions: str | None,
        interruptible: bool,
    ) -> dict[str, Any]:
        replay_path, result = await self._render_tts(
            message, voice=voice, speed=speed, instructions=instructions
        )
        finished = await self._play_and_wait(replay_path, interruptible=interruptible)
        # Checked regardless of how playback ended: a barge-in during synthesis
        # can leave the span itself completing normally.
        with self._active_lock:
            barged = self._barge_in_fired
        if barged:
            return {
                "status": "barge_in",
                "backend": result.backend,
                "elapsed_ms": result.elapsed_ms,
            }
        if not finished:
            self._return_idle_if_active()
            return {"status": "cancelled", "backend": result.backend, "elapsed_ms": result.elapsed_ms}
        self.state.complete_turn(client_id=client_id)
        self._log(
            "tts.completed",
            client_id=client_id,
            backend=result.backend,
            elapsed_ms=result.elapsed_ms,
        )
        return {
            "status": "completed",
            "backend": result.backend,
            "fallback": result.fallback,
            "elapsed_ms": result.elapsed_ms,
            "audio_path": str(replay_path),
        }

    async def _speak_streaming(
        self,
        client_id: str,
        chunks: list[str],
        *,
        voice: str | None,
        speed: float,
        instructions: str | None,
        interruptible: bool,
    ) -> dict[str, Any]:
        """Speak sentence-by-sentence so playback starts after the first span.

        The next span synthesizes while the current one plays, so time-to-first
        audio is synth(first sentence) instead of synth(whole response).
        """

        first_backend: str | None = None
        first_replay: Path | None = None
        fallback_any = False
        elapsed_ms = 0
        cancelled = False
        pending: asyncio.Task[tuple[Path, SpeechResult]] | None = asyncio.create_task(
            self._render_tts(chunks[0], voice=voice, speed=speed, instructions=instructions)
        )
        for index in range(len(chunks)):
            assert pending is not None
            try:
                replay_path, result = await pending
            except asyncio.CancelledError:
                cancelled = True
                break
            if first_backend is None:
                first_backend, first_replay = result.backend, replay_path
            fallback_any = fallback_any or result.fallback
            elapsed_ms += result.elapsed_ms
            # Start the next span synthesizing before we block on this playback.
            pending = (
                asyncio.create_task(
                    self._render_tts(
                        chunks[index + 1], voice=voice, speed=speed, instructions=instructions
                    )
                )
                if index + 1 < len(chunks)
                else None
            )
            finished = await self._play_and_wait(replay_path, interruptible=interruptible)
            with self._active_lock:
                barged = self._barge_in_fired
            if barged or not finished:
                cancelled = True
                # Abort the whole remaining span sequence, not just the span
                # that was playing: the user is already talking over sentence
                # three, so sentence four must never arrive.
                if pending is not None:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                break
        if cancelled:
            with self._active_lock:
                barged = self._barge_in_fired
            if barged:
                return {
                    "status": "barge_in",
                    "backend": first_backend or "kokoro",
                    "elapsed_ms": elapsed_ms,
                }
            self._return_idle_if_active()
            return {
                "status": "cancelled",
                "backend": first_backend or "kokoro",
                "elapsed_ms": elapsed_ms,
            }
        self.state.complete_turn(client_id=client_id)
        self._log(
            "tts.completed",
            client_id=client_id,
            backend=first_backend,
            elapsed_ms=elapsed_ms,
            chunks=len(chunks),
        )
        return {
            "status": "completed",
            "backend": first_backend,
            "fallback": fallback_any,
            "elapsed_ms": elapsed_ms,
            "audio_path": str(first_replay) if first_replay is not None else None,
        }

    def _earcon_files(self) -> tuple[Path, Path, Path] | None:
        if not self._earcons_enabled:
            return None
        if self._earcon_paths is None:
            try:
                self._earcon_paths = ensure_earcons(self.home / "assets")
            except Exception:
                # A cue is a courtesy; failing to synthesize it must never keep
                # the microphone from opening.
                self._earcons_enabled = False
                return None
        return self._earcon_paths

    async def _play_listen_start_cue(self) -> None:
        if self._hot_listen_after_tts:
            return
        cues = self._earcon_files()
        if cues is None:
            return
        try:
            # Blocking and off the loop, *before* the mic opens, so the tone can
            # never bleed into the recording.
            await asyncio.to_thread(
                self.player.play_file, cues[0], volume=self.volume, blocking=True
            )
        except Exception:
            pass

    def _play_listen_stop_cue(self) -> None:
        cues = self._earcon_files()
        if cues is None:
            return
        try:
            self.player.play_file(cues[1], volume=self.volume, blocking=False)
        except Exception:
            pass

    def _play_error_cue(self) -> None:
        """Say "that did not happen" out loud.

        A hotkey that quietly does nothing reads as a broken hotkey, and the
        user presses it again harder rather than looking for the reason.
        """

        cues = self._earcon_files()
        if cues is None:
            return
        try:
            self.player.play_file(cues[2], volume=self.volume, blocking=False)
        except Exception:
            pass

    def armed_capture_config(self) -> CaptureConfig:
        """Capture settings deaf enough to listen through our own playback.

        Only the pre-onset gate is tightened.  Once the user has actually
        started talking the capture endpoints on the normal settings, so a
        barge-in reply gets the same trailing-silence treatment as any other
        turn.  The fast-rising noise floor is the load-bearing part: it lets
        the steady speaker bleed become the floor within a few hundred
        milliseconds, so the margin is measured against Kokoro rather than
        against the silence Kokoro is no longer providing.
        """

        base = self.recorder.config
        # Only raise the floor, never lower it, and never above the ceiling —
        # CaptureConfig rejects vad_margin_db > max_vad_margin_db, and a bad pair
        # here would take barge-in down with a ValueError mid-turn.
        vad_margin_db = min(
            self._barge_in_max_vad_margin_db,
            max(base.vad_margin_db, self._barge_in_vad_margin_db),
        )
        return replace(
            base,
            speech_start_s=self._barge_in_speech_start_s,
            speech_margin_db=self._barge_in_speech_margin_db,
            noise_spread_k=self._barge_in_noise_spread_k,
            vad_margin_db=vad_margin_db,
            max_vad_margin_db=self._barge_in_max_vad_margin_db,
            noise_window_s=self._barge_in_noise_window_s,
            # No onset timeout: this capture ends when the reply does, and
            # _disarm_barge_in is what stops it. Inheriting the ordinary 15 s
            # timeout let the microphone close itself partway through a long
            # reply — measured dying at 15.1 s of a 72 s answer, leaving 79% of
            # it uninterruptible with the glyph correctly showing a closed mic
            # and nothing explaining why talking no longer worked.
            onset_timeout_s=None,
        )

    async def _capture_once(
        self,
        client_id: str,
        *,
        recorder: Any | None = None,
        language: str | None = None,
        skip_open_guard: bool = False,
    ) -> tuple[CaptureResult, SpeechResult | None]:
        self.state.begin_listening(client_id=client_id)
        self._log("listening.started", client_id=client_id)
        # Published before anything that can block. A turn spends its first
        # second warming up — waiting out the stream-open guard, playing the
        # cue — and a user who taps the key twice quickly must not have the
        # second tap land on nothing. With the control already live, an early
        # end is remembered and the capture closes the instant it opens.
        control = CaptureControl()
        with self._active_lock:
            self._capture_control = control
        source = await self._ensure_source()
        # Everything between taking the hold and attaching the done-callback that
        # gives it back can raise or be cancelled — the guard sleep and the cue
        # both await. A hold leaked there would pin the device open for the rest
        # of the session, which is the exact failure being fixed.
        # A user-initiated note/reply skips that wait: they are already talking
        # into the key, and eating the first second is worse than a Bluetooth
        # click at the start of the clip.
        skip_guard = skip_open_guard or self._hot_listen_after_tts
        try:
            if source is not None:
                # Hold the gate shut across the cue. The cue is played through
                # the same headset the microphone is in, and on a fresh stream
                # the Bluetooth open transient has not finished decaying yet —
                # letting either into the endpointer is how a turn opens on a
                # sound nobody made. Waiting the guard out here is also what
                # makes the rising blip honest: "I can hear you now", not "soon".
                source.close_gate()
                remaining = 0.0 if skip_guard else source.guard_remaining_s
                if remaining > 0:
                    await asyncio.sleep(remaining)
            if not skip_open_guard and not (
                control.cancelled or control.manual_end_requested or control.text_delivered
            ):
                # Never announce an open microphone to someone who already closed it.
                # Notes skip the cue: the key press is the start signal, and the
                # blip would false-trigger onset if we opened the gate under it.
                await self._play_listen_start_cue()
            loop = asyncio.get_running_loop()
            selected_recorder = recorder or self.recorder
            if source is not None:
                # The gate is what "the microphone is open" now means: frames
                # only exist while a capture holds it.
                source.open_gate()
                future = loop.run_in_executor(
                    None,
                    lambda: selected_recorder.capture_from_frames(
                        source.frames(control, respect_guard=not skip_guard),
                        source.sample_rate,
                        control=control,
                    ),
                )
            else:
                future = loop.run_in_executor(
                    None,
                    lambda: selected_recorder.capture(device=self.input_device, control=control),
                )
        except BaseException:
            if source is not None:
                source.close_gate()
                self._release_source()
            raise
        with self._active_lock:
            self._microphone_active = True
            self._microphone_closing = False

        def microphone_closed(_future: asyncio.Future[Any]) -> None:
            if source is not None:
                source.close_gate()
                # Shutting the gate is not enough to put the macOS indicator
                # out; the device has to go too.
                self._release_source()
            with self._active_lock:
                self._microphone_active = False
                self._microphone_closing = False
            # Mark the mic closed in every path (normal, cancel, interrupt,
            # error) so the user always hears when the window ends.
            self._play_listen_stop_cue()

        future.add_done_callback(microphone_closed)
        try:
            capture = await asyncio.shield(future)
        except asyncio.CancelledError:
            with self._active_lock:
                self._microphone_closing = True
            # A transport/host drop is NOT a deliberate user cancel: preserve the
            # captured speech and let the recorder persist it to the recovery wav
            # so a crash mid-utterance is never lost (transcribe(latest=true) /
            # claim_undelivered can retrieve it).
            control.interrupt()
            # Never release the operation gate while a recorder thread may
            # still own CoreAudio. Repeated transport cancellation is ignored
            # until the hardware worker confirms closure. A wedged driver
            # leaves Vox fail-closed; the native app can restart the runtime to
            # force OS cleanup.
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    control.interrupt()
                    continue
                except Exception:
                    break
            raise
        finally:
            with self._active_lock:
                self._capture_control = None

        self._log(
            "listening.stopped",
            client_id=client_id,
            reason=capture.reason.value,
            duration_s=round(capture.audio_duration_s, 3),
            speech=capture.speech_detected,
        )
        if capture.reason is CaptureStopReason.DELIVERED_TEXT:
            # The user typed instead of speaking. There is no audio to
            # transcribe — deliberately, it was discarded — so Whisper is
            # skipped and the typed text becomes the turn. Returning a
            # SpeechResult here rather than a special case means everything
            # downstream is unchanged: the text is recorded for recovery and
            # classified for intent exactly as speech is, so typing "stop"
            # stops the session just like saying it.
            # Read from the local control, not self._capture_control: the
            # finally above has already cleared the published reference, so
            # going through it would always find the text gone.
            typed = control.delivered_text or ""
            # Still moves through PROCESSING even though there is nothing to
            # transcribe: the turn is settled by the same code as a spoken one,
            # and complete_turn is only legal from PROCESSING or SPEAKING.
            self.state.begin_processing(client_id=client_id)
            self._log("listening.delivered_text", client_id=client_id, chars=len(typed))
            return capture, SpeechResult(
                backend="delivered_text",
                elapsed_ms=0,
                text=typed,
            )

        if (
            capture.reason is CaptureStopReason.CANCELLED
            or not capture.speech_detected
            or capture.latest_wav_path is None
        ):
            self._return_idle_if_active()
            return capture, None

        self.state.begin_processing(client_id=client_id)
        transcription = await self.speech.transcribe(
            capture.latest_wav_path,
            language=language or self.default_language,
        )
        if is_non_speech_transcript(transcription.text):
            # Audio tripped the gate but Whisper found no words in it — a noise
            # burst, or a marker like [BLANK_AUDIO]. Handing that back as the
            # user's turn lets an annotation answer on their behalf, so it is
            # silence, reported the same way an unspoken turn is.
            self._log(
                "stt.non_speech",
                client_id=client_id,
                backend=transcription.backend,
                elapsed_ms=transcription.elapsed_ms,
                transcript=transcription.text,
            )
            self._return_idle_if_active()
            return capture, None
        self._log(
            "stt.completed",
            client_id=client_id,
            backend=transcription.backend,
            elapsed_ms=transcription.elapsed_ms,
            transcript=transcription.text,
        )
        return capture, transcription

    async def _listen_locked(
        self,
        client_id: str,
        *,
        repeat_limit: int = 2,
        wait_limit: int = 2,
        listen_duration_max: float | None = None,
        listen_duration_min: float | None = None,
        trailing_silence_s: float | None = None,
        onset_timeout: float | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        selected_recorder: Any = self.recorder
        if isinstance(self.recorder, AudioRecorder) and (
            listen_duration_max is not None
            or listen_duration_min is not None
            or trailing_silence_s is not None
            or onset_timeout is not None
        ):
            capture_config = replace(
                self.recorder.config,
                max_duration_s=(
                    listen_duration_max
                    if listen_duration_max is not None
                    else self.recorder.config.max_duration_s
                ),
                min_duration_s=(
                    listen_duration_min
                    if listen_duration_min is not None
                    else self.recorder.config.min_duration_s
                ),
                trailing_silence_s=(
                    trailing_silence_s
                    if trailing_silence_s is not None
                    else self.recorder.config.trailing_silence_s
                ),
                # A caller who names a number gets that number. The
                # utterance-length scaling silently overrode it for anything
                # short of a paragraph — ask for 1.2 s, get the 0.6 s default
                # floor, which is what the runtime then reported back. Naming
                # the close collapses the scaling onto it rather than leaving a
                # floor nobody asked for underneath.
                short_trailing_silence_s=(
                    trailing_silence_s
                    if trailing_silence_s is not None
                    else self.recorder.config.short_trailing_silence_s
                ),
                # A short onset_timeout is the "reply window": if the user does
                # not start speaking within it, the listen returns no_speech fast
                # instead of holding the mic open, so a declined reply closes
                # itself rather than hanging.
                onset_timeout_s=(
                    onset_timeout
                    if onset_timeout is not None
                    else self.recorder.config.onset_timeout_s
                ),
            )
            selected_recorder = AudioRecorder(capture_config)
        repeats = 0
        waits = 0
        while True:
            capture, transcription = await self._capture_once(
                client_id,
                recorder=selected_recorder,
                language=language,
            )
            if transcription is None:
                return self._silent_heard_result(capture)

            transcript, turn_id, intent = self._record_heard(client_id, capture, transcription)
            if intent.intent is SpokenIntent.REPEAT and repeats < repeat_limit:
                repeats += 1
                self.state.complete_turn(client_id=client_id)
                replay = self.store.replay(0)
                if replay is not None:
                    self.state.begin_speaking(client_id=client_id)
                    handle = self.player.play_file(replay, volume=self.volume, blocking=False)
                    with self._active_lock:
                        self._playback = handle
                    await asyncio.to_thread(handle.wait)
                    with self._active_lock:
                        self._playback = None
                    self.state.complete_turn(client_id=client_id)
                continue

            if intent.intent is SpokenIntent.WAIT and waits < wait_limit:
                waits += 1
                self.state.complete_turn(client_id=client_id)
                wait_seconds = intent.duration_seconds or self.default_wait_seconds
                self._log("turn.waiting", seconds=wait_seconds, client_id=client_id)
                await asyncio.sleep(wait_seconds)
                continue

            return await self._heard_result(
                client_id, capture, transcription, transcript, turn_id, intent
            )

    def _silent_heard_result(self, capture: CaptureResult) -> dict[str, Any]:
        """The result for a capture that produced no transcript."""

        return {
            "status": (
                "cancelled" if capture.reason is CaptureStopReason.CANCELLED else "no_speech"
            ),
            "reason": capture.reason.value,
            "capture": _capture_dict(capture),
            "session": self.state.snapshot().to_dict(),
        }

    def _record_heard(
        self,
        client_id: str,
        capture: CaptureResult,
        transcription: SpeechResult,
    ) -> tuple[str, str, Any]:
        """Make a fresh transcript durable and classify what it asked for."""

        transcript = transcription.text or ""
        turn_id = uuid.uuid4().hex
        with self._active_lock:
            pending_agent = self._pending_agent
        self.last_heard.write(
            transcript=transcript,
            reason=capture.reason.value,
            session_id=self.state.snapshot().session_id,
            client_id=client_id,
            agent=pending_agent,
            turn_id=turn_id,
            delivered=False,
        )
        if (
            self._clipboard_transcript
            and capture.reason is not CaptureStopReason.DELIVERED_TEXT
        ):
            # Everything spoken is left on the clipboard, so a turn the user
            # meant to land somewhere can always be pasted by hand. Skipped for
            # delivered text, which the user typed and already has.
            copy_to_clipboard(transcript, runner=self._clipboard_runner)
        return transcript, turn_id, classify_spoken_intent(transcript)

    async def _heard_result(
        self,
        client_id: str,
        capture: CaptureResult,
        transcription: SpeechResult,
        transcript: str,
        turn_id: str,
        intent: Any,
    ) -> dict[str, Any]:
        """Settle the session for a completed turn and shape its result.

        Shared by the normal listen loop and the barge-in path so intent
        handling, session bookkeeping, and undelivered-transcript recovery
        cannot drift apart between them.
        """

        result: dict[str, Any] = {
            "status": "completed",
            "transcript": transcript,
            "intent": intent.intent.value,
            "backend": transcription.backend,
            "fallback": transcription.fallback,
            "timings": {
                "capture_seconds": round(capture.audio_duration_s, 3),
                "stt_ms": transcription.elapsed_ms,
            },
            "capture": _capture_dict(capture),
            "turn_id": turn_id,
        }
        if intent.intent is SpokenIntent.STOP:
            self.state.stop(StopReason.USER_REQUEST, client_id=client_id)
            await self.lease.release(client_id)
            result["control"] = {"action": "stop", "session_ended": True}
        elif intent.intent is SpokenIntent.PAUSE:
            self.state.pause("spoken pause", client_id=client_id)
            result["control"] = {"action": "pause"}
        else:
            self.state.complete_turn(client_id=client_id)
            if intent.intent is not SpokenIntent.NONE:
                result["control"] = {"action": intent.intent.value}
        result["session"] = self.state.snapshot().to_dict()
        # Durable before the tool returns so host cancel cannot erase it.
        self._set_pending_heard(result, turn_id)
        return result

    async def converse(
        self,
        client_id: str,
        message: str,
        *,
        wait_for_response: bool = True,
        voice: str | None = None,
        speed: float = 1.0,
        instructions: str | None = None,
        listen_duration_max: float | None = None,
        listen_duration_min: float | None = None,
        trailing_silence_s: float | None = None,
        onset_timeout: float | None = None,
        language: str | None = None,
        agent: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        mode = self._io_mode
        # Session io_mode is the panel/user preference; it wins over the caller's
        # wait_for_response when it forces narrate (no mic) or dictate (no TTS).
        effective_wait = wait_for_response and mode != "narrate"
        skip_speak = mode == "dictate"

        async def operation() -> dict[str, Any]:
            # Resolved inside the turn, not before it: resolving first would let
            # voice-lookup latency decide queue order instead of arrival order.
            # An explicit voice= always wins over the agent's assigned voice.
            self._hot_listen_after_tts = effective_wait and not skip_speak
            try:
                if skip_speak:
                    spoken = {
                        "status": "skipped",
                        "reason": "io_mode_dictate",
                        "backend": None,
                        "elapsed_ms": 0,
                    }
                else:
                    spoken = await self._speak_locked(
                        client_id,
                        message,
                        voice=voice or await self._agent_voice(agent),
                        speed=speed,
                        instructions=instructions,
                        interruptible=True,
                        barge_in=effective_wait and self.barge_in_availability()["available"],
                    )
                result: dict[str, Any] = {"spoken": spoken, "io_mode": mode}
                if spoken.get("status") == "barge_in":
                    # The user talked over the reply. The capture that detected
                    # them is still running and already holds their opening words
                    # in its pre-roll, so it *is* the answer — opening a second
                    # listen here would ask them to repeat themselves.
                    result["barge_in"] = True
                    result["heard"] = await self._harvest_barge_in(client_id, language=language)
                elif effective_wait and spoken.get("status") != "cancelled":
                    result["heard"] = await self._listen_locked(
                        client_id,
                        listen_duration_max=listen_duration_max,
                        listen_duration_min=listen_duration_min,
                        trailing_silence_s=trailing_silence_s,
                        onset_timeout=onset_timeout,
                        language=language,
                    )
                result["status"] = (
                    result.get("heard", {}).get("status")
                    if effective_wait
                    else spoken.get("status")
                )
                result["session"] = self.state.snapshot().to_dict()
                return result
            finally:
                self._hot_listen_after_tts = False

        return await self._run_operation(client_id, "converse", operation, agent=agent)

    async def speak(
        self,
        client_id: str,
        message: str,
        *,
        voice: str | None = None,
        speed: float = 1.0,
        instructions: str | None = None,
        interruptible: bool = True,
        agent: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            if self._io_mode == "dictate":
                return {
                    "status": "skipped",
                    "reason": "io_mode_dictate",
                    "io_mode": self._io_mode,
                    "session": self.state.snapshot().to_dict(),
                }
            result = await self._speak_locked(
                client_id,
                message,
                voice=voice or await self._agent_voice(agent),
                speed=speed,
                instructions=instructions,
                interruptible=interruptible,
            )
            return {**result, "io_mode": self._io_mode, "session": self.state.snapshot().to_dict()}

        return await self._run_operation(client_id, "speak", operation, agent=agent)

    async def read_aloud(self, client_id: str, *, text: str | None = None, **_: Any) -> dict[str, Any]:
        """Read the user's selection back to them, exactly as written.

        The verbatim guarantee is structural rather than a promise: this goes
        straight to Kokoro, so there is no model, prompt, or rewrite anywhere
        on the path that could paraphrase a number, a name, or a line of code.
        Deliberately not routed through converse for that reason.

        It queues behind an agent that is already speaking instead of cutting
        it off — which is simply what the operation gate already does.
        """

        message = (text or "").strip()
        if not message:
            return {"status": "skipped", "reason": "empty_selection"}

        async def operation() -> dict[str, Any]:
            # io_mode is about what agents may do with the speaker; this is the
            # user asking out loud, so it applies regardless of mode.
            result = await self._speak_locked(
                client_id,
                message,
                voice=await self._agent_voice(None),
                speed=1.0,
                instructions=None,
                interruptible=True,
            )
            self._log("read_aloud.completed", client_id=client_id, chars=len(message))
            return {**result, "session": self.state.snapshot().to_dict()}

        return await self._run_operation(client_id, "read_aloud", operation)

    async def listen(
        self,
        client_id: str,
        *,
        listen_duration_max: float | None = None,
        listen_duration_min: float | None = None,
        trailing_silence_s: float | None = None,
        onset_timeout: float | None = None,
        language: str | None = None,
        agent: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        return await self._run_operation(
            client_id,
            "listen",
            lambda: self._listen_locked(
                client_id,
                listen_duration_max=listen_duration_max,
                listen_duration_min=listen_duration_min,
                trailing_silence_s=trailing_silence_s,
                onset_timeout=onset_timeout,
                language=language,
            ),
            agent=agent,
        )

    async def note(
        self,
        client_id: str,
        *,
        target_agent: str | None = None,
        language: str | None = None,
        agent: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """User-initiated capture addressed to one agent. Records an utterance
        and holds it for ``target_agent`` (recognised by its voice/project) to
        claim on its next turn — "leave a note and walk away." An empty target
        broadcasts to whichever agent asks first.
        """

        return await self._run_operation(
            client_id,
            "note",
            lambda: self._note_locked(client_id, target_agent=target_agent, language=language),
            agent=agent,
        )

    async def reply(
        self,
        client_id: str,
        *,
        language: str | None = None,
        agent: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """User-initiated "reply" — the same capture-and-hold as a note, but
        auto-addressed to the agent whose voice last spoke, so the user can
        answer what they just heard without picking an agent. Falls back to a
        broadcast if nothing has spoken yet.
        """

        with self._active_lock:
            target = self._last_spoken_agent
        return await self._run_operation(
            client_id,
            "note",
            lambda: self._note_locked(client_id, target_agent=target, language=language),
            agent=agent,
        )

    async def _gate_open(self, client_id: str) -> dict[str, Any]:
        """Open the gate and, if nothing is listening yet, start the turn.

        This is the first tap of the turn key. When a converse is already
        waiting on the microphone it simply starts receiving frames; otherwise
        the runtime opens a turn of its own, addressed to whoever last spoke,
        exactly as the reply hotkey does.
        """

        source = await self._ensure_source()
        if source is None:
            raise ServiceUnavailableError(
                "no persistent capture stream is available; use end_turn instead"
            )
        opened = source.open_gate()
        with self._active_lock:
            listening = self._capture_control is not None
            target = self._last_spoken_agent
        if listening:
            # The listen already holds the device; this tap only opened its gate.
            self._release_source()
            self._log("gate.opened", client_id=client_id, started_turn=False)
            return {
                "status": "gate_opened",
                "gate_open": True,
                "opened": opened,
                "listening": True,
            }

        task = asyncio.create_task(
            self._run_operation(
                client_id,
                "note",
                lambda: self._note_locked(
                    client_id,
                    target_agent=target,
                    recorder=self._open_ended_recorder(),
                ),
            )
        )
        # Without a strong reference the loop may collect the task mid-capture.
        self._detached_turns.add(task)
        task.add_done_callback(self._detached_turns.discard)
        # Held until the turn this tap started is actually under way. The turn's
        # own _capture_once takes a hold of its own; the linger covers the gap
        # between this release and that claim.
        task.add_done_callback(lambda _task: self._release_source())
        self._log("gate.opened", client_id=client_id, started_turn=True)
        return {
            "status": "gate_opened",
            "gate_open": True,
            "opened": opened,
            "listening": False,
        }

    async def dictate(self, client_id: str, action: str, **_: Any) -> dict[str, Any]:
        """Hold-to-talk dictation: no agent, no TTS, no MCP session required."""

        action = action.lower().strip()
        if action == "dictate_start":
            return await self._dictate_start(client_id)
        if action == "dictate_end":
            return await self._dictate_end(client_id)
        raise VoxError(f"Unknown dictation action: {action}")

    async def _dictate_start(self, client_id: str) -> dict[str, Any]:
        with self._active_lock:
            if self._dictation_control is not None:
                return {"status": "already_dictating"}
        # Dictation is an explicit, physical act by the user: it outranks
        # whatever Vox was doing. Any live turn is ended the honest way — the
        # words already spoken are still transcribed and submitted — and
        # playback stops so the headset is not talking over the dictation.
        self._signal_cancel(manual_end=True, cancel_task=False)
        self.player.cancel_all()
        if not await self._wait_for_microphone_closed():
            raise ServiceUnavailableError("microphone is still closing; try again in a moment")

        recorder = self.recorder if isinstance(self.recorder, AudioRecorder) else AudioRecorder()
        control = CaptureControl()
        loop = asyncio.get_running_loop()
        source = await self._ensure_source()
        owned = False
        if source is None:
            # Persistent capture is switched off, but dictation still needs a
            # stream. Borrow one for the length of the hold and give it back.
            device, sounddevice, stream_factory = self._capture_backend(recorder)
            source = PersistentCaptureSource(
                recorder.config,
                device=device,
                sounddevice=sounddevice,
                stream_factory=stream_factory,
                open_guard_s=self._stream_open_guard_s,
                on_event=self._log,
            )
            await asyncio.to_thread(source.open)
            owned = True

        # Deliberately *not* waiting out the stream-open guard, unlike a spoken
        # turn. The guard exists to stop the Bluetooth open transient from
        # satisfying speech onset, and dictation runs capture_raw_from_frames,
        # which has no onset detection to fool — the pop just reaches Whisper as
        # a click, which it handles. Waiting here would instead cost a full
        # second of dead air on a key the user is already talking into, and eat
        # their first word every single time.
        source.open_gate()
        held = source
        try:
            destination = self.store.new_work_path("stt")
            future = loop.run_in_executor(
                None,
                lambda: recorder.capture_raw_from_frames(
                    # respect_guard=False, or the callback would keep dropping
                    # frames for the whole open guard and swallow the first
                    # second of the hold — the very thing skipping the wait was
                    # supposed to prevent.
                    held.frames(control, respect_guard=False),
                    held.sample_rate,
                    destination=destination,
                    control=control,
                    max_duration_s=self._dictation_max_s,
                ),
            )
        except BaseException:
            # Nothing will ever call the done-callback that gives the device
            # back, so hand it back here or the indicator stays lit.
            held.close_gate()
            if owned:
                await asyncio.to_thread(held.close)
            else:
                self._release_source()
            raise
        with self._active_lock:
            self._dictation_control = control
            self._dictation_future = future
            self._dictation_source = source if owned else None
            self._microphone_active = True
            self._microphone_closing = False

        def microphone_closed(_future: asyncio.Future[Any]) -> None:
            held.close_gate()
            if not owned:
                # Dictation used to hand a shared stream back still *open* and
                # nothing ever closed it, so a single hold-to-talk press left the
                # macOS microphone indicator lit until the next pause or stop —
                # with no session running and the gate shut. Releasing here
                # covers the key-released path and the max-duration path alike.
                self._release_source()
            with self._active_lock:
                self._microphone_active = False
                self._microphone_closing = False

        future.add_done_callback(microphone_closed)
        self._log("dictation.started", client_id=client_id)
        return {"status": "dictating"}

    async def _dictate_end(self, client_id: str) -> dict[str, Any]:
        with self._active_lock:
            control = self._dictation_control
            future = self._dictation_future
            owned_source = self._dictation_source
            self._dictation_control = None
            self._dictation_future = None
            self._dictation_source = None
        if control is None or future is None:
            return {"status": "not_dictating", "text": ""}
        control.end_utterance()
        try:
            capture = await asyncio.wait_for(asyncio.shield(future), timeout=10.0)
        except Exception as exc:
            self._log("dictation.failed", client_id=client_id, error=type(exc).__name__)
            return {"status": "failed", "text": ""}
        finally:
            if owned_source is not None:
                await asyncio.to_thread(owned_source.close)

        if capture.latest_wav_path is None or not capture.speech_detected:
            self._log("dictation.empty", client_id=client_id, reason=capture.reason.value)
            return {"status": "no_speech", "text": ""}
        try:
            transcription = await self.speech.transcribe(
                capture.latest_wav_path, language=self.default_language
            )
        finally:
            # Dictation has no recovery story — the text goes straight to the
            # cursor — so the recording has no reason to outlive the transcribe.
            capture.latest_wav_path.unlink(missing_ok=True)
        text = clean_dictation(transcription.text, mode=self._dictation_cleanup)
        # The event log redacts any field whose name looks like a transcript;
        # the dictated words are the user's, going to the user's own cursor,
        # and have no business being written to disk here at all.
        self._log(
            "dictation.completed",
            client_id=client_id,
            chars=len(text),
            seconds=round(capture.audio_duration_s, 2),
            backend=transcription.backend,
        )
        if not text:
            return {"status": "no_speech", "text": ""}
        return {"status": "dictated", "text": text, "chars": len(text)}

    def _open_ended_recorder(self) -> Any:
        """A recorder that will not hang up while the key says "I'm talking"."""

        if not isinstance(self.recorder, AudioRecorder):
            return self.recorder
        return AudioRecorder(replace(self.recorder.config, onset_timeout_s=None))

    async def _note_locked(
        self,
        client_id: str,
        *,
        target_agent: str | None = None,
        language: str | None = None,
        recorder: Any | None = None,
    ) -> dict[str, Any]:
        capture, transcription = await self._capture_once(
            client_id,
            recorder=recorder,
            language=language,
            skip_open_guard=True,
        )
        if transcription is None:
            return {
                "status": (
                    "cancelled"
                    if capture.reason is CaptureStopReason.CANCELLED
                    else "no_speech"
                ),
                "reason": capture.reason.value,
                "session": self.state.snapshot().to_dict(),
            }
        transcript = transcription.text or ""
        turn_id = uuid.uuid4().hex
        self.notes.put(
            target_agent,
            transcript=transcript,
            turn_id=turn_id,
            reason=capture.reason.value,
        )
        self.state.complete_turn(client_id=client_id)
        self._log(
            "note.captured",
            client_id=client_id,
            target_agent=target_agent or "*",
            chars=len(transcript),
        )
        return {
            "status": "noted",
            "transcript": transcript,
            "turn_id": turn_id,
            "target_agent": (target_agent or "").strip() or "*",
            "session": self.state.snapshot().to_dict(),
        }

    async def session(
        self,
        client_id: str,
        action: str,
        *,
        pause_seconds: float | None = None,
        target_client_id: str | None = None,
        force: bool = False,
        agent: str | None = None,
        mode: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        action = action.lower().strip()
        administrative = client_id == "http-control"
        if action == "status":
            return await self._status_for_agent(agent)
        if action in {"set_mode", "mode"}:
            selected = str(mode or "").strip().lower()
            if not selected:
                raise VoxError("set_mode requires mode=talk|narrate|dictate")
            self._save_io_mode(selected)
            self._log(
                "session.set_mode",
                client_id=client_id,
                agent=agent or DEFAULT_AGENT,
                mode=selected,
            )
            return await self._status_for_agent(agent)
        if action == "cycle_mode":
            idx = IO_MODES.index(self._io_mode) if self._io_mode in IO_MODES else 0
            selected = IO_MODES[(idx + 1) % len(IO_MODES)]
            self._save_io_mode(selected)
            self._log(
                "session.cycle_mode",
                client_id=client_id,
                agent=agent or DEFAULT_AGENT,
                mode=selected,
            )
            return await self._status_for_agent(agent)
        if action == "claim_undelivered":
            # A note addressed to this agent takes priority over the global
            # crash-recovery slot, so each agent gets only what was meant for it.
            note = self.notes.claim(agent or DEFAULT_AGENT)
            payload = await self._status_for_agent(agent)
            payload["undelivered_heard"] = {"present": False}
            if note is not None:
                payload["claimed_heard"] = {
                    "transcript": str(note.get("transcript", "")),
                    "kind": "note",
                    "target_agent": note.get("target_agent", "*"),
                    # How many separate times the user spoke while this agent
                    # was busy. One transcript, but not one sentence.
                    "count": int(note.get("count", 1)),
                }
                return payload
            # Addressed, like the note above. The recovery slot is global and
            # carries the agent it was captured for, so claiming it unfiltered
            # let a different project walk off with these words.
            claimed = self.last_heard.claim(agent or DEFAULT_AGENT)
            payload["claimed_heard"] = (
                claimed.public(include_transcript=True) if claimed is not None else None
            )
            return payload
        if action == "start":
            if administrative:
                if self.state.state is SessionState.OFF:
                    self.state.start(client_id=None)
            else:
                await self._claim(client_id)
        elif action in {"pause", "mute"}:
            if not administrative and not force:
                await self._claim(client_id)
            self._signal_cancel(manual_end=False, force=True)
            # A turn that queued before the pause would otherwise inherit the
            # mic afterwards and speak straight through a privacy hold.
            self.gate.drain("paused")
            if not await self._wait_for_microphone_closed():
                raise ServiceUnavailableError("microphone is still closing; Vox remains fail-closed")
            # Pause is the privacy stop, not the gate: the stream itself goes,
            # so the device is released and the indicator light goes out.
            await self._close_source()
            if self.state.state is not SessionState.PAUSED:
                if self.state.state not in {SessionState.IDLE, SessionState.OFF}:
                    self._return_idle_if_active()
                if self.state.state is SessionState.IDLE:
                    until = time.time() + pause_seconds if pause_seconds else None
                    self.state.pause(action, until=until, client_id=None)
        elif action == "resume":
            if not administrative and not force:
                await self._claim(client_id, start=False)
            self.state.resume(client_id=None)
        elif action == "stop":
            self._signal_cancel(manual_end=False, force=True)
            self.gate.drain("stopped")
            if not await self._wait_for_microphone_closed():
                raise ServiceUnavailableError("microphone is still closing; Vox remains fail-closed")
            await self._close_source()
            if self.state.state is not SessionState.OFF:
                self.state.stop(StopReason.USER_REQUEST, client_id=None)
            # Stop is a global privacy/safety control. Anyone on the local MCP
            # surface may close the microphone and release stale ownership.
            await self.lease.release(client_id, force=True)
        elif action == "handoff":
            # Shared session: no exclusive ownership to transfer. Optionally
            # record the target as last actor for diagnostics.
            if not target_client_id:
                raise VoxError("handoff requires target_client_id")
            target_client_id = _canonical_client_id(target_client_id)
            result = await self.lease.handoff(client_id, target_client_id)
            self._log("session.handoff", client_id=client_id, agent=agent or DEFAULT_AGENT)
            payload = await self._status_for_agent(agent)
            payload.update(
                {
                    "status": "shared",
                    "detail": "shared session: no exclusive handoff; queue serializes audio",
                    "handoff": result,
                }
            )
            return payload
        elif action == "takeover":
            # Shared session: "takeover" means cancel whatever is holding the
            # mic and drain the queue so this client can speak next — not
            # steal exclusive ownership (there is none).
            del force
            self._signal_cancel(manual_end=False, force=True)
            self.gate.drain("takeover")
            if not await self._wait_for_microphone_closed():
                raise ServiceUnavailableError(
                    "cannot take over while the previous microphone turn is still closing"
                )
            self._return_idle_if_active()
            await self.lease.claim(client_id)
            if self.state.state is SessionState.OFF:
                self.state.start(client_id)
            elif self.state.state is SessionState.ERROR:
                try:
                    self.state.recover(client_id=client_id)
                except Exception:
                    self.state.stop(StopReason.ERROR, client_id=None)
                    self.state.start(client_id)
            elif self.state.state is SessionState.PAUSED:
                self.state.resume(client_id=None)
        else:
            raise VoxError(f"Unknown voice session action: {action}")
        self._log(f"session.{action}", client_id=client_id, agent=agent or DEFAULT_AGENT)
        return await self._status_for_agent(agent)

    async def control(
        self,
        client_id: str,
        action: str,
        *,
        offset: int = 0,
        text: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        action = action.lower().strip()
        if action in {"cancel", "skip_forward", "stop_audio"}:
            signal = self._signal_cancel(manual_end=False)
            # Cancelling only the active turn would leave the queue behind it
            # waiting to speak, so the mic frees up and then talks anyway.
            drained = self.gate.drain("cancelled")
            return {"status": "cancel_signalled", "queue_drained": drained, **signal}
        if action in {"manual_end", "push_to_talk_end"}:
            signal = self._signal_cancel(manual_end=True, cancel_task=False)
            return {"status": "manual_end_signalled", **signal}
        if action == "gate_open":
            return await self._gate_open(client_id)
        if action == "gate_close":
            # Manual end first, then the gate: ending the capture is what
            # transcribes and submits what was said, and the reason has to name
            # what the user actually did.
            signal = self._signal_cancel(manual_end=True, cancel_task=False)
            source = self._source
            closed = source.close_gate() if source is not None else False
            self._log("gate.closed", client_id=client_id, ended=signal.get("signalled", False))
            return {"status": "gate_closed", "gate_open": False, "closed": closed, **signal}
        if action == "deliver_text":
            return self._deliver_text(text)
        if action in {"repeat", "skip_back"}:
            await self._claim(client_id)
            replay = self.store.replay(offset)
            if replay is None:
                return {"status": "not_found", "offset": offset}

            async def replay_operation() -> dict[str, Any]:
                self.state.begin_speaking(client_id=client_id)
                handle = self.player.play_file(replay, volume=self.volume, blocking=False)
                with self._active_lock:
                    self._playback = handle
                code = await asyncio.to_thread(handle.wait)
                self.state.complete_turn(client_id=client_id)
                return {"status": "completed", "return_code": code, "offset": offset}

            return await self._run_operation(client_id, "replay", replay_operation)
        if action == "status":
            return await self.status()
        raise VoxError(f"Unknown voice control action: {action}")

    async def service(
        self,
        client_id: str,
        service_name: str,
        action: str,
        *,
        lines: int = 50,
        **_: Any,
    ) -> dict[str, Any]:
        del client_id
        action = action.lower()
        if service_name == "all":
            if action not in {"status", "health"}:
                raise VoxError("Service 'all' supports status/health only")
            return {
                name: status.to_dict()
                for name, status in (await self.supervisor.all_statuses()).items()
            }
        if service_name == "vox":
            if action in {"status", "health"}:
                return await self.health()
            raise VoxError(
                "The running Vox process cannot manage its own launchd job; "
                "use `vox restart-runtime` from the CLI"
            )
        if action == "status":
            return (await self.supervisor.status(service_name)).to_dict()
        if action == "start":
            return (await self.supervisor.start(service_name)).to_dict()
        if action == "stop":
            return (await self.supervisor.stop(service_name)).to_dict()
        if action == "restart":
            return (await self.supervisor.restart(service_name)).to_dict()
        if action == "logs":
            return (await self.supervisor.tail_logs(service_name, lines=lines)).to_dict()
        if action in {"enable", "disable"}:
            raise VoxError("Vox uses explicit launchd install/rollback, not runtime enable toggles")
        raise VoxError(f"Unknown service action: {action}")

    async def diagnostics(
        self, client_id: str, *, section: str = "all", **_: Any
    ) -> dict[str, Any]:
        del client_id
        result = static_diagnostics()
        result["services"] = {
            name: status.to_dict()
            for name, status in (await self.supervisor.all_statuses()).items()
        }
        result["runtime"] = await self.status()
        result["privacy"] = {
            "local_only": True,
            "persist_audio": self.config.persist_audio,
            "persist_transcripts": self.config.persist_transcripts,
            "microphone_open": self.microphone_open,
            # When enabled, the microphone is open during playback so you can
            # interrupt by talking. That is a real widening of when the mic is
            # live, so it is reported here and not only in the tuning knobs.
            "barge_in_enabled": self.config.barge_in_enabled,
            "barge_in": self.barge_in_availability(),
            "mic_armed_for_barge_in": self.barge_in_armed,
            "control_token_path": str(self.control_token_path),
            # local_only above is a true statement about this process: Vox
            # itself opens no outbound socket, ever. But a companion turn is
            # answered by grokctl, which calls xAI on your behalf, and pretending
            # that is purely local would be the dishonest half of a true claim.
            "companion": {
                "enabled": self.config.companion_enabled,
                "backend": "grokctl",
                "egress": self.config.companion_enabled,
                "note": (
                    "Companion turns are answered by a separate local process "
                    "(grokctl) which calls xAI. Vox opens no outbound sockets."
                ),
            },
        }
        try:
            result["events"] = read_events(self.config.event_log_path)[-50:]
        except Exception:
            result["events"] = []
        if section == "all":
            return result
        if section not in result:
            raise VoxError(f"Unknown diagnostics section: {section}")
        return {section: result[section]}

    async def transcribe(
        self,
        client_id: str,
        *,
        path: str | None = None,
        latest: bool = False,
        language: str | None = None,
        prompt: str | None = None,
        output_format: str = "text",
        word_timestamps: bool = False,
        **_: Any,
    ) -> dict[str, Any]:
        del client_id
        audio = self.store.latest_stt if latest or path is None else Path(path).expanduser()
        if output_format not in {"text", "json", "srt", "vtt", "csv"}:
            raise VoxError(f"Unsupported transcription format: {output_format}")
        result = await self.speech.transcribe(
            audio,
            language=language or self.default_language,
            prompt=prompt,
            word_timestamps=word_timestamps,
        )
        payload = {**result.to_dict(), "status": "completed", "output_format": output_format}
        if output_format == "text":
            payload["formatted_content"] = result.text or ""
        elif output_format != "json":
            payload["formatted_content"] = _format_transcription(payload, output_format)
        return payload

    async def _available_voices(self) -> list[str]:
        """Return the local Kokoro voices, cached; the pool rarely changes."""

        if self._voice_pool is not None:
            return self._voice_pool
        endpoint = self.config.tts_url.rstrip("/") + "/audio/voices"
        try:
            async with httpx.AsyncClient(
                timeout=3.0, trust_env=False, follow_redirects=False
            ) as client:
                response = await client.get(endpoint)
                response.raise_for_status()
                payload = response.json()
            voices = payload if isinstance(payload, list) else payload.get("voices", [])
            pool = [str(voice) for voice in voices if str(voice).strip()]
        except Exception:
            pool = []
        if not pool:
            # Do not cache a fallback: Kokoro may just be starting up.
            return list(FALLBACK_VOICES)
        self._voice_pool = pool
        return pool

    async def _agent_voice(self, agent: str | None) -> str:
        label = agent or DEFAULT_AGENT
        if label == DEFAULT_AGENT:
            return self.default_voice
        # Only a first-ever assignment needs the live pool; a known agent must
        # never wait on Kokoro to find out what it already sounds like.
        existing = self.agent_voices.assignments.get(label)
        if existing:
            return existing
        return self.agent_voices.resolve(label, await self._available_voices())

    async def voice_registry(
        self, client_id: str, *, provider: str | None = None, **_: Any
    ) -> dict[str, Any]:
        del client_id
        if provider not in {None, "kokoro", "local"}:
            raise VoxError("Vox local-only mode exposes only local Kokoro voices")
        voices = await self._available_voices()
        return {
            "provider": "kokoro",
            "endpoint": self.config.tts_url,
            "voices": voices,
            "default": self.default_voice,
            "agents": dict(self.agent_voices.assignments),
            "local_only": True,
        }

    async def companion(
        self,
        client_id: str,
        *,
        action: str = "handoff",
        brief: str = "",
        budget_turns: int = 6,
        agent: str = COMPANION_AGENT,
        **_: Any,
    ) -> dict[str, Any]:
        """Hand the conversation to a fast model while the real agent works.

        This is the answer to dead air, not to hard questions: the companion
        holds small talk at conversational speed and escalates everything else
        straight back.  It runs through ``_run_operation`` like any other turn,
        so the privacy pause and the FIFO gate apply unchanged.
        """

        if action != "handoff":
            raise VoxError(f"Unknown companion action: {action}")
        if not self.config.companion_enabled:
            return {
                "status": "disabled",
                "reason": "VOX_COMPANION_ENABLED is not set",
                "turns": [],
            }
        budget = max(1, min(int(budget_turns), 20))

        async def operation() -> dict[str, Any]:
            turns: list[dict[str, Any]] = []
            voice = await self._agent_voice(agent)
            opening = brief.strip() or "Give me a minute, I'm working on it."
            outcome = "completed"
            reason = "budget_exhausted"

            spoken = await self._speak_locked(
                client_id, opening, voice=voice, speed=1.0, instructions=None
            )
            if spoken.get("status") == "cancelled":
                return {"status": "cancelled", "turns": turns, "reason": "cancelled"}

            silences = 0
            for _ in range(budget):
                heard = await self._listen_locked(client_id)
                status = heard.get("status")
                if status != "completed":
                    # One quiet moment is not the user walking away, and a noise
                    # burst that trips the gate without words is not a turn at
                    # all. Reopen rather than handing back after the first
                    # silence — the budget still bounds how long that goes on.
                    silences += 1
                    if status == "no_speech" and silences < 3:
                        continue
                    reason = str(status or "no_speech")
                    break
                silences = 0
                transcript = str(heard.get("transcript") or "")
                if heard.get("control", {}).get("action") in {
                    "stop",
                    "pause",
                } or companion_should_stop(transcript):
                    outcome, reason = "completed", "user_stopped"
                    turns.append({"heard": transcript, "said": None})
                    break
                if not companion_may_answer(transcript):
                    # Whitelist-only: anything that might be about the work goes
                    # back to the agent that actually knows it.
                    outcome, reason = "escalated", "out_of_scope"
                    turns.append({"heard": transcript, "said": None, "escalated": True})
                    break
                reply = await ask_companion(transcript)
                if not reply.ok:
                    outcome, reason = "escalated", reply.reason
                    turns.append({"heard": transcript, "said": None, "escalated": True})
                    break
                turns.append(
                    {"heard": transcript, "said": reply.text, "elapsed_ms": reply.elapsed_ms}
                )
                self._log(
                    "companion.turn",
                    client_id=client_id,
                    backend=reply.backend,
                    elapsed_ms=reply.elapsed_ms,
                )
                said = await self._speak_locked(
                    client_id, reply.text, voice=voice, speed=1.0, instructions=None
                )
                if said.get("status") == "cancelled":
                    outcome, reason = "cancelled", "cancelled"
                    break

            return {
                "status": outcome,
                "reason": reason,
                "turns": turns,
                "agent": agent,
                # Everything the companion heard, so the real agent can pick the
                # conversation up without asking the user to repeat themselves.
                "transcript": [turn["heard"] for turn in turns if turn.get("heard")],
            }

        return await self._run_operation(client_id, "companion", operation, agent=agent)

    async def survey(
        self,
        client_id: str,
        turns: list[dict[str, Any]],
        agent: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        if not 1 <= len(turns) <= 50:
            raise VoxError("voice_survey requires 1-50 turns")

        async def operation() -> dict[str, Any]:
            results: list[dict[str, Any]] = []
            # One voice for the whole run: a scripted interview read by a
            # rotating cast would sound like a fault, not a feature.
            default_voice = await self._agent_voice(agent) if agent else None
            for index, turn in enumerate(turns):
                message = str(turn.get("message", ""))
                try:
                    spoken = await self._speak_locked(
                        client_id,
                        message,
                        voice=turn.get("voice") or default_voice,
                        speed=float(turn.get("speed", 1.0)),
                        instructions=turn.get("instructions"),
                    )
                    heard = (
                        await self._listen_locked(client_id)
                        if turn.get("wait_for_response", True)
                        and spoken.get("status") != "cancelled"
                        else None
                    )
                    results.append({"index": index, "spoken": spoken, "heard": heard})
                    if heard and heard.get("control", {}).get("action") == "stop":
                        break
                except Exception as exc:
                    self._signal_cancel(manual_end=False, cancel_task=False, force=True)
                    await self._wait_for_microphone_closed()
                    self._return_idle_if_active()
                    results.append(
                        {
                            "index": index,
                            "status": "failed",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    break
            completed = sum(1 for item in results if "error" not in item)
            return {
                "status": "completed" if completed == len(turns) else "partial",
                "results": results,
                "completed": completed,
                "requested": len(turns),
            }

        return await self._run_operation(client_id, "survey", operation)

    async def exchange_history(
        self,
        client_id: str,
        *,
        action: str = "list",
        limit: int = 20,
        days: int = 7,
        **_: Any,
    ) -> dict[str, Any]:
        del client_id
        if action not in {"list", "stats", "conversations"}:
            raise VoxError(f"Unsupported exchange history action: {action}")
        if not 1 <= limit <= 200 or not 1 <= days <= 90:
            raise VoxError("exchange history requires limit 1-200 and days 1-90")
        return await asyncio.to_thread(
            _privacy_filtered_exchange_history,
            Path.home() / ".voicemode" / "logs" / "conversations",
            action,
            limit,
            days,
        )

    async def _status_for_agent(self, agent: str | None) -> dict[str, Any]:
        """Status plus who is asking and which voice they will speak in.

        A note addressed to this agent surfaces as ``undelivered_heard`` here (and
        only here), so an agent sees only its own notes and the existing
        claim_undelivered flow delivers it.
        """

        label = agent or DEFAULT_AGENT
        status = await self.status()
        status["agent"] = {"id": label, "voice": await self._agent_voice(label)}
        note = self.notes.get(label)
        if note is not None:
            transcript = str(note.get("transcript", ""))
            status["undelivered_heard"] = {
                "present": True,
                "kind": "note",
                "char_count": len(transcript),
                # Separate times the user spoke while this agent was busy, and
                # how long the first of them has been waiting.
                "count": int(note.get("count", 1)),
                "age_s": round(time.time() - float(note.get("captured_at", time.time())), 1),
            }
        return status

    async def status(self) -> dict[str, Any]:
        self.state.resume_if_due()
        self.state.expire_if_idle()
        session = self.state.snapshot().to_dict()
        session["microphone_open"] = self.microphone_open
        microphone_open = self.microphone_open
        detail = _state_detail(self.state.state)
        with self._active_lock:
            microphone_closing = self._microphone_closing
            barge_in_armed = self._barge_in_control is not None
        # An armed barge-in is the one case where an open microphone during
        # SPEAKING is intended rather than a mic that failed to close, so it
        # has to be tested before the fail-closed branches below.
        mic_armed_for_barge_in = (
            barge_in_armed and microphone_open and self.state.state is SessionState.SPEAKING
        )
        if mic_armed_for_barge_in:
            detail = "Playing local speech; microphone open so you can interrupt"
        elif microphone_open and microphone_closing:
            detail = "Microphone is still closing; Vox is fail-closed to new audio turns"
        elif microphone_open and self.state.state is not SessionState.LISTENING:
            detail = "Microphone is still closing; Vox is fail-closed to new audio turns"
        elif microphone_open:
            detail = "Listening for one bounded turn"
        undelivered = self.last_heard.undelivered()
        return {
            "status": "ok",
            "state": self.state.state.value,
            "detail": detail,
            "session": session,
            "lease": await self.lease.status(),
            "operation": await self.gate.status(),
            "storage": self.store.status(),
            "local_only": True,
            "io_mode": self._io_mode,
            "barge_in_enabled": self.config.barge_in_enabled,
            "mic_armed_for_barge_in": mic_armed_for_barge_in,
            "undelivered_heard": (
                undelivered.public() if undelivered is not None else {"present": False}
            ),
        }

    @property
    def microphone_open(self) -> bool:
        with self._active_lock:
            return self._microphone_active

    async def health(
        self, *, levels_since: int = 0, tts_levels_since: int = 0
    ) -> dict[str, Any]:
        """Runtime state, plus the waveform samples newer than ``levels_since``.

        The caller passes back the ``mic_levels_seq`` it last saw so it receives
        only what has arrived since. Reading is non-destructive, so a second
        caller — `vox doctor`, another agent's health probe — cannot steal the
        status app's waveform. ``tts_levels_since`` is the same contract for the
        speaking waveform, published from the samples actually being played.
        """

        status = await self.status()
        session = status["session"]
        undelivered = status.get("undelivered_heard") or {"present": False}
        with self._active_lock:
            mic_active = self._microphone_active
            # Every capture that can hold the microphone, in the order they can
            # be live. An armed capture publishes levels through the window where
            # the mic is open but the session still reads SPEAKING, and a
            # *dictation* publishes through the whole hold — leaving it out meant
            # the waveform sat dead flat for the one mode used from other apps.
            control = (
                self._capture_control or self._barge_in_control or self._dictation_control
            )
        # Live loudness for the waveform; 0 and empty whenever the mic is closed,
        # so a stale value can never make the meter look like it is hearing you.
        mic_level = control.level if (mic_active and control is not None) else 0.0
        # Every level measured since the caller's last poll, oldest first. Frames
        # are 20 ms, so this carries ~50 Hz of real detail regardless of how often
        # the app asks — a single sample per poll threw three of every four away
        # and made a 12.5 Hz staircase out of a smooth signal.
        if mic_active and control is not None:
            mic_levels, mic_levels_seq = control.levels_since(levels_since)
        else:
            mic_levels, mic_levels_seq = [], 0
        tts_levels, tts_levels_seq = self.playback_levels.levels_since(tts_levels_since)
        return {
            "status": "ok",
            "state": status["state"],
            "detail": status["detail"],
            "version": "0.1.0",
            "local_only": True,
            "microphone_open": session["microphone_open"],
            # The gate and the device are not the same thing:
            # gate_open is whether audio can reach Whisper, stream_open is
            # merely whether the device is held. The glyph tracks the gate.
            "gate_open": self.gate_open,
            "stream_open": self.stream_open,
            "barge_in_enabled": self.config.barge_in_enabled,
            "mic_armed_for_barge_in": status.get("mic_armed_for_barge_in", False),
            "mic_level": round(mic_level, 3),
            "mic_levels": [round(value, 3) for value in mic_levels],
            "mic_levels_seq": mic_levels_seq,
            # The speaking waveform, measured from the file being played and
            # released frame by frame as wall clock plays it — never synthetic.
            "tts_level": round(self.playback_levels.level, 3),
            "tts_levels": tts_levels,
            "tts_levels_seq": tts_levels_seq,
            # Kept deliberately compact and transcript-free for the native
            # status panel. This makes an automatic idle stop explainable
            # instead of looking like a crash.
            "last_stop_reason": session["last_stop_reason"],
            "idle_deadline_at": session["idle_deadline_at"],
            "io_mode": status.get("io_mode", "talk"),
            # Which room the voice is actually in. Without this, a phone that
            # silently dropped looks identical to a laptop nobody is answering.
            "phone": self.phone_status(),
            "undelivered_heard": {
                "present": bool(undelivered.get("present")),
                "age_s": undelivered.get("age_s"),
                "char_count": undelivered.get("char_count"),
            },
            # For the menu bar's note picker: the agents you can address (by
            # project/voice) and which of them already have a note waiting.
            "agents": sorted(self.agent_voices.assignments.keys()),
            "notes_waiting": self.notes.pending_targets(),
            # Who a menu-bar "Reply" would be addressed to (the last voice heard).
            "last_spoken_agent": self._last_spoken_agent,
        }


def _capture_dict(result: CaptureResult) -> dict[str, Any]:
    return {
        "reason": result.reason.value,
        "speech_detected": result.speech_detected,
        "sample_rate": result.sample_rate,
        # How long the microphone was actually open, which is not the same as
        # how much audio was kept: a discarded false start leaves the window
        # running, so only this number says whether the caller got the reply
        # window it asked for.
        "elapsed_seconds": round(result.elapsed_s, 3),
        "audio_duration_seconds": round(result.audio_duration_s, 3),
        "speech_duration_seconds": round(result.speech_duration_s, 3),
        "trailing_silence_seconds": round(result.trailing_silence_s, 3),
        "dropped_frames": result.dropped_frames,
        "recovery_path": str(result.latest_wav_path) if result.latest_wav_path else None,
    }


def _format_transcription(payload: dict[str, Any], output_format: str) -> str:
    """Render local Whisper segments without importing cloud-aware legacy code."""

    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments:
        text = str(payload.get("text") or "").strip()
        duration = float(payload.get("duration") or 0.0)
        segments = [{"text": text, "start": 0.0, "end": duration}]

    if output_format == "srt":
        lines: list[str] = []
        for index, segment in enumerate(segments, 1):
            lines.extend(
                [
                    str(index),
                    f"{_subtitle_timestamp(segment.get('start', 0), comma=True)} --> "
                    f"{_subtitle_timestamp(segment.get('end', 0), comma=True)}",
                    str(segment.get("text", "")).strip(),
                    "",
                ]
            )
        return "\n".join(lines)
    if output_format == "vtt":
        lines = ["WEBVTT", ""]
        for segment in segments:
            lines.extend(
                [
                    f"{_subtitle_timestamp(segment.get('start', 0))} --> "
                    f"{_subtitle_timestamp(segment.get('end', 0))}",
                    str(segment.get("text", "")).strip(),
                    "",
                ]
            )
        return "\n".join(lines)
    if output_format == "csv":
        output = io.StringIO()
        words = payload.get("words")
        rows = words if isinstance(words, list) and words else segments
        word_rows = bool(isinstance(words, list) and words)
        fields = ["word", "start", "end"] if word_rows else ["text", "start", "end"]
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            if isinstance(row, dict):
                writer.writerow(row)
        return output.getvalue()
    raise VoxError(f"Unsupported transcription format: {output_format}")


def _subtitle_timestamp(value: Any, *, comma: bool = False) -> str:
    seconds = max(0.0, float(value or 0.0))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    remainder = seconds % 60
    rendered = f"{hours:02d}:{minutes:02d}:{remainder:06.3f}"
    return rendered.replace(".", ",") if comma else rendered


def _privacy_filtered_exchange_history(
    directory: Path,
    action: str,
    limit: int,
    days: int,
) -> dict[str, Any]:
    """Read legacy exchange metadata while never returning transcript text or paths."""

    cutoff = date.today() - timedelta(days=days - 1)
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("exchanges_*.jsonl")):
        try:
            file_date = date.fromisoformat(path.stem.removeprefix("exchanges_"))
        except ValueError:
            continue
        if file_date < cutoff:
            continue
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                source = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(source, dict):
                continue
            metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
            text = source.get("text")
            rows.append(
                {
                    "timestamp": source.get("timestamp"),
                    "conversation_id": source.get("conversation_id"),
                    "type": source.get("type"),
                    "text_redacted": True,
                    "text_characters": len(text) if isinstance(text, str) else 0,
                    "duration_ms": source.get("duration_ms"),
                    "provider": metadata.get("provider"),
                    "provider_type": metadata.get("provider_type"),
                    "transport": metadata.get("transport"),
                    "voice": metadata.get("voice") if source.get("type") == "tts" else None,
                    "error": bool(metadata.get("error")),
                }
            )

    type_counts: dict[str, int] = {}
    providers: dict[str, int] = {}
    conversations: dict[str, int] = {}
    for row in rows:
        kind = str(row.get("type") or "unknown")
        type_counts[kind] = type_counts.get(kind, 0) + 1
        provider = str(row.get("provider") or "unknown")
        providers[provider] = providers.get(provider, 0) + 1
        conversation = str(row.get("conversation_id") or "unknown")
        conversations[conversation] = conversations.get(conversation, 0) + 1

    common = {
        "status": "completed",
        "privacy": "transcripts, project paths, audio paths, and provider URLs redacted",
        "days": days,
        "total_exchanges": len(rows),
    }
    if action == "list":
        return {**common, "exchanges": rows[-limit:]}
    if action == "conversations":
        return {
            **common,
            "conversations": [
                {"conversation_id": key, "exchange_count": value}
                for key, value in sorted(conversations.items(), key=lambda item: item[1], reverse=True)[
                    :limit
                ]
            ],
        }
    return {
        **common,
        "conversation_count": len(conversations),
        "by_type": type_counts,
        "by_provider": providers,
        "errors": sum(1 for row in rows if row["error"]),
    }


def _state_detail(state: SessionState) -> str:
    return {
        SessionState.OFF: "Voice mode is off; microphone closed",
        SessionState.IDLE: "Voice session ready; microphone closed",
        SessionState.LISTENING: "Listening for one bounded turn",
        SessionState.PROCESSING: "Transcribing locally; microphone closed",
        SessionState.SPEAKING: "Playing local speech; microphone closed",
        SessionState.PAUSED: "Voice session paused; microphone closed",
        SessionState.ERROR: "Voice runtime degraded; microphone closed",
    }[state]


def _canonical_client_id(value: str) -> str:
    candidate = value.strip()
    if candidate.startswith(("mcp:", "http-control")):
        return candidate
    normalized = re.sub(r"[^a-z0-9._-]+", "-", candidate.lower()).strip("-")
    if not normalized:
        raise VoxError("handoff target must name an MCP host")
    return f"mcp:host:{normalized[:96]}"
