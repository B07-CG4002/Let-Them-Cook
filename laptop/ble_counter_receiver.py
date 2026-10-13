"""Receive and validate Gate B's diagnostic BLE counter notifications."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Optional

from bleak import BleakClient, BleakScanner
from laptop.ble_connection import make_ble_client


EXPECTED_SERVICE_UUID = "6e1c0001-7a45-4dc4-b678-3f2d5a9c1001"
NOTIFY_CHARACTERISTIC_UUID = "6e1c0002-7a45-4dc4-b678-3f2d5a9c1001"
COUNTER_BYTES = 4
UINT32_MASK = 0xFFFFFFFF
HALF_UINT32_RANGE = 0x80000000
MAX_RECONNECT_DELAY_SECONDS = 10.0
# A duration-bound run may spend at most this shared grace after its deadline
# quiescing a subscribed link and attempting disconnect cleanup.
TOTAL_CLEANUP_GRACE_SECONDS = 1.0
MINIMUM_DISCONNECT_ATTEMPT_SECONDS = 0.25

LOGGER = logging.getLogger(__name__)


def decode_counter(payload: bytes) -> int:
    """Decode the diagnostic payload's one little-endian uint32 value."""
    if len(payload) != COUNTER_BYTES:
        raise ValueError(f"expected {COUNTER_BYTES}-byte counter payload, got {len(payload)}")
    return int.from_bytes(payload, byteorder="little", signed=False)


def advertises_expected_service(service_uuids: Optional[Iterable[str]]) -> bool:
    """Return whether the advertisement includes the exact required service."""
    return any(uuid.lower() == EXPECTED_SERVICE_UUID for uuid in service_uuids or ())


@dataclass(frozen=True)
class CounterObservation:
    kind: str
    counter: Optional[int]
    missing: int = 0


class CounterTracker:
    """Track the counter stream, resetting ordering state for each link."""

    def __init__(self) -> None:
        self.received_count = 0
        self.gap_count = 0
        self.duplicate_count = 0
        self.out_of_order_count = 0
        self.malformed_count = 0
        self.queue_drop_count = 0
        self.last_counter: Optional[int] = None

    def start_connection(self) -> None:
        """Make the next valid value the baseline for this connection."""
        self.last_counter = None

    def observe(self, payload: bytes) -> CounterObservation:
        """Decode and classify one queued payload outside the BLE callback."""
        try:
            counter = decode_counter(payload)
        except ValueError:
            self.malformed_count += 1
            return CounterObservation(kind="malformed", counter=None)

        self.received_count += 1
        if self.last_counter is None:
            self.last_counter = counter
            return CounterObservation(kind="baseline", counter=counter)

        delta = (counter - self.last_counter) & UINT32_MASK
        if delta == 1:
            self.last_counter = counter
            return CounterObservation(kind="sequential", counter=counter)
        if delta == 0:
            self.duplicate_count += 1
            return CounterObservation(kind="duplicate", counter=counter)
        if delta < HALF_UINT32_RANGE:
            self.gap_count += delta - 1
            self.last_counter = counter
            return CounterObservation(kind="gap", counter=counter, missing=delta - 1)

        self.out_of_order_count += 1
        return CounterObservation(kind="out_of_order", counter=counter)


class NotificationInbox:
    """A bounded transport inbox; the worker accounts for callback drops."""

    def __init__(self, maxsize: int) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=maxsize)
        self._pending_drops = 0

    def put_from_callback(self, payload: bytes) -> bool:
        """Copy and enqueue only; no counter decoding, classification, or logging."""
        try:
            self._queue.put_nowait(bytes(payload))
        except asyncio.QueueFull:
            self._pending_drops += 1
            return False
        return True

    async def get(self) -> bytes:
        return await self._queue.get()

    def get_nowait(self) -> bytes:
        return self._queue.get_nowait()

    def take_drop_count(self) -> int:
        pending = self._pending_drops
        self._pending_drops = 0
        return pending


@dataclass(frozen=True)
class StopCriteria:
    target_received: Optional[int] = None
    duration_seconds: Optional[float] = None
    require_reconnect: bool = False

    def __post_init__(self) -> None:
        if self.target_received is None and self.duration_seconds is None:
            raise ValueError("provide a target received count or an elapsed duration")
        if self.target_received is not None and self.target_received < 1:
            raise ValueError("target received count must be positive")
        if self.duration_seconds is not None and self.duration_seconds <= 0:
            raise ValueError("elapsed duration must be positive")

    def reached(self, received_count: int, elapsed_seconds: float) -> bool:
        return (
            self.target_received is not None and received_count >= self.target_received
        ) or (
            self.duration_seconds is not None and elapsed_seconds >= self.duration_seconds
        )

    def should_stop(
        self, received_count: int, elapsed_seconds: float, reconnect_count: int
    ) -> bool:
        """Stop at the duration deadline, or at a satisfied counter target."""
        if self.duration_seconds is not None and elapsed_seconds >= self.duration_seconds:
            return True
        return (
            self.target_received is not None
            and received_count >= self.target_received
            and (not self.require_reconnect or reconnect_count > 0)
        )


@dataclass(frozen=True)
class RunSummary:
    received_count: int
    gap_count: int
    duplicate_count: int
    out_of_order_count: int
    malformed_count: int
    queue_drop_count: int
    reconnect_count: int
    elapsed_seconds: float
    observations: dict[str, list[float]] = field(default_factory=dict)
    cleanup_error_count: int = 0

    def exit_code(self, criteria: StopCriteria) -> int:
        criteria_failed = not criteria.reached(self.received_count, self.elapsed_seconds)
        reconnect_missing = criteria.require_reconnect and self.reconnect_count == 0
        anomalies = any(
            (
                self.gap_count,
                self.duplicate_count,
                self.out_of_order_count,
                self.malformed_count,
                self.queue_drop_count,
                self.cleanup_error_count,
            )
        )
        return 1 if criteria_failed or reconnect_missing or anomalies else 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


class RequiredGattMissing(RuntimeError):
    """The connected peripheral does not expose Gate B's diagnostic GATT API."""


class BleCounterReceiver:
    """Reconnectable Bleak receiver with a small, non-blocking callback."""

    def __init__(
        self,
        *,
        scan_timeout_seconds: float = 5.0,
        reconnect_delay_seconds: float = 1.0,
        queue_size: int = 128,
        scanner: type[BleakScanner] = BleakScanner,
        client_factory: Callable[..., BleakClient] = make_ble_client,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue size must be positive")
        self._scan_timeout_seconds = scan_timeout_seconds
        self._reconnect_delay_seconds = min(
            max(reconnect_delay_seconds, 0.0), MAX_RECONNECT_DELAY_SECONDS
        )
        self._scanner = scanner
        self._client_factory = client_factory
        self._queue_size = queue_size
        self._tracker = CounterTracker()
        self._disconnect_event = asyncio.Event()
        self._disconnect_callback_at: Optional[float] = None
        self._pending_reconnect_started_at: Optional[float] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connection_generation = 0
        self._active_connection_generation: Optional[int] = None
        self._reconnect_count = 0
        self._cleanup_error_count = 0
        self._detached_cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._observations: dict[str, list[float]] = {}

    async def run(self, criteria: StopCriteria) -> RunSummary:
        """Run until the requested counter or elapsed-time criterion is met."""
        self._loop = asyncio.get_running_loop()
        started_at = time.monotonic()
        try:
            while not criteria.should_stop(
                self._tracker.received_count,
                time.monotonic() - started_at,
                self._reconnect_count,
            ):
                client: Optional[BleakClient] = None
                subscribed = False
                inbox = NotificationInbox(self._queue_size)
                reconnect_started_at = self._pending_reconnect_started_at
                self._disconnect_event.clear()
                self._disconnect_callback_at = None
                connection_generation: Optional[int] = None
                try:
                    device = await self._scan_for_device(
                        self._remaining_duration(criteria, started_at)
                    )
                    self._connection_generation += 1
                    connection_generation = self._connection_generation
                    self._active_connection_generation = connection_generation
                    client = self._client_factory(
                        device,
                        disconnected_callback=self._make_disconnect_callback(
                            connection_generation
                        ),
                    )
                    connect_started = time.monotonic()
                    await self._await_with_duration(
                        client.connect(), criteria, started_at
                    )
                    self._observe_timing("connection_seconds", connect_started)
                    await self._await_with_duration(
                        self._verify_required_gatt(client), criteria, started_at
                    )
                    self._tracker.start_connection()
                    subscribe_started = time.monotonic()
                    await self._await_with_duration(
                        client.start_notify(
                            NOTIFY_CHARACTERISTIC_UUID,
                            self._make_notification_callback(inbox),
                        ),
                        criteria,
                        started_at,
                    )
                    subscribed = True
                    self._observe_timing("subscription_seconds", subscribe_started)
                    reconnected_at = await self._consume_connection(
                        inbox, criteria, started_at, reconnect_started_at is not None
                    )
                    if reconnected_at is not None and reconnect_started_at is not None:
                        self._reconnect_count += 1
                        self._observations.setdefault(
                            "disconnect_callback_to_resubscribe_seconds", []
                        ).append(round(reconnected_at - reconnect_started_at, 6))
                        self._pending_reconnect_started_at = None
                except RequiredGattMissing:
                    LOGGER.error("connected device is missing the required diagnostic GATT service")
                except asyncio.TimeoutError:
                    pass
                except Exception as exc:  # hardware and adapter errors are retried after a bounded delay
                    LOGGER.warning("BLE connection attempt failed: %s", exc)
                finally:
                    if self._active_connection_generation == connection_generation:
                        self._active_connection_generation = None
                    if client is not None and client.is_connected:
                        await self._quiesce_drain_and_disconnect(client, inbox, subscribed)
                    else:
                        self._drain_inbox(inbox)

                if criteria.should_stop(
                    self._tracker.received_count,
                    time.monotonic() - started_at,
                    self._reconnect_count,
                ):
                    break
                if self._disconnect_event.is_set():
                    self._pending_reconnect_started_at = (
                        self._disconnect_callback_at or time.monotonic()
                    )
                await self._sleep_with_duration(criteria, started_at)

            return self.summary(time.monotonic() - started_at)
        finally:
            self._active_connection_generation = None
            self._loop = None

    def summary(self, elapsed_seconds: float) -> RunSummary:
        return RunSummary(
            received_count=self._tracker.received_count,
            gap_count=self._tracker.gap_count,
            duplicate_count=self._tracker.duplicate_count,
            out_of_order_count=self._tracker.out_of_order_count,
            malformed_count=self._tracker.malformed_count,
            queue_drop_count=self._tracker.queue_drop_count,
            reconnect_count=self._reconnect_count,
            elapsed_seconds=round(elapsed_seconds, 3),
            observations=self._observations,
            cleanup_error_count=self._cleanup_error_count,
        )

    async def _scan_for_device(self, remaining_duration: Optional[float]) -> Any:
        scan_started = time.monotonic()
        scan_timeout = self._scan_timeout_seconds
        if remaining_duration is not None:
            scan_timeout = min(scan_timeout, remaining_duration)
        device = await asyncio.wait_for(
            self._scanner.find_device_by_filter(
                lambda _device, advertisement: advertises_expected_service(
                    advertisement.service_uuids
                ),
                timeout=scan_timeout,
            ),
            timeout=scan_timeout,
        )
        self._observe_timing("scan_seconds", scan_started)
        if device is None:
            raise TimeoutError("no peripheral advertised the required service UUID")
        return device

    async def _verify_required_gatt(self, client: BleakClient) -> None:
        service = client.services.get_service(EXPECTED_SERVICE_UUID)
        if service is None:
            raise RequiredGattMissing(EXPECTED_SERVICE_UUID)
        characteristic = service.get_characteristic(NOTIFY_CHARACTERISTIC_UUID)
        if characteristic is None:
            raise RequiredGattMissing(NOTIFY_CHARACTERISTIC_UUID)
        properties = {str(property_name).lower() for property_name in characteristic.properties}
        if "notify" not in properties:
            raise RequiredGattMissing(
                f"{NOTIFY_CHARACTERISTIC_UUID} does not support notifications"
            )

    def _make_notification_callback(
        self, inbox: NotificationInbox
    ) -> Callable[[Any, bytearray], None]:
        def on_notification(_sender: Any, payload: bytearray) -> None:
            inbox.put_from_callback(bytes(payload))

        return on_notification

    def _make_disconnect_callback(
        self, connection_generation: int
    ) -> Callable[[BleakClient], None]:
        def on_disconnect(_client: BleakClient) -> None:
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            callback_at = time.monotonic()
            try:
                loop.call_soon_threadsafe(
                    self._mark_unexpected_disconnect,
                    connection_generation,
                    callback_at,
                )
            except RuntimeError:
                return

        return on_disconnect

    def _mark_unexpected_disconnect(
        self, connection_generation: int, callback_at: float
    ) -> None:
        if connection_generation != self._active_connection_generation:
            return
        self._disconnect_callback_at = callback_at
        self._observations.setdefault("disconnect_callback_to_loop_seconds", []).append(
            round(time.monotonic() - callback_at, 6)
        )
        self._disconnect_event.set()

    async def _consume_connection(
        self,
        inbox: NotificationInbox,
        criteria: StopCriteria,
        started_at: float,
        reconnect_pending: bool,
    ) -> Optional[float]:
        reconnected_at: Optional[float] = None
        while not criteria.should_stop(
            self._tracker.received_count,
            time.monotonic() - started_at,
            self._reconnect_count + int(reconnected_at is not None),
        ):
            self._collect_queue_drops(inbox)
            payload_task = asyncio.create_task(inbox.get())
            disconnected_task = asyncio.create_task(self._disconnect_event.wait())
            timeout = self._remaining_duration(criteria, started_at)
            done, pending = await asyncio.wait(
                (payload_task, disconnected_task),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if not done:
                return reconnected_at
            if payload_task in done:
                observation = self._process_payload(payload_task.result())
                if reconnect_pending and reconnected_at is None and observation.kind == "baseline":
                    reconnected_at = time.monotonic()
            if disconnected_task in done:
                return reconnected_at
        return reconnected_at

    def _remaining_duration(self, criteria: StopCriteria, started_at: float) -> Optional[float]:
        if criteria.duration_seconds is None:
            return None
        return max(0.0, criteria.duration_seconds - (time.monotonic() - started_at))

    async def _await_with_duration(
        self, awaitable: Any, criteria: StopCriteria, started_at: float
    ) -> Any:
        remaining_duration = self._remaining_duration(criteria, started_at)
        if remaining_duration is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, timeout=remaining_duration)

    async def _sleep_with_duration(self, criteria: StopCriteria, started_at: float) -> None:
        remaining_duration = self._remaining_duration(criteria, started_at)
        if remaining_duration is None:
            await asyncio.sleep(self._reconnect_delay_seconds)
            return
        await asyncio.sleep(min(self._reconnect_delay_seconds, remaining_duration))

    async def _quiesce_drain_and_disconnect(
        self, client: BleakClient, inbox: NotificationInbox, subscribed: bool
    ) -> None:
        cleanup_deadline = time.monotonic() + TOTAL_CLEANUP_GRACE_SECONDS
        if subscribed:
            stop_budget = max(
                0.0,
                cleanup_deadline
                - time.monotonic()
                - MINIMUM_DISCONNECT_ATTEMPT_SECONDS,
            )
            stop_succeeded = await self._cleanup_operation(
                "stop-notify",
                client.stop_notify(NOTIFY_CHARACTERISTIC_UUID),
                stop_budget,
            )
            if not stop_succeeded:
                await asyncio.sleep(0)
        disconnect_budget = max(0.0, cleanup_deadline - time.monotonic())
        await self._cleanup_operation("disconnect", client.disconnect(), disconnect_budget)
        await asyncio.sleep(0)
        self._drain_inbox(inbox)

    async def _cleanup_operation(
        self, operation_name: str, awaitable: Any, timeout: float
    ) -> bool:
        task = asyncio.create_task(awaitable)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            self._cleanup_error_count += 1
            LOGGER.warning("BLE %s cleanup timed out", operation_name)
            self._cancel_detached_cleanup_task(task)
            return False
        except asyncio.CancelledError:
            self._cancel_detached_cleanup_task(task)
            raise
        except Exception as exc:
            self._cleanup_error_count += 1
            LOGGER.warning("BLE %s cleanup failed: %s", operation_name, exc)
            return False
        return True

    def _cancel_detached_cleanup_task(self, task: asyncio.Task[Any]) -> None:
        self._detached_cleanup_tasks.add(task)
        task.cancel()
        task.add_done_callback(self._consume_detached_cleanup_result)

    def _consume_detached_cleanup_result(self, task: asyncio.Task[Any]) -> None:
        self._detached_cleanup_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    def _drain_inbox(self, inbox: NotificationInbox) -> None:
        self._collect_queue_drops(inbox)
        while True:
            try:
                self._process_payload(inbox.get_nowait())
            except asyncio.QueueEmpty:
                return

    def _process_payload(self, payload: bytes) -> CounterObservation:
        observation = self._tracker.observe(payload)
        if observation.kind not in ("baseline", "sequential"):
            LOGGER.warning("counter observation: %s", observation)
        return observation

    def _collect_queue_drops(self, inbox: NotificationInbox) -> None:
        self._tracker.queue_drop_count += inbox.take_drop_count()

    def _observe_timing(self, key: str, started_at: float) -> None:
        self._observations.setdefault(key, []).append(round(time.monotonic() - started_at, 6))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    stop_group = parser.add_mutually_exclusive_group(required=True)
    stop_group.add_argument("--target-received", type=int)
    stop_group.add_argument("--duration-seconds", type=float)
    parser.add_argument("--require-reconnect", action="store_true")
    parser.add_argument("--scan-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--reconnect-delay-seconds", type=float, default=1.0)
    parser.add_argument("--queue-size", type=int, default=128)
    return parser


async def _run_from_args(args: argparse.Namespace) -> int:
    criteria = StopCriteria(
        target_received=args.target_received,
        duration_seconds=args.duration_seconds,
        require_reconnect=args.require_reconnect,
    )
    receiver = BleCounterReceiver(
        scan_timeout_seconds=args.scan_timeout_seconds,
        reconnect_delay_seconds=args.reconnect_delay_seconds,
        queue_size=args.queue_size,
    )
    summary = await receiver.run(criteria)
    print(summary.to_json())
    return summary.exit_code(criteria)


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run_from_args(args))


if __name__ == "__main__":
    raise SystemExit(main())
