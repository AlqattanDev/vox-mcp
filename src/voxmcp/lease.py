"""In-process client ownership and FIFO-queued audio operation gating."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Callable

from .errors import BusyError

DEFAULT_QUEUE_TIMEOUT_SECONDS = 30.0
DEFAULT_AGENT = "default"


@dataclass(slots=True)
class ClientLease:
    owner_id: str
    acquired_at: float
    heartbeat_at: float
    expires_at: float
    reconnect_until: float | None = None
    reserved_for: str | None = None

    def public(self, *, now: float | None = None) -> dict[str, object]:
        current = time.time() if now is None else now
        return {
            "owned": True,
            "owner_id": self.owner_id,
            "age_seconds": max(0, round(current - self.acquired_at, 3)),
            "expires_in_seconds": max(0, round(self.expires_at - current, 3)),
            "reconnect_grace": (
                max(0, round(self.reconnect_until - current, 3))
                if self.reconnect_until is not None
                else None
            ),
            "reserved_for": self.reserved_for,
        }


class LeaseManager:
    def __init__(
        self,
        *,
        ttl_seconds: float = 600.0,
        reconnect_grace_seconds: float = 30.0,
        clock=time.time,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.reconnect_grace_seconds = reconnect_grace_seconds
        self.clock = clock
        self._lease: ClientLease | None = None
        self._lock = asyncio.Lock()

    async def claim(self, client_id: str, *, force: bool = False) -> dict[str, object]:
        now = self.clock()
        async with self._lock:
            self._expire(now)
            lease = self._lease
            if lease is None:
                self._lease = ClientLease(
                    owner_id=client_id,
                    acquired_at=now,
                    heartbeat_at=now,
                    expires_at=now + self.ttl_seconds,
                )
                return {"claimed": True, **self._lease.public(now=now)}
            if lease.owner_id == client_id:
                lease.heartbeat_at = now
                lease.expires_at = now + self.ttl_seconds
                lease.reconnect_until = None
                return {"claimed": True, "existing": True, **lease.public(now=now)}
            if lease.reserved_for == client_id or force:
                previous = lease.owner_id
                self._lease = ClientLease(
                    owner_id=client_id,
                    acquired_at=now,
                    heartbeat_at=now,
                    expires_at=now + self.ttl_seconds,
                )
                return {"claimed": True, "previous_owner": previous, **self._lease.public(now=now)}
            return {"claimed": False, "busy": True, **lease.public(now=now)}

    async def require(self, client_id: str, *, auto_claim: bool = True) -> None:
        result = await self.claim(client_id) if auto_claim else await self.status()
        if result.get("owner_id") != client_id:
            raise BusyError(f"Audio is owned by {result.get('owner_id', 'another client')}")

    async def heartbeat(self, client_id: str) -> bool:
        now = self.clock()
        async with self._lock:
            self._expire(now)
            if self._lease is None or self._lease.owner_id != client_id:
                return False
            self._lease.heartbeat_at = now
            self._lease.expires_at = now + self.ttl_seconds
            self._lease.reconnect_until = None
            return True

    async def disconnect(self, client_id: str) -> bool:
        now = self.clock()
        async with self._lock:
            if self._lease is None or self._lease.owner_id != client_id:
                return False
            self._lease.reconnect_until = now + self.reconnect_grace_seconds
            self._lease.expires_at = min(
                self._lease.expires_at, self._lease.reconnect_until
            )
            return True

    async def release(self, client_id: str, *, force: bool = False) -> bool:
        async with self._lock:
            if self._lease is None:
                return True
            if not force and self._lease.owner_id != client_id:
                return False
            self._lease = None
            return True

    async def handoff(self, client_id: str, target_id: str) -> dict[str, object]:
        now = self.clock()
        async with self._lock:
            self._expire(now)
            if self._lease is None or self._lease.owner_id != client_id:
                return {"success": False, "reason": "not_owner"}
            self._lease.reserved_for = target_id
            self._lease.expires_at = min(
                self._lease.expires_at, now + self.reconnect_grace_seconds
            )
            return {
                "success": True,
                "from": client_id,
                "reserved_for": target_id,
                "expires_in_seconds": self.reconnect_grace_seconds,
            }

    async def status(self) -> dict[str, object]:
        now = self.clock()
        async with self._lock:
            self._expire(now)
            return self._lease.public(now=now) if self._lease else {"owned": False}

    def _expire(self, now: float) -> None:
        if self._lease is not None and self._lease.expires_at <= now:
            self._lease = None


@dataclass(slots=True)
class _ActiveOperation:
    client_id: str
    action: str
    agent: str
    started_at: float


@dataclass(slots=True)
class _Waiter:
    client_id: str
    action: str
    agent: str
    enqueued_at: float
    future: "asyncio.Future[None]" = field(repr=False)


class OperationGate:
    """Serialises audio operations, queueing competitors in FIFO order.

    Only one operation may own the microphone and speakers at a time, but a
    competitor waits its turn instead of failing.  Rejecting outright meant an
    agent's own second ``converse`` raced its first and lost the user's reply.

    Every critical section below is synchronous, so the event loop cannot
    interleave two of them; that is what keeps the queue consistent without a
    lock, and it lets the timeout callback mutate the queue safely.
    """

    def __init__(
        self,
        *,
        timeout: float | None = DEFAULT_QUEUE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[..., None] | None = None,
    ) -> None:
        self.default_timeout = timeout
        self.on_event = on_event
        self._clock = clock
        self._active: _ActiveOperation | None = None
        self._waiters: deque[_Waiter] = deque()

    @asynccontextmanager
    async def operation(
        self,
        client_id: str,
        action: str,
        *,
        agent: str = DEFAULT_AGENT,
        wait: bool = True,
        timeout: float | None = ...,  # type: ignore[assignment]
    ) -> AsyncIterator[None]:
        if timeout is ...:
            timeout = self.default_timeout
        await self._acquire(client_id, action, agent, wait, timeout)
        try:
            yield
        finally:
            self._release()

    async def _acquire(
        self,
        client_id: str,
        action: str,
        agent: str,
        wait: bool,
        timeout: float | None,
    ) -> None:
        now = self._clock()
        # Barge in only when nobody is queued, so arrival order is preserved.
        if self._active is None and not self._waiters:
            self._active = _ActiveOperation(client_id, action, agent, now)
            return
        if not wait:
            raise BusyError(self._busy_message())
        loop = asyncio.get_running_loop()
        waiter = _Waiter(
            client_id=client_id,
            action=action,
            agent=agent,
            enqueued_at=now,
            future=loop.create_future(),
        )
        self._waiters.append(waiter)
        self._emit("queue.enter", waiter, depth=len(self._waiters))
        timer = (
            loop.call_later(timeout, self._expire, waiter, timeout)
            if timeout is not None and timeout > 0
            else None
        )
        try:
            await waiter.future
        except asyncio.CancelledError:
            # The baton may have arrived in the same tick the caller went away.
            # Handing it straight on is what keeps the gate from wedging.
            if self._holds_baton(waiter):
                self._release()
            else:
                self._discard(waiter)
            raise
        finally:
            if timer is not None:
                timer.cancel()
        self._emit("queue.exit", waiter, waited_s=round(self._clock() - waiter.enqueued_at, 3))

    def _holds_baton(self, waiter: _Waiter) -> bool:
        future = waiter.future
        return future.done() and not future.cancelled() and future.exception() is None

    def _release(self) -> None:
        self._active = None
        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.future.done():
                continue  # Already timed out, drained, or cancelled.
            self._active = _ActiveOperation(
                waiter.client_id, waiter.action, waiter.agent, self._clock()
            )
            waiter.future.set_result(None)
            return

    def _discard(self, waiter: _Waiter) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            pass

    def _expire(self, waiter: _Waiter, timeout: float) -> None:
        if waiter.future.done():
            return
        self._discard(waiter)
        waited = self._clock() - waiter.enqueued_at
        waiter.future.set_exception(
            BusyError(
                f"{waiter.action} waited {waited:.1f}s for the microphone but "
                f"{self._busy_message()}; giving up after {timeout:g}s"
            )
        )
        self._emit("queue.timeout", waiter, waited_s=round(waited, 3))

    def drain(self, reason: str = "cancelled") -> int:
        """Fail every queued waiter so a cancel or stop never orphans one."""

        drained = 0
        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.future.done():
                continue
            waiter.future.set_exception(
                BusyError(f"queued {waiter.action} was dropped because voice was {reason}")
            )
            self._emit(
                "queue.drained",
                waiter,
                reason=reason,
                waited_s=round(self._clock() - waiter.enqueued_at, 3),
            )
            drained += 1
        return drained

    def _busy_message(self) -> str:
        active = self._active
        if active is None:
            return "audio is already running"
        return f"{active.action or 'audio'} is already running for {active.client_id}"

    def _emit(self, event: str, waiter: _Waiter, **data: Any) -> None:
        callback = self.on_event
        if callback is None:
            return
        try:
            callback(
                event,
                client_id=waiter.client_id,
                agent=waiter.agent,
                action=waiter.action,
                **data,
            )
        except Exception:
            pass  # Observability must never break a voice turn.

    async def status(self) -> dict[str, object]:
        now = self._clock()
        active = self._active
        return {
            "busy": active is not None,
            "client_id": active.client_id if active else None,
            "action": active.action if active else None,
            "agent": active.agent if active else None,
            "queue_depth": len(self._waiters),
            "queue": [
                {
                    "agent": waiter.agent,
                    "client_id": waiter.client_id,
                    "action": waiter.action,
                    "waiting_s": round(now - waiter.enqueued_at, 3),
                }
                for waiter in self._waiters
            ],
        }

