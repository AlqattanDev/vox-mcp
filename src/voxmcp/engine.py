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
from dataclasses import replace
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .audio import (
    AudioPlayer,
    AudioRecorder,
    CaptureConfig,
    CaptureControl,
    CaptureResult,
    CaptureStopReason,
    PlaybackHandle,
)
from .compat import LegacyCompatibility
from .config import VoxConfig
from .diagnostics import static_diagnostics
from .errors import BusyError, PrivacyError, ServiceUnavailableError, VoxError
from .eventlog import JsonlEventLogger, read_events
from .intents import classify_spoken_intent
from .agents import AgentVoices, FALLBACK_VOICES
from .lease import DEFAULT_AGENT, LeaseManager, OperationGate
from .models import SessionState, SpokenIntent, StopReason
from .services import ServiceSupervisor
from .speech import LocalSpeechClient, SpeechResult, synthesize_with_macos_say
from .state import IllegalStateTransition, VoiceStateMachine
from .storage import AudioStore


def _default_home() -> Path:
    return Path(os.environ.get("VOX_HOME", "~/.vox")).expanduser()


class VoxEngine:
    """Coordinates state, hardware, local speech services, and compatibility tools."""

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
        compatibility: LegacyCompatibility,
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
        self.compatibility = compatibility
        self.lease = lease or LeaseManager(ttl_seconds=max(30.0, config.idle_timeout_seconds))
        self.gate = gate or OperationGate()
        self.gate.on_event = self._log_queue_event

        self.default_voice = os.environ.get("VOX_VOICE", "af_sky").lower()
        self.agent_voices = AgentVoices(home / "agents.json", default_voice=self.default_voice)
        self._voice_pool: list[str] | None = None
        self.default_language = os.environ.get("VOX_LANGUAGE", "en")
        self.default_wait_seconds = float(os.environ.get("VOX_WAIT_SECONDS", "60"))
        self.input_device: int | str | None = os.environ.get("VOX_INPUT_DEVICE")
        self.volume = min(1.0, max(0.0, float(os.environ.get("VOX_VOLUME", "1"))))

        self._active_lock = threading.RLock()
        self._active_task: asyncio.Task[Any] | None = None
        self._active_client: str | None = None
        self._capture_control: CaptureControl | None = None
        self._playback: PlaybackHandle | None = None
        self._playback_interruptible = True
        self._cancel_requested = False
        self._microphone_active = False
        self._microphone_closing = False
        self._audio_tempdir: tempfile.TemporaryDirectory[str] | None = None

    @classmethod
    def default(cls) -> "VoxEngine":
        home = _default_home()
        for directory in (home, home / "state", home / "logs"):
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
            onset_timeout_s=float(os.environ.get("VOX_ONSET_TIMEOUT_SECONDS", "15")),
            trailing_silence_s=float(os.environ.get("VOX_TRAILING_SILENCE_SECONDS", "1.2")),
            min_duration_s=float(os.environ.get("VOX_MIN_UTTERANCE_SECONDS", "0.5")),
            max_duration_s=float(os.environ.get("VOX_MAX_UTTERANCE_SECONDS", "300")),
            pre_roll_s=float(os.environ.get("VOX_PRE_ROLL_SECONDS", "0.3")),
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
            clone_profiles_path=Path.home() / ".voicemode" / "voices.json",
            clone_voices_dir=Path.home() / ".voicemode" / "voices",
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
            compatibility=LegacyCompatibility(
                tts_base_url=config.tts_url,
                stt_base_url=config.stt_url,
                base_dir=Path.home() / ".voicemode",
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
        result = await self.lease.claim(client_id)
        if not result.get("claimed"):
            raise BusyError(f"Audio belongs to {result.get('owner_id', 'another client')}")
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
        elif snapshot.owner_id not in {None, client_id}:
            await self.lease.release(client_id, force=True)
            raise BusyError(f"Session belongs to {snapshot.owner_id}")

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
            self._cancel_requested = False

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
            try:
                return await operation()
            except asyncio.CancelledError:
                # Set by control(cancel) before it cancelled us, so it tells a
                # deliberate cancel apart from the host dropping the request.
                with self._active_lock:
                    user_requested = self._cancel_requested
                self._signal_cancel(manual_end=False, cancel_task=False)
                self._return_idle_if_active()
                self._log("turn.cancelled", client_id=client_id, action=action)
                if user_requested:
                    # Cancelling mid-speech used to leave the caller with no
                    # response at all: the tool call never completed and the
                    # agent hung. A cancel is an answer, so report it as one.
                    return {
                        "status": "cancelled",
                        "action": action,
                        "session": self.state.snapshot().to_dict(),
                    }
                raise
            except Exception as exc:
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
            playback = self._playback
            interruptible = self._playback_interruptible
            client = self._active_client
        cancel_control = control is not None
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
            and (playback is None or force or interruptible)
            and task is not None
            and task is not asyncio.current_task()
        )
        signalled = cancel_control or cancel_playback or cancel_async_task
        if signalled and not manual_end:
            with self._active_lock:
                self._cancel_requested = True
        if control is not None:
            with self._active_lock:
                self._microphone_closing = True
            control.end_utterance() if manual_end else control.cancel()
        if cancel_playback and playback is not None:
            playback.cancel()
        # A capture runs in a worker thread.  Cancelling its asyncio wrapper
        # would release the operation gate before the microphone confirms it
        # has closed.  Signal CaptureControl and let the turn unwind normally.
        if cancel_async_task and task is not None:
            task.cancel()
        return {"signalled": signalled, "client_id": client, "manual_end": manual_end}

    async def _speak_locked(
        self,
        client_id: str,
        message: str,
        *,
        voice: str | None,
        speed: float,
        instructions: str | None,
        interruptible: bool = True,
    ) -> dict[str, Any]:
        if not message.strip():
            return {"status": "skipped", "reason": "empty_message"}
        try:
            from voice_mode.pronounce import get_manager, is_enabled

            if is_enabled():
                message = get_manager().process_tts(message)
        except Exception:
            # Pronunciation customization is optional; malformed legacy rules
            # must never take the entire local speech path down.
            pass
        self.state.begin_speaking(client_id=client_id)
        self._log("tts.started", client_id=client_id, chars=len(message))
        destination = self.store.new_work_path("tts")
        try:
            speech_result = await self.speech.synthesize(
                message,
                destination,
                voice=(voice or self.default_voice).lower(),
                speed=speed,
                instructions=instructions,
            )
        except ServiceUnavailableError:
            speech_result = await synthesize_with_macos_say(message, destination)

        replay_path = self.store.commit_tts(Path(speech_result.path or destination))
        handle = self.player.play_file(replay_path, volume=self.volume, blocking=False)
        with self._active_lock:
            self._playback = handle
            self._playback_interruptible = interruptible
        return_code = await asyncio.to_thread(handle.wait)
        with self._active_lock:
            cancelled = self._cancel_requested
            self._playback = None
            self._playback_interruptible = True
        if cancelled or return_code < 0:
            self._return_idle_if_active()
            return {
                "status": "cancelled",
                "backend": speech_result.backend,
                "elapsed_ms": speech_result.elapsed_ms,
            }
        self.state.complete_turn(client_id=client_id)
        self._log(
            "tts.completed",
            client_id=client_id,
            backend=speech_result.backend,
            elapsed_ms=speech_result.elapsed_ms,
        )
        return {
            "status": "completed",
            "backend": speech_result.backend,
            "fallback": speech_result.fallback,
            "elapsed_ms": speech_result.elapsed_ms,
            "audio_path": str(replay_path),
        }

    async def _capture_once(
        self,
        client_id: str,
        *,
        recorder: Any | None = None,
        language: str | None = None,
    ) -> tuple[CaptureResult, SpeechResult | None]:
        self.state.begin_listening(client_id=client_id)
        self._log("listening.started", client_id=client_id)
        control = CaptureControl()
        with self._active_lock:
            self._capture_control = control
        loop = asyncio.get_running_loop()
        selected_recorder = recorder or self.recorder
        future = loop.run_in_executor(
            None,
            lambda: selected_recorder.capture(device=self.input_device, control=control),
        )
        with self._active_lock:
            self._microphone_active = True
            self._microphone_closing = False

        def microphone_closed(_future: asyncio.Future[Any]) -> None:
            with self._active_lock:
                self._microphone_active = False
                self._microphone_closing = False

        future.add_done_callback(microphone_closed)
        try:
            capture = await asyncio.shield(future)
        except asyncio.CancelledError:
            with self._active_lock:
                self._microphone_closing = True
            control.cancel()
            # Never release the operation gate while a recorder thread may
            # still own CoreAudio. Repeated transport cancellation is ignored
            # until the hardware worker confirms closure. A wedged driver
            # leaves Vox fail-closed; the native app can restart the runtime to
            # force OS cleanup.
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    control.cancel()
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
        try:
            from voice_mode.pronounce import get_manager, is_enabled

            if is_enabled() and transcription.text:
                transcription.text = get_manager().process_stt(transcription.text)
        except Exception:
            pass
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
        language: str | None = None,
    ) -> dict[str, Any]:
        selected_recorder: Any = self.recorder
        if isinstance(self.recorder, AudioRecorder) and (
            listen_duration_max is not None
            or listen_duration_min is not None
            or trailing_silence_s is not None
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
                return {
                    "status": (
                        "cancelled"
                        if capture.reason is CaptureStopReason.CANCELLED
                        else "no_speech"
                    ),
                    "reason": capture.reason.value,
                    "capture": _capture_dict(capture),
                    "session": self.state.snapshot().to_dict(),
                }

            transcript = transcription.text or ""
            intent = classify_spoken_intent(transcript)
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
        language: str | None = None,
        agent: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            # Resolved inside the turn, not before it: resolving first would let
            # voice-lookup latency decide queue order instead of arrival order.
            # An explicit voice= always wins over the agent's assigned voice.
            spoken = await self._speak_locked(
                client_id,
                message,
                voice=voice or await self._agent_voice(agent),
                speed=speed,
                instructions=instructions,
                interruptible=True,
            )
            result: dict[str, Any] = {"spoken": spoken}
            if wait_for_response and spoken.get("status") != "cancelled":
                result["heard"] = await self._listen_locked(
                    client_id,
                    listen_duration_max=listen_duration_max,
                    listen_duration_min=listen_duration_min,
                    trailing_silence_s=trailing_silence_s,
                    language=language,
                )
            result["status"] = (
                result.get("heard", {}).get("status") if wait_for_response else spoken.get("status")
            )
            result["session"] = self.state.snapshot().to_dict()
            return result

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
            result = await self._speak_locked(
                client_id,
                message,
                voice=voice or await self._agent_voice(agent),
                speed=speed,
                instructions=instructions,
                interruptible=interruptible,
            )
            return {**result, "session": self.state.snapshot().to_dict()}

        return await self._run_operation(client_id, "speak", operation, agent=agent)

    async def listen(
        self,
        client_id: str,
        *,
        listen_duration_max: float | None = None,
        listen_duration_min: float | None = None,
        trailing_silence_s: float | None = None,
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
                language=language,
            ),
            agent=agent,
        )

    async def session(
        self,
        client_id: str,
        action: str,
        *,
        pause_seconds: float | None = None,
        target_client_id: str | None = None,
        force: bool = False,
        agent: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        action = action.lower().strip()
        administrative = client_id == "http-control"
        if action == "status":
            return await self._status_for_agent(agent)
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
            if self.state.state is not SessionState.OFF:
                self.state.stop(StopReason.USER_REQUEST, client_id=None)
            # Stop is a global privacy/safety control. Anyone on the local MCP
            # surface may close the microphone and release stale ownership.
            await self.lease.release(client_id, force=True)
        elif action == "handoff":
            if not target_client_id:
                raise VoxError("handoff requires target_client_id")
            target_client_id = _canonical_client_id(target_client_id)
            lease_status = await self.lease.status()
            if lease_status.get("owner_id") != client_id:
                raise BusyError("Only the current owner can hand off voice")
            # Hardware is quiet before ownership changes. The target claims on
            # its next call; the old durable session is intentionally closed.
            self._signal_cancel(manual_end=False, force=True)
            if not await self._wait_for_microphone_closed():
                raise ServiceUnavailableError("cannot hand off while the microphone is still closing")
            result = await self.lease.handoff(client_id, target_client_id)
            if not result.get("success"):
                raise BusyError("Voice ownership changed before handoff completed")
            if self.state.state is not SessionState.OFF:
                self.state.stop(StopReason.OWNER_DISCONNECTED, client_id=None)
            return {"status": "handoff_pending", "handoff": result}
        elif action == "takeover":
            if force:
                self._signal_cancel(manual_end=False, force=True)
                if not await self._wait_for_microphone_closed():
                    raise ServiceUnavailableError(
                        "cannot take over while the previous microphone turn is still closing"
                    )
                self._return_idle_if_active()
            claim = await self.lease.claim(client_id, force=force)
            if not claim.get("claimed"):
                raise BusyError("takeover requires force=true while another owner is active")
            if self.state.state is not SessionState.OFF:
                self.state.stop(StopReason.OWNER_DISCONNECTED, client_id=None)
            self.state.start(client_id)
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
            "control_token_path": str(self.control_token_path),
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
        clone_names: list[str] = []
        try:
            clone_document = json.loads((Path.home() / ".voicemode" / "voices.json").read_text())
            clone_names = sorted(
                name
                for name, value in clone_document.get("voices", {}).items()
                if isinstance(name, str) and isinstance(value, dict)
            )
        except (OSError, ValueError, TypeError):
            pass
        clone_backend_ready = False
        if clone_names:
            try:
                async with httpx.AsyncClient(
                    timeout=1.0, trust_env=False, follow_redirects=False
                ) as client:
                    probe = await client.get("http://127.0.0.1:8890/v1/models")
                    clone_backend_ready = probe.is_success
            except httpx.HTTPError:
                pass
        return {
            "provider": "kokoro",
            "endpoint": self.config.tts_url,
            "voices": voices,
            "clone_voices": clone_names,
            "clone_backend": {
                "endpoint": "http://127.0.0.1:8890/v1",
                "ready": clone_backend_ready,
                "local_only": True,
            },
            "default": self.default_voice,
            "agents": dict(self.agent_voices.assignments),
            "local_only": True,
        }

    async def survey(
        self,
        client_id: str,
        turns: list[dict[str, Any]],
        **_: Any,
    ) -> dict[str, Any]:
        if not 1 <= len(turns) <= 50:
            raise VoxError("voice_survey requires 1-50 turns")

        async def operation() -> dict[str, Any]:
            results: list[dict[str, Any]] = []
            for index, turn in enumerate(turns):
                message = str(turn.get("message", ""))
                try:
                    spoken = await self._speak_locked(
                        client_id,
                        message,
                        voice=turn.get("voice"),
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

    async def dj(self, client_id: str, action: str, **kwargs: Any) -> dict[str, Any]:
        del client_id
        result = await self.compatibility.dj(
            action,
            target=kwargs.get("target"),
            volume=kwargs.get("volume"),
            limit=int(kwargs.get("limit", 50)),
        )
        return result.to_dict()

    async def clone_voice(
        self, client_id: str, action: str, **kwargs: Any
    ) -> dict[str, Any]:
        del client_id
        audio = Path(kwargs["audio_path"]).expanduser() if kwargs.get("audio_path") else None
        result = await self.compatibility.clone(
            action,
            name=kwargs.get("name"),
            audio=audio,
            reference_text=kwargs.get("reference_text"),
        )
        return result.to_dict()

    async def soundfonts(
        self,
        client_id: str,
        *,
        enabled: bool | None = None,
        action: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        del client_id
        if action is not None:
            action = action.lower().strip()
            if action == "on":
                enabled = True
            elif action == "off":
                enabled = False
            elif action != "status":
                raise VoxError(f"Unknown soundfonts action: {action}")
        if enabled is None:
            disabled = Path.home() / ".voicemode" / "soundfonts-disabled"
            return {
                "enabled": not disabled.exists(),
                "implementation": "frozen VoiceMode compatibility layer",
            }
        return (await self.compatibility.soundfonts(enabled)).to_dict()

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
        """Status plus who is asking and which voice they will speak in."""

        label = agent or DEFAULT_AGENT
        status = await self.status()
        status["agent"] = {"id": label, "voice": await self._agent_voice(label)}
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
        if microphone_open and microphone_closing:
            detail = "Microphone is still closing; Vox is fail-closed to new audio turns"
        elif microphone_open and self.state.state is not SessionState.LISTENING:
            detail = "Microphone is still closing; Vox is fail-closed to new audio turns"
        elif microphone_open:
            detail = "Listening for one bounded turn"
        return {
            "status": "ok",
            "state": self.state.state.value,
            "detail": detail,
            "session": session,
            "lease": await self.lease.status(),
            "operation": await self.gate.status(),
            "storage": self.store.status(),
            "local_only": True,
        }

    @property
    def microphone_open(self) -> bool:
        with self._active_lock:
            return self._microphone_active

    async def health(self) -> dict[str, Any]:
        status = await self.status()
        session = status["session"]
        return {
            "status": "ok",
            "state": status["state"],
            "detail": status["detail"],
            "version": "0.1.0",
            "local_only": True,
            "microphone_open": session["microphone_open"],
            # Kept deliberately compact and transcript-free for the native
            # status panel. This makes an automatic idle stop explainable
            # instead of looking like a crash.
            "last_stop_reason": session["last_stop_reason"],
            "idle_deadline_at": session["idle_deadline_at"],
        }


def _capture_dict(result: CaptureResult) -> dict[str, Any]:
    return {
        "reason": result.reason.value,
        "speech_detected": result.speech_detected,
        "sample_rate": result.sample_rate,
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
