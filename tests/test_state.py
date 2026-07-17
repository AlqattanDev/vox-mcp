from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from voxmcp.models import SessionState, StopReason
from voxmcp.state import (
    IllegalStateTransition,

    VoiceStateMachine,
    read_snapshot,
)


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_complete_turn_lifecycle_and_atomic_snapshot(tmp_path: Path) -> None:
    clock = FakeClock()
    path = tmp_path / "state.json"
    machine = VoiceStateMachine(snapshot_path=path, clock=clock)

    assert machine.state is SessionState.OFF
    started = machine.start("codex-client")
    session_id = started.session_id
    assert started.state is SessionState.IDLE
    assert started.microphone_open is False

    listening = machine.begin_listening(client_id="codex-client")
    assert listening.microphone_open is True
    assert json.loads(path.read_text())["microphone_open"] is True

    assert machine.utterance_complete(client_id="codex-client").state is SessionState.PROCESSING
    assert machine.begin_speaking(client_id="codex-client").state is SessionState.SPEAKING
    completed = machine.complete_turn(client_id="codex-client")
    assert completed.state is SessionState.IDLE
    assert completed.session_id == session_id

    stopped = machine.stop(client_id="codex-client")
    assert stopped.state is SessionState.OFF
    assert stopped.session_id is None
    assert stopped.owner_id is None
    assert read_snapshot(path) == stopped
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_illegal_transitions_are_rejected(tmp_path: Path) -> None:
    machine = VoiceStateMachine(snapshot_path=tmp_path / "state.json")

    with pytest.raises(IllegalStateTransition):
        machine.begin_listening()
    machine.start()
    with pytest.raises(IllegalStateTransition):
        machine.begin_processing()
    with pytest.raises(IllegalStateTransition):
        machine.transition(SessionState.IDLE)


def test_cancel_turn_never_ends_session(tmp_path: Path) -> None:
    machine = VoiceStateMachine(snapshot_path=tmp_path / "state.json")
    original = machine.start("owner")
    machine.begin_listening(client_id="owner")

    cancelled = machine.cancel_turn(client_id="owner")

    assert cancelled.state is SessionState.IDLE
    assert cancelled.session_id == original.session_id
    assert cancelled.owner_id == "owner"
    assert cancelled.microphone_open is False


def test_manual_resume_is_privacy_safe_idle(tmp_path: Path) -> None:
    machine = VoiceStateMachine(snapshot_path=tmp_path / "state.json")
    machine.start()
    machine.begin_listening()
    paused = machine.pause("user requested pause")
    assert paused.state is SessionState.PAUSED
    assert paused.paused_from is SessionState.LISTENING
    assert paused.microphone_open is False

    resumed = machine.resume()
    assert resumed.state is SessionState.IDLE
    assert resumed.microphone_open is False


def test_timed_wait_resumes_idle_without_reopening_the_microphone(tmp_path: Path) -> None:
    clock = FakeClock()
    machine = VoiceStateMachine(snapshot_path=tmp_path / "state.json", clock=clock)
    machine.start()
    machine.begin_listening()
    machine.pause(until=clock.now + 10)

    clock.advance(9)
    assert machine.resume_if_due() is False
    clock.advance(1)
    assert machine.resume_if_due() is True
    assert machine.state is SessionState.IDLE
    assert machine.microphone_open is False


def test_idle_expiry_only_applies_in_idle(tmp_path: Path) -> None:
    clock = FakeClock()
    machine = VoiceStateMachine(
        snapshot_path=tmp_path / "state.json",
        idle_timeout_seconds=30,
        clock=clock,
    )
    machine.start("owner")
    clock.advance(29)
    assert machine.expire_if_idle() is False

    machine.heartbeat(client_id="owner")
    clock.advance(20)
    machine.begin_listening(client_id="owner")
    clock.advance(100)
    assert machine.expire_if_idle() is False
    assert machine.state is SessionState.LISTENING

    machine.cancel_turn(client_id="owner")
    clock.advance(30)
    assert machine.expire_lease() is True
    assert machine.state is SessionState.OFF
    assert machine.snapshot().last_stop_reason is StopReason.IDLE_TIMEOUT


def test_shared_session_allows_any_client_to_drive_turns(tmp_path: Path) -> None:
    """Shared session: owner_id is last_actor only; other hosts may use the mic."""

    machine = VoiceStateMachine(snapshot_path=tmp_path / "state.json")
    machine.start("claude-session")

    machine.begin_listening(client_id="codex-session")
    assert machine.state is SessionState.LISTENING
    machine.cancel_turn(client_id="codex-session")
    assert machine.state is SessionState.IDLE


def test_transient_crash_snapshot_restores_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    machine = VoiceStateMachine(snapshot_path=path)
    machine.start("owner")
    machine.begin_listening(client_id="owner")
    assert read_snapshot(path).state is SessionState.LISTENING

    restored = VoiceStateMachine(snapshot_path=path)

    assert restored.state is SessionState.ERROR
    assert restored.microphone_open is False
    assert restored.snapshot().error == "session interrupted by daemon restart"


def test_privacy_flag_is_durable(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    machine = VoiceStateMachine(snapshot_path=path, privacy_enabled=True)
    assert machine.set_privacy(False).privacy_enabled is False
    assert read_snapshot(path).privacy_enabled is False
