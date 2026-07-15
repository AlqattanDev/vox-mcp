"""Stable per-agent voice assignment persisted in ~/.vox/agents.json.

The voice is the whole signal: when a queued agent finally speaks, the user
must hear which project is talking without being told. That only works if a
label always maps to the same voice, so assignments are hashed (never random,
never insertion-ordered) and written to disk the first time they are made.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Sequence

from .lease import DEFAULT_AGENT

# Used only when the local Kokoro service cannot be reached for its real list.
FALLBACK_VOICES: tuple[str, ...] = (
    "af_sky",
    "af_heart",
    "af_bella",
    "am_adam",
    "am_michael",
    "bf_emma",
    "bm_george",
)

# Kokoro encodes language in the first letter of a voice id: a/b are American
# and British English, while e/f/h/i/j/p/z are Spanish, French, Hindi, Italian,
# Japanese, Portuguese and Mandarin. Auto-assignment must stay in English or an
# agent ends up reading English status updates in a Mandarin voice.
ENGLISH_PREFIXES: tuple[str, ...] = ("af_", "am_", "bf_", "bm_")


def is_english_voice(voice: str) -> bool:
    """True for an English Kokoro voice that is not a superseded v0 variant."""

    return voice.startswith(ENGLISH_PREFIXES) and "_v0" not in voice


class AgentVoices:
    """Maps agent labels to voices, remembering every assignment it makes."""

    def __init__(self, path: Path, *, default_voice: str) -> None:
        self.path = path
        self.default_voice = default_voice
        self._assignments: dict[str, str] | None = None

    @property
    def assignments(self) -> dict[str, str]:
        if self._assignments is None:
            self._assignments = self._load()
        return self._assignments

    def _load(self) -> dict[str, str]:
        try:
            document = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(document, dict):
            return {}
        return {
            str(agent): str(voice)
            for agent, voice in document.items()
            if isinstance(agent, str) and isinstance(voice, str) and voice.strip()
        }

    def _save(self) -> None:
        payload = json.dumps(self.assignments, indent=2, sort_keys=True) + "\n"
        temporary = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(payload)
            os.replace(temporary, self.path)
        except OSError:
            # A voice that cannot be persisted is still better than no voice;
            # it just falls back to the same hash next time.
            pass

    def resolve(self, agent: str, available: Sequence[str] | None = None) -> str:
        """Return the agent's voice, assigning and persisting one if needed."""

        label = (agent or DEFAULT_AGENT).strip().lower()
        if not label or label == DEFAULT_AGENT:
            # Single-agent use must sound exactly as it did before.
            return self.default_voice
        existing = self.assignments.get(label)
        if existing:
            return existing
        assigned = self._assign(label, available)
        self.assignments[label] = assigned
        self._save()
        return assigned

    def _assign(self, label: str, available: Sequence[str] | None) -> str:
        offered = [voice for voice in (available or FALLBACK_VOICES) if isinstance(voice, str)]
        offered = sorted({voice.strip() for voice in offered if voice.strip()})
        # An explicit mapping in agents.json is honoured verbatim; only what we
        # pick on someone's behalf is held to English.
        pool = [voice for voice in offered if is_english_voice(voice)] or offered
        if not pool:
            return self.default_voice
        # sha256, not hash(): Python salts str hashes per process, which would
        # hand an agent a new voice on every restart.
        digest = hashlib.sha256(label.encode("utf-8")).digest()
        start = int.from_bytes(digest[:8], "big") % len(pool)
        taken = set(self.assignments.values()) | {self.default_voice}
        for offset in range(len(pool)):
            candidate = pool[(start + offset) % len(pool)]
            if candidate not in taken:
                return candidate
        return pool[start]  # More agents than voices: collisions are allowed.


__all__ = ["AgentVoices", "FALLBACK_VOICES"]
