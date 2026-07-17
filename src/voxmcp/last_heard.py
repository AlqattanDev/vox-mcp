"""Durable last-heard transcript so host cancel cannot erase finished STT."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class LastHeard:
    transcript: str
    reason: str
    session_id: str | None
    client_id: str | None
    agent: str | None
    turn_id: str
    captured_at: float
    delivered: bool
    char_count: int
    word_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public(self, *, now: float | None = None, include_transcript: bool = False) -> dict[str, Any]:
        current = time.time() if now is None else now
        payload: dict[str, Any] = {
            "present": True,
            "delivered": self.delivered,
            "age_s": max(0, round(current - self.captured_at, 3)),
            "char_count": self.char_count,
            "word_count": self.word_count,
            "reason": self.reason,
            "session_id": self.session_id,
            "client_id": self.client_id,
            "agent": self.agent,
            "turn_id": self.turn_id,
            "captured_at": self.captured_at,
        }
        if include_transcript:
            payload["transcript"] = self.transcript
        return payload


class LastHeardStore:
    """Atomic JSON store for the most recent completed listen transcript."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def write(
        self,
        *,
        transcript: str,
        reason: str,
        session_id: str | None,
        client_id: str | None,
        agent: str | None,
        turn_id: str,
        delivered: bool = False,
    ) -> LastHeard:
        text = (transcript or "").strip()
        record = LastHeard(
            transcript=text,
            reason=reason,
            session_id=session_id,
            client_id=client_id,
            agent=agent,
            turn_id=turn_id,
            captured_at=time.time(),
            delivered=delivered,
            char_count=len(text),
            word_count=len(text.split()) if text else 0,
        )
        self._atomic_write(record.to_dict())
        return record

    def mark_delivered(self, turn_id: str | None = None) -> LastHeard | None:
        current = self.read()
        if current is None:
            return None
        if turn_id is not None and current.turn_id != turn_id:
            return current
        if current.delivered:
            return current
        current.delivered = True
        self._atomic_write(current.to_dict())
        return current

    def read(self) -> LastHeard | None:
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not payload.get("transcript"):
            return None
        try:
            return LastHeard(
                transcript=str(payload["transcript"]),
                reason=str(payload.get("reason") or "unknown"),
                session_id=payload.get("session_id"),
                client_id=payload.get("client_id"),
                agent=payload.get("agent"),
                turn_id=str(payload.get("turn_id") or ""),
                captured_at=float(payload.get("captured_at") or 0),
                delivered=bool(payload.get("delivered")),
                char_count=int(payload.get("char_count") or 0),
                word_count=int(payload.get("word_count") or 0),
            )
        except (TypeError, ValueError):
            return None

    def undelivered(self, *, max_age_s: float = 86400.0) -> LastHeard | None:
        current = self.read()
        if current is None or current.delivered:
            return None
        if max_age_s > 0 and (time.time() - current.captured_at) > max_age_s:
            return None
        return current

    def claim(self) -> LastHeard | None:
        """Return undelivered transcript and mark it delivered (one-shot recovery)."""

        current = self.undelivered()
        if current is None:
            return None
        self.mark_delivered(current.turn_id)
        current.delivered = True
        return current

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
