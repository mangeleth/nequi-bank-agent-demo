"""The dispute queue: a buffer between accepting a dispute and working on it (ADR-0018).

A message carries only the dispute ID. Everything else (who the customer is, what they asked)
is read from PostgreSQL when the work starts, so the queue holds no customer data and no token.

How a queue copes with a worker that dies:
    a worker RECEIVES a message   -> the queue hides it from other workers (a lock)
    the worker COMPLETES it       -> the message is gone
    the worker ABANDONS it        -> the message is offered again; its delivery count goes up
    the worker dies               -> the lock expires and the message is offered again
    too many deliveries           -> the message moves to the DEAD-LETTER queue, for a person

`DisputeQueue` is the port. `InMemoryQueue` is for tests and single-process local runs; the
cluster uses Azure Service Bus.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

MAX_DELIVERIES = 2  # one attempt and one retry; then the dead-letter queue


@dataclass
class Delivery:
    """One delivery of one message to one worker."""

    dispute_id: UUID
    delivery_count: int  # 1 the first time, 2 on the retry
    handle: Any = field(default=None, repr=False)  # the adapter's own message object

    @property
    def is_last(self) -> bool:
        return self.delivery_count >= MAX_DELIVERIES


class QueueUnavailable(Exception):
    """The queue cannot be reached."""


class DisputeQueue(Protocol):
    async def send(self, dispute_id: UUID) -> None: ...

    def receive(self) -> AsyncIterator[Delivery]:
        """Deliveries, one at a time, for as long as the caller keeps iterating."""

    async def complete(self, delivery: Delivery) -> None:
        """The work is done: remove the message."""

    async def abandon(self, delivery: Delivery) -> None:
        """The work failed: offer the message again (or dead-letter it after the last delivery)."""

    async def dead_letter(self, delivery: Delivery, reason: str) -> None:
        """Stop retrying: move the message aside for a person."""

    async def ping(self) -> bool: ...


class InMemoryQueue:
    """Single-process queue with the same delivery rules as the real one."""

    def __init__(self) -> None:
        self._ready: asyncio.Queue[tuple[UUID, int]] = asyncio.Queue()
        self._locked: dict[UUID, int] = {}  # received and not yet completed or abandoned
        self.dead_letters: list[tuple[UUID, str]] = []

    async def send(self, dispute_id: UUID) -> None:
        await self._ready.put((dispute_id, 1))

    async def receive(self) -> AsyncIterator[Delivery]:
        while True:
            dispute_id, count = await self._ready.get()
            self._locked[dispute_id] = count
            yield Delivery(dispute_id=dispute_id, delivery_count=count)

    async def complete(self, delivery: Delivery) -> None:
        self._locked.pop(delivery.dispute_id, None)

    async def abandon(self, delivery: Delivery) -> None:
        self._locked.pop(delivery.dispute_id, None)
        if delivery.is_last:
            self.dead_letters.append((delivery.dispute_id, "delivery limit reached"))
        else:
            await self._ready.put((delivery.dispute_id, delivery.delivery_count + 1))

    async def dead_letter(self, delivery: Delivery, reason: str) -> None:
        self._locked.pop(delivery.dispute_id, None)
        self.dead_letters.append((delivery.dispute_id, reason))

    async def expire_locks(self) -> None:
        """What the real queue does when a worker dies holding messages: offer them again."""
        for dispute_id, count in list(self._locked.items()):
            await self.abandon(Delivery(dispute_id=dispute_id, delivery_count=count))

    async def ping(self) -> bool:
        return True
