"""One long-lived input stream with a software gate in front of it.

Vox used to open a fresh ``InputStream`` for every listen — twice per
conversational turn, counting the armed barge-in capture.  On a Bluetooth HFP
headset each of those opens emits a broadband transient roughly 240 ms after
the stream engages: silence while the pipeline warms up, then a burst ~24 dB
above the initial threshold decaying over ~350 ms.  WebRTC VAD endorses
broadband noise, 60 ms of it satisfies ``speech_start_s``, and the turn opens
on a pop nobody made.  Whisper then returns non-speech and the window closes
around 0.9 s — while the user's actual words land in the dead air between two
phantom windows.

The fix is to stop opening streams.  This source opens one stream per session
and puts a gate in front of it: while the gate is closed the realtime callback
drops frames on the floor, so nothing is queued, buffered, or classified, and
"deaf" genuinely means deaf.  Frames only exist while the user has said they
are talking.

The gate is *not* the same thing as ``pause``/``mute``, which tear the stream
down entirely and block until the device confirms closure.  That remains the
privacy stop; this is the lighter layer above it.
"""

from __future__ import annotations

import queue
import threading
import time
import weakref
from typing import Any, Callable, Iterator

from .audio import (
    AudioDeviceError,
    AudioError,
    CaptureConfig,
    CaptureControl,
    FloatAudio,
    _as_mono_float32,
    _load_sounddevice,
    _PortAudioInUse,
    resolve_input_device,
)


# The transient arrives ~240 ms *after* the stream opens and decays over
# ~350 ms, so a guard shorter than half a second does not actually cover it.
DEFAULT_OPEN_GUARD_SECONDS = 0.5

EventSink = Callable[..., Any]


class PersistentCaptureSource:
    """A session-lived microphone stream that only passes audio when gated open."""

    def __init__(
        self,
        config: CaptureConfig,
        *,
        device: int | str | None = None,
        sounddevice: Any | None = None,
        stream_factory: Callable[..., Any] | None = None,
        open_guard_s: float = DEFAULT_OPEN_GUARD_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        on_event: EventSink | None = None,
    ) -> None:
        if open_guard_s < 0:
            raise ValueError("open_guard_s must not be negative")
        self.config = config
        self._device = device
        self._sounddevice = sounddevice
        self._stream_factory = stream_factory
        self._open_guard_s = open_guard_s
        self._clock = clock
        self._on_event = on_event

        self._lock = threading.Lock()
        self._gate = threading.Event()
        self._stream: Any | None = None
        self._portaudio: _PortAudioInUse | None = None
        self._queue: queue.Queue[FloatAudio | BaseException] | None = None
        self._sample_rate = 0
        self._device_name = ""
        self._guard_until = 0.0
        self._subscribed = False
        self._ungated = 0
        self._dropped_frames = 0

    # ------------------------------------------------------------------ state

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def stream_open(self) -> bool:
        return self._stream is not None

    @property
    def gate_open(self) -> bool:
        return self._gate.is_set()

    @property
    def dropped_frames(self) -> int:
        with self._lock:
            return self._dropped_frames

    def _emit(self, event: str, **data: Any) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, **data)
        except Exception:
            # Instrumentation must never take the microphone down with it.
            pass

    # ------------------------------------------------------------- lifecycle

    def open(self) -> bool:
        """Open the stream.  Returns False when one is already open."""

        with self._lock:
            if self._stream is not None:
                return False

        sounddevice = self._sounddevice or _load_sounddevice()
        device_info = resolve_input_device(sounddevice, self._device)
        frames: queue.Queue[FloatAudio | BaseException] = queue.Queue(
            maxsize=self.config.queue_capacity_frames
        )

        def put_frame(value: FloatAudio | BaseException) -> None:
            try:
                frames.put_nowait(value)
            except queue.Full:
                try:
                    frames.get_nowait()
                except queue.Empty:
                    pass
                with self._lock:
                    self._dropped_frames += 1
                try:
                    frames.put_nowait(value)
                except queue.Full:
                    pass

        def callback(indata: Any, _frame_count: int, _time_info: Any, status: Any) -> None:
            if status and not getattr(status, "input_overflow", False):
                put_frame(AudioDeviceError(f"Audio input status: {status}"))
                return
            # Everything below the gate happens on the realtime thread, so it
            # stays to three cheap reads. A closed gate drops the frame here,
            # before it is ever copied — nothing to leak, nothing to drain.
            if self._clock() < self._guard_until:
                return
            if not self._gate.is_set() and self._ungated <= 0:
                return
            try:
                put_frame(_as_mono_float32(indata).copy())
            except Exception as exc:
                put_frame(AudioDeviceError(f"Invalid audio input frame: {exc}"))

        factory = self._stream_factory or sounddevice.InputStream
        blocksize = max(1, round(device_info.sample_rate * self.config.frame_ms / 1000))
        portaudio = _PortAudioInUse()
        portaudio.__enter__()
        try:
            stream = factory(
                samplerate=device_info.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=blocksize,
                callback=callback,
                device=device_info.index,
            )
            start = getattr(stream, "start", None)
            if callable(start):
                start()
        except Exception as exc:
            portaudio.__exit__(None, None, None)
            if isinstance(exc, AudioError):
                raise
            raise AudioDeviceError(
                f"Could not open the capture stream on {device_info.name}: {exc}"
            ) from exc

        with self._lock:
            self._stream = stream
            self._portaudio = portaudio
            self._queue = frames
            self._sample_rate = device_info.sample_rate
            self._device_name = device_info.name
            self._guard_until = self._clock() + self._open_guard_s
        self._emit(
            "capture.stream_opened",
            device=device_info.name,
            sample_rate=device_info.sample_rate,
            guard_s=round(self._open_guard_s, 3),
        )
        return True

    def close(self) -> bool:
        """Tear the stream down.  Returns False when none was open."""

        with self._lock:
            stream = self._stream
            portaudio = self._portaudio
            self._stream = None
            self._portaudio = None
            self._queue = None
            self._gate.clear()
        if stream is None:
            return False
        for name in ("stop", "close"):
            method = getattr(stream, name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass
        if portaudio is not None:
            portaudio.__exit__(None, None, None)
        self._emit("capture.stream_closed", device=self._device_name)
        return True

    # ------------------------------------------------------------------ gate

    def open_gate(self) -> bool:
        """Let audio through.  Returns False when the gate was already open."""

        if self._gate.is_set():
            return False
        with self._lock:
            frames = self._queue
        # Anything queued across the flip is from before the user said they
        # were talking; the endpointer must not see it.
        if frames is not None:
            while True:
                try:
                    frames.get_nowait()
                except queue.Empty:
                    break
        self._gate.set()
        self._emit("capture.gate_opened")
        return True

    def close_gate(self) -> bool:
        """Go deaf.  Returns False when the gate was already closed."""

        if not self._gate.is_set():
            return False
        self._gate.clear()
        self._emit("capture.gate_closed")
        return True

    # ----------------------------------------------------------- subscription

    def frames(
        self,
        control: CaptureControl,
        *,
        respect_gate: bool = True,
    ) -> Iterator[FloatAudio]:
        """Subscribe to the stream for one capture, observing ``control``.

        The subscription is taken here rather than on the first ``next()``: a
        generator body does not run until it is iterated, and an ungated
        subscriber that registered late would have its opening frames dropped
        at the callback before it ever asked for them.

        The generator ends itself on cancel/interrupt/manual-end/delivered-text
        rather than relying on the consumer, because
        ``AudioRecorder.capture_from_frames`` resolves an exhausted iterable
        into exactly the right stop reason.  That is what lets a gated turn
        reuse the whole tested endpointing path unchanged.

        ``respect_gate=False`` is for the armed barge-in capture, which is its
        own explicit consent act with its own hardened thresholds and must be
        able to hear an interruption while the turn gate is shut.
        """

        with self._lock:
            if self._subscribed:
                raise RuntimeError("capture source already has an active subscriber")
            if self._stream is None:
                raise AudioDeviceError("capture stream is not open")
            frames = self._queue
            self._subscribed = True
            if not respect_gate:
                self._ungated += 1
        assert frames is not None

        released = threading.Event()

        def release() -> None:
            if released.is_set():
                return
            released.set()
            with self._lock:
                self._subscribed = False
                if not respect_gate:
                    self._ungated -= 1

        pump = self._pump(control, frames, respect_gate=respect_gate, release=release)
        # A generator that is subscribed but never iterated would otherwise hold
        # the only subscription slot for the life of the session.
        weakref.finalize(pump, release)
        return pump

    def _pump(
        self,
        control: CaptureControl,
        frames: "queue.Queue[FloatAudio | BaseException]",
        *,
        respect_gate: bool,
        release: Callable[[], None],
    ) -> Iterator[FloatAudio]:
        stall_after = self.config.source_stall_timeout_s
        last_frame_at = self._clock()
        try:
            while True:
                if (
                    control.cancelled
                    or control.text_delivered
                    or control.interrupted
                    or control.manual_end_requested
                ):
                    return
                if self._stream is None:
                    return
                if respect_gate and not self._gate.is_set():
                    return
                try:
                    item = frames.get(timeout=0.02)
                except queue.Empty:
                    if self._clock() - last_frame_at > stall_after:
                        raise AudioDeviceError(
                            f"No audio frames from {self._device_name} for "
                            f"{stall_after:g} seconds"
                        )
                    continue
                last_frame_at = self._clock()
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            release()
