"""Shared, dependency-free data models for Vox MCP.

The models in this module deliberately contain no audio, network, or MCP
runtime code.  Keeping the contracts small and serialisable lets the daemon,
the Claude Code adapter, and the Codex adapter evolve independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class SessionState(str, Enum):
    """The complete, externally visible lifecycle of the voice daemon."""

    OFF = "off"
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    PAUSED = "paused"
    ERROR = "error"


class SpokenIntent(str, Enum):
    """Control intents that may be recognized from a whole utterance."""

    NONE = "none"
    STOP = "stop"
    PAUSE = "pause"
    RESUME = "resume"
    REPEAT = "repeat"
    WAIT = "wait"
    END_TURN = "end_turn"


class StopReason(str, Enum):
    """Why a session entered :attr:`SessionState.OFF`."""

    USER_REQUEST = "user_request"
    IDLE_TIMEOUT = "idle_timeout"
    OWNER_DISCONNECTED = "owner_disconnected"
    SHUTDOWN = "shutdown"
    ERROR = "error"


class TurnStatus(str, Enum):
    """Portable result status for a single conversational turn."""

    COMPLETED = "completed"
    NO_SPEECH = "no_speech"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class IntentMatch:
    """Result of conservative spoken-intent classification."""

    intent: SpokenIntent
    normalized_text: str
    matched_phrase: str | None = None
    duration_seconds: float | None = None

    @property
    def matched(self) -> bool:
        return self.intent is not SpokenIntent.NONE


@dataclass(frozen=True, slots=True)
class TurnResult:
    """A host-neutral result from one listen/process/speak cycle.

    ``to_dict`` omits the transcript unless a caller explicitly opts in.  This
    makes the safe representation appropriate for event logs and diagnostics.
    """

    status: TurnStatus
    session_id: str | None = None
    transcript: str | None = None
    recoverable: bool = True
    backend: str | None = None
    timings: Mapping[str, float] = field(default_factory=dict)
    error_code: str | None = None

    def to_dict(self, *, include_transcript: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status.value,
            "session_id": self.session_id,
            "recoverable": self.recoverable,
            "backend": self.backend,
            "timings": {key: float(value) for key, value in self.timings.items()},
            "error_code": self.error_code,
        }
        if self.transcript is not None:
            if include_transcript:
                result["transcript"] = self.transcript
            else:
                result["transcript_redacted"] = True
                result["transcript_char_count"] = len(self.transcript)
                result["transcript_word_count"] = len(self.transcript.split())
        return result


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """Durable state written atomically by the voice state machine."""

    schema_version: int
    state: SessionState
    revision: int
    updated_at: float
    last_activity_at: float
    privacy_enabled: bool
    session_id: str | None = None
    owner_id: str | None = None
    idle_deadline_at: float | None = None
    paused_from: SessionState | None = None
    pause_until: float | None = None
    last_stop_reason: StopReason | None = None
    error: str | None = None
    # Who paused: "pause"/"mute" for a person, "auto:<why>" for the machine.
    pause_reason: str | None = None

    @property
    def microphone_open(self) -> bool:
        """The microphone is permitted to be open only while listening."""

        return self.state is SessionState.LISTENING

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "state": self.state.value,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "last_activity_at": self.last_activity_at,
            "privacy_enabled": self.privacy_enabled,
            "microphone_open": self.microphone_open,
            "session_id": self.session_id,
            "owner_id": self.owner_id,
            "idle_deadline_at": self.idle_deadline_at,
            "paused_from": self.paused_from.value if self.paused_from else None,
            "pause_until": self.pause_until,
            "last_stop_reason": (
                self.last_stop_reason.value if self.last_stop_reason else None
            ),
            "error": self.error,
            "pause_reason": self.pause_reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StateSnapshot":
        """Parse and minimally validate a serialized snapshot."""

        try:
            schema_version = int(value["schema_version"])
            state = SessionState(str(value["state"]))
            revision = int(value["revision"])
            updated_at = float(value["updated_at"])
            last_activity_at = float(value["last_activity_at"])
            privacy_enabled = value["privacy_enabled"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid state snapshot") from exc

        if schema_version != 1:
            raise ValueError(f"unsupported snapshot schema version: {schema_version}")
        if revision < 0:
            raise ValueError("snapshot revision cannot be negative")
        if not isinstance(privacy_enabled, bool):
            raise ValueError("privacy_enabled must be a boolean")

        paused_raw = value.get("paused_from")
        stop_raw = value.get("last_stop_reason")
        return cls(
            schema_version=schema_version,
            state=state,
            revision=revision,
            updated_at=updated_at,
            last_activity_at=last_activity_at,
            privacy_enabled=privacy_enabled,
            session_id=_optional_string(value.get("session_id")),
            owner_id=_optional_string(value.get("owner_id")),
            idle_deadline_at=_optional_float(value.get("idle_deadline_at")),
            paused_from=SessionState(str(paused_raw)) if paused_raw else None,
            pause_until=_optional_float(value.get("pause_until")),
            last_stop_reason=StopReason(str(stop_raw)) if stop_raw else None,
            error=_optional_string(value.get("error")),
            pause_reason=_optional_string(value.get("pause_reason")),
        )


@dataclass(frozen=True, slots=True)
class StateTransition:
    """A compact description of an accepted state transition."""

    from_state: SessionState
    to_state: SessionState
    revision: int
    at: float
    reason: str | None = None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected a string or null")
    return value
