"""Local audio capture, endpointing, recovery, and process playback.

The capture state machine in this module is deliberately independent of audio
hardware.  ``AdaptiveCaptureState.feed`` can be driven with recorded or
synthetic frames in unit tests, while ``AudioRecorder.capture`` is the thin
``sounddevice`` adapter used at runtime.

Vox records at the input device's native sample rate.  On this Mac that is
48 kHz; there is no shared 24 kHz constant and no FFT resample in the realtime
callback.  A transcription backend may resample once, after endpointing.
"""

from __future__ import annotations

import importlib
import math
import os
import platform
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from collections import deque
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray


FloatAudio = NDArray[np.float32]
SpeechClassifier = Callable[[FloatAudio, int], bool | None]


class AudioError(RuntimeError):
    """Base class for local capture and playback failures."""


class AudioDeviceError(AudioError):
    """The configured input device could not be queried or used."""


class PlaybackError(AudioError):
    """No supported local player could be started."""


class CapturePhase(str, Enum):
    """Observable phases of the pure endpointing state machine."""

    WAITING_FOR_SPEECH = "waiting_for_speech"
    CAPTURING = "capturing"
    FINISHED = "finished"


class CaptureStopReason(str, Enum):
    """Why a capture call returned."""

    TRAILING_SILENCE = "trailing_silence"
    ONSET_TIMEOUT = "onset_timeout"
    MAX_DURATION = "max_duration"
    MANUAL_END = "manual_end"
    CANCELLED = "cancelled"
    # A host/transport drop (crash, session teardown) — NOT a deliberate user
    # cancel. The captured speech is preserved and written to the recovery wav
    # so a mid-utterance interruption is never lost, unlike CANCELLED which
    # discards audio on purpose for privacy.
    INTERRUPTED = "interrupted"
    SOURCE_ENDED = "source_ended"
    DEVICE_ERROR = "device_error"


def default_latest_recording_path() -> Path:
    """Return the single rolling recovery recording path.

    This is intentionally one stable filename, not a timestamped archive.  A
    successful recording atomically replaces it; cancelled and empty captures
    leave the previous recoverable utterance untouched.
    """

    base = Path(os.environ.get("VOX_DATA_DIR", "~/.vox")).expanduser()
    return base / "audio" / "latest.wav"


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    """Endpointing and persistence settings for one recording."""

    onset_timeout_s: float = 15.0
    trailing_silence_s: float = 1.6
    short_trailing_silence_s: float = 0.6
    short_utterance_speech_s: float = 1.5
    long_utterance_speech_s: float = 3.0
    min_duration_s: float = 0.5
    max_duration_s: float = 75.0
    pre_roll_s: float = 0.3
    speech_start_s: float = 0.06
    frame_ms: int = 20
    initial_noise_floor_dbfs: float = -60.0
    minimum_speech_dbfs: float = -48.0
    speech_margin_db: float = 9.0
    noise_smoothing: float = 0.95
    noise_rise_smoothing: float = 0.999
    queue_capacity_frames: int = 512
    source_stall_timeout_s: float = 2.0
    save_latest: bool = True
    latest_wav_path: Path | None = field(default_factory=default_latest_recording_path)

    def __post_init__(self) -> None:
        if not 0 < self.onset_timeout_s <= 15.0:
            raise ValueError("onset_timeout_s must be in (0, 15]")
        if not 0 < self.trailing_silence_s <= 10.0:
            raise ValueError("trailing_silence_s must be in (0, 10]")
        if self.short_trailing_silence_s <= 0:
            raise ValueError("short_trailing_silence_s must be positive")
        if self.short_trailing_silence_s > self.trailing_silence_s:
            # Clamped rather than rejected: a caller who lowers
            # trailing_silence_s below the short default wants a faster close,
            # not a startup crash.  The floor simply collapses onto the ceiling
            # and the utterance-length scaling turns itself off.
            object.__setattr__(self, "short_trailing_silence_s", self.trailing_silence_s)
        if self.short_utterance_speech_s < 0:
            raise ValueError("short_utterance_speech_s must not be negative")
        if self.short_utterance_speech_s > self.long_utterance_speech_s:
            raise ValueError("short_utterance_speech_s must not exceed long_utterance_speech_s")
        if not 0 <= self.min_duration_s <= 300.0:
            raise ValueError("min_duration_s must be in [0, 300]")
        if not 0 < self.max_duration_s <= 300.0:
            raise ValueError("max_duration_s must be in (0, 300]")
        if self.min_duration_s > self.max_duration_s:
            raise ValueError("min_duration_s must not exceed max_duration_s")
        if not 0 <= self.pre_roll_s <= 2.0:
            raise ValueError("pre_roll_s must be in [0, 2]")
        if not 0 < self.speech_start_s <= 1.0:
            raise ValueError("speech_start_s must be in (0, 1]")
        if self.frame_ms not in (10, 20, 30):
            raise ValueError("frame_ms must be 10, 20, or 30")
        if not -96.0 <= self.initial_noise_floor_dbfs <= 0.0:
            raise ValueError("initial_noise_floor_dbfs must be in [-96, 0]")
        if not -96.0 <= self.minimum_speech_dbfs <= 0.0:
            raise ValueError("minimum_speech_dbfs must be in [-96, 0]")
        if not 0 < self.speech_margin_db <= 40.0:
            raise ValueError("speech_margin_db must be in (0, 40]")
        if not 0 <= self.noise_smoothing < 1.0:
            raise ValueError("noise_smoothing must be in [0, 1)")
        if not 0 <= self.noise_rise_smoothing < 1.0:
            raise ValueError("noise_rise_smoothing must be in [0, 1)")
        if self.queue_capacity_frames < 2:
            raise ValueError("queue_capacity_frames must be at least 2")
        if self.source_stall_timeout_s <= 0:
            raise ValueError("source_stall_timeout_s must be positive")
        if self.latest_wav_path is not None and not isinstance(self.latest_wav_path, Path):
            object.__setattr__(self, "latest_wav_path", Path(self.latest_wav_path))


@dataclass(frozen=True, slots=True)
class AudioDeviceInfo:
    """The subset of sounddevice metadata that capture needs."""

    index: int | str | None
    name: str
    sample_rate: int
    input_channels: int


@dataclass(frozen=True, slots=True)
class LevelMeasurement:
    """Loudness statistics for a fixed listening window. No audio retained."""

    device: str
    frames: int
    median_dbfs: float
    p90_dbfs: float
    peak_dbfs: float

    @classmethod
    def from_dbfs(cls, levels: list[float], *, device: str) -> "LevelMeasurement":
        if not levels:
            return cls(device=device, frames=0, median_dbfs=-96.0, p90_dbfs=-96.0, peak_dbfs=-96.0)
        array = np.asarray(levels, dtype=np.float64)
        return cls(
            device=device,
            frames=len(levels),
            median_dbfs=float(np.median(array)),
            p90_dbfs=float(np.percentile(array, 90)),
            peak_dbfs=float(np.max(array)),
        )


@dataclass(frozen=True, slots=True)
class FrameDecision:
    """The endpoint detector's decision for one input frame."""

    phase: CapturePhase
    is_speech: bool
    dbfs: float
    threshold_dbfs: float
    noise_floor_dbfs: float
    speech_started: bool = False
    stop_reason: CaptureStopReason | None = None


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """A completed capture and enough metadata for diagnostics."""

    samples: FloatAudio
    sample_rate: int
    reason: CaptureStopReason
    speech_detected: bool
    elapsed_s: float
    audio_duration_s: float
    speech_duration_s: float
    trailing_silence_s: float
    noise_floor_dbfs: float
    dropped_frames: int = 0
    latest_wav_path: Path | None = None


class CaptureControl:
    """Thread-safe, low-latency control for a blocking capture call."""

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._manual_end = threading.Event()
        self._interrupt = threading.Event()
        self._level = 0.0
        self._level_lock = threading.Lock()

    def publish_level(self, dbfs: float) -> None:
        """Record the latest frame loudness for the menu-bar meter.

        dBFS runs from near-silence (~-96) to full-scale (0); speech sits
        around -30..-10.  Map it to a 0..1 value where -60 dBFS reads as a
        flat baseline so the waveform only lifts when there is real signal.
        """

        level = (dbfs + 60.0) / 60.0
        level = 0.0 if level < 0.0 else 1.0 if level > 1.0 else level
        with self._level_lock:
            self._level = level

    @property
    def level(self) -> float:
        """The latest 0..1 mic loudness published by the capture loop."""

        with self._level_lock:
            return self._level

    def cancel(self) -> None:
        """Cancel the turn and discard audio captured by this call."""

        self._cancel.set()

    def end_utterance(self) -> None:
        """Finish normally at the user's explicit end-of-turn signal."""

        self._manual_end.set()

    def interrupt(self) -> None:
        """Stop for a host/transport drop, preserving audio for recovery.

        Unlike ``cancel``, this is not a deliberate user discard: the captured
        speech is kept and persisted so a crash mid-utterance can be recovered.
        """

        self._interrupt.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def manual_end_requested(self) -> bool:
        return self._manual_end.is_set()

    @property
    def interrupted(self) -> bool:
        return self._interrupt.is_set()


def _notify_speech_started(callback: Callable[[], None]) -> None:
    """Fire an onset callback without letting it take the capture down.

    The callback runs on the recorder's worker thread while the audio device
    is open.  A raise here would abandon a live InputStream, so a broken
    listener loses its notification rather than the user losing their turn.
    """

    try:
        callback()
    except Exception:
        pass


def _as_mono_float32(samples: Any) -> FloatAudio:
    """Normalize an integer/float, mono/multichannel frame to mono float32."""

    array = np.asarray(samples)
    if array.size == 0:
        raise ValueError("audio frame must not be empty")

    if np.issubdtype(array.dtype, np.integer):
        scale = float(max(abs(np.iinfo(array.dtype).min), np.iinfo(array.dtype).max))
        normalized = array.astype(np.float32) / scale
    else:
        normalized = array.astype(np.float32, copy=False)

    if normalized.ndim == 2:
        normalized = normalized.mean(axis=1, dtype=np.float32)
    elif normalized.ndim != 1:
        normalized = normalized.reshape(-1)

    normalized = np.nan_to_num(normalized, nan=0.0, posinf=1.0, neginf=-1.0)
    return np.clip(normalized, -1.0, 1.0).astype(np.float32, copy=False)


def _dbfs(samples: FloatAudio) -> float:
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    if rms <= 1e-8:
        return -96.0
    return max(-96.0, min(0.0, 20.0 * math.log10(rms)))


class WebRTCVADClassifier:
    """Optional WebRTC VAD vote at a device-native supported sample rate.

    WebRTC accepts 8, 16, 32, and 48 kHz frames of 10, 20, or 30 ms.  If a
    device uses another native rate, Vox keeps recording at that rate and the
    adaptive energy detector remains active; resampling belongs after capture.
    """

    SUPPORTED_SAMPLE_RATES = frozenset({8_000, 16_000, 32_000, 48_000})

    def __init__(self, vad: Any) -> None:
        self._vad = vad

    @classmethod
    def create(cls, aggressiveness: int = 2) -> WebRTCVADClassifier | None:
        try:
            module = importlib.import_module("webrtcvad")
            return cls(module.Vad(aggressiveness))
        except (ImportError, OSError, AttributeError):
            return None

    def __call__(self, samples: FloatAudio, sample_rate: int) -> bool | None:
        if sample_rate not in self.SUPPORTED_SAMPLE_RATES:
            return None
        duration_ms = round(len(samples) * 1000 / sample_rate)
        if duration_ms not in (10, 20, 30):
            return None
        pcm = np.round(np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
        try:
            return bool(self._vad.is_speech(pcm.tobytes(), sample_rate))
        except (ValueError, RuntimeError):
            return None


class AdaptiveCaptureState:
    """Pure adaptive endpoint detector with pre-roll retention."""

    def __init__(
        self,
        sample_rate: int,
        config: CaptureConfig | None = None,
        speech_classifier: SpeechClassifier | None = None,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        self.sample_rate = int(sample_rate)
        self.config = config or CaptureConfig()
        self.speech_classifier = speech_classifier
        self.phase = CapturePhase.WAITING_FOR_SPEECH
        self.noise_floor_dbfs = self.config.initial_noise_floor_dbfs
        self.elapsed_s = 0.0
        self.speech_duration_s = 0.0
        self.trailing_silence_s = 0.0
        self._speech_run_s = 0.0
        self._utterance_elapsed_s = 0.0
        self._pre_roll: deque[FloatAudio] = deque()
        self._pre_roll_samples = 0
        self._capture_parts: list[FloatAudio] = []
        self._stop_reason: CaptureStopReason | None = None

    @property
    def speech_detected(self) -> bool:
        return self.phase is CapturePhase.CAPTURING or bool(self._capture_parts)

    @property
    def finished(self) -> bool:
        return self.phase is CapturePhase.FINISHED

    @property
    def threshold_dbfs(self) -> float:
        return max(
            self.config.minimum_speech_dbfs,
            self.noise_floor_dbfs + self.config.speech_margin_db,
        )

    def _effective_trailing_silence_s(self) -> float:
        """How much silence ends *this* utterance, scaled to how much was said.

        A one-word answer and a rambling paragraph should not wait the same
        time to be called finished.  This keys on ``speech_duration_s`` rather
        than wall time: it only grows on speech-classified frames and resumes
        growing after a mid-utterance pause, so stopping to think does not
        demote a long answer onto the fast path.  Between the two anchors the
        value is interpolated — a hard cliff would give 1.49 s of speech a
        0.6 s close and 1.51 s a 1.6 s close, which reads as the runtime
        randomly changing its mind.
        """

        config = self.config
        low = config.short_utterance_speech_s
        high = config.long_utterance_speech_s
        if high <= low:
            return config.trailing_silence_s
        speech = self.speech_duration_s
        if speech <= low:
            return config.short_trailing_silence_s
        if speech >= high:
            return config.trailing_silence_s
        ratio = (speech - low) / (high - low)
        span = config.trailing_silence_s - config.short_trailing_silence_s
        return config.short_trailing_silence_s + ratio * span

    def _remember_pre_roll(self, frame: FloatAudio) -> None:
        # Even with pre-roll disabled, retain the short run of frames used to
        # confirm speech.  Otherwise the first 60 ms of an utterance disappears
        # precisely when speech_start_s transitions the state to CAPTURING.
        maximum = max(
            round(self.config.pre_roll_s * self.sample_rate),
            round(self.config.speech_start_s * self.sample_rate),
        )
        self._pre_roll.append(frame.copy())
        self._pre_roll_samples += len(frame)
        while self._pre_roll and self._pre_roll_samples - len(self._pre_roll[0]) >= maximum:
            removed = self._pre_roll.popleft()
            self._pre_roll_samples -= len(removed)
        if self._pre_roll_samples > maximum and self._pre_roll:
            excess = self._pre_roll_samples - maximum
            first = self._pre_roll.popleft()
            trimmed = first[excess:].copy()
            self._pre_roll.appendleft(trimmed)
            self._pre_roll_samples -= excess

    def _classify(self, frame: FloatAudio, dbfs: float) -> bool:
        threshold = self.threshold_dbfs
        energy_speech = dbfs >= threshold
        vad_vote: bool | None = None
        if self.speech_classifier is not None:
            try:
                vad_vote = self.speech_classifier(frame, self.sample_rate)
            except Exception:
                # VAD is an enhancement.  A classifier failure must not make the
                # microphone unusable; adaptive energy remains deterministic.
                vad_vote = None

        if vad_vote is None:
            is_speech = energy_speech
        elif vad_vote:
            # Allow VAD to recognize quiet speech, but still require a small
            # rise above the learned floor to reject stationary fan noise —
            # and never below the absolute floor, whatever the VAD claims.
            # WebRTC VAD votes speech on ordinary room tone, and a room whose
            # ambient sits within 3 dB of its own learned floor would otherwise
            # read as continuous speech: trailing silence never accumulates and
            # the turn cannot endpoint. minimum_speech_dbfs is the promise that
            # a level this quiet is not speech no matter who says otherwise.
            is_speech = (
                dbfs >= self.noise_floor_dbfs + 3.0
                and dbfs >= self.config.minimum_speech_dbfs
            )
        else:
            # A very strong transient may be clipped speech that VAD missed.
            is_speech = energy_speech and dbfs >= threshold + 12.0

        if not is_speech or vad_vote is False:
            # The VAD calling a loud frame non-speech is the signature of a
            # droning room (fan, AC).  Let the floor keep learning it at the
            # normal rate even when raw energy trips the threshold; otherwise a
            # room whose ambient starts above the threshold reads as endless
            # speech and the turn can never endpoint.
            alpha = self.config.noise_smoothing
            self.noise_floor_dbfs = alpha * self.noise_floor_dbfs + (1.0 - alpha) * dbfs
        elif dbfs > self.noise_floor_dbfs:
            # Speech-accepted frames still lift the floor, only very slowly:
            # real speech has inter-word gaps that pull it back down, while a
            # sustained drone that fools the VAD eventually reads as the new
            # silence so the turn can endpoint.
            alpha = self.config.noise_rise_smoothing
            self.noise_floor_dbfs = alpha * self.noise_floor_dbfs + (1.0 - alpha) * dbfs
        self.noise_floor_dbfs = max(-96.0, min(-3.0, self.noise_floor_dbfs))
        return is_speech

    def feed(self, samples: Any) -> FrameDecision:
        """Consume one frame and return its endpoint decision."""

        if self.finished:
            raise RuntimeError("cannot feed a finished capture")

        frame = _as_mono_float32(samples)
        duration_s = len(frame) / self.sample_rate
        self.elapsed_s += duration_s
        frame_dbfs = _dbfs(frame)
        threshold_before_update = self.threshold_dbfs
        is_speech = self._classify(frame, frame_dbfs)
        started_now = False

        if self.phase is CapturePhase.WAITING_FOR_SPEECH:
            self._remember_pre_roll(frame)
            if is_speech:
                self._speech_run_s += duration_s
            else:
                self._speech_run_s = 0.0

            if self._speech_run_s + 1e-9 >= self.config.speech_start_s:
                self.phase = CapturePhase.CAPTURING
                self._capture_parts.extend(part.copy() for part in self._pre_roll)
                self._pre_roll.clear()
                self._pre_roll_samples = 0
                self._utterance_elapsed_s = self._speech_run_s
                self.speech_duration_s = self._speech_run_s
                started_now = True
            elif self.elapsed_s + 1e-9 >= self.config.onset_timeout_s:
                self.stop(CaptureStopReason.ONSET_TIMEOUT)

        else:
            self._capture_parts.append(frame.copy())
            self._utterance_elapsed_s += duration_s
            if is_speech:
                self.speech_duration_s += duration_s
                self.trailing_silence_s = 0.0
            else:
                self.trailing_silence_s += duration_s

            if (
                self._utterance_elapsed_s + 1e-9 >= self.config.min_duration_s
                and self.trailing_silence_s + 1e-9 >= self._effective_trailing_silence_s()
            ):
                self.stop(CaptureStopReason.TRAILING_SILENCE)
            elif self._utterance_elapsed_s + 1e-9 >= self.config.max_duration_s:
                self.stop(CaptureStopReason.MAX_DURATION)

        return FrameDecision(
            phase=self.phase,
            is_speech=is_speech,
            dbfs=frame_dbfs,
            threshold_dbfs=threshold_before_update,
            noise_floor_dbfs=self.noise_floor_dbfs,
            speech_started=started_now,
            stop_reason=self._stop_reason,
        )

    def stop(self, reason: CaptureStopReason) -> None:
        if not self.finished:
            self._stop_reason = reason
            self.phase = CapturePhase.FINISHED

    def result(self, *, dropped_frames: int = 0) -> CaptureResult:
        if not self.finished or self._stop_reason is None:
            raise RuntimeError("capture has not finished")

        if self._stop_reason is CaptureStopReason.CANCELLED or not self._capture_parts:
            audio = np.empty(0, dtype=np.float32)
        else:
            audio = np.concatenate(self._capture_parts).astype(np.float32, copy=False)

        return CaptureResult(
            samples=audio,
            sample_rate=self.sample_rate,
            reason=self._stop_reason,
            speech_detected=bool(self._capture_parts),
            elapsed_s=self.elapsed_s,
            audio_duration_s=len(audio) / self.sample_rate,
            speech_duration_s=self.speech_duration_s,
            trailing_silence_s=self.trailing_silence_s,
            noise_floor_dbfs=self.noise_floor_dbfs,
            dropped_frames=dropped_frames,
        )


def resolve_input_device(sounddevice: Any, device: int | str | None = None) -> AudioDeviceInfo:
    """Resolve the configured/default input and preserve its native rate."""

    resolved: int | str | None = device
    if resolved is None:
        default = getattr(getattr(sounddevice, "default", None), "device", None)
        if isinstance(default, (tuple, list)) and default:
            resolved = default[0]
        elif isinstance(default, int):
            resolved = default

    try:
        info = sounddevice.query_devices(resolved, "input")
    except Exception as exc:
        raise AudioDeviceError(f"Cannot query input device {resolved!r}: {exc}") from exc

    try:
        sample_rate = int(round(float(info["default_samplerate"])))
        channels = int(info["max_input_channels"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AudioDeviceError(f"Input device metadata is incomplete: {info!r}") from exc
    if sample_rate <= 0 or channels < 1:
        raise AudioDeviceError(f"Input device is not recordable: {info!r}")
    return AudioDeviceInfo(
        index=resolved,
        name=str(info.get("name", resolved if resolved is not None else "default input")),
        sample_rate=sample_rate,
        input_channels=channels,
    )


def _load_sounddevice() -> Any:
    try:
        return importlib.import_module("sounddevice")
    except (ImportError, OSError) as exc:
        raise AudioDeviceError(f"sounddevice/PortAudio is unavailable: {exc}") from exc


def write_wav_atomic(path: Path | str, samples: Any, sample_rate: int) -> Path:
    """Atomically replace ``path`` with a private 16-bit mono PCM WAV."""

    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    audio = _as_mono_float32(samples)
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    fd, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        temporary.chmod(0o600)
        pcm = np.round(audio * 32767.0).astype("<i2", copy=False)
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(int(sample_rate))
            output.writeframes(pcm.tobytes())
        with temporary.open("rb") as written:
            os.fsync(written.fileno())
        os.replace(temporary, destination)
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
        return destination
    finally:
        temporary.unlink(missing_ok=True)


class AudioRecorder:
    """Hardware adapter around :class:`AdaptiveCaptureState`."""

    def __init__(
        self,
        config: CaptureConfig | None = None,
        *,
        sounddevice: Any | None = None,
        stream_factory: Callable[..., Any] | None = None,
        speech_classifier: SpeechClassifier | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or CaptureConfig()
        self._sounddevice = sounddevice
        self._stream_factory = stream_factory
        self._speech_classifier = speech_classifier
        self._clock = clock

    def _finish(self, state: AdaptiveCaptureState, dropped_frames: int) -> CaptureResult:
        result = state.result(dropped_frames=dropped_frames)
        if (
            self.config.save_latest
            and self.config.latest_wav_path is not None
            and result.speech_detected
            and result.samples.size
            and result.reason is not CaptureStopReason.CANCELLED
        ):
            latest = write_wav_atomic(
                self.config.latest_wav_path,
                result.samples,
                result.sample_rate,
            )
            result = replace(result, latest_wav_path=latest)
        return result

    def capture_from_frames(
        self,
        frames: Iterable[Any],
        sample_rate: int,
        *,
        control: CaptureControl | None = None,
        on_speech_started: Callable[[], None] | None = None,
    ) -> CaptureResult:
        """Drive the complete capture path without opening an audio device."""

        control = control or CaptureControl()
        classifier = self._speech_classifier
        state = AdaptiveCaptureState(sample_rate, self.config, classifier)
        for frame in frames:
            if control.cancelled:
                state.stop(CaptureStopReason.CANCELLED)
                break
            if control.interrupted:
                state.stop(CaptureStopReason.INTERRUPTED)
                break
            if control.manual_end_requested:
                state.stop(CaptureStopReason.MANUAL_END)
                break
            decision = state.feed(frame)
            control.publish_level(decision.dbfs)
            if decision.speech_started and on_speech_started is not None:
                _notify_speech_started(on_speech_started)
            if state.finished:
                break
        if not state.finished:
            if control.cancelled:
                state.stop(CaptureStopReason.CANCELLED)
            elif control.interrupted:
                state.stop(CaptureStopReason.INTERRUPTED)
            elif control.manual_end_requested:
                state.stop(CaptureStopReason.MANUAL_END)
            else:
                state.stop(CaptureStopReason.SOURCE_ENDED)
        return self._finish(state, dropped_frames=0)

    def measure(
        self,
        seconds: float,
        *,
        device: int | str | None = None,
    ) -> "LevelMeasurement":
        """Watch the microphone for a fixed window and report loudness only.

        No endpointing, no retained audio, no transcript — this exists so the
        barge-in echo gate can be set from measured speaker bleed instead of a
        guess.  Frames are reduced to dBFS the instant they arrive and the
        samples are dropped.
        """

        if seconds <= 0:
            raise ValueError("seconds must be positive")
        sounddevice = self._sounddevice or _load_sounddevice()
        device_info = resolve_input_device(sounddevice, device)
        levels: list[float] = []
        levels_lock = threading.Lock()

        def callback(indata: Any, _frame_count: int, _time_info: Any, status: Any) -> None:
            if status and not getattr(status, "input_overflow", False):
                return
            try:
                dbfs = _dbfs(_as_mono_float32(indata))
            except Exception:
                return
            with levels_lock:
                levels.append(dbfs)

        factory = self._stream_factory or sounddevice.InputStream
        blocksize = max(1, round(device_info.sample_rate * self.config.frame_ms / 1000))
        deadline = self._clock() + seconds
        try:
            with factory(
                samplerate=device_info.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=blocksize,
                callback=callback,
                device=device_info.index,
            ):
                while self._clock() < deadline:
                    time.sleep(min(0.02, self.config.frame_ms / 1000))
        except Exception as exc:
            if isinstance(exc, AudioError):
                raise
            raise AudioDeviceError(
                f"Audio measurement failed on {device_info.name}: {exc}"
            ) from exc

        with levels_lock:
            observed = list(levels)
        return LevelMeasurement.from_dbfs(observed, device=device_info.name)

    def capture(
        self,
        *,
        device: int | str | None = None,
        control: CaptureControl | None = None,
        on_speech_started: Callable[[], None] | None = None,
    ) -> CaptureResult:
        """Record one utterance from the native-rate default input device.

        ``on_speech_started`` fires once, on this worker thread, the moment the
        detector confirms speech.  Barge-in uses it to kill playback the
        instant the user starts talking; the pre-roll means the words that
        triggered it are still part of the recording.
        """

        control = control or CaptureControl()
        sounddevice = self._sounddevice or _load_sounddevice()
        device_info = resolve_input_device(sounddevice, device)
        classifier = self._speech_classifier
        if classifier is None:
            classifier = WebRTCVADClassifier.create()
        state = AdaptiveCaptureState(device_info.sample_rate, self.config, classifier)
        frames: queue.Queue[FloatAudio | BaseException] = queue.Queue(
            maxsize=self.config.queue_capacity_frames
        )
        dropped_frames = 0
        dropped_lock = threading.Lock()

        def put_frame(value: FloatAudio | BaseException) -> None:
            nonlocal dropped_frames
            try:
                frames.put_nowait(value)
            except queue.Full:
                try:
                    frames.get_nowait()
                except queue.Empty:
                    pass
                with dropped_lock:
                    dropped_frames += 1
                try:
                    frames.put_nowait(value)
                except queue.Full:
                    pass

        def callback(indata: Any, _frame_count: int, _time_info: Any, status: Any) -> None:
            if status and not getattr(status, "input_overflow", False):
                put_frame(AudioDeviceError(f"Audio input status: {status}"))
                return
            try:
                put_frame(_as_mono_float32(indata).copy())
            except Exception as exc:
                put_frame(AudioDeviceError(f"Invalid audio input frame: {exc}"))

        factory = self._stream_factory or sounddevice.InputStream
        blocksize = max(1, round(device_info.sample_rate * self.config.frame_ms / 1000))
        stream_kwargs = {
            "samplerate": device_info.sample_rate,
            "channels": 1,
            "dtype": "float32",
            "blocksize": blocksize,
            "callback": callback,
            "device": device_info.index,
        }
        started_at = self._clock()
        last_frame_at = started_at
        speech_started_at: float | None = None

        try:
            with factory(**stream_kwargs):
                while not state.finished:
                    if control.cancelled:
                        state.stop(CaptureStopReason.CANCELLED)
                        break
                    if control.interrupted:
                        state.stop(CaptureStopReason.INTERRUPTED)
                        break
                    if control.manual_end_requested:
                        state.stop(CaptureStopReason.MANUAL_END)
                        break

                    now = self._clock()
                    if state.phase is CapturePhase.WAITING_FOR_SPEECH:
                        if now - started_at >= self.config.onset_timeout_s:
                            state.stop(CaptureStopReason.ONSET_TIMEOUT)
                            break
                    elif speech_started_at is not None:
                        if now - speech_started_at >= self.config.max_duration_s:
                            state.stop(CaptureStopReason.MAX_DURATION)
                            break
                    if now - last_frame_at >= self.config.source_stall_timeout_s:
                        state.stop(CaptureStopReason.DEVICE_ERROR)
                        break

                    try:
                        item = frames.get(timeout=min(0.02, self.config.frame_ms / 1000))
                    except queue.Empty:
                        continue
                    last_frame_at = self._clock()
                    if isinstance(item, BaseException):
                        state.stop(CaptureStopReason.DEVICE_ERROR)
                        break
                    decision = state.feed(item)
                    control.publish_level(decision.dbfs)
                    if decision.speech_started:
                        speech_started_at = self._clock()
                        if on_speech_started is not None:
                            _notify_speech_started(on_speech_started)
        except Exception as exc:
            if not state.finished:
                state.stop(CaptureStopReason.DEVICE_ERROR)
            if isinstance(exc, AudioError):
                raise
            raise AudioDeviceError(f"Audio capture failed on {device_info.name}: {exc}") from exc

        with dropped_lock:
            final_dropped = dropped_frames
        return self._finish(state, dropped_frames=final_dropped)


@runtime_checkable
class PlaybackProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class PlaybackRegistry:
    """Tracks only player subprocesses created by the current Vox process."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handles: dict[str, PlaybackHandle] = {}

    def register(self, handle: PlaybackHandle) -> None:
        with self._lock:
            self._handles[handle.id] = handle

    def unregister(self, playback_id: str) -> None:
        with self._lock:
            self._handles.pop(playback_id, None)

    def get(self, playback_id: str) -> PlaybackHandle | None:
        with self._lock:
            return self._handles.get(playback_id)

    def active(self) -> tuple[PlaybackHandle, ...]:
        with self._lock:
            handles = tuple(self._handles.values())
        return tuple(handle for handle in handles if handle.running)

    def cancel(self, playback_id: str, grace_s: float = 0.2) -> bool:
        handle = self.get(playback_id)
        if handle is None:
            return False
        handle.cancel(grace_s=grace_s)
        return True

    def cancel_all(self, grace_s: float = 0.2) -> int:
        handles = self.active()
        for handle in handles:
            handle.cancel(grace_s=grace_s)
        return len(handles)


class PlaybackHandle:
    """A cancellable local player process."""

    def __init__(
        self,
        process: PlaybackProcess,
        command: tuple[str, ...],
        registry: PlaybackRegistry,
        cleanup_paths: Iterable[Path] = (),
    ) -> None:
        self.id = uuid.uuid4().hex
        self.process = process
        self.command = command
        self._registry = registry
        self._cleanup_paths = tuple(cleanup_paths)
        self._finalize_lock = threading.Lock()
        self._finalized = False

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    def _finalize(self) -> None:
        with self._finalize_lock:
            if self._finalized:
                return
            self._finalized = True
            self._registry.unregister(self.id)
            for path in self._cleanup_paths:
                path.unlink(missing_ok=True)

    def wait(self, timeout: float | None = None) -> int:
        try:
            return self.process.wait(timeout=timeout)
        finally:
            if self.process.poll() is not None:
                self._finalize()

    def cancel(self, grace_s: float = 0.2) -> None:
        if grace_s < 0:
            raise ValueError("grace_s must not be negative")
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=grace_s)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=max(grace_s, 0.1))
        finally:
            self._finalize()

    def monitor(self) -> None:
        try:
            self.process.wait()
        finally:
            self._finalize()


PLAYBACK_REGISTRY = PlaybackRegistry()


class AudioPlayer:
    """Spawn cancellable ``afplay``/``ffplay`` processes without a shell."""

    def __init__(
        self,
        *,
        registry: PlaybackRegistry | None = None,
        popen_factory: Callable[..., PlaybackProcess] = subprocess.Popen,
        which: Callable[[str], str | None] = shutil.which,
        platform_name: str | None = None,
    ) -> None:
        self.registry = registry or PLAYBACK_REGISTRY
        self._popen = popen_factory
        self._which = which
        self._platform = platform_name or platform.system()

    def _resolve_player(self, preferred: str | None = None) -> str:
        candidates: tuple[str, ...]
        if preferred:
            candidates = (preferred,)
        elif self._platform == "Darwin":
            candidates = ("afplay", "ffplay")
        else:
            candidates = ("ffplay", "afplay")

        for candidate in candidates:
            if os.sep in candidate:
                path = Path(candidate).expanduser()
                if path.is_file() and os.access(path, os.X_OK):
                    return str(path)
            else:
                resolved = self._which(candidate)
                if resolved:
                    return resolved
        raise PlaybackError("Neither afplay nor ffplay is available")

    @staticmethod
    def _command(player: str, path: Path, volume: float) -> tuple[str, ...]:
        if not 0.0 <= volume <= 1.0:
            raise ValueError("volume must be in [0, 1]")
        name = Path(player).name.lower()
        if name == "afplay":
            return (player, "-v", f"{volume:.3f}", str(path))
        if "ffplay" in name:
            return (
                player,
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "error",
                "-volume",
                str(round(volume * 100)),
                str(path),
            )
        raise PlaybackError(f"Unsupported player executable: {player}")

    def play_file(
        self,
        path: Path | str,
        *,
        volume: float = 1.0,
        blocking: bool = False,
        preferred_player: str | None = None,
        cleanup_paths: Iterable[Path] = (),
    ) -> PlaybackHandle:
        audio_path = Path(path).expanduser().resolve(strict=True)
        player = self._resolve_player(preferred_player)
        command = self._command(player, audio_path, volume)
        try:
            process = self._popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            for cleanup in cleanup_paths:
                cleanup.unlink(missing_ok=True)
            raise PlaybackError(f"Could not start {Path(player).name}: {exc}") from exc

        handle = PlaybackHandle(process, command, self.registry, cleanup_paths)
        self.registry.register(handle)
        if blocking:
            handle.wait()
        else:
            threading.Thread(
                target=handle.monitor,
                name=f"vox-playback-{handle.id[:8]}",
                daemon=True,
            ).start()
        return handle

    def play_samples(
        self,
        samples: Any,
        sample_rate: int,
        *,
        volume: float = 1.0,
        blocking: bool = False,
        preferred_player: str | None = None,
    ) -> PlaybackHandle:
        """Play samples through a short-lived WAV that is deleted on exit."""

        temp_dir = Path(tempfile.gettempdir()) / "vox-playback"
        temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, raw_path = tempfile.mkstemp(dir=temp_dir, prefix="audio-", suffix=".wav")
        os.close(fd)
        temporary = Path(raw_path)
        temporary.unlink(missing_ok=True)
        try:
            write_wav_atomic(temporary, samples, sample_rate)
            return self.play_file(
                temporary,
                volume=volume,
                blocking=blocking,
                preferred_player=preferred_player,
                cleanup_paths=(temporary,),
            )
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def cancel_all(self, grace_s: float = 0.2) -> int:
        return self.registry.cancel_all(grace_s=grace_s)


def cancel_all_playback(grace_s: float = 0.2) -> int:
    """Cancel every player child launched by this Python process."""

    return PLAYBACK_REGISTRY.cancel_all(grace_s=grace_s)


__all__ = [
    "AdaptiveCaptureState",
    "AudioDeviceError",
    "AudioDeviceInfo",
    "AudioError",
    "AudioPlayer",
    "AudioRecorder",
    "CaptureConfig",
    "CaptureControl",
    "CapturePhase",
    "CaptureResult",
    "CaptureStopReason",
    "FrameDecision",
    "PLAYBACK_REGISTRY",
    "PlaybackError",
    "PlaybackHandle",
    "PlaybackRegistry",
    "WebRTCVADClassifier",
    "cancel_all_playback",
    "default_latest_recording_path",
    "resolve_input_device",
    "write_wav_atomic",
]
