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

MAX_DELIVERIES = 2  # dispute queue: one attempt and one retry; then the dead-letter queue


@dataclass
class Delivery:
    """One delivery of one message to one worker."""

    dispute_id: UUID
    delivery_count: int  # 1 the first time, 2 on the first retry, ...
    handle: Any = field(default=None, repr=False)  # the adapter's own message object
    max_deliveries: int = MAX_DELIVERIES  # the queue's limit; must match its broker setting

    @property
    def is_last(self) -> bool:
        return self.delivery_count >= self.max_deliveries


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

    def __init__(self, max_deliveries: int = MAX_DELIVERIES) -> None:
        self.max_deliveries = max_deliveries
        self._ready: asyncio.Queue[tuple[UUID, int]] = asyncio.Queue()
        self._locked: dict[UUID, int] = {}  # received and not yet completed or abandoned
        self.dead_letters: list[tuple[UUID, str]] = []

    async def send(self, dispute_id: UUID) -> None:
        await self._ready.put((dispute_id, 1))

    async def receive(self) -> AsyncIterator[Delivery]:
        while True:
            dispute_id, count = await self._ready.get()
            self._locked[dispute_id] = count
            yield Delivery(dispute_id=dispute_id, delivery_count=count, max_deliveries=self.max_deliveries)

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
            await self.abandon(Delivery(dispute_id=dispute_id, delivery_count=count, max_deliveries=self.max_deliveries))

    async def ping(self) -> bool:
        return True


class ServiceBusQueue:
    """Azure Service Bus adapter. Logs in with Entra ID (Workload Identity in the cluster); the
    namespace has connection strings disabled.

    The delivery rules live in the queue's own settings (`make servicebus-create`): a 5-minute
    lock, and at most `max_deliveries` deliveries before the broker itself dead-letters a message.
    So a worker that dies needs no code of ours: its lock expires and the message returns.
    """

    def __init__(self, namespace: str, queue_name: str, credential=None, max_deliveries: int = MAX_DELIVERIES) -> None:
        from azure.identity.aio import DefaultAzureCredential
        from azure.servicebus.aio import ServiceBusClient

        self._credential = credential or DefaultAzureCredential()
        self._client = ServiceBusClient(f"{namespace}.servicebus.windows.net", self._credential)
        self._queue_name = queue_name
        self.max_deliveries = max_deliveries  # must equal the queue's --max-delivery-count
        self._sender = None
        self._receiver = None

    async def _get_sender(self):
        if self._sender is None:
            self._sender = self._client.get_queue_sender(self._queue_name)
        return self._sender

    async def send(self, dispute_id: UUID) -> None:
        from azure.servicebus import ServiceBusMessage
        from azure.servicebus.exceptions import ServiceBusError

        try:
            sender = await self._get_sender()
            # The body is only the ID. message_id lets the broker's own tools trace a dispute.
            await sender.send_messages(ServiceBusMessage(str(dispute_id), message_id=str(dispute_id)))
        except ServiceBusError as exc:
            raise QueueUnavailable(type(exc).__name__) from exc

    async def receive(self) -> AsyncIterator[Delivery]:
        # One receiver for the worker's lifetime: a message must be settled on the receiver that
        # received it. No prefetch, so we never lock messages we are not yet working on.
        # It stays open until close(), not until this loop ends: at shutdown the loop stops first,
        # and the runs still in progress need the receiver to complete their messages.
        self._receiver = self._client.get_queue_receiver(self._queue_name, prefetch_count=0)
        async for message in self._receiver:
            try:
                dispute_id = UUID(str(message))
            except ValueError:
                await self._receiver.dead_letter_message(message, reason="the message is not a dispute ID")
                continue
            # The broker counts deliveries that already failed; this one is the next.
            yield Delivery(dispute_id=dispute_id, delivery_count=(message.delivery_count or 0) + 1, handle=message,
                           max_deliveries=self.max_deliveries)

    async def complete(self, delivery: Delivery) -> None:
        await self._receiver.complete_message(delivery.handle)

    async def abandon(self, delivery: Delivery) -> None:
        # After the last allowed delivery the broker moves the message to the dead-letter queue.
        await self._receiver.abandon_message(delivery.handle)

    async def dead_letter(self, delivery: Delivery, reason: str) -> None:
        await self._receiver.dead_letter_message(delivery.handle, reason=reason[:1024])

    async def dead_letter_ids(self, limit: int = 50) -> list[UUID]:
        """Peek at the dead-letter queue, for operations and tests."""
        from azure.servicebus import ServiceBusSubQueue

        async with self._client.get_queue_receiver(self._queue_name, sub_queue=ServiceBusSubQueue.DEAD_LETTER) as dlq:
            return [UUID(str(m)) for m in await dlq.peek_messages(max_message_count=limit)]

    async def ping(self) -> bool:
        try:
            sender = await self._get_sender()
            await asyncio.wait_for(sender.create_message_batch(), timeout=5)  # opens the link, logs in
            return True
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.close()  # also closes the sender and the receiver
        await self._credential.close()
