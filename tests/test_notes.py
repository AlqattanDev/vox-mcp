"""The notes store — a queue per agent, and what it refuses to lose."""

from __future__ import annotations

import json
import time
from pathlib import Path

from voxmcp.notes import MAX_PER_AGENT, NotesStore


def store(tmp_path: Path) -> NotesStore:
    return NotesStore(tmp_path / "notes.json")


def test_a_second_note_does_not_overwrite_the_first(tmp_path: Path):
    notes = store(tmp_path)
    notes.put("mobilescape", transcript="first", turn_id="a", reason="key")
    notes.put("mobilescape", transcript="second", turn_id="b", reason="key")

    waiting = notes.get("mobilescape")
    assert waiting is not None
    assert waiting["transcript"] == "first\nsecond"
    assert waiting["count"] == 2

    claimed = notes.claim("mobilescape")
    assert claimed is not None
    assert claimed["transcript"] == "first\nsecond"
    assert notes.get("mobilescape") is None


def test_the_age_reported_is_of_the_oldest_note(tmp_path: Path):
    # How long the user has been waiting to be heard, not when they last added
    # to it — the whole point of surfacing an age at all.
    notes = store(tmp_path)
    notes.put("mobilescape", transcript="first", turn_id="a", reason="key")
    oldest = notes.get("mobilescape")["captured_at"]
    time.sleep(0.01)
    notes.put("mobilescape", transcript="second", turn_id="b", reason="key")
    assert notes.get("mobilescape")["captured_at"] == oldest


def test_agents_do_not_share_a_queue(tmp_path: Path):
    notes = store(tmp_path)
    notes.put("mobilescape", transcript="for you", turn_id="a", reason="key")
    notes.put("infra", transcript="for them", turn_id="b", reason="key")

    assert notes.claim("mobilescape")["transcript"] == "for you"
    assert notes.get("mobilescape") is None
    assert notes.get("infra")["transcript"] == "for them"


def test_a_stale_note_is_neither_shown_nor_claimed(tmp_path: Path):
    notes = store(tmp_path)
    notes.put("mobilescape", transcript="from last week", turn_id="a", reason="key")
    assert notes.get("mobilescape", max_age_s=0.0001) is None
    assert notes.claim("mobilescape", max_age_s=0.0001) is None
    assert notes.pending_targets(max_age_s=0.0001) == []


def test_a_broadcast_note_is_the_fallback_not_the_first_choice(tmp_path: Path):
    notes = store(tmp_path)
    notes.put(None, transcript="anyone", turn_id="a", reason="key")
    notes.put("mobilescape", transcript="specifically you", turn_id="b", reason="key")

    # Addressed wins while it exists; the broadcast is still there afterwards.
    assert notes.claim("mobilescape")["transcript"] == "specifically you"
    assert notes.claim("mobilescape")["transcript"] == "anyone"
    assert notes.claim("mobilescape") is None


def test_the_queue_has_a_ceiling_and_drops_the_oldest(tmp_path: Path):
    notes = store(tmp_path)
    for index in range(MAX_PER_AGENT + 5):
        notes.put("mobilescape", transcript=f"n{index}", turn_id=str(index), reason="key")
    claimed = notes.claim("mobilescape")
    assert claimed["count"] == MAX_PER_AGENT
    lines = claimed["transcript"].splitlines()
    assert lines[0] == "n5"
    assert lines[-1] == f"n{MAX_PER_AGENT + 4}"


def test_a_file_written_before_queues_is_read_not_discarded(tmp_path: Path):
    # There is a real `~/.vox/state/notes.json` in the one-note-per-agent shape
    # on any machine that ran an earlier build. An upgrade must not eat it.
    path = tmp_path / "notes.json"
    path.write_text(
        json.dumps(
            {
                "vox": {
                    "captured_at": time.time(),
                    "reason": "trailing_silence",
                    "target_agent": "vox",
                    "transcript": "Yeah.",
                    "turn_id": "f0c1c4df",
                }
            }
        )
    )
    notes = NotesStore(path)
    assert notes.pending_targets() == ["vox"]
    assert notes.get("vox")["transcript"] == "Yeah."

    # And it keeps working as a queue from there.
    notes.put("vox", transcript="and this too", turn_id="b", reason="key")
    assert notes.claim("vox")["transcript"] == "Yeah.\nand this too"


def test_an_empty_transcript_is_not_a_note(tmp_path: Path):
    notes = store(tmp_path)
    notes.put("mobilescape", transcript="   ", turn_id="a", reason="key")
    assert notes.get("mobilescape") is None
    assert notes.pending_targets() == []
