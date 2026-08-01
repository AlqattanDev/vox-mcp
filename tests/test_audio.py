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
    _dbfs,
    AdaptiveCaptureState,
    AudioDeviceError,
    AudioPlayer,
    AudioRecorder,
    CaptureConfig,
    CaptureControl,
    CapturePhase,
    CaptureStopReason,
    LevelMeasurement,
    PlaybackRegistry,
    WebRTCVADClassifier,
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
    # Five, not fifteen: this is how long the microphone stays open after the
    # agent stops speaking, and a fifteen-second window is fifteen seconds in
    # which the room can interrupt an agent that has gone back to work.
    assert defaults.onset_timeout_s == 5.0
    assert defaults.trailing_silence_s == 1.6
    assert defaults.min_duration_s == 0.5
    assert defaults.max_duration_s == 75.0
    assert defaults.pre_roll_s == 0.3

    # Fifteen stays reachable deliberately; above it does not.
    assert CaptureConfig(onset_timeout_s=15.0).onset_timeout_s == 15.0
    assert CaptureConfig(onset_timeout_s=None).onset_timeout_s is None
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

    for _ in range(12):  # a real utterance, not a click: see false_onset_speech_s
        state.feed(frame(0.2))
    for _ in range(6):
        decision = state.feed(frame(0.001))

    assert decision.stop_reason is CaptureStopReason.TRAILING_SILENCE
    result = state.result()
    assert result.reason is CaptureStopReason.TRAILING_SILENCE
    assert result.speech_detected is True
    assert result.trailing_silence_s == pytest.approx(0.12)
    assert result.speech_duration_s == pytest.approx(0.24)
    # At speech confirmation: 300 ms pre-roll. Afterwards: 180 ms speech and
    # 120 ms trailing silence.
    assert result.audio_duration_s == pytest.approx(0.60)
    assert np.allclose(result.samples[:20], 0.001)
    assert np.max(result.samples) == pytest.approx(0.2)


def test_trailing_silence_cannot_end_before_minimum_turn_duration() -> None:
    state = AdaptiveCaptureState(
        1_000,
        config(min_duration_s=0.40, trailing_silence_s=0.04, speech_start_s=0.02),
        speech_vote,
    )
    for _ in range(10):  # 200 ms — an utterance, so only min_duration holds it
        state.feed(frame(0.2))
    for _ in range(9):
        decision = state.feed(frame(0.001))
        assert decision.stop_reason is None
    decision = state.feed(frame(0.001))
    assert decision.stop_reason is CaptureStopReason.TRAILING_SILENCE


def adaptive_config(**overrides: Any) -> CaptureConfig:
    """Endpointing config with the real trailing-silence scaling in play."""

    values: dict[str, Any] = {
        "trailing_silence_s": 1.6,
        "short_trailing_silence_s": 0.6,
        "short_utterance_speech_s": 1.5,
        "long_utterance_speech_s": 3.0,
        "min_duration_s": 0.0,
        "max_duration_s": 30.0,
        "speech_start_s": 0.02,
    }
    values.update(overrides)
    return config(**values)


def feed_speech(state: AdaptiveCaptureState, seconds: float) -> None:
    """Feed `seconds` of speech-shaped audio, dips and all.

    A flat tone is a drone, not speech, and the detector is right to say so —
    a steady level becomes its own noise floor. Real speech breathes: short
    quiet gaps between words are what keep the floor down where the room is.
    """

    for index in range(round(seconds / 0.02)):
        if index and index % 4 == 0:  # a 20 ms inter-word gap, too short to close
            state.feed(frame(0.002))
        state.feed(frame(0.2))


def silence_until_stop(state: AdaptiveCaptureState, limit_s: float = 5.0) -> float:
    """Feed silence until the utterance endpoints; return the silence spent."""

    for index in range(round(limit_s / 0.02)):
        decision = state.feed(frame(0.001))
        if decision.stop_reason is not None:
            return (index + 1) * 0.02
    raise AssertionError("capture never endpointed")


def test_short_answer_endpoints_on_the_short_trailing_silence() -> None:
    # "no, keep it" — well under short_utterance_speech_s, so it should not wait
    # 1.6s. Long enough to be a sentence someone finished, which is the line the
    # fast close now sits behind.
    state = AdaptiveCaptureState(1_000, adaptive_config(), speech_vote)
    feed_speech(state, 0.9)
    assert silence_until_stop(state) == pytest.approx(0.6, abs=0.02)


def test_a_blip_does_not_get_the_fastest_hang_up_in_the_range() -> None:
    """The likeliest false endpoint used to close quickest of all.

    Measured live: 0.10 s and 0.36 s of speech, both ending on
    ``trailing_silence`` at 0.6 s while Ali was still drawing breath — the turn
    was over before his sentence started, twice inside a minute. Scaling the
    close to how much was said assumes something was said; under
    ``min_speech_s`` nothing has been, so the full window applies and the
    sentence has room to arrive.
    """

    state = AdaptiveCaptureState(1_000, adaptive_config(), speech_vote)
    feed_speech(state, 0.2)
    assert state.speech_duration_s < state.config.min_speech_s
    assert silence_until_stop(state) == pytest.approx(1.6, abs=0.02)


def test_the_guard_is_bounded_by_the_full_window_not_the_max_duration() -> None:
    # The guard escalates the close, it never removes it: a blip that is never
    # followed by speech still ends on trailing_silence rather than holding the
    # microphone open to max_duration.
    state = AdaptiveCaptureState(1_000, adaptive_config(), speech_vote)
    feed_speech(state, 0.1)
    spent = silence_until_stop(state, limit_s=10.0)
    assert spent == pytest.approx(1.6, abs=0.02)
    assert spent < state.config.max_duration_s


def reply_window_config(**overrides: Any) -> CaptureConfig:
    """What a converse reply window actually is: 5 s of waiting, real closes."""

    values: dict[str, Any] = {
        "onset_timeout_s": 5.0,
        "trailing_silence_s": 1.6,
        "short_trailing_silence_s": 0.6,
        "short_utterance_speech_s": 1.5,
        "long_utterance_speech_s": 3.0,
        "min_duration_s": 0.5,
        "max_duration_s": 30.0,
        "speech_start_s": 0.06,
    }
    values.update(overrides)
    return config(**values)


def feed_silence(state: AdaptiveCaptureState, seconds: float) -> None:
    for _ in range(round(seconds / 0.02)):
        if state.finished:
            return
        state.feed(frame(0.001))


def test_a_click_does_not_eat_the_reply_window() -> None:
    """The bug Ali hit on Pi: 5 s of reply window spent in 1.68 s.

    A click, a breath or a chair satisfies ``speech_start_s`` in 60 ms, and from
    that moment the turn is an utterance — the only way out is trailing silence,
    which arrives 1.6 s later and hangs up. Measured live: 1.68 s of audio,
    0.08 s of it speech, and Ali never got to answer. A close that carries
    almost no speech is not a close; the window has to come back.
    """

    state = AdaptiveCaptureState(1_000, reply_window_config(), speech_vote)
    for _ in range(4):  # 80 ms — exactly the blip that ended the live turn
        state.feed(frame(0.2))
    assert state.phase is CapturePhase.CAPTURING
    assert state.speech_duration_s == pytest.approx(0.08)

    feed_silence(state, 2.0)  # past the 1.68 s the microphone used to close at
    assert state.finished is False
    assert state.phase is CapturePhase.WAITING_FOR_SPEECH
    assert state.false_onsets == 1

    feed_silence(state, 3.5)
    assert state.finished is True
    result = state.result()
    # The window is five seconds of waiting in total, not five per false start.
    assert result.elapsed_s == pytest.approx(5.0, abs=0.05)
    assert result.reason is CaptureStopReason.ONSET_TIMEOUT
    # The click is thrown away rather than sent to Whisper as a turn.
    assert result.samples.size == 0
    assert result.speech_detected is False


def test_the_window_comes_back_in_time_for_a_late_answer() -> None:
    # The point of giving the window back: an answer that starts after the click
    # is still the user's turn, and endpoints on its own trailing silence.
    state = AdaptiveCaptureState(1_000, reply_window_config(), speech_vote)
    for _ in range(4):
        state.feed(frame(0.2))
    feed_silence(state, 2.0)
    assert state.false_onsets == 1

    feed_speech(state, 0.9)  # "no, keep it" — three seconds into the window
    assert silence_until_stop(state) == pytest.approx(0.6, abs=0.02)
    result = state.result()
    assert result.reason is CaptureStopReason.TRAILING_SILENCE
    assert result.speech_duration_s == pytest.approx(0.9, abs=0.02)


def test_a_real_one_word_answer_is_still_a_turn() -> None:
    # The line has to sit above a click and below "yes". A one-word answer
    # measures 0.3-0.4 s of speech; discarding it would be the worse bug.
    state = AdaptiveCaptureState(1_000, reply_window_config(), speech_vote)
    feed_speech(state, 0.4)
    assert state.speech_duration_s >= state.config.false_onset_speech_s
    assert silence_until_stop(state) == pytest.approx(1.6, abs=0.02)
    assert state.false_onsets == 0
    assert state.result().reason is CaptureStopReason.TRAILING_SILENCE


def test_a_restless_room_cannot_hold_the_microphone_open() -> None:
    # Every false start is forgiven, none of them buys more time: a room that
    # clicks once a second must still hand the window back on schedule.
    state = AdaptiveCaptureState(1_000, reply_window_config(), speech_vote)
    for _ in range(12):
        for _ in range(4):
            if not state.finished:
                state.feed(frame(0.2))
        feed_silence(state, 2.0)
        if state.finished:
            break

    assert state.finished is True
    result = state.result()
    assert result.reason is CaptureStopReason.ONSET_TIMEOUT
    assert result.samples.size == 0
    # Bounded by the window plus the one close being waited on when it expired —
    # never by max_duration, and never open indefinitely.
    assert result.elapsed_s < 5.0 + state.config.trailing_silence_s + 0.1


def test_an_armed_barge_in_mic_is_untouched_by_the_reply_window_rule() -> None:
    # onset_timeout_s=None means there is no window to give back, and on both
    # paths that pass it — barge-in, and a turn opened by holding the key —
    # someone is already talking. They keep today's close exactly.
    state = AdaptiveCaptureState(
        1_000, reply_window_config(onset_timeout_s=None), speech_vote
    )
    for _ in range(4):
        state.feed(frame(0.2))
    assert silence_until_stop(state, limit_s=5.0) == pytest.approx(1.6, abs=0.02)
    assert state.false_onsets == 0
    assert state.result().reason is CaptureStopReason.TRAILING_SILENCE


def test_the_reply_window_runs_the_real_capture_path() -> None:
    # Through AudioRecorder, the way a listen actually runs: a click at the top
    # of the window must not end the capture, and the frames must keep being
    # consumed for the whole five seconds.
    consumed = 0

    def clicked_then_silent() -> Any:
        nonlocal consumed
        for index in range(400):  # 8 s of frames available
            consumed = index + 1
            yield frame(0.2 if index < 4 else 0.001)

    recorder = AudioRecorder(
        reply_window_config(), speech_classifier=speech_vote
    )
    result = recorder.capture_from_frames(clicked_then_silent(), 1_000)
    assert result.reason is CaptureStopReason.ONSET_TIMEOUT
    assert result.samples.size == 0
    assert consumed == pytest.approx(250, abs=3)  # 5 s at 20 ms a frame


def test_cancelling_during_a_resumed_reply_window_cancels_only_the_turn() -> None:
    # Cancel keeps naming itself: the resumed window must not launder a user
    # cancel into a timeout, and the audio is still discarded for privacy.
    cancelled = CaptureControl()

    def click_then_cancel() -> Any:
        for index in range(400):
            if index == 150:  # 3 s in, mid-window, after the click was dropped
                cancelled.cancel()
            yield frame(0.2 if index < 4 else 0.001)

    recorder = AudioRecorder(
        reply_window_config(), speech_classifier=speech_vote
    )
    result = recorder.capture_from_frames(
        click_then_cancel(), 1_000, control=cancelled
    )
    assert result.reason is CaptureStopReason.CANCELLED
    assert result.samples.size == 0


def test_naming_the_close_applies_it_to_every_utterance_length() -> None:
    # What the engine builds when a caller passes trailing_silence_s explicitly:
    # floor collapsed onto the request. Proven here at the endpointer, so the
    # engine-side test only has to show it produces this config.
    named = adaptive_config(trailing_silence_s=1.2, short_trailing_silence_s=1.2)
    for speech_s in (0.2, 0.9, 2.25, 4.0):
        state = AdaptiveCaptureState(1_000, named, speech_vote)
        feed_speech(state, speech_s)
        assert silence_until_stop(state) == pytest.approx(1.2, abs=0.03), speech_s


def test_long_answer_keeps_the_full_trailing_silence() -> None:
    # A rambling turn must retain every bit of the patience it has today.
    state = AdaptiveCaptureState(1_000, adaptive_config(), speech_vote)
    feed_speech(state, 4.0)
    assert silence_until_stop(state) == pytest.approx(1.6, abs=0.02)


def test_medium_answer_interpolates_between_the_two_anchors() -> None:
    # 2.25s of speech sits at the midpoint of the 1.5s..3.0s ramp, so it should
    # want the midpoint of 0.6s..1.6s rather than either extreme.
    state = AdaptiveCaptureState(1_000, adaptive_config(), speech_vote)
    feed_speech(state, 2.25)
    assert silence_until_stop(state) == pytest.approx(1.1, abs=0.03)


def test_pausing_mid_thought_does_not_demote_a_long_answer() -> None:
    # Speech duration, not wall time, drives the scaling: thinking mid-sentence
    # must not hand a long answer the short-answer deadline.
    state = AdaptiveCaptureState(1_000, adaptive_config(), speech_vote)
    feed_speech(state, 2.0)
    for _ in range(20):  # 400 ms of hesitation, short of any close
        assert state.feed(frame(0.001)).stop_reason is None
    feed_speech(state, 2.0)  # 4.0s of speech in total
    assert silence_until_stop(state) == pytest.approx(1.6, abs=0.02)


def test_short_trailing_silence_clamps_to_a_lower_ceiling() -> None:
    # Lowering the ceiling below the floor collapses the ramp instead of
    # raising: VOX_TRAILING_SILENCE_SECONDS=0.4 must not crash the runtime.
    clamped = CaptureConfig(
        trailing_silence_s=0.4, save_latest=False, latest_wav_path=None
    )
    assert clamped.short_trailing_silence_s == pytest.approx(0.4)

    with pytest.raises(ValueError, match="short_trailing_silence_s"):
        CaptureConfig(short_trailing_silence_s=0.0)
    with pytest.raises(ValueError, match="short_utterance_speech_s"):
        CaptureConfig(short_utterance_speech_s=4.0, long_utterance_speech_s=3.0)


def test_state_waits_indefinitely_when_onset_timeout_is_disabled() -> None:
    # The barge-in capture must stay open for as long as the reply lasts, and
    # what ends it is the reply finishing rather than a clock. Inheriting the
    # ordinary onset timeout closed the microphone at 15.1 s of a measured 72 s
    # answer, leaving 79% of it silently uninterruptible.
    state = AdaptiveCaptureState(1_000, config(onset_timeout_s=None), speech_vote)
    for _ in range(2_000):  # 40 s of silence, far past the 15 s hard bound
        decision = state.feed(frame(0.001))
        assert decision.stop_reason is None

    assert state.finished is False
    assert state.elapsed_s > 15.0

    # ...and it still hears speech whenever it eventually arrives.
    for _ in range(40):
        state.feed(frame(0.9))
    assert state.speech_detected is True


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
    # Even when the VAD wrongly votes speech on a constant drone, the room
    # reads as the room: a steady level becomes its own floor, and a frame
    # sitting *at* the floor never clears the rise above it. No tuning knob
    # is involved — that is the point of reading the floor from raw levels.
    state = AdaptiveCaptureState(1_000, config(), lambda _samples, _sr: True)

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


def test_publish_level_maps_dbfs_to_meter_range() -> None:
    control = CaptureControl()
    assert control.level == 0.0
    control.publish_level(-96.0)  # silence floor
    assert control.level == 0.0
    control.publish_level(0.0)  # full scale
    assert control.level == pytest.approx(1.0)
    control.publish_level(-30.0)  # typical speech
    assert control.level == pytest.approx(0.5)


def test_capture_publishes_live_level_for_the_meter() -> None:
    # The menu-bar waveform reads CaptureControl.level; the real capture loop
    # must feed it, so a loud utterance has to leave a non-zero level behind.
    control = CaptureControl()

    def loud_frames() -> Any:
        for index in range(8):
            if index == 5:
                control.end_utterance()
            yield frame(0.6)

    recorder = AudioRecorder(
        config(speech_start_s=0.02, trailing_silence_s=1.0),
        speech_classifier=speech_vote,
    )
    recorder.capture_from_frames(loud_frames(), 1_000, control=control)
    assert control.level > 0.5


def test_levels_since_hands_over_every_sample_not_just_the_latest() -> None:
    """A reader slower than the frame rate must not lose three samples in four.

    Frames are 20 ms, so levels are measured at ~50 Hz while the status app polls
    at 12.5 Hz. Reading only the newest value turned a smooth signal into a
    staircase; the window is what decouples the two rates.
    """

    control = CaptureControl()
    assert control.levels_since(0) == ([], 0)

    for dbfs in (-60.0, -30.0, 0.0):
        control.publish_level(dbfs)
    levels, seq = control.levels_since(0)
    assert levels == pytest.approx([0.0, 0.5, 1.0])
    assert seq == 3

    # Asking again from the same cursor yields nothing new, so a burst is never
    # drawn twice.
    assert control.levels_since(seq) == ([], 3)

    control.publish_level(-30.0)
    levels, seq = control.levels_since(seq)
    assert levels == pytest.approx([0.5])
    assert seq == 4


def test_reading_levels_does_not_consume_them() -> None:
    """`/health` has more callers than the status app.

    A destructive read let whichever polled first steal the waveform, so running
    `vox doctor` froze the meter. Two readers must each see the whole signal.
    """

    control = CaptureControl()
    for dbfs in (-60.0, -30.0, 0.0):
        control.publish_level(dbfs)

    app_levels, app_seq = control.levels_since(0)
    doctor_levels, doctor_seq = control.levels_since(0)
    assert app_levels == pytest.approx(doctor_levels)
    assert app_seq == doctor_seq == 3

    # And each keeps its own position independently.
    control.publish_level(-30.0)
    assert control.levels_since(app_seq)[0] == pytest.approx([0.5])
    assert len(control.levels_since(0)[0]) == 4


def test_the_level_window_is_bounded_and_keeps_the_newest() -> None:
    """Nobody polling must not grow without limit, and stale levels are worthless."""

    control = CaptureControl()
    for index in range(400):
        control.publish_level(-60.0 + index * 0.1)
    levels, seq = control.levels_since(0)
    assert len(levels) == 128
    assert seq == 400
    # The tail, not the head: the last thing published is the last thing read.
    assert levels[-1] == pytest.approx(control.level)

    # A reader that fell further behind than the window gets what survives, not
    # a slice of nothing.
    assert len(control.levels_since(1)[0]) == 128


def test_a_reader_whose_capture_was_replaced_starts_over() -> None:
    """Each capture is a new control, so the sequence restarts at zero.

    A reader still holding the old cursor would compute a negative delta; it must
    fall back to the whole window rather than drawing nothing forever.
    """

    fresh = CaptureControl()
    for dbfs in (-30.0, -20.0):
        fresh.publish_level(dbfs)
    levels, seq = fresh.levels_since(500)  # a cursor from the previous capture
    assert len(levels) == 2
    assert seq == 2


def test_raw_dictation_capture_also_fills_the_level_buffer(tmp_path: Path) -> None:
    """Dictation is where the waveform matters most — it runs from other apps."""

    control = CaptureControl()

    def loud_frames() -> Any:
        for index in range(8):
            if index == 5:
                control.end_utterance()
            yield frame(0.6)

    recorder = AudioRecorder(config(), speech_classifier=speech_vote)
    recorder.capture_raw_from_frames(
        loud_frames(), 1_000, destination=tmp_path / "dictation.wav", control=control
    )
    drained, _ = control.levels_since(0)
    assert len(drained) >= 5
    assert max(drained) > 0.5


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


def test_frame_driven_capture_reaches_for_vad_like_the_device_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gated turn runs through capture_from_frames; it must not lose VAD.

    The device path builds the classifier lazily when none was injected. This
    one used to skip that and silently fall back to energy-only detection.
    """

    calls: list[int] = []

    def fake_create() -> Any:
        calls.append(1)
        return speech_vote

    monkeypatch.setattr(WebRTCVADClassifier, "create", staticmethod(fake_create))
    recorder = AudioRecorder(config(speech_start_s=0.02, trailing_silence_s=0.06))
    result = recorder.capture_from_frames(
        iter([frame(0.4)] * 12 + [frame(0.0)] * 5),
        1_000,
    )
    assert calls == [1]
    assert result.speech_detected is True


def test_a_dying_frame_source_is_reported_as_a_device_error() -> None:
    """A source that raises mid-utterance is a device failure, not silence."""

    def failing_frames() -> Any:
        yield frame(0.4)
        yield frame(0.4)
        raise AudioDeviceError("stream went away")

    recorder = AudioRecorder(config(speech_start_s=0.02), speech_classifier=speech_vote)
    with pytest.raises(AudioDeviceError, match="stream went away"):
        recorder.capture_from_frames(failing_frames(), 1_000)


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


@pytest.mark.parametrize(
    "room_dbfs,wander_db,speech_dbfs,label",
    [
        (-70.0, 3.0, -35.0, "silent bedroom at night"),
        (-48.8, 6.0, -28.0, "measured MacBook room"),
        (-40.0, 8.0, -25.0, "same room, machines running"),
        (-32.0, 8.0, -18.0, "office or cafe"),
    ],
)
def test_one_rule_holds_across_rooms_25db_apart(
    room_dbfs: float, wander_db: float, speech_dbfs: float, label: str
) -> None:
    """Silence stays silence and speech stays speech, wherever you are.

    Vox has to work in the room you are in, not the room it was calibrated in.
    These four differ by nearly 40 dB of absolute level; the gate has to move
    with them without anyone typing a number. WebRTC is simulated at its worst
    here — a VAD that calls every single frame speech — because that is the
    vote that made an idle room read as continuous talking.
    """

    rng = np.random.default_rng(7)
    always_speech = lambda _samples, _sr: True  # noqa: E731

    def room_frame() -> np.ndarray:
        return frame(10 ** ((room_dbfs + rng.uniform(-wander_db, wander_db)) / 20.0))

    def speech_frame() -> np.ndarray:
        return frame(10 ** ((speech_dbfs + rng.uniform(-2.0, 2.0)) / 20.0))

    def classify(samples: np.ndarray) -> bool:
        return state._classify(samples, _dbfs(samples))

    state = AdaptiveCaptureState(1_000, config(), always_speech)
    for _ in range(50):  # a second to read the room
        classify(room_frame())

    false_speech = sum(1 for _ in range(200) if classify(room_frame()))
    heard = sum(1 for _ in range(25) if classify(speech_frame()))

    assert false_speech <= 4, f"{label}: room read as speech {false_speech}/200"
    assert heard >= 22, f"{label}: real speech missed, only {heard}/25"


def test_room_tone_the_vad_calls_speech_is_still_below_the_absolute_floor() -> None:
    # Measured on a MacBook Pro mic: an idle room sits around -45 dBFS, and
    # WebRTC VAD votes speech on it. Honouring that vote against the learned
    # floor alone made silence read as continuous speech — trailing silence
    # never accumulated and a one-word answer took seven seconds to endpoint.
    # minimum_speech_dbfs has to mean "not speech", whoever disagrees.
    always_speech = lambda _samples, _sr: True  # noqa: E731 - a VAD that never says no
    state = AdaptiveCaptureState(
        1_000,
        config(minimum_speech_dbfs=-38.0, trailing_silence_s=0.4, min_duration_s=0.0),
        always_speech,
    )

    # Room tone at roughly -45 dBFS: above the noise floor, below the floor
    # that decides what counts as speech at all.
    room = frame(0.0056)
    for _ in range(10):
        assert state.feed(room).is_speech is False

    # Real speech clears it and is still detected.
    assert state.feed(frame(0.2)).is_speech is True


def test_the_absolute_floor_does_not_deafen_the_energy_path() -> None:
    # Raising the floor must not break the no-VAD fallback: loud speech is
    # still speech when webrtcvad is unavailable.
    state = AdaptiveCaptureState(1_000, config(minimum_speech_dbfs=-38.0), None)
    assert state.feed(frame(0.001)).is_speech is False
    assert state.feed(frame(0.3)).is_speech is True


def test_measure_reports_loudness_without_retaining_audio() -> None:
    # Two thirds quiet, one third loud: the median must land on the quiet
    # bleed and the peak on the loud frames, which is exactly the shape the
    # barge-in calibration reads.
    quiet = [np.full(960, 0.001, dtype=np.float32) for _ in range(20)]
    loud = [np.full(960, 0.2, dtype=np.float32) for _ in range(10)]
    backend = FakeSoundDevice(quiet + loud)
    recorder = AudioRecorder(
        config(),
        sounddevice=backend,
        clock=iter([0.0, 1.0]).__next__,
    )

    measurement = recorder.measure(0.5)

    assert measurement.frames == 30
    assert measurement.device == "Mock 48 kHz microphone"
    assert measurement.median_dbfs == pytest.approx(-60.0, abs=1.0)
    assert measurement.peak_dbfs == pytest.approx(-14.0, abs=1.0)
    assert measurement.median_dbfs < measurement.peak_dbfs
    assert not hasattr(measurement, "samples")


def test_measure_rejects_a_non_positive_window() -> None:
    recorder = AudioRecorder(config(), sounddevice=FakeSoundDevice([]))
    with pytest.raises(ValueError, match="seconds must be positive"):
        recorder.measure(0.0)


def test_level_measurement_of_an_empty_window_is_silence() -> None:
    empty = LevelMeasurement.from_dbfs([], device="none")
    assert empty.frames == 0
    assert empty.median_dbfs == -96.0
    assert empty.peak_dbfs == -96.0


def test_default_input_uses_native_device_rate() -> None:
    device = resolve_input_device(FakeSoundDevice())
    assert device.index == 7
    assert device.sample_rate == 48_000
    assert device.input_channels == 2


class DisconnectingSoundDevice:
    """A backend whose launch-time snapshot points at unplugged earbuds.

    Only reinitializing PortAudio reveals that the default input moved to the
    built-in microphone — exactly what happens when Bluetooth headphones
    disconnect mid-session.
    """

    def __init__(self) -> None:
        class Default:
            device = (7, 8)

        self.default = Default()
        self.refreshed = False

    def _terminate(self) -> None:
        pass

    def _initialize(self) -> None:
        self.refreshed = True
        self.default.device = (3, 4)

    def query_devices(self, device: Any, kind: str) -> dict[str, Any]:
        assert kind == "input"
        if device == 7:
            raise OSError("Internal PortAudio error [PaErrorCode -9986]")
        assert device == 3
        return {
            "name": "MacBook Pro Microphone",
            "default_samplerate": 48_000.0,
            "max_input_channels": 1,
        }


def test_resolving_input_refreshes_the_device_list_first() -> None:
    """Disconnected headphones must not leave capture opening a ghost device.

    Measured live: the FreeClip 2 disconnected and every ⌘§ press failed with
    PaErrorCode -9986 on the ghost device until a runtime restart, because
    resolution trusted PortAudio's launch-time snapshot.
    """

    backend = DisconnectingSoundDevice()
    device = resolve_input_device(backend)
    assert backend.refreshed
    assert device.index == 3
    assert device.name == "MacBook Pro Microphone"


def _write_tone_wav(path: Path, *, loud_seconds: float, quiet_seconds: float) -> None:
    """A wav that is loud, then near-silent — an envelope a test can see."""

    import wave as wave_module

    sample_rate = 24_000
    loud = 0.5 * np.sin(2 * np.pi * 440 * np.arange(int(sample_rate * loud_seconds)) / sample_rate)
    quiet = np.zeros(int(sample_rate * quiet_seconds))
    samples = (np.concatenate([loud, quiet]) * 32767).astype(np.int16)
    with wave_module.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(samples.tobytes())


def test_wav_envelope_tracks_the_actual_signal(tmp_path: Path) -> None:
    from voxmcp.audio import wav_envelope

    clip = tmp_path / "clip.wav"
    _write_tone_wav(clip, loud_seconds=0.2, quiet_seconds=0.2)
    envelope = wav_envelope(clip)

    assert len(envelope) == 20  # 400 ms of 20 ms frames
    assert all(level > 0.5 for level in envelope[:9])
    assert all(level < 0.1 for level in envelope[11:])


def test_wav_envelope_failure_is_empty_not_raised(tmp_path: Path) -> None:
    from voxmcp.audio import wav_envelope

    missing = tmp_path / "missing.wav"
    garbage = tmp_path / "garbage.wav"
    garbage.write_bytes(b"not a wav at all")
    assert wav_envelope(missing) == []
    assert wav_envelope(garbage) == []


def test_playback_levels_release_frames_only_as_the_clock_plays_them(tmp_path: Path) -> None:
    """The speaking waveform must stay synchronized to the audio.

    Handing the whole envelope over at once would let the meter sprint through
    the clip at poll speed and finish drawing before the voice finished
    talking — which is a different animation, not the signal.
    """

    from voxmcp.audio import PlaybackLevels

    clip = tmp_path / "clip.wav"
    _write_tone_wav(clip, loud_seconds=0.2, quiet_seconds=0.2)
    now = [0.0]
    levels = PlaybackLevels(clock=lambda: now[0])

    levels.begin(clip)
    burst, seq = levels.levels_since(0)
    assert burst == [] and seq == 0

    now[0] += 0.1  # five 20 ms frames have played
    burst, seq = levels.levels_since(0)
    assert len(burst) == 5 and seq == 5
    assert all(level > 0.5 for level in burst)

    now[0] += 10.0  # long past the end: the envelope is bounded by the clip
    burst, seq = levels.levels_since(seq)
    assert len(burst) == 15 and seq == 20

    levels.end()
    assert levels.level == 0.0
    burst, seq = levels.levels_since(seq)
    assert burst == [] and seq == 20


def test_playback_levels_readers_do_not_starve_each_other(tmp_path: Path) -> None:
    from voxmcp.audio import PlaybackLevels

    clip = tmp_path / "clip.wav"
    _write_tone_wav(clip, loud_seconds=0.2, quiet_seconds=0.0)
    now = [0.0]
    levels = PlaybackLevels(clock=lambda: now[0])
    levels.begin(clip)
    now[0] += 0.1

    first, first_seq = levels.levels_since(0)
    second, second_seq = levels.levels_since(0)
    assert first == second and first_seq == second_seq


def test_playback_levels_sequence_survives_the_next_span(tmp_path: Path) -> None:
    """Streamed TTS plays span after span; the cursor must never rewind."""

    from voxmcp.audio import PlaybackLevels

    clip = tmp_path / "clip.wav"
    _write_tone_wav(clip, loud_seconds=0.1, quiet_seconds=0.0)
    now = [0.0]
    levels = PlaybackLevels(clock=lambda: now[0])

    levels.begin(clip)
    now[0] += 1.0
    _, seq = levels.levels_since(0)
    assert seq == 5

    levels.begin(clip)  # the next span starts where the last one left off
    now[0] += 1.0
    burst, seq = levels.levels_since(seq)
    assert len(burst) == 5 and seq == 10


def test_resolution_leaves_the_snapshot_alone_under_a_live_stream() -> None:
    """Reinitializing PortAudio under an open InputStream would kill the turn."""

    from voxmcp.audio import _PortAudioInUse

    backend = DisconnectingSoundDevice()
    with _PortAudioInUse():
        with pytest.raises(AudioDeviceError, match="Cannot query input device"):
            resolve_input_device(backend)
    assert not backend.refreshed


def test_recorder_opens_mock_stream_at_native_rate_without_microphone() -> None:
    def native_frame(value: float) -> np.ndarray:
        return frame(value, sample_rate=48_000)

    backend = FakeSoundDevice(
        [
            native_frame(0.001),
            native_frame(0.001),
        ]
        + [native_frame(0.2)] * 12  # an utterance, so the close is a real close
        + [
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


def test_live_capture_ends_at_once_when_text_is_delivered() -> None:
    # Typing while the mic is open should not mean waiting the mic out. The
    # capture stops on the delivered text rather than on any audio condition.
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
    control.deliver_text("here is what I typed")
    worker.join(timeout=0.5)

    assert not worker.is_alive()
    assert time.monotonic() - started < 0.5
    assert completed[0].reason is CaptureStopReason.DELIVERED_TEXT
    # The user chose not to speak, so whatever the room said meanwhile is not
    # their turn: no audio survives and nothing is left to transcribe.
    assert completed[0].samples.size == 0
    assert completed[0].latest_wav_path is None
    assert control.delivered_text == "here is what I typed"


def test_delivered_text_is_readable_only_once_set() -> None:
    control = CaptureControl()
    assert control.text_delivered is False
    assert control.delivered_text is None

    control.deliver_text("typed")

    assert control.text_delivered is True
    assert control.delivered_text == "typed"


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


def test_only_recognised_headphones_count_as_isolated_output() -> None:
    # Playback the microphone can hear makes barge-in undecidable, so anything
    # unrecognised has to be assumed shared. Guessing wrong the other way means
    # the agent interrupting itself on its own voice.
    from voxmcp.audio import output_is_isolated

    for name in ("AirPods Pro", "Ali's AirPods", "Sony WH-1000XM5 Headphones",
                 "Beats Studio Buds", "USB Headset", "External Headphones"):
        assert output_is_isolated(name) is True, name

    for name in ("MacBook Pro Speakers", "Studio Display Speakers", "",
                 "BlackHole 2ch", "Soundcore Desk Speaker", "unknown"):
        assert output_is_isolated(name) is False, name


def test_talking_without_pausing_is_never_cut_off_mid_sentence() -> None:
    # A rolling floor read from recent audio will climb into the speaker's own
    # voice if they talk for longer than the window: the quietest tenth stops
    # being the room and becomes them, so the speech stops clearing its own
    # gate and the turn endpoints mid-word. Ali hit this live on a sentence
    # that ran past the 3 s window. During an utterance the floor may only fall.
    state = AdaptiveCaptureState(
        1_000,
        adaptive_config(max_duration_s=60.0),
        lambda _samples, _sr: True,  # WebRTC endorsing every frame
    )

    feed_speech(state, 0.4)  # get past onset
    assert state.phase is CapturePhase.CAPTURING

    # Twelve unbroken seconds — four times the noise window. Real speech varies
    # syllable to syllable even when nobody pauses, which is exactly what tells
    # it apart from a drone.
    rng = np.random.default_rng(11)
    for index in range(600):
        loudness = 0.2 * float(rng.uniform(0.35, 1.0))
        decision = state.feed(frame(loudness))
        assert decision.stop_reason is None, (
            f"cut off after {state.speech_duration_s:.1f}s of continuous speech"
        )

    # And it still ends when the speaker actually stops.
    assert silence_until_stop(state) == pytest.approx(1.6, abs=0.03)


def test_during_an_utterance_the_floor_may_fall_but_never_climb() -> None:
    # The rule that stops a long sentence being cut off, stated directly.
    # Rising into the speaker's own voice is what truncates them; falling is
    # how a room that quietens mid-turn still gets tracked.
    rng = np.random.default_rng(5)
    state = AdaptiveCaptureState(1_000, adaptive_config(), speech_vote)

    for _ in range(60):  # settle on a room around -30 dBFS
        state._observe_room(-30.0)
    settled = state.noise_floor_dbfs
    assert settled == pytest.approx(-30.0, abs=0.5)

    state.phase = CapturePhase.CAPTURING
    for _ in range(300):  # someone talking loudly, without pausing
        state._observe_room(-14.0 + float(rng.uniform(-8.0, 0.0)))
    assert state.noise_floor_dbfs <= settled + 0.01, "floor climbed into the speaker"

    for _ in range(300):  # the room itself drops away underneath them
        state._observe_room(-58.0)
    assert state.noise_floor_dbfs < settled - 10.0


# ---------------------------------------------------------------------------
# The room the capture is actually in — not the one it guessed at startup.
# ---------------------------------------------------------------------------


def room_and_speech(room_dbfs: float, speech_dbfs: float, seed: int = 11):
    """A room at a given level, and a voice a given distance above it.

    ``talk`` leaves the 20 ms inter-word gaps real speech has: the floor is the
    quietest tenth of the recent window, so a voice with no gaps in it is not a
    signal any of this was designed to read.
    """

    rng = np.random.default_rng(seed)
    counter = {"n": 0}

    def room() -> np.ndarray:
        return frame(10 ** ((room_dbfs + rng.uniform(-1.5, 1.5)) / 20.0))

    def talk() -> np.ndarray:
        counter["n"] += 1
        if counter["n"] % 4 == 0:
            return room()
        return frame(10 ** ((speech_dbfs + rng.uniform(-2.0, 2.0)) / 20.0))

    return room, talk


# WebRTC votes speech on ordinary room tone; this is that vote at its worst,
# the same model the cross-room contract above uses.
def always_speech(_samples, _sample_rate):
    return True


def test_the_floor_is_measured_even_when_the_turn_opens_on_speech() -> None:
    """The starting guess must never survive the turn.

    ``initial_noise_floor_dbfs`` is a guess made before the microphone was
    open. The "floor may only fall while capturing" rule exists to stop a
    *measured* floor climbing into the speaker's voice — applied to the guess
    it pinned it instead, for the whole turn. Speech starts at 60 ms and the
    noise window needs 240 ms to fill, so anyone who answered immediately
    locked the guess in: measured in Ali's room, a floor of -60 dBFS against a
    room actually at -47.
    """

    _room, talk = room_and_speech(room_dbfs=-47.0, speech_dbfs=-38.0)
    state = AdaptiveCaptureState(
        1_000,
        config(initial_noise_floor_dbfs=-60.0, trailing_silence_s=1.6, max_duration_s=60.0),
        always_speech,
    )
    # Talking from the very first frame, which is what used to pin it.
    for _ in range(200):
        state.feed(talk())

    assert state.phase is CapturePhase.CAPTURING
    assert state.noise_floor_dbfs > -55.0, (
        f"floor still pinned near the guess: {state.noise_floor_dbfs:.1f}"
    )


def test_a_long_answer_still_closes_in_a_room_louder_than_the_guess() -> None:
    """The reported bug: talk long enough and the turn never ends.

    With the floor pinned below the room, the room's own tone keeps clearing
    the gate, so trailing silence resets forever and only ``max_duration`` or a
    hand on the menu bar can end the turn. Measured live at 44.20 s of speech
    inside a 60.00 s capture that ended on ``max_duration`` with trailing
    silence still reading 0.
    """

    for speech_seconds in (5.0, 20.0, 45.0):
        room, talk = room_and_speech(room_dbfs=-47.0, speech_dbfs=-38.0)
        state = AdaptiveCaptureState(
            1_000,
            config(
                initial_noise_floor_dbfs=-60.0,
                trailing_silence_s=1.6,
                short_trailing_silence_s=0.6,
                max_duration_s=300.0,
            ),
            always_speech,
        )
        for _ in range(round(speech_seconds / 0.02)):
            assert state.feed(talk()).stop_reason is None, speech_seconds

        closed_after = None
        for index in range(round(30.0 / 0.02)):
            if state.feed(room()).stop_reason is not None:
                closed_after = (index + 1) * 0.02
                break

        assert closed_after is not None, f"{speech_seconds}s of speech never closed"
        assert state._stop_reason is CaptureStopReason.TRAILING_SILENCE
        # Ends on its own window, not on the hard cap.
        assert closed_after < 4.0, f"{speech_seconds}s took {closed_after}s to close"


def test_the_room_spread_is_not_learned_from_the_speaker() -> None:
    """Spread means how restless the *room* is, and it sizes the speech gate.

    A window full of speech measures the speaker's dynamic range instead —
    median on the voice, floor on the gaps between words — which inflates the
    required rise and lifts the gate above the very speech being measured.
    """

    rng = np.random.default_rng(5)
    counter = {"n": 0}

    def room() -> np.ndarray:
        return frame(10 ** ((-47.0 + rng.uniform(-1.0, 1.0)) / 20.0))

    def talk() -> np.ndarray:
        # Every fifth frame is a breath — quieter than the room itself, which is
        # what lets the floor keep falling. The floor guard only blocks a
        # *climb*, so on this shape it never fires and the window is read every
        # time; unguarded, the spread it reads is the voice, not the room.
        counter["n"] += 1
        if counter["n"] % 5 == 0:
            return frame(10 ** (-58.0 / 20.0))
        return frame(10 ** ((-28.0 + rng.uniform(-2.0, 2.0)) / 20.0))

    state = AdaptiveCaptureState(
        1_000,
        # Patience well past the run, so this measures the gate rather than
        # racing an endpoint decision.
        config(trailing_silence_s=10.0, max_duration_s=60.0),
        always_speech,
    )
    for _ in range(100):  # read the room first
        state.feed(room())
    settled = state._vad_required_rise_db()
    for _ in range(500):  # then a long stretch of talking
        state.feed(talk())

    # The rise a VAD-endorsed frame must clear is what the gate is made of, so
    # assert on it rather than on the statistic behind it. Read from the room it
    # stays a few dB; read from the speaker it pins to max_vad_margin_db and the
    # gate closes over the voice.
    assert state._vad_required_rise_db() < settled + 4.0, (
        f"required rise climbed from {settled:.1f} dB to "
        f"{state._vad_required_rise_db():.1f} dB on speech "
        f"(spread {state.noise_spread_db:.1f})"
    )
