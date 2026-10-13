"""Protocol-neutral bounded storage for opaque telemetry items.

This queue is local groundwork only.  It does not prove BLE, Ultra96, or
end-to-end behavior and intentionally contains no transport, codec, timing,
sensor, session, freshness, or protocol concepts.
"""

from __future__ import annotations

import asyncio
from collections import deque
from queue import Empty
from typing import Deque, Generic, Optional, TypeVar


ItemT = TypeVar("ItemT")


class BoundedTelemetryQueue(Generic[ItemT]):
    """A bounded FIFO queue that evicts its oldest item when full."""

    def __init__(self, capacity: int) -> None:
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        self._capacity = capacity
        self._items: Deque[ItemT] = deque()
        self._item_available = asyncio.Event()
        self._eviction_count = 0

    @property
    def capacity(self) -> int:
        """Return the fixed maximum number of queued items."""
        return self._capacity

    @property
    def size(self) -> int:
        """Return the number of items currently queued."""
        return len(self._items)

    def qsize(self) -> int:
        """Return the number of items currently queued."""
        return self.size

    @property
    def eviction_count(self) -> int:
        """Return the cumulative number of items evicted due to capacity."""
        return self._eviction_count

    def enqueue(self, item: ItemT) -> Optional[ItemT]:
        """Enqueue an item without blocking, returning any evicted item."""
        evicted: Optional[ItemT] = None
        if len(self._items) >= self._capacity:
            evicted = self._items.popleft()
            self._eviction_count += 1
        self._items.append(item)
        self._item_available.set()
        return evicted

    async def dequeue(self) -> ItemT:
        """Remove and return the oldest item, waiting asynchronously if empty."""
        while not self._items:
            await self._item_available.wait()
        item = self._items.popleft()
        if not self._items:
            self._item_available.clear()
        return item

    def dequeue_nowait(self) -> ItemT:
        """Remove and return the oldest item, raising ``queue.Empty`` if empty."""
        try:
            item = self._items.popleft()
        except IndexError:
            raise Empty from None
        if not self._items:
            self._item_available.clear()
        return item
