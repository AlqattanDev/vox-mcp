import asyncio

import pytest

from voxmcp.errors import BusyError
from voxmcp.lease import LeaseManager, OperationGate


@pytest.mark.asyncio
async def test_lease_is_immediately_busy_for_other_client():
    lease = LeaseManager()
    assert (await lease.claim("claude"))["claimed"] is True
    other = await lease.claim("codex")
    assert other["claimed"] is False
    assert other["owner_id"] == "claude"


@pytest.mark.asyncio
async def test_handoff_reserves_lease_for_target():
    lease = LeaseManager()
    await lease.claim("claude")
    assert (await lease.handoff("claude", "codex"))["success"] is True
    claimed = await lease.claim("codex")
    assert claimed["claimed"] is True
    assert claimed["previous_owner"] == "claude"


@pytest.mark.asyncio
async def test_operation_gate_rejects_competitor_when_wait_is_disabled():
    gate = OperationGate()
    async with gate.operation("claude", "listen"):
        with pytest.raises(BusyError):
            async with gate.operation("codex", "speak", wait=False):
                pass


async def _record(gate, client_id, action, log, *, hold=0.0, **kwargs):
    """Drive one real gated operation and record when it actually ran."""

    async with gate.operation(client_id, action, **kwargs):
        log.append(("start", client_id, action))
        if hold:
            await asyncio.sleep(hold)
        log.append(("end", client_id, action))


@pytest.mark.asyncio
async def test_two_agents_queue_instead_of_failing():
    gate = OperationGate()
    log: list[tuple[str, str, str]] = []

    first = asyncio.create_task(
        _record(gate, "claude", "converse", log, hold=0.15, agent="infra")
    )
    await asyncio.sleep(0.02)  # Let A take the mic before B arrives.
    second = asyncio.create_task(_record(gate, "claude", "speak", log, agent="mobilescape"))

    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

    # B must not start until A has finished: no overlapping audio, no BusyError.
    assert log == [
        ("start", "claude", "converse"),
        ("end", "claude", "converse"),
        ("start", "claude", "speak"),
        ("end", "claude", "speak"),
    ]


@pytest.mark.asyncio
async def test_same_client_converse_calls_queue():
    """The bug that bit us: an agent's own second converse used to raise."""

    gate = OperationGate()
    log: list[tuple[str, str, str]] = []

    first = asyncio.create_task(_record(gate, "mcp:host:claude-code", "converse", log, hold=0.1))
    await asyncio.sleep(0.02)
    second = asyncio.create_task(_record(gate, "mcp:host:claude-code", "converse", log))

    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    assert [entry[0] for entry in log] == ["start", "end", "start", "end"]


@pytest.mark.asyncio
async def test_queue_runs_in_arrival_order():
    gate = OperationGate()
    log: list[tuple[str, str, str]] = []

    holder = asyncio.create_task(_record(gate, "holder", "converse", log, hold=0.2))
    await asyncio.sleep(0.02)

    waiters = []
    for name in ("first", "second", "third"):
        waiters.append(asyncio.create_task(_record(gate, name, "speak", log)))
        await asyncio.sleep(0.02)  # Establish a distinct arrival order.

    await asyncio.wait_for(asyncio.gather(holder, *waiters), timeout=5)

    started = [client for event, client, _ in log if event == "start"]
    assert started == ["holder", "first", "second", "third"]


@pytest.mark.asyncio
async def test_waiter_times_out_naming_holder_and_duration():
    gate = OperationGate()

    async def hold() -> None:
        async with gate.operation("claude", "converse", agent="infra"):
            await asyncio.sleep(0.5)

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0.02)

    with pytest.raises(BusyError) as excinfo:
        async with gate.operation("codex", "speak", timeout=0.05):
            pass

    message = str(excinfo.value)
    assert "converse" in message  # names what held the mic
    assert "claude" in message  # names the holder
    assert "waited" in message  # names the wait duration

    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder


@pytest.mark.asyncio
async def test_drain_releases_queued_waiters():
    gate = OperationGate()
    started = asyncio.Event()

    async def hold() -> None:
        async with gate.operation("claude", "converse"):
            started.set()
            await asyncio.sleep(5)

    holder = asyncio.create_task(hold())
    await asyncio.wait_for(started.wait(), timeout=1)

    async def queued(name: str) -> None:
        async with gate.operation(name, "speak"):
            pass

    waiters = [asyncio.create_task(queued(name)) for name in ("a", "b")]
    await asyncio.sleep(0.05)
    assert (await gate.status())["queue_depth"] == 2

    assert gate.drain("cancelled") == 2

    # Neither waiter is left hanging.
    for task in waiters:
        with pytest.raises(BusyError):
            await asyncio.wait_for(task, timeout=1)

    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder

    status = await gate.status()
    assert status["queue_depth"] == 0
    assert status["busy"] is False


@pytest.mark.asyncio
async def test_status_reports_active_agent_and_queue():
    gate = OperationGate()
    started = asyncio.Event()

    async def hold() -> None:
        async with gate.operation("claude", "converse", agent="infra"):
            started.set()
            await asyncio.sleep(5)

    holder = asyncio.create_task(hold())
    await asyncio.wait_for(started.wait(), timeout=1)

    waiter = asyncio.create_task(
        _record(gate, "claude", "speak", [], agent="mobilescape")
    )
    await asyncio.sleep(0.05)

    status = await gate.status()
    assert status["busy"] is True
    assert status["agent"] == "infra"
    assert status["queue"][0]["agent"] == "mobilescape"
    assert status["queue"][0]["action"] == "speak"
    assert status["queue"][0]["waiting_s"] >= 0

    gate.drain("stopped")
    with pytest.raises(BusyError):
        await waiter
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_wedge_the_gate():
    gate = OperationGate()
    started = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with gate.operation("claude", "converse"):
            started.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await asyncio.wait_for(started.wait(), timeout=1)

    waiter = asyncio.create_task(_record(gate, "codex", "speak", []))
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    release.set()
    await asyncio.wait_for(holder, timeout=1)

    # A later arrival still gets the mic.
    log: list[tuple[str, str, str]] = []
    await asyncio.wait_for(_record(gate, "late", "speak", log), timeout=1)
    assert log == [("start", "late", "speak"), ("end", "late", "speak")]
    assert (await gate.status())["busy"] is False
