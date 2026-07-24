"""Addressed voice notes — one pending note per agent.

A note is proactive user speech left for a *specific* agent (recognised by its
voice/project), not the reactive answer to a listen. In a shared multi-agent
session the single last-heard slot could not say who a note was for, so whoever
polled first took it. This store addresses each note to one agent so only that
agent surfaces and claims it; notes for different agents coexist.

An empty target means "broadcast" — any agent may claim it, preserving the old
first-come behaviour for callers that do not name an agent.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

BROADCAST = "*"


class NotesStore:
    """Atomic JSON map of ``agent -> pending note`` (one note per agent)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def put(self, agent: str | None, *, transcript: str, turn_id: str, reason: str) -> None:
        text = (transcript or "").strip()
        if not text:
            return
        target = (agent or BROADCAST).strip() or BROADCAST
        payload = self._read()
        payload[target] = {
            "transcript": text,
            "turn_id": turn_id,
            "reason": reason,
            "captured_at": time.time(),
            "target_agent": target,
        }
        self._write(payload)

    def _match_key(self, agent: str | None) -> str | None:
        payload = self._read()
        label = (agent or "").strip()
        if label and label in payload:
            return label
        if BROADCAST in payload:
            return BROADCAST
        return None

    def get(self, agent: str | None, *, max_age_s: float = 86400.0) -> dict[str, Any] | None:
        """The note addressed to ``agent`` (or a broadcast note), if any."""

        key = self._match_key(agent)
        if key is None:
            return None
        note = self._read().get(key)
        if not isinstance(note, dict):
            return None
        if max_age_s > 0 and (time.time() - float(note.get("captured_at", 0))) > max_age_s:
            return None
        return note

    def claim(self, agent: str | None) -> dict[str, Any] | None:
        """Return and remove the note addressed to ``agent`` (or a broadcast note)."""

        key = self._match_key(agent)
        if key is None:
            return None
        payload = self._read()
        note = payload.pop(key, None)
        self._write(payload)
        return note if isinstance(note, dict) else None

    def pending_targets(self, *, max_age_s: float = 86400.0) -> list[str]:
        """Agents (and ``*``) with a live pending note, for the status panel."""

        now = time.time()
        out = []
        for target, note in self._read().items():
            if not isinstance(note, dict):
                continue
            if max_age_s > 0 and (now - float(note.get("captured_at", 0))) > max_age_s:
                continue
            out.append(target)
        return sorted(out)
