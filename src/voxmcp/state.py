"""Explicit, durable voice-session state machine."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping

from .models import SessionState, StateSnapshot, StopReason


SNAPSHOT_SCHEMA_VERSION = 1


LEGAL_TRANSITIONS: Mapping[SessionState, frozenset[SessionState]] = {
    SessionState.OFF: frozenset({SessionState.IDLE, SessionState.ERROR}),
    SessionState.IDLE: frozenset(
        {
            SessionState.OFF,
            SessionState.LISTENING,
            SessionState.SPEAKING,
            SessionState.PAUSED,
            SessionState.ERROR,
        }
    ),
    SessionState.LISTENING: frozenset(
        {
            SessionState.IDLE,
            SessionState.PROCESSING,
            SessionState.PAUSED,
            SessionState.OFF,
            SessionState.ERROR,
        }
    ),
    SessionState.PROCESSING: frozenset(
        {
            SessionState.IDLE,
            SessionState.SPEAKING,
            SessionState.PAUSED,
            SessionState.OFF,
            SessionState.ERROR,
        }
    ),
    SessionState.SPEAKING: frozenset(
        {
            SessionState.IDLE,
            SessionState.LISTENING,
            SessionState.PAUSED,
            SessionState.OFF,
            SessionState.ERROR,
        }
    ),
    SessionState.PAUSED: frozenset(
        {
            SessionState.IDLE,
            SessionState.LISTENING,
            SessionState.PROCESSING,
            SessionState.SPEAKING,
            SessionState.OFF,
            SessionState.ERROR,
        }
    ),
    SessionState.ERROR: frozenset({SessionState.IDLE, SessionState.OFF}),
}


class StateMachineError(RuntimeError):
    """Base class for state-machine errors."""


class IllegalStateTransition(StateMachineError):
    """Raised when a caller asks for a transition outside the legal graph."""


class SessionOwnershipError(StateMachineError):
    """Raised when a different MCP client tries to mutate an owned session."""


class SnapshotError(StateMachineError):
    """Raised when durable state cannot be parsed or written atomically."""


class VoiceStateMachine:
    """Thread-safe state machine with an optional atomic JSON snapshot.

    The state itself is immutable.  Each accepted operation builds a candidate
    snapshot, writes it atomically, and only then publishes it in memory.  A
    failed disk write therefore cannot leave memory ahead of durable state.
    """

    def __init__(
        self,
        *,
        snapshot_path: str | Path | None = None,
        idle_timeout_seconds: float = 300.0,
        privacy_enabled: bool = True,
        clock: Callable[[], float] = time.time,
        restore: bool = True,
    ) -> None:
        if idle_timeout_seconds < 0:
            raise ValueError("idle_timeout_seconds cannot be negative")
        if not isinstance(privacy_enabled, bool):
            raise TypeError("privacy_enabled must be a boolean")

        self._path = Path(snapshot_path) if snapshot_path is not None else None
        self._idle_timeout = float(idle_timeout_seconds)
        self._clock = clock
        self._lock = threading.RLock()

        now = float(self._clock())
        self._snapshot = StateSnapshot(
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            state=SessionState.OFF,
            revision=0,
            updated_at=now,
            last_activity_at=now,
            privacy_enabled=privacy_enabled,
        )

        if restore and self._path is not None and self._path.exists():
            restored = read_snapshot(self._path)
            self._snapshot = self._safe_restored_snapshot(restored, now)
            if self._snapshot != restored:
                self._write_snapshot(self._snapshot)
        elif self._path is not None:
            self._write_snapshot(self._snapshot)

    @property
    def state(self) -> SessionState:
        return self._snapshot.state

    @property
    def microphone_open(self) -> bool:
        return self._snapshot.microphone_open

    def snapshot(self) -> StateSnapshot:
        with self._lock:
            return self._snapshot

    def start(self, client_id: str | None = None) -> StateSnapshot:
        """Start a session or idempotently refresh its idle lease."""

        with self._lock:
            if self.state is SessionState.IDLE:
                self._assert_owner(client_id)
                return self._heartbeat_locked(client_id)
            if self.state is not SessionState.OFF:
                raise IllegalStateTransition(
                    f"cannot start while state is {self.state.value}"
                )
            return self._transition_locked(
                SessionState.IDLE,
                owner_id=client_id,
                session_id=uuid.uuid4().hex,
            )

    def transition(
        self,
        target: SessionState,
        *,
        client_id: str | None = None,
        error: str | None = None,
    ) -> StateSnapshot:
        with self._lock:
            self._assert_owner(client_id)
            return self._transition_locked(target, error=error)

    def begin_listening(self, *, client_id: str | None = None) -> StateSnapshot:
        """Open the microphone.  ``SPEAKING -> LISTENING`` is barge-in."""

        return self.transition(SessionState.LISTENING, client_id=client_id)

    def begin_processing(self, *, client_id: str | None = None) -> StateSnapshot:
        return self.transition(SessionState.PROCESSING, client_id=client_id)

    utterance_complete = begin_processing

    def begin_speaking(self, *, client_id: str | None = None) -> StateSnapshot:
        return self.transition(SessionState.SPEAKING, client_id=client_id)

    def complete_turn(self, *, client_id: str | None = None) -> StateSnapshot:
        with self._lock:
            self._assert_owner(client_id)
            if self.state not in {SessionState.PROCESSING, SessionState.SPEAKING}:
                raise IllegalStateTransition(
                    f"cannot complete a turn while state is {self.state.value}"
                )
            return self._transition_locked(SessionState.IDLE)

    def cancel_turn(self, *, client_id: str | None = None) -> StateSnapshot:
        """Cancel only the active turn; the voice session remains alive."""

        with self._lock:
            self._assert_owner(client_id)
            if self.state is SessionState.IDLE:
                return self._heartbeat_locked(client_id)
            if self.state not in {
                SessionState.LISTENING,
                SessionState.PROCESSING,
                SessionState.SPEAKING,
                SessionState.PAUSED,
            }:
                raise IllegalStateTransition(
                    f"cannot cancel a turn while state is {self.state.value}"
                )
            return self._transition_locked(SessionState.IDLE)

    def pause(
        self,
        reason: str | None = None,
        *,
        until: float | None = None,
        client_id: str | None = None,
    ) -> StateSnapshot:
        with self._lock:
            self._assert_owner(client_id)
            if self.state is SessionState.PAUSED:
                return self._update_pause_locked(until)
            if self.state not in {
                SessionState.IDLE,
                SessionState.LISTENING,
                SessionState.PROCESSING,
                SessionState.SPEAKING,
            }:
                raise IllegalStateTransition(
                    f"cannot pause while state is {self.state.value}"
                )
            return self._transition_locked(
                SessionState.PAUSED,
                paused_from=self.state,
                pause_until=until,
                error=_bounded_text(reason),
            )

    def resume(self, *, client_id: str | None = None) -> StateSnapshot:
        """Resume to a safe idle state without opening the microphone."""

        with self._lock:
            self._assert_owner(client_id)
            if self.state is not SessionState.PAUSED:
                raise IllegalStateTransition(
                    f"cannot resume while state is {self.state.value}"
                )
            return self._transition_locked(SessionState.IDLE)

    def resume_if_due(self, *, now: float | None = None) -> bool:
        with self._lock:
            deadline = self._snapshot.pause_until
            current = float(self._clock() if now is None else now)
            if self.state is not SessionState.PAUSED or deadline is None or current < deadline:
                return False
            # Timed resume follows the same privacy rule as manual resume: it
            # returns to IDLE and never reopens the microphone automatically.
            self._transition_locked(SessionState.IDLE, now=current)
            return True

    def stop(
        self,
        reason: StopReason = StopReason.USER_REQUEST,
        *,
        client_id: str | None = None,
    ) -> StateSnapshot:
        with self._lock:
            self._assert_owner(client_id)
            if self.state is SessionState.OFF:
                return self._snapshot
            return self._transition_locked(SessionState.OFF, stop_reason=reason)

    def mark_error(
        self, message: str, *, client_id: str | None = None
    ) -> StateSnapshot:
        with self._lock:
            self._assert_owner(client_id)
            if self.state is SessionState.ERROR:
                candidate = replace(
                    self._snapshot,
                    revision=self._snapshot.revision + 1,
                    updated_at=float(self._clock()),
                    error=_bounded_text(message),
                )
                return self._commit_locked(candidate)
            return self._transition_locked(
                SessionState.ERROR, error=_bounded_text(message)
            )

    def recover(self, *, client_id: str | None = None) -> StateSnapshot:
        with self._lock:
            self._assert_owner(client_id)
            if self.state is not SessionState.ERROR:
                raise IllegalStateTransition(
                    f"cannot recover while state is {self.state.value}"
                )
            target = SessionState.IDLE if self._snapshot.session_id else SessionState.OFF
            return self._transition_locked(target)

    def set_privacy(self, enabled: bool) -> StateSnapshot:
        if not isinstance(enabled, bool):
            raise TypeError("privacy flag must be a boolean")
        with self._lock:
            if self._snapshot.privacy_enabled is enabled:
                return self._snapshot
            now = float(self._clock())
            candidate = replace(
                self._snapshot,
                privacy_enabled=enabled,
                revision=self._snapshot.revision + 1,
                updated_at=now,
            )
            return self._commit_locked(candidate)

    def heartbeat(self, *, client_id: str | None = None) -> StateSnapshot:
        with self._lock:
            self._assert_owner(client_id)
            if self.state is SessionState.OFF:
                raise IllegalStateTransition("cannot heartbeat an off session")
            return self._heartbeat_locked(client_id)

    def expire_if_idle(self, *, now: float | None = None) -> bool:
        """Stop an IDLE session whose deadline has elapsed."""

        with self._lock:
            current = float(self._clock() if now is None else now)
            deadline = self._snapshot.idle_deadline_at
            if self.state is not SessionState.IDLE or deadline is None or current < deadline:
                return False
            self._transition_locked(
                SessionState.OFF,
                stop_reason=StopReason.IDLE_TIMEOUT,
                now=current,
            )
            return True

    expire_lease = expire_if_idle

    def _assert_owner(self, client_id: str | None) -> None:
        # Shared session: owner_id is last_actor for diagnostics only.
        # Any client may drive transitions; the OperationGate serializes audio.
        del client_id

    def _heartbeat_locked(self, client_id: str | None) -> StateSnapshot:
        now = float(self._clock())
        deadline = (
            now + self._idle_timeout
            if self.state is SessionState.IDLE and self._idle_timeout > 0
            else self._snapshot.idle_deadline_at
        )
        candidate = replace(
            self._snapshot,
            # last actor for events/status; never used to exclude a client
            owner_id=client_id if client_id is not None else self._snapshot.owner_id,
            revision=self._snapshot.revision + 1,
            updated_at=now,
            last_activity_at=now,
            idle_deadline_at=deadline,
        )
        return self._commit_locked(candidate)

    def _update_pause_locked(self, until: float | None) -> StateSnapshot:
        now = float(self._clock())
        candidate = replace(
            self._snapshot,
            revision=self._snapshot.revision + 1,
            updated_at=now,
            last_activity_at=now,
            pause_until=until,
        )
        return self._commit_locked(candidate)

    def _transition_locked(
        self,
        target: SessionState,
        *,
        owner_id: str | None = None,
        session_id: str | None = None,
        paused_from: SessionState | None = None,
        pause_until: float | None = None,
        stop_reason: StopReason | None = None,
        error: str | None = None,
        now: float | None = None,
    ) -> StateSnapshot:
        source = self.state
        if target is source:
            raise IllegalStateTransition(f"session is already {target.value}")
        if target not in LEGAL_TRANSITIONS[source]:
            raise IllegalStateTransition(
                f"illegal voice state transition: {source.value} -> {target.value}"
            )

        current = float(self._clock() if now is None else now)
        next_session_id = session_id if session_id is not None else self._snapshot.session_id
        next_owner_id = owner_id if owner_id is not None else self._snapshot.owner_id
        if source is SessionState.OFF and target is SessionState.IDLE:
            next_session_id = next_session_id or uuid.uuid4().hex
        if target is SessionState.OFF:
            next_session_id = None
            next_owner_id = None

        idle_deadline = (
            current + self._idle_timeout
            if target is SessionState.IDLE and self._idle_timeout > 0
            else None
        )
        candidate = StateSnapshot(
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            state=target,
            revision=self._snapshot.revision + 1,
            updated_at=current,
            last_activity_at=current,
            privacy_enabled=self._snapshot.privacy_enabled,
            session_id=next_session_id,
            owner_id=next_owner_id,
            idle_deadline_at=idle_deadline,
            paused_from=paused_from if target is SessionState.PAUSED else None,
            pause_until=pause_until if target is SessionState.PAUSED else None,
            last_stop_reason=stop_reason if target is SessionState.OFF else None,
            error=error if target is SessionState.ERROR else None,
        )
        return self._commit_locked(candidate)

    def _commit_locked(self, candidate: StateSnapshot) -> StateSnapshot:
        self._write_snapshot(candidate)
        self._snapshot = candidate
        return candidate

    def _write_snapshot(self, snapshot: StateSnapshot) -> None:
        if self._path is None:
            return
        try:
            _atomic_write_json(self._path, snapshot.to_dict())
        except OSError as exc:
            raise SnapshotError(f"failed to write state snapshot: {exc}") from exc

    def _safe_restored_snapshot(
        self, restored: StateSnapshot, now: float
    ) -> StateSnapshot:
        # Audio operations cannot survive a daemon restart.  Fail closed rather
        # than restoring a stale LISTENING snapshot that claims the mic is open.
        if restored.state in {
            SessionState.LISTENING,
            SessionState.PROCESSING,
            SessionState.SPEAKING,
        }:
            return replace(
                restored,
                state=SessionState.ERROR,
                revision=restored.revision + 1,
                updated_at=now,
                last_activity_at=now,
                idle_deadline_at=None,
                paused_from=None,
                pause_until=None,
                error="session interrupted by daemon restart",
            )
        if (
            restored.state is SessionState.IDLE
            and restored.idle_deadline_at is not None
            and now >= restored.idle_deadline_at
        ):
            return replace(
                restored,
                state=SessionState.OFF,
                revision=restored.revision + 1,
                updated_at=now,
                last_activity_at=now,
                session_id=None,
                owner_id=None,
                idle_deadline_at=None,
                last_stop_reason=StopReason.IDLE_TIMEOUT,
            )
        return restored


# The lifecycle-oriented name reads naturally in MCP integration code.
SessionStateMachine = VoiceStateMachine


def read_snapshot(path: str | Path) -> StateSnapshot:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError("snapshot root must be an object")
        return StateSnapshot.from_dict(value)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SnapshotError(f"failed to read state snapshot: {exc}") from exc


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        directory_fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def _bounded_text(value: str | None, limit: int = 512) -> str | None:
    if value is None:
        return None
    return str(value).replace("\x00", "")[:limit]
