"""Addressed voice notes — a queue per agent.

A note is proactive user speech left for a *specific* agent (recognised by its
voice/project), not the reactive answer to a listen. In a shared multi-agent
session the single last-heard slot could not say who a note was for, so whoever
polled first took it. This store addresses each note to one agent so only that
agent surfaces and claims it; notes for different agents coexist.

Each agent holds a **queue**, not a slot. It used to hold one note and the next
one overwrote it, so saying two things to an agent that was busy lost the first
without a word — the user had spoken and the machine had thrown it away. Claim
hands over everything that is waiting, oldest first, because two sentences said
in a row are one thought.

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

DEFAULT_MAX_AGE_S = 86400.0

# A queue that grows without bound is a store of speech nobody will ever hear.
# Twenty is far past any real backlog; past it the oldest goes.
MAX_PER_AGENT = 20


class NotesStore:
    """Atomic JSON map of ``agent -> pending notes``, oldest first."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, list[dict[str, Any]]]:
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        queues: dict[str, list[dict[str, Any]]] = {}
        for target, value in payload.items():
            # A file written before notes became queues holds one note per
            # agent. Reading it as a queue of one is what stops an upgrade
            # from silently dropping whatever was already waiting.
            if isinstance(value, dict):
                queues[target] = [value]
            elif isinstance(value, list):
                queues[target] = [note for note in value if isinstance(note, dict)]
        return queues

    def _write(self, payload: dict[str, list[dict[str, Any]]]) -> None:
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
        queue = payload.setdefault(target, [])
        queue.append(
            {
                "transcript": text,
                "turn_id": turn_id,
                "reason": reason,
                "captured_at": time.time(),
                "target_agent": target,
            }
        )
        if len(queue) > MAX_PER_AGENT:
            del queue[: len(queue) - MAX_PER_AGENT]
        self._write(payload)

    @staticmethod
    def _live(
        queue: list[dict[str, Any]], max_age_s: float
    ) -> list[dict[str, Any]]:
        if max_age_s <= 0:
            return list(queue)
        now = time.time()
        return [
            note
            for note in queue
            if (now - float(note.get("captured_at", 0))) <= max_age_s
        ]

    @staticmethod
    def _merge(target: str, notes: list[dict[str, Any]]) -> dict[str, Any]:
        """Everything waiting for one agent, as one note.

        The timestamp is the *oldest* of them, because what the caller wants to
        know is how long the user has been waiting to be heard, not when they
        last added to it.
        """

        return {
            "transcript": "\n".join(str(note.get("transcript", "")) for note in notes),
            "turn_id": str(notes[-1].get("turn_id", "")),
            "reason": str(notes[-1].get("reason", "")),
            "captured_at": float(notes[0].get("captured_at", 0)),
            "target_agent": target,
            "count": len(notes),
        }

    def _match_key(self, agent: str | None, max_age_s: float) -> str | None:
        payload = self._read()
        label = (agent or "").strip()
        if label and self._live(payload.get(label, []), max_age_s):
            return label
        if self._live(payload.get(BROADCAST, []), max_age_s):
            return BROADCAST
        return None

    def get(
        self, agent: str | None, *, max_age_s: float = DEFAULT_MAX_AGE_S
    ) -> dict[str, Any] | None:
        """Everything waiting for ``agent`` (or broadcast), as one note."""

        key = self._match_key(agent, max_age_s)
        if key is None:
            return None
        live = self._live(self._read().get(key, []), max_age_s)
        return self._merge(key, live) if live else None

    def claim(
        self, agent: str | None, *, max_age_s: float = DEFAULT_MAX_AGE_S
    ) -> dict[str, Any] | None:
        """Return and remove everything waiting for ``agent`` (or broadcast).

        Stale notes are dropped rather than delivered. `get` has always hidden
        them, so handing one over on claim meant the panel said nothing was
        waiting and the agent was then told something from yesterday.
        """

        key = self._match_key(agent, max_age_s)
        if key is None:
            return None
        payload = self._read()
        live = self._live(payload.pop(key, []), max_age_s)
        self._write(payload)
        return self._merge(key, live) if live else None

    def pending_targets(self, *, max_age_s: float = DEFAULT_MAX_AGE_S) -> list[str]:
        """Agents (and ``*``) with a live pending note, for the status panel."""

        return sorted(
            target
            for target, queue in self._read().items()
            if self._live(queue, max_age_s)
        )
