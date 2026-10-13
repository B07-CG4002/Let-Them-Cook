"""Behavioral tests for the protocol-neutral bounded telemetry queue."""

from __future__ import annotations

import asyncio
import unittest
from queue import Empty

from laptop.bounded_telemetry_queue import BoundedTelemetryQueue


class BoundedTelemetryQueueTests(unittest.IsolatedAsyncioTestCase):
    def test_rejects_non_positive_or_non_integer_capacity(self) -> None:
        for capacity in (0, -1, 1.5, True, "2"):
            with self.subTest(capacity=capacity):
                with self.assertRaises(ValueError):
                    BoundedTelemetryQueue(capacity)

    def test_enqueue_below_capacity_preserves_fifo_without_eviction(self) -> None:
        queue = BoundedTelemetryQueue[str](capacity=3)

        self.assertIsNone(queue.enqueue("first"))
        self.assertIsNone(queue.enqueue("second"))
        self.assertIsNone(queue.enqueue("third"))
        self.assertEqual(queue.size, 3)
        self.assertEqual(queue.eviction_count, 0)
        self.assertEqual(queue.dequeue_nowait(), "first")
        self.assertEqual(queue.dequeue_nowait(), "second")
        self.assertEqual(queue.dequeue_nowait(), "third")

    def test_full_enqueue_evicts_exactly_oldest_and_returns_it(self) -> None:
        queue = BoundedTelemetryQueue[str](capacity=2)
        queue.enqueue("oldest")
        queue.enqueue("newer")

        self.assertEqual(queue.enqueue("newest"), "oldest")
        self.assertEqual(queue.size, 2)
        self.assertEqual(queue.eviction_count, 1)
        self.assertEqual(queue.dequeue_nowait(), "newer")
        self.assertEqual(queue.dequeue_nowait(), "newest")

    def test_repeated_evictions_increment_cumulative_count(self) -> None:
        queue = BoundedTelemetryQueue[int](capacity=1)

        queue.enqueue(10)
        self.assertEqual(queue.enqueue(20), 10)
        self.assertEqual(queue.enqueue(30), 20)
        self.assertEqual(queue.eviction_count, 2)
        self.assertEqual(queue.dequeue_nowait(), 30)

    def test_empty_dequeue_nowait_raises_queue_empty(self) -> None:
        """Catches replacing the empty-queue exception with a sentinel value."""
        queue = BoundedTelemetryQueue[object](capacity=1)

        with self.assertRaises(Empty):
            queue.dequeue_nowait()

    async def test_waiting_consumer_receives_item_enqueued_later(self) -> None:
        queue = BoundedTelemetryQueue[str](capacity=1)
        consumer = asyncio.create_task(queue.dequeue())
        await asyncio.sleep(0)

        self.assertFalse(consumer.done())
        queue.enqueue("later")

        self.assertEqual(await consumer, "later")
        self.assertEqual(queue.size, 0)

    async def test_cancelling_woken_consumer_retains_enqueued_item(self) -> None:
        """Catches handing an item only to a waiter that can be cancelled."""
        queue = BoundedTelemetryQueue[str](capacity=1)
        consumer = asyncio.create_task(queue.dequeue())
        await asyncio.sleep(0)

        self.assertFalse(consumer.done())
        self.assertIsNone(queue.enqueue("retained"))
        consumer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await consumer

        self.assertEqual(queue.dequeue_nowait(), "retained")
        self.assertEqual(queue.eviction_count, 0)


if __name__ == "__main__":
    unittest.main()
