"""What `vox barge-in calibrate` recommends, and in which direction.

This had no coverage at all, which is how a recommendation that pointed the
wrong way shipped: it tuned the *ceiling* of the required rise, so a small gap
produced a small number, and lowering the ceiling loosens the gate. Following
the tool's own advice on speakers would have made Kokoro interrupt itself more
readily than the defaults did.
"""

from typing import Any

import pytest

from voxmcp import cli as cli_module
from voxmcp.audio import LevelMeasurement


def measurement(*, median: float, p90: float, peak: float) -> LevelMeasurement:
    return LevelMeasurement(
        device="Fake Output",
        frames=300,
        median_dbfs=median,
        p90_dbfs=p90,
        peak_dbfs=peak,
    )


class RecordingDependencies:
    """Just enough of _Dependencies to run calibration without hardware."""

    def __init__(self) -> None:
        self.spoken: list[str] = []

    def caller(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.spoken.append(str(arguments.get("message", "")))
        return {"status": "completed"}


def calibrate(monkeypatch, *, bleed: LevelMeasurement, voice: LevelMeasurement) -> dict:
    windows = iter([bleed, voice])
    monkeypatch.setattr(cli_module, "_measure_input", lambda _seconds: next(windows))
    monkeypatch.delenv("VOX_BARGE_IN_DUCK_VOLUME", raising=False)
    return cli_module._calibrate_barge_in(
        RecordingDependencies(),  # type: ignore[arg-type]
        seconds=1.0,
        safety_db=6.0,
        countdown=0.0,
    )


def test_headphones_recommend_the_floor_of_the_required_rise(monkeypatch) -> None:
    # Ali's real FreeClip numbers: bleed p90 -63.3, voice median -37.2.
    result = calibrate(
        monkeypatch,
        bleed=measurement(median=-91.5, p90=-63.3, peak=-22.3),
        voice=measurement(median=-37.2, p90=-22.1, peak=-16.0),
    )

    assert result["usable"] is True
    assert result["gap_db"] == pytest.approx(27.6, abs=0.3)
    # VAD_MARGIN_DB is the floor of clamp(k * spread, floor, ceiling) and the
    # only value that can *guarantee* a separation rather than merely permit one.
    assert result["recommended"]["VOX_BARGE_IN_VAD_MARGIN_DB"] == pytest.approx(
        21.6, abs=0.3
    )
    # The ceiling is only lifted so it cannot sit below the floor; CaptureConfig
    # rejects vad_margin_db > max_vad_margin_db and would kill barge-in mid-turn.
    ceiling = result["recommended"]["VOX_BARGE_IN_MAX_VAD_MARGIN_DB"]
    assert ceiling >= result["recommended"]["VOX_BARGE_IN_VAD_MARGIN_DB"]
    assert ceiling >= 24.0
    assert "vox set" in result["apply"]
    # launchctl setenv provably never reaches the runtime under launchd.
    assert "launchctl" not in result["apply"]


def test_speakers_recommend_nothing_because_no_setting_invents_a_gap(monkeypatch) -> None:
    # This MacBook's speakers: the user's voice sits *below* his own echo, so
    # the honest output is "this cannot work", not a smaller number to try.
    result = calibrate(
        monkeypatch,
        bleed=measurement(median=-46.0, p90=-22.0, peak=-17.6),
        voice=measurement(median=-46.0, p90=-33.0, peak=-29.8),
    )

    assert result["usable"] is False
    assert result["gap_db"] < 0
    assert result["recommended"] is None
    assert result["apply"] is None
    assert "headphones" in result["verdict"].lower()


def test_a_hopeless_gap_never_loosens_the_gate(monkeypatch) -> None:
    # The regression this file exists for. The old formula was
    # max(6.0, gap - safety) applied to MAX_VAD_MARGIN_DB, so the worse the
    # hardware the *lower* the ceiling it suggested — 6.0 here, versus a default
    # of 24.0 — which loosens the very gate that was already failing.
    result = calibrate(
        monkeypatch,
        bleed=measurement(median=-40.0, p90=-20.0, peak=-15.0),
        voice=measurement(median=-38.0, p90=-25.0, peak=-20.0),
    )

    assert result["usable"] is False
    assert result["recommended"] is None
