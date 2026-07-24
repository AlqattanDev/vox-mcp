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
    start, stop = ensure_earcons(tmp_path)
    assert start.is_file() and stop.is_file()
    for cue in (start, stop):
        with wave.open(str(cue), "rb") as handle:
            assert handle.getnchannels() == 1
            assert handle.getsampwidth() == 2
            assert handle.getnframes() > 0

    # Second call is a cache hit: same paths, not rewritten.
    start_mtime = start.stat().st_mtime_ns
    again_start, again_stop = ensure_earcons(tmp_path)
    assert (again_start, again_stop) == (start, stop)
    assert start.stat().st_mtime_ns == start_mtime
