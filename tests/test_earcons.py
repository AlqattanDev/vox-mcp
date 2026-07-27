from __future__ import annotations

import wave
from pathlib import Path

from voxmcp.earcons import earcons_enabled, ensure_earcons


def test_earcons_enabled_defaults_on_and_respects_opt_out() -> None:
    assert earcons_enabled({}) is True
    assert earcons_enabled({"VOX_EARCONS": "1"}) is True
    for off in ("0", "false", "no", "off", "OFF"):
        assert earcons_enabled({"VOX_EARCONS": off}) is False


def test_ensure_earcons_synthesizes_playable_wavs_and_caches(tmp_path: Path) -> None:
    start, stop, error = ensure_earcons(tmp_path)
    assert start.is_file() and stop.is_file() and error.is_file()
    for cue in (start, stop, error):
        with wave.open(str(cue), "rb") as handle:
            assert handle.getnchannels() == 1
            assert handle.getsampwidth() == 2
            assert handle.getnframes() > 0

    # Second call is a cache hit: same paths, not rewritten.
    start_mtime = start.stat().st_mtime_ns
    again = ensure_earcons(tmp_path)
    assert again == (start, stop, error)
    assert start.stat().st_mtime_ns == start_mtime


def test_the_three_cues_are_distinguishable_from_each_other(tmp_path: Path) -> None:
    """A cue nobody can tell apart from another cue is not a cue."""

    cues = ensure_earcons(tmp_path)
    payloads = []
    for cue in cues:
        with wave.open(str(cue), "rb") as handle:
            payloads.append(handle.readframes(handle.getnframes()))
    assert len(set(payloads)) == 3
