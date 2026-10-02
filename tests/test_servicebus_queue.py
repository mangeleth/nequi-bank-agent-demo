"""The Service Bus adapter against the real service. Runs only when TEST_SERVICEBUS_NAMESPACE is
set (`make test-servicebus`), on a separate queue so it never touches real disputes.

These tests check the broker's own behaviour that the worker relies on: redelivery after an
abandon, the delivery count, and dead-lettering after the last delivery.
"""

import asyncio
import os
from uuid import uuid4

import pytest

from services.supervisor.queue import MAX_DELIVERIES, ServiceBusQueue

NAMESPACE = os.environ.get("TEST_SERVICEBUS_NAMESPACE", "")
QUEUE = os.environ.get("TEST_SERVICEBUS_QUEUE", "disputes-test")

pytestmark = pytest.mark.skipif(not NAMESPACE, reason="needs Azure Service Bus: run `make test-servicebus`")


async def next_delivery(deliveries, wanted):
    """The next delivery for our message (other test runs may have left messages behind)."""
    while True:
        delivery = await asyncio.wait_for(deliveries.__anext__(), timeout=60)
        if delivery.dispute_id == wanted:
            return delivery


async def test_send_receive_complete():
    queue, dispute_id = ServiceBusQueue(NAMESPACE, QUEUE), uuid4()
    try:
        assert await queue.ping()
        await queue.send(dispute_id)
        deliveries = queue.receive()
        delivery = await next_delivery(deliveries, dispute_id)
        assert delivery.delivery_count == 1 and not delivery.is_last
        await queue.complete(delivery)
        await deliveries.aclose()
    finally:
        await queue.close()


async def test_abandon_redelivers_then_the_broker_dead_letters():
    queue, dispute_id = ServiceBusQueue(NAMESPACE, QUEUE), uuid4()
    try:
        await queue.send(dispute_id)
        deliveries = queue.receive()

        first = await next_delivery(deliveries, dispute_id)
        await queue.abandon(first)
        second = await next_delivery(deliveries, dispute_id)
        assert (first.delivery_count, second.delivery_count) == (1, 2)
        assert second.is_last and second.delivery_count == MAX_DELIVERIES

        await queue.abandon(second)  # the last allowed delivery failed
        await deliveries.aclose()
        await asyncio.sleep(2)
        assert dispute_id in await queue.dead_letter_ids()  # moved aside by the broker, not by our code
    finally:
        await queue.close()


async def test_explicit_dead_letter():
    queue, dispute_id = ServiceBusQueue(NAMESPACE, QUEUE), uuid4()
    try:
        await queue.send(dispute_id)
        deliveries = queue.receive()
        delivery = await next_delivery(deliveries, dispute_id)
        await queue.dead_letter(delivery, "the run failed on its last delivery")
        await deliveries.aclose()
        await asyncio.sleep(2)
        assert dispute_id in await queue.dead_letter_ids()
    finally:
        await queue.close()
