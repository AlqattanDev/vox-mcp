from __future__ import annotations

import stat
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from voxmcp.audio import (
    AdaptiveCaptureState,
    AudioPlayer,
    AudioRecorder,
    CaptureConfig,
    CaptureControl,
    CapturePhase,
    CaptureStopReason,
    PlaybackRegistry,
    resolve_input_device,
    write_wav_atomic,
)


def config(**overrides: Any) -> CaptureConfig:
    values: dict[str, Any] = {
        "onset_timeout_s": 1.0,
        "trailing_silence_s": 0.12,
        "min_duration_s": 0.0,
        "max_duration_s": 3.0,
        "pre_roll_s": 0.3,
        "speech_start_s": 0.06,
        "frame_ms": 20,
        "save_latest": False,
        "latest_wav_path": None,
    }
    values.update(overrides)
    return CaptureConfig(**values)


def frame(amplitude: float, sample_rate: int = 1_000, frame_ms: int = 20) -> np.ndarray:
    return np.full(round(sample_rate * frame_ms / 1_000), amplitude, dtype=np.float32)


def speech_vote(samples: np.ndarray, _sample_rate: int) -> bool:
    return bool(np.max(np.abs(samples)) > 0.05)


def test_capture_config_enforces_safety_bounds() -> None:
    defaults = CaptureConfig(save_latest=False, latest_wav_path=None)
    assert defaults.onset_timeout_s == 15.0
    assert defaults.trailing_silence_s == 1.6
    assert defaults.min_duration_s == 0.5
    assert defaults.max_duration_s == 75.0
    assert defaults.pre_roll_s == 0.3

    with pytest.raises(ValueError, match="onset_timeout"):
        CaptureConfig(onset_timeout_s=15.01)
    with pytest.raises(ValueError, match="max_duration"):
        CaptureConfig(max_duration_s=300.01)
    with pytest.raises(ValueError, match="min_duration"):
        CaptureConfig(min_duration_s=2, max_duration_s=1)
    with pytest.raises(ValueError, match="frame_ms"):
        CaptureConfig(frame_ms=25)


def test_state_retains_preroll_and_stops_on_trailing_silence() -> None:
    state = AdaptiveCaptureState(1_000, config(), speech_vote)

    for _ in range(20):  # 400 ms of silence; only the last 300 ms is retained.
        decision = state.feed(frame(0.001))
        assert decision.phase is CapturePhase.WAITING_FOR_SPEECH

    for _ in range(5):
        state.feed(frame(0.2))
    for _ in range(6):
        decision = state.feed(frame(0.001))

    assert decision.stop_reason is CaptureStopReason.TRAILING_SILENCE
    result = state.result()
    assert result.reason is CaptureStopReason.TRAILING_SILENCE
    assert result.speech_detected is True
    assert result.trailing_silence_s == pytest.approx(0.12)
    assert result.speech_duration_s == pytest.approx(0.10)
    # At speech confirmation: 300 ms pre-roll. Afterwards: 40 ms speech and
    # 120 ms trailing silence.
    assert result.audio_duration_s == pytest.approx(0.46)
    assert np.allclose(result.samples[:20], 0.001)
    assert np.max(result.samples) == pytest.approx(0.2)


def test_trailing_silence_cannot_end_before_minimum_turn_duration() -> None:
    state = AdaptiveCaptureState(
        1_000,
        config(min_duration_s=0.20, trailing_silence_s=0.04, speech_start_s=0.02),
        speech_vote,
    )
    state.feed(frame(0.2))
    for _ in range(8):
        decision = state.feed(frame(0.001))
        assert decision.stop_reason is None
    decision = state.feed(frame(0.001))
    assert decision.stop_reason is CaptureStopReason.TRAILING_SILENCE


def test_state_times_out_if_speech_never_starts() -> None:
    state = AdaptiveCaptureState(
        1_000,
        config(onset_timeout_s=0.10),
        speech_vote,
    )
    for _ in range(5):
        decision = state.feed(frame(0.001))

    assert decision.stop_reason is CaptureStopReason.ONSET_TIMEOUT
    result = state.result()
    assert result.samples.size == 0
    assert result.speech_detected is False
    assert result.elapsed_s == pytest.approx(0.10)


def test_state_stops_at_configured_max_utterance_duration() -> None:
    state = AdaptiveCaptureState(
        1_000,
        config(
            max_duration_s=0.10,
            speech_start_s=0.02,
            pre_roll_s=0.0,
            trailing_silence_s=1.0,
        ),
        speech_vote,
    )
    for _ in range(5):
        decision = state.feed(frame(0.2))

    assert decision.stop_reason is CaptureStopReason.MAX_DURATION
    result = state.result()
    assert result.audio_duration_s == pytest.approx(0.10)
    assert result.speech_duration_s == pytest.approx(0.10)


def test_noise_floor_adapts_without_promoting_stationary_noise() -> None:
    state = AdaptiveCaptureState(
        1_000,
        config(minimum_speech_dbfs=-55.0, speech_start_s=0.02),
        speech_vote,
    )
    initial_floor = state.noise_floor_dbfs

    for _ in range(20):
        decision = state.feed(frame(0.01))
        assert decision.is_speech is False

    assert state.noise_floor_dbfs > initial_floor
    decision = state.feed(frame(0.2))
    assert decision.is_speech is True
    assert decision.speech_started is True


def test_loud_drone_the_vad_rejects_endpoints_instead_of_recording_forever() -> None:
    # A fan/AC room whose ambient sits above the threshold: raw energy trips
    # the detector but the VAD keeps voting non-speech.  The floor must learn
    # the drone so the turn endpoints instead of running to max_duration.
    state = AdaptiveCaptureState(1_000, config(), lambda _samples, _sr: False)

    decision = None
    for _ in range(40):
        decision = state.feed(frame(0.05))
        if state.finished:
            break

    assert decision is not None
    assert decision.stop_reason is CaptureStopReason.TRAILING_SILENCE
    assert state.elapsed_s < 0.8


def test_sustained_drone_that_fools_the_vad_still_endpoints() -> None:
    # Even when the VAD wrongly votes speech on a constant drone, the slow
    # upward floor drift must eventually read it as the new silence.
    state = AdaptiveCaptureState(
        1_000,
        config(noise_rise_smoothing=0.9),
        lambda _samples, _sr: True,
    )

    decision = None
    for _ in range(80):
        decision = state.feed(frame(0.05))
        if state.finished:
            break

    assert decision is not None
    assert decision.stop_reason is CaptureStopReason.TRAILING_SILENCE


def test_cancel_discards_partial_audio_but_manual_end_preserves_it() -> None:
    cancelled = CaptureControl()

    def cancelled_frames() -> Any:
        for index in range(10):
            if index == 4:
                cancelled.cancel()
            yield frame(0.2)

    recorder = AudioRecorder(
        config(speech_start_s=0.02, trailing_silence_s=1.0),
        speech_classifier=speech_vote,
    )
    result = recorder.capture_from_frames(cancelled_frames(), 1_000, control=cancelled)
    assert result.reason is CaptureStopReason.CANCELLED
    assert result.speech_detected is True
    assert result.samples.size == 0

    manual = CaptureControl()

    def manually_ended_frames() -> Any:
        for index in range(10):
            if index == 4:
                manual.end_utterance()
            yield frame(0.2)

    result = recorder.capture_from_frames(manually_ended_frames(), 1_000, control=manual)
    assert result.reason is CaptureStopReason.MANUAL_END
    assert result.speech_detected is True
    assert result.audio_duration_s == pytest.approx(0.08)


def test_interrupt_preserves_audio_and_persists_recovery_wav(tmp_path: Path) -> None:
    # A host/transport drop (not a deliberate user cancel) must keep the speech
    # and write it to the recovery wav so a mid-utterance crash is recoverable.
    recovery = tmp_path / "latest.wav"
    interrupted = CaptureControl()

    def interrupted_frames() -> Any:
        for index in range(10):
            if index == 4:
                interrupted.interrupt()
            yield frame(0.2)

    recorder = AudioRecorder(
        config(
            speech_start_s=0.02,
            trailing_silence_s=1.0,
            save_latest=True,
            latest_wav_path=recovery,
        ),
        speech_classifier=speech_vote,
    )
    result = recorder.capture_from_frames(interrupted_frames(), 1_000, control=interrupted)
    assert result.reason is CaptureStopReason.INTERRUPTED
    assert result.speech_detected is True
    assert result.samples.size > 0  # unlike CANCELLED, audio is kept
    assert result.latest_wav_path == recovery
    assert recovery.is_file() and recovery.stat().st_size > 44


def test_atomic_latest_wav_is_private_and_overwritten(tmp_path: Path) -> None:
    latest = tmp_path / "latest.wav"
    first = np.full(80, 0.25, dtype=np.float32)
    second = np.full(40, -0.25, dtype=np.float32)

    assert write_wav_atomic(latest, first, 8_000) == latest
    assert stat.S_IMODE(latest.stat().st_mode) == 0o600
    with wave.open(str(latest), "rb") as recording:
        assert recording.getnchannels() == 1
        assert recording.getsampwidth() == 2
        assert recording.getframerate() == 8_000
        assert recording.getnframes() == 80

    write_wav_atomic(latest, second, 8_000)
    with wave.open(str(latest), "rb") as recording:
        assert recording.getnframes() == 40
        decoded = np.frombuffer(recording.readframes(40), dtype="<i2")
    assert np.all(decoded < 0)
    assert list(tmp_path.glob(".latest.wav.*.tmp")) == []


class FakeDefault:
    device = (7, 8)


class FakeSoundDevice:
    default = FakeDefault()

    def __init__(self, frames: list[np.ndarray] | None = None) -> None:
        self.frames = frames or []
        self.stream_kwargs: dict[str, Any] | None = None
        self.entered = threading.Event()

    def query_devices(self, device: Any, kind: str) -> dict[str, Any]:
        assert device == 7
        assert kind == "input"
        return {
            "name": "Mock 48 kHz microphone",
            "default_samplerate": 48_000.0,
            "max_input_channels": 2,
        }

    def InputStream(self, **kwargs: Any) -> Any:  # noqa: N802 - sounddevice API
        self.stream_kwargs = kwargs
        owner = self

        class Stream:
            def __enter__(self) -> Stream:
                owner.entered.set()
                callback = kwargs["callback"]
                for value in owner.frames:
                    callback(value.reshape(-1, 1), len(value), None, None)
                return self

            def __exit__(self, *_args: Any) -> None:
                return None

        return Stream()


def test_default_input_uses_native_device_rate() -> None:
    device = resolve_input_device(FakeSoundDevice())
    assert device.index == 7
    assert device.sample_rate == 48_000
    assert device.input_channels == 2


def test_recorder_opens_mock_stream_at_native_rate_without_microphone() -> None:
    def native_frame(value: float) -> np.ndarray:
        return frame(value, sample_rate=48_000)

    backend = FakeSoundDevice(
        [
            native_frame(0.001),
            native_frame(0.001),
            native_frame(0.2),
            native_frame(0.2),
            native_frame(0.001),
            native_frame(0.001),
        ]
    )
    recorder = AudioRecorder(
        config(
            onset_timeout_s=0.5,
            trailing_silence_s=0.04,
            speech_start_s=0.02,
            pre_roll_s=0.04,
        ),
        sounddevice=backend,
        speech_classifier=speech_vote,
    )

    result = recorder.capture()
    assert result.reason is CaptureStopReason.TRAILING_SILENCE
    assert result.sample_rate == 48_000
    assert backend.stream_kwargs is not None
    assert backend.stream_kwargs["samplerate"] == 48_000
    assert backend.stream_kwargs["blocksize"] == 960


def test_live_capture_cancel_wakes_without_waiting_for_onset_timeout() -> None:
    backend = FakeSoundDevice()
    control = CaptureControl()
    recorder = AudioRecorder(
        config(onset_timeout_s=15.0, source_stall_timeout_s=2.0),
        sounddevice=backend,
        speech_classifier=speech_vote,
    )
    completed: list[Any] = []

    worker = threading.Thread(target=lambda: completed.append(recorder.capture(control=control)))
    worker.start()
    assert backend.entered.wait(timeout=0.5)
    started = time.monotonic()
    control.cancel()
    worker.join(timeout=0.5)

    assert not worker.is_alive()
    assert time.monotonic() - started < 0.5
    assert completed[0].reason is CaptureStopReason.CANCELLED


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 12345
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._done = threading.Event()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("fake-player", timeout)
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._done.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._done.set()


def test_playback_is_cancellable_and_registry_cleans_temporary_audio() -> None:
    registry = PlaybackRegistry()
    process = FakeProcess()
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def popen(command: list[str], **kwargs: Any) -> FakeProcess:
        calls.append((command, kwargs))
        return process

    player = AudioPlayer(
        registry=registry,
        popen_factory=popen,
        which=lambda name: f"/mock/{name}" if name == "afplay" else None,
        platform_name="Darwin",
    )
    handle = player.play_samples(np.full(80, 0.1, dtype=np.float32), 8_000)
    temporary = Path(calls[0][0][-1])

    assert handle.running is True
    assert registry.get(handle.id) is handle
    assert calls[0][0][:3] == ["/mock/afplay", "-v", "1.000"]
    assert calls[0][1]["start_new_session"] is True
    assert temporary.exists()

    handle.cancel(grace_s=0.01)
    assert process.terminated is True
    assert process.killed is False
    assert registry.get(handle.id) is None
    assert not temporary.exists()
