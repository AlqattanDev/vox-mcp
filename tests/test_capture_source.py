from __future__ import annotations

from typing import Any, Iterator

import numpy as np
import pytest

from voxmcp.audio import (
    AudioDeviceError,
    AudioRecorder,
    CaptureConfig,
    CaptureControl,
    CaptureStopReason,
)
from voxmcp.capture_source import PersistentCaptureSource


SAMPLE_RATE = 1_000


def config(**overrides: Any) -> CaptureConfig:
    values: dict[str, Any] = {
        "onset_timeout_s": 1.0,
        "trailing_silence_s": 0.12,
        "short_trailing_silence_s": 0.12,
        "min_duration_s": 0.0,
        "max_duration_s": 3.0,
        "pre_roll_s": 0.0,
        "speech_start_s": 0.06,
        "frame_ms": 20,
        "source_stall_timeout_s": 0.2,
        "save_latest": False,
        "latest_wav_path": None,
    }
    values.update(overrides)
    return CaptureConfig(**values)


def frame(amplitude: float) -> np.ndarray:
    return np.full(round(SAMPLE_RATE * 20 / 1_000), amplitude, dtype=np.float32)


def speech_vote(samples: np.ndarray, _sample_rate: int) -> bool:
    return bool(np.max(np.abs(samples)) > 0.05)


class FakeDefault:
    device = (3, 4)


class FakeSoundDevice:
    """A stream whose frames the test pushes by hand, one callback at a time."""

    default = FakeDefault()

    def __init__(self) -> None:
        self.callback: Any = None
        self.started = False
        self.stopped = False
        self.closed = False
        self.kwargs: dict[str, Any] = {}

    def query_devices(self, device: Any, kind: str) -> dict[str, Any]:
        assert device == 3
        assert kind == "input"
        return {
            "name": "Fake gated microphone",
            "default_samplerate": float(SAMPLE_RATE),
            "max_input_channels": 1,
        }

    def InputStream(self, **kwargs: Any) -> Any:  # noqa: N802 - sounddevice API
        self.kwargs = kwargs
        self.callback = kwargs["callback"]
        owner = self

        class Stream:
            def start(self) -> None:
                owner.started = True

            def stop(self) -> None:
                owner.stopped = True

            def close(self) -> None:
                owner.closed = True

        return Stream()

    def emit(self, *frames: np.ndarray) -> None:
        for value in frames:
            self.callback(value.reshape(-1, 1), len(value), None, None)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build(**overrides: Any) -> tuple[PersistentCaptureSource, FakeSoundDevice, list[tuple]]:
    events: list[tuple] = []
    device = FakeSoundDevice()
    # The guard is off unless a test is about the guard: with a real clock it
    # would swallow every frame the test emits and hide what is being asserted.
    overrides.setdefault("open_guard_s", 0.0)
    source = PersistentCaptureSource(
        config(**overrides.pop("config", {})),
        sounddevice=device,
        on_event=lambda event, **data: events.append((event, data)),
        **overrides,
    )
    return source, device, events


def recorder(**overrides: Any) -> AudioRecorder:
    return AudioRecorder(config(**overrides), speech_classifier=speech_vote)


def flag_at(iterator: Iterator[Any], index: int, action: Any) -> Iterator[Any]:
    """Trip a control flag after a known number of frames, mid-capture."""

    for position, value in enumerate(iterator):
        if position == index:
            action()
        yield value


# --------------------------------------------------------------------- gate


@pytest.mark.timeout(10)
def test_a_closed_gate_is_genuinely_deaf() -> None:
    """Background audio must never reach the endpointer, let alone Whisper."""

    source, device, _ = build()
    source.open()
    assert source.gate_open is False

    # Minutes of music, as far as the microphone is concerned.
    device.emit(*[frame(0.8) for _ in range(200)])

    control = CaptureControl()
    result = recorder().capture_from_frames(
        source.frames(control), source.sample_rate, control=control
    )

    assert result.reason is CaptureStopReason.SOURCE_ENDED
    assert result.speech_detected is False
    assert result.samples.size == 0
    source.close()


@pytest.mark.timeout(10)
def test_opening_the_gate_drops_whatever_was_already_in_flight() -> None:
    source, device, _ = build()
    source.open()
    device.emit(frame(0.9))  # dropped at the callback: gate still shut
    source.open_gate()
    device.emit(*[frame(0.4) for _ in range(5)])
    device.emit(*[frame(0.0) for _ in range(10)])

    control = CaptureControl()
    result = recorder().capture_from_frames(
        source.frames(control), source.sample_rate, control=control
    )

    assert result.reason is CaptureStopReason.TRAILING_SILENCE
    assert result.speech_detected is True
    # Five 20 ms speech frames, and none of the pre-gate audio.
    assert float(np.max(np.abs(result.samples))) == pytest.approx(0.4, abs=1e-6)
    source.close()


@pytest.mark.timeout(10)
def test_gate_flips_report_whether_they_changed_anything() -> None:
    source, _, events = build()
    source.open()
    assert source.open_gate() is True
    assert source.open_gate() is False
    assert source.close_gate() is True
    assert source.close_gate() is False
    names = [name for name, _ in events]
    assert names.count("capture.gate_opened") == 1
    assert names.count("capture.gate_closed") == 1
    source.close()


# ---------------------------------------------------------------- open guard


@pytest.mark.timeout(10)
def test_the_open_guard_swallows_the_bluetooth_stream_open_transient() -> None:
    """The HFP burst lands ~240 ms after open; it must reach nothing."""

    clock = Clock()
    source, device, _ = build(clock=clock, open_guard_s=0.5)
    source.open()
    source.open_gate()

    clock.advance(0.24)
    device.emit(*[frame(0.9) for _ in range(18)])  # the pop, at full tilt
    clock.advance(0.4)  # now past the guard
    device.emit(*[frame(0.3) for _ in range(5)])
    device.emit(*[frame(0.0) for _ in range(10)])

    control = CaptureControl()
    result = recorder().capture_from_frames(
        source.frames(control), source.sample_rate, control=control
    )

    assert result.speech_detected is True
    # The burst is 0.9; if any of it survived the guard this is not 0.3.
    assert float(np.max(np.abs(result.samples))) == pytest.approx(0.3, abs=1e-6)
    source.close()


# ------------------------------------------------------------ turn control


@pytest.mark.timeout(10)
def test_manual_end_through_the_gate_keeps_the_audio() -> None:
    """This is what the hotkey's second tap does: end the turn and submit it."""

    source, device, _ = build()
    source.open()
    source.open_gate()
    device.emit(*[frame(0.4) for _ in range(10)])

    control = CaptureControl()
    result = recorder().capture_from_frames(
        flag_at(source.frames(control), 6, control.end_utterance),
        source.sample_rate,
        control=control,
    )

    assert result.reason is CaptureStopReason.MANUAL_END
    assert result.speech_detected is True
    assert result.samples.size > 0
    source.close()


@pytest.mark.timeout(10)
def test_cancelling_through_the_gate_discards_the_audio() -> None:
    source, device, _ = build()
    source.open()
    source.open_gate()
    device.emit(*[frame(0.4) for _ in range(10)])

    control = CaptureControl()
    result = recorder().capture_from_frames(
        flag_at(source.frames(control), 6, control.cancel),
        source.sample_rate,
        control=control,
    )

    assert result.reason is CaptureStopReason.CANCELLED
    assert result.samples.size == 0
    source.close()


@pytest.mark.timeout(10)
def test_typing_ends_a_gated_turn_without_transcribing_it() -> None:
    """deliver_text has to work through the gate, or typed turns vanish."""

    source, device, _ = build()
    source.open()
    source.open_gate()
    device.emit(*[frame(0.4) for _ in range(10)])

    control = CaptureControl()
    result = recorder().capture_from_frames(
        flag_at(source.frames(control), 4, lambda: control.deliver_text("typed instead")),
        source.sample_rate,
        control=control,
    )

    assert result.reason is CaptureStopReason.DELIVERED_TEXT
    assert result.samples.size == 0
    assert control.delivered_text == "typed instead"
    source.close()


# ------------------------------------------------------------- subscription


@pytest.mark.timeout(10)
def test_only_one_capture_may_hold_the_source_at_a_time() -> None:
    """Two consumers of one queue would steal each other's frames."""

    source, device, _ = build()
    source.open()
    source.open_gate()
    device.emit(frame(0.4))

    first = source.frames(CaptureControl())
    next(first)
    with pytest.raises(RuntimeError, match="active subscriber"):
        next(source.frames(CaptureControl()))
    first.close()

    # The slot is released once the first capture lets go.
    device.emit(frame(0.4))
    second = source.frames(CaptureControl())
    next(second)
    second.close()
    source.close()


@pytest.mark.timeout(10)
def test_barge_in_hears_through_a_closed_gate() -> None:
    """Arming is its own consent act with its own hardened thresholds."""

    source, device, _ = build()
    source.open()
    assert source.gate_open is False

    armed = source.frames(CaptureControl(), respect_gate=False)
    device.emit(frame(0.7))
    assert float(np.max(np.abs(next(armed)))) == pytest.approx(0.7, abs=1e-6)
    armed.close()

    # And the ungated subscription does not leave the source permanently open.
    device.emit(frame(0.7))
    control = CaptureControl()
    result = recorder().capture_from_frames(
        source.frames(control), source.sample_rate, control=control
    )
    assert result.samples.size == 0
    source.close()


@pytest.mark.timeout(10)
def test_subscribing_to_a_closed_stream_is_an_error() -> None:
    source, _, _ = build()
    with pytest.raises(AudioDeviceError, match="not open"):
        next(source.frames(CaptureControl()))


# ------------------------------------------------------------------- device


@pytest.mark.timeout(10)
def test_a_silent_device_stalls_into_a_device_error() -> None:
    """A wedged stream must surface, not hang the turn forever."""

    source, _, _ = build()
    source.open()
    source.open_gate()

    control = CaptureControl()
    with pytest.raises(AudioDeviceError, match="No audio frames"):
        recorder().capture_from_frames(
            source.frames(control), source.sample_rate, control=control
        )
    source.close()


@pytest.mark.timeout(10)
def test_a_driver_status_error_reaches_the_capture() -> None:
    source, device, _ = build()
    source.open()
    source.open_gate()
    device.callback(frame(0.4).reshape(-1, 1), 20, None, "input underflow")

    control = CaptureControl()
    with pytest.raises(AudioDeviceError, match="Audio input status"):
        recorder().capture_from_frames(
            source.frames(control), source.sample_rate, control=control
        )
    source.close()


# ---------------------------------------------------------------- lifecycle


@pytest.mark.timeout(10)
def test_the_stream_opens_once_per_session_and_says_so() -> None:
    source, device, events = build()

    assert source.open() is True
    assert source.open() is False  # the whole point: no churn between turns
    assert source.stream_open is True
    assert source.sample_rate == SAMPLE_RATE
    assert device.started is True
    assert device.kwargs["samplerate"] == SAMPLE_RATE
    assert device.kwargs["channels"] == 1
    assert device.kwargs["blocksize"] == 20

    assert source.close() is True
    assert source.close() is False
    assert device.stopped is True
    assert device.closed is True
    assert source.stream_open is False

    names = [name for name, _ in events]
    assert names.count("capture.stream_opened") == 1
    assert names.count("capture.stream_closed") == 1


@pytest.mark.timeout(10)
def test_closing_the_stream_shuts_the_gate() -> None:
    source, _, _ = build()
    source.open()
    source.open_gate()
    source.close()
    assert source.gate_open is False
