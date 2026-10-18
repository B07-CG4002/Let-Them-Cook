"""Read-only board monitor: per-glove counters and recent results for one laptop view.

The service counts its inputs and results per glove here at all times: plain
integers updated on the event-loop thread, with no I/O. Events (published
results, refusals, subscriber changes) are kept only while a monitor connection
is attached, in a bounded deque. That connection is send-only after its MONITOR
frame; its own task builds, encodes and writes one MONITOR_UPDATE every
MONITOR_INTERVAL seconds. The monitor never touches the phone subscriber, its
result queue or the observer.
"""
import asyncio
from collections import deque
import time

from common.wire import MAX_FRAME_SIZE, ProtocolError, encode_frame, write_frame
from ultra96.protocol import MAX_MONITOR_EVENTS, MONITOR_INTERVAL

DEVICE_COUNTERS = ("accepted", "duplicates", "results", "commands", "refused", "sent", "unsent")
SERVICE_COUNTERS = ("rejected", "client_limit", "idle_evicted", "subscribers", "replaced",
                    "model_samples", "model_errors", "model_slow_calls", "model_results_late")
EVENT_CAPACITY = 4096
WRITE_TIMEOUT = 2.0
# At most this many updates per tick, back to back while more events are pending.
FRAMES_PER_TICK = 4


def _label(name):
    """The model name as 1..128 printable characters, as the update schema requires."""
    text = "".join(character if character.isprintable() else "?" for character in str(name)[:128])
    return text or "?"


class _Attachment:
    """The attached monitor connection and the events pending for it."""
    def __init__(self, writer):
        self.writer = writer
        self.events = deque(maxlen=EVENT_CAPACITY)
        self.dropped = 0
        self.replaced = False


class ServiceMonitor:
    """Loop thread only. Counting is O(1) and ignores a device_id other than 1 or 2."""

    def __init__(self):
        self.started = time.monotonic()
        self.devices = {device: dict.fromkeys(DEVICE_COUNTERS, 0) for device in (1, 2)}
        self._attached = None

    def count(self, device_id, key):
        counters = self.devices.get(device_id)
        if counters is not None:
            counters[key] += 1

    def published(self, result):
        """A model result, or a command echo (request_id set), about to be published."""
        command = result["request_id"] is not None
        self.count(result["device_id"], "commands" if command else "results")
        if self._attached is not None:
            self._push(("command" if command else "result", result["device_id"],
                        result["boot_id"], result["seq"], result["gesture"],
                        result["confidence"], time.monotonic()))

    def refused(self, device_id, boot_id, seq, reason):
        self.count(device_id, "refused")
        if self._attached is not None:
            self._push(("refused", device_id, boot_id, seq, reason, time.monotonic()))

    def subscriber(self, change, subscriber_id, reason=None):
        if self._attached is not None:
            self._push(("subscriber", change, subscriber_id, reason, time.monotonic()))

    def _push(self, event):
        attached = self._attached
        if len(attached.events) == EVENT_CAPACITY:
            attached.dropped += 1  # the deque pushes its oldest event out
        attached.events.append(event)

    def _uptime(self, at):
        return round(max(0.0, at - self.started), 3)

    async def serve(self, reader, writer, session_id, status):
        """Run one monitor connection after its valid MONITOR frame.

        status() returns the service-wide fields: model, subscriber, identities,
        identity_limit and metrics. A newer monitor replaces this one.
        """
        attached = _Attachment(writer)
        previous, self._attached = self._attached, attached
        if previous is not None:
            previous.replaced = True
            previous.writer.close()
            # Python 3.10's TLS close waits for a close_notify a vanished peer never
            # sends; abort so the replaced connection frees its slot at once.
            previous.writer.transport.abort()
        sender = watch = None
        try:
            await write_frame(writer, {"v": 1, "type": "MONITORING", "session_id": session_id},
                              timeout=WRITE_TIMEOUT)
            sender = asyncio.create_task(self._send_updates(attached, session_id, status))
            # Send-only, like the subscriber: EOF or any inbound byte ends the session.
            watch = asyncio.create_task(reader.read(1))
            finished, _ = await asyncio.wait((sender, watch), return_when=asyncio.FIRST_COMPLETED)
            # A clean end of the watch ends the session even if a write failed meanwhile.
            (watch if watch in finished else sender).result()
        except Exception:
            # A replaced monitor's writer was closed under it; that is not a peer error.
            if not attached.replaced:
                raise
        finally:
            if self._attached is attached:
                self._attached = None
            for task in (sender, watch):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(task for task in (sender, watch) if task is not None),
                                 return_exceptions=True)

    async def _send_updates(self, attached, session_id, status):
        writer = attached.writer
        loop = asyncio.get_running_loop()
        number = 0
        deadline = loop.time()
        backlog_since = None
        # Ends once this connection is no longer the attached one: replaced, or its session
        # ended. On Python 3.10 a cancellation that lands in wait_for just after the drain
        # finished can be swallowed, so the loop must not rely on cancellation alone.
        while self._attached is attached:
            for _ in range(FRAMES_PER_TICK):
                # Never queue updates behind a slow reader; skip the tick instead.
                if writer.transport.get_write_buffer_size() > MAX_FRAME_SIZE:
                    # drain() waits only above the transport's high-water mark (32768),
                    # so a peer that stops reading below it would never time out there.
                    # A buffer that stays above this mark for WRITE_TIMEOUT is a slow write.
                    if backlog_since is None:
                        backlog_since = loop.time()
                    elif loop.time() - backlog_since > WRITE_TIMEOUT:
                        raise asyncio.TimeoutError("monitor peer stopped reading")
                    break
                backlog_since = None
                writer.write(self._update(attached, number, session_id, status()))
                number += 1
                await asyncio.wait_for(writer.drain(), timeout=WRITE_TIMEOUT)
                if not attached.events:
                    break
            deadline = max(deadline + MONITOR_INTERVAL, loop.time())
            await asyncio.sleep(deadline - loop.time())

    def _update(self, attached, number, session_id, state):
        """Encode one update; events that do not fit in the frame wait for the next one."""
        metrics = state["metrics"]
        service = {key: metrics.get(key, 0) for key in SERVICE_COUNTERS}
        service["model_max_call_ms"] = metrics.get("model_max_call_ms", 0.0)
        message = {"v": 1, "type": "MONITOR_UPDATE", "session_id": session_id, "update": number,
                   "uptime_seconds": self._uptime(time.monotonic()),
                   "model": _label(state["model"]), "subscriber": state["subscriber"],
                   "identities": state["identities"], "identity_limit": state["identity_limit"],
                   "devices": {str(device): dict(counters)
                               for device, counters in self.devices.items()},
                   "service": service, "events": [], "events_dropped": attached.dropped}
        pending = attached.events
        taken = [pending.popleft() for _ in range(min(len(pending), MAX_MONITOR_EVENTS))]
        events = [list(event[:-1]) + [self._uptime(event[-1])] for event in taken]
        count = len(events)
        while True:
            message["events"] = events[:count]
            try:
                frame = encode_frame(message)
                break
            except ProtocolError:
                if not count:
                    raise
                count //= 2
        # Nothing was appended meanwhile, so the deque has room for these again.
        pending.extendleft(reversed(taken[count:]))
        return frame
