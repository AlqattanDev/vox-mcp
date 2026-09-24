"""Stable per-agent voice assignment persisted in ~/.vox/agents.json.

The voice is the whole signal: when a queued agent finally speaks, the user
must hear which project is talking without being told. That only works if a
label always maps to the same voice, so assignments are hashed (never random,
never insertion-ordered) and written to disk the first time they are made.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Sequence

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
VOICE_ID = re.compile(r"^[a-z]{2}_[a-z0-9]+$")


def is_english_voice(voice: str) -> bool:
    """True for an English Kokoro voice that is not a superseded v0 variant."""

    return voice.startswith(ENGLISH_PREFIXES) and "_v0" not in voice


def _voice_id(value: Any) -> str | None:
    """Extract one Kokoro id from the plain or UI-shaped persisted value."""

    if isinstance(value, dict):
        return _voice_id(value.get("id")) or _voice_id(value.get("name"))
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if VOICE_ID.fullmatch(candidate):
        return candidate
    # Some MCP/UI clients persisted the selected object through ``str()`` and
    # left values such as "{'id': 'bm_daniel', 'name': 'bm_daniel'}".  It is
    # still recoverable, but sending that whole literal to Kokoro makes every
    # turn fall through to the OS voice.
    if candidate.startswith("{") and candidate.endswith("}"):
        try:
            decoded = json.loads(candidate)
        except ValueError:
            try:
                decoded = ast.literal_eval(candidate)
            except (SyntaxError, ValueError):
                return None
        return _voice_id(decoded)
    return None


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
        assignments = {
            agent: voice_id
            for agent, voice in document.items()
            if isinstance(agent, str) and (voice_id := _voice_id(voice)) is not None
        }
        # Normalize recoverable object-shaped values immediately. A restart
        # must not load the same poisoned voice again.
        if assignments != document:
            self._write(assignments)
        return assignments

    def _save(self) -> None:
        self._write(self.assignments)

    def _write(self, assignments: dict[str, str]) -> None:
        payload = json.dumps(assignments, indent=2, sort_keys=True) + "\n"
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
        offered = sorted({
            voice_id
            for voice in (available or FALLBACK_VOICES)
            if (voice_id := _voice_id(voice)) is not None
        })
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
