"""Unit tests for the Gate C BLE diagnostic counter receiver."""

from __future__ import annotations

import asyncio
import json
import time
import unittest

from laptop.ble_counter_receiver import (
    EXPECTED_SERVICE_UUID,
    MINIMUM_DISCONNECT_ATTEMPT_SECONDS,
    NOTIFY_CHARACTERISTIC_UUID,
    TOTAL_CLEANUP_GRACE_SECONDS,
    BleCounterReceiver,
    CounterTracker,
    NotificationInbox,
    RunSummary,
    RequiredGattMissing,
    StopCriteria,
    advertises_expected_service,
    decode_counter,
)


class DecodeCounterTests(unittest.TestCase):
    def test_decodes_exactly_four_little_endian_bytes(self) -> None:
        self.assertEqual(decode_counter(b"\x78\x56\x34\x12"), 0x12345678)

    def test_rejects_payloads_that_are_not_exactly_four_bytes(self) -> None:
        for payload in (b"", b"\x00\x00\x00", b"\x00\x00\x00\x00\x00"):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    decode_counter(payload)


class AdvertisementMatchingTests(unittest.TestCase):
    def test_matches_only_the_exact_advertised_service_uuid(self) -> None:
        self.assertTrue(advertises_expected_service([EXPECTED_SERVICE_UUID]))
        self.assertFalse(advertises_expected_service(["6e1c0001-7a45-4dc4-b678-3f2d5a9c1002"]))


class CounterTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = CounterTracker()
        self.tracker.start_connection()

    def test_first_valid_counter_is_connection_baseline(self) -> None:
        observation = self.tracker.observe(b"\x2a\x00\x00\x00")

        self.assertEqual(observation.kind, "baseline")
        self.assertEqual(observation.counter, 42)
        self.assertEqual(self.tracker.received_count, 1)

    def test_sequential_counters_have_no_anomaly(self) -> None:
        self.tracker.observe(b"\x0a\x00\x00\x00")
        observation = self.tracker.observe(b"\x0b\x00\x00\x00")

        self.assertEqual(observation.kind, "sequential")
        self.assertEqual(self.tracker.gap_count, 0)

    def test_gap_counts_each_missing_counter(self) -> None:
        self.tracker.observe(b"\x0a\x00\x00\x00")
        observation = self.tracker.observe(b"\x0d\x00\x00\x00")

        self.assertEqual(observation.kind, "gap")
        self.assertEqual(observation.missing, 2)
        self.assertEqual(self.tracker.gap_count, 2)

    def test_duplicate_counter_is_counted(self) -> None:
        self.tracker.observe(b"\x08\x00\x00\x00")
        observation = self.tracker.observe(b"\x08\x00\x00\x00")

        self.assertEqual(observation.kind, "duplicate")
        self.assertEqual(self.tracker.duplicate_count, 1)

    def test_out_of_order_counter_is_counted(self) -> None:
        self.tracker.observe(b"\x0a\x00\x00\x00")
        observation = self.tracker.observe(b"\x09\x00\x00\x00")

        self.assertEqual(observation.kind, "out_of_order")
        self.assertEqual(self.tracker.out_of_order_count, 1)

    def test_32_bit_wrap_is_sequential(self) -> None:
        self.tracker.observe(b"\xfe\xff\xff\xff")
        self.tracker.observe(b"\xff\xff\xff\xff")
        observation = self.tracker.observe(b"\x00\x00\x00\x00")

        self.assertEqual(observation.kind, "sequential")
        self.assertEqual(self.tracker.out_of_order_count, 0)

    def test_malformed_payload_is_counted_without_changing_baseline(self) -> None:
        self.tracker.observe(b"\x07\x00\x00\x00")
        observation = self.tracker.observe(b"\x07\x00\x00")

        self.assertEqual(observation.kind, "malformed")
        self.assertEqual(self.tracker.malformed_count, 1)
        self.assertEqual(self.tracker.last_counter, 7)

    def test_new_connection_resets_the_baseline(self) -> None:
        self.tracker.observe(b"\x84\x03\x00\x00")
        self.tracker.start_connection()
        observation = self.tracker.observe(b"\x00\x00\x00\x00")

        self.assertEqual(observation.kind, "baseline")
        self.assertEqual(self.tracker.out_of_order_count, 0)


class NotificationInboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_bounded_queue_records_drop_for_worker_to_collect(self) -> None:
        inbox = NotificationInbox(maxsize=1)

        self.assertTrue(inbox.put_from_callback(b"\x01\x00\x00\x00"))
        self.assertFalse(inbox.put_from_callback(b"\x02\x00\x00\x00"))
        self.assertEqual(await inbox.get(), b"\x01\x00\x00\x00")
        self.assertEqual(inbox.take_drop_count(), 1)


class ReconnectAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_mode_waits_for_a_new_connection_baseline(self) -> None:
        first_device = object()
        second_device = object()
        client_factory = _FakeClientFactory()
        receiver = BleCounterReceiver(
            scan_timeout_seconds=0.01,
            reconnect_delay_seconds=0.0,
            scanner=_FakeScanner([first_device, second_device]),
            client_factory=client_factory,
        )

        summary = await receiver.run(
            StopCriteria(target_received=1, require_reconnect=True)
        )

        self.assertIs(client_factory.devices[0], first_device)
        self.assertIs(client_factory.devices[1], second_device)
        self.assertEqual(summary.received_count, 2)
        self.assertEqual(summary.reconnect_count, 1)
        self.assertEqual(summary.out_of_order_count, 0)
        self.assertEqual(summary.exit_code(StopCriteria(target_received=1, require_reconnect=True)), 0)
        self.assertIn("disconnect_callback_to_resubscribe_seconds", summary.observations)

    async def test_stale_disconnect_callback_does_not_disconnect_new_generation(self) -> None:
        receiver = BleCounterReceiver()
        receiver._loop = asyncio.get_running_loop()
        receiver._active_connection_generation = 2

        receiver._make_disconnect_callback(1)(object())
        await asyncio.sleep(0)

        self.assertFalse(receiver._disconnect_event.is_set())

    async def test_disconnect_cleanup_failure_does_not_prevent_summary(self) -> None:
        receiver = BleCounterReceiver(
            reconnect_delay_seconds=0.0,
            scanner=_FakeScanner([object()]),
            client_factory=_CleanupFailureClientFactory(),
        )

        with self.assertLogs("laptop.ble_counter_receiver", level="WARNING"):
            summary = await receiver.run(StopCriteria(target_received=1))

        self.assertEqual(summary.received_count, 1)
        self.assertEqual(summary.cleanup_error_count, 1)
        self.assertEqual(summary.exit_code(StopCriteria(target_received=1)), 1)


class GattVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_required_characteristic_without_notify_property(self) -> None:
        receiver = BleCounterReceiver()

        with self.assertRaises(RequiredGattMissing):
            await receiver._verify_required_gatt(_GattClient(properties=["read"]))

    async def test_accepts_required_characteristic_with_notify_property(self) -> None:
        receiver = BleCounterReceiver()

        await receiver._verify_required_gatt(_GattClient(properties=["notify", "read"]))


class LifecycleBoundTests(unittest.IsolatedAsyncioTestCase):
    async def test_duration_bounds_slow_scan(self) -> None:
        receiver = BleCounterReceiver(
            scanner=_SlowScanner(delay_seconds=0.3),
            reconnect_delay_seconds=0.3,
        )

        started_at = time.monotonic()
        await receiver.run(StopCriteria(duration_seconds=0.05))

        self.assertLess(time.monotonic() - started_at, 0.2)

    async def test_duration_bounds_slow_connect(self) -> None:
        receiver = BleCounterReceiver(
            scanner=_FakeScanner([object()]),
            client_factory=_SlowConnectClientFactory(delay_seconds=0.3),
        )

        started_at = time.monotonic()
        await receiver.run(StopCriteria(duration_seconds=0.05))

        self.assertLess(time.monotonic() - started_at, 0.2)

    async def test_duration_bounds_retry_delay_after_scan_failure(self) -> None:
        receiver = BleCounterReceiver(
            scanner=_NoDeviceScanner(),
            reconnect_delay_seconds=0.3,
        )

        started_at = time.monotonic()
        await receiver.run(StopCriteria(duration_seconds=0.05))

        self.assertLess(time.monotonic() - started_at, 0.2)

    async def test_cleanup_timeout_bounds_slow_stop_notify(self) -> None:
        receiver = BleCounterReceiver(
            scanner=_FakeScanner([object()]),
            client_factory=_SlowCleanupClientFactory(delay_seconds=1.5),
        )

        started_at = time.monotonic()
        with self.assertLogs("laptop.ble_counter_receiver", level="WARNING"):
            summary = await receiver.run(StopCriteria(target_received=1))

        self.assertEqual(summary.received_count, 1)
        self.assertLess(time.monotonic() - started_at, 1.15)
        self.assertEqual(summary.cleanup_error_count, 1)
        self.assertEqual(summary.exit_code(StopCriteria(target_received=1)), 1)

    async def test_windows_cleanup_latencies_fit_within_one_shared_deadline(self) -> None:
        receiver = BleCounterReceiver(
            scanner=_FakeScanner([object()]),
            client_factory=_WindowsCleanupLatencyClientFactory(),
        )

        summary = await receiver.run(StopCriteria(target_received=1))

        self.assertEqual(summary.cleanup_error_count, 0)
        self.assertEqual(summary.exit_code(StopCriteria(target_received=1)), 0)

    async def test_stop_timeout_does_not_delay_reserved_disconnect_attempt(self) -> None:
        client_factory = _CancellationDelayingCleanupClientFactory()
        receiver = BleCounterReceiver(
            scanner=_FakeScanner([object()]),
            client_factory=client_factory,
        )

        started_at = time.monotonic()
        with self.assertLogs("laptop.ble_counter_receiver", level="WARNING") as logs:
            summary = await receiver.run(StopCriteria(target_received=1))

        client = client_factory.client
        self.assertLess(time.monotonic() - started_at, 1.15)
        self.assertEqual(summary.cleanup_error_count, 1)
        self.assertIn("BLE stop-notify cleanup timed out", logs.output[0])
        self.assertEqual(client.disconnect_calls, 1)
        self.assertIsNotNone(client.stop_started_at)
        self.assertIsNotNone(client.disconnect_started_at)
        self.assertLess(
            client.disconnect_started_at - client.stop_started_at,
            TOTAL_CLEANUP_GRACE_SECONDS - MINIMUM_DISCONNECT_ATTEMPT_SECONDS + 0.05,
        )
        self.assertEqual(len(getattr(receiver, "_detached_cleanup_tasks", ())), 1)
        await asyncio.wait_for(
            client.stop_finished.wait(), timeout=TOTAL_CLEANUP_GRACE_SECONDS
        )
        self.assertEqual(len(getattr(receiver, "_detached_cleanup_tasks", ())), 0)

    async def test_outer_cleanup_cancellation_detaches_child_then_reraises(self) -> None:
        receiver = BleCounterReceiver()
        client = _CancellationDelayingCleanupClient()
        operation_task = asyncio.create_task(
            receiver._cleanup_operation("stop-notify", client.stop_notify("unused"), 10.0)
        )
        await asyncio.sleep(0)

        operation_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation_task

        self.assertEqual(len(getattr(receiver, "_detached_cleanup_tasks", ())), 1)
        await asyncio.wait_for(
            client.stop_finished.wait(), timeout=TOTAL_CLEANUP_GRACE_SECONDS
        )
        self.assertEqual(len(getattr(receiver, "_detached_cleanup_tasks", ())), 0)

    async def test_unsubscribed_slow_disconnect_stays_within_cleanup_grace(self) -> None:
        client_factory = _SlowUnsubscribedDisconnectClientFactory()
        receiver = BleCounterReceiver(
            reconnect_delay_seconds=0.0,
            scanner=_FakeScanner([object()]),
            client_factory=client_factory,
        )

        started_at = time.monotonic()
        with self.assertLogs("laptop.ble_counter_receiver", level="WARNING"):
            summary = await receiver.run(StopCriteria(duration_seconds=0.05))

        client = client_factory.client
        self.assertLess(time.monotonic() - started_at, 1.15)
        self.assertEqual(client.stop_notify_calls, 0)
        self.assertEqual(client.disconnect_calls, 1)
        self.assertEqual(summary.cleanup_error_count, 1)

    async def test_missing_gatt_skips_unsubscribed_stop_notify_but_disconnects(self) -> None:
        client_factory = _MissingGattCleanupClientFactory()
        receiver = BleCounterReceiver(
            reconnect_delay_seconds=0.0,
            scanner=_FakeScanner([object()]),
            client_factory=client_factory,
        )

        with self.assertLogs("laptop.ble_counter_receiver", level="WARNING"):
            summary = await receiver.run(StopCriteria(duration_seconds=0.05))

        client = client_factory.client
        self.assertEqual(client.start_notify_calls, 0)
        self.assertEqual(client.stop_notify_calls, 0)
        self.assertEqual(client.disconnect_calls, 1)
        self.assertEqual(summary.cleanup_error_count, 0)

    async def test_start_notify_failure_skips_unsubscribed_stop_notify_but_disconnects(self) -> None:
        client_factory = _StartNotifyFailureClientFactory()
        receiver = BleCounterReceiver(
            reconnect_delay_seconds=0.0,
            scanner=_FakeScanner([object()]),
            client_factory=client_factory,
        )

        with self.assertLogs("laptop.ble_counter_receiver", level="WARNING"):
            summary = await receiver.run(StopCriteria(duration_seconds=0.05))

        client = client_factory.client
        self.assertEqual(client.start_notify_calls, 1)
        self.assertEqual(client.stop_notify_calls, 0)
        self.assertEqual(client.disconnect_calls, 1)
        self.assertEqual(summary.cleanup_error_count, 0)

    async def test_duration_allows_only_one_total_cleanup_grace(self) -> None:
        receiver = BleCounterReceiver(
            scanner=_FakeScanner([object()]),
            client_factory=_DualSlowCleanupClientFactory(delay_seconds=0.5),
        )

        started_at = time.monotonic()
        with self.assertLogs("laptop.ble_counter_receiver", level="WARNING"):
            summary = await receiver.run(StopCriteria(duration_seconds=0.05))

        self.assertLess(time.monotonic() - started_at, 1.15)
        self.assertGreaterEqual(summary.cleanup_error_count, 1)
        self.assertEqual(summary.exit_code(StopCriteria(duration_seconds=0.05)), 1)

    async def test_stop_notify_quiesces_before_final_inbox_drain(self) -> None:
        receiver = BleCounterReceiver(
            queue_size=2,
            scanner=_FakeScanner([object()]),
            client_factory=_StopInjectingClientFactory(),
        )

        summary = await receiver.run(StopCriteria(target_received=1))

        self.assertEqual(summary.received_count, 2)
        self.assertEqual(summary.queue_drop_count, 0)
        self.assertEqual(summary.cleanup_error_count, 0)

    async def test_stop_timeout_injected_notification_is_final_accounted(self) -> None:
        receiver = BleCounterReceiver(
            queue_size=2,
            scanner=_FakeScanner([object()]),
            client_factory=_StopTimeoutInjectingClientFactory(),
        )

        with self.assertLogs("laptop.ble_counter_receiver", level="WARNING"):
            summary = await receiver.run(StopCriteria(target_received=1))

        self.assertEqual(summary.received_count, 2)
        self.assertEqual(summary.cleanup_error_count, 1)
        self.assertEqual(summary.exit_code(StopCriteria(target_received=1)), 1)

    async def test_stop_exception_is_reported_as_cleanup_failure(self) -> None:
        receiver = BleCounterReceiver(
            scanner=_FakeScanner([object()]),
            client_factory=_StopExceptionClientFactory(),
        )

        with self.assertLogs("laptop.ble_counter_receiver", level="WARNING"):
            summary = await receiver.run(StopCriteria(target_received=1))

        self.assertEqual(summary.cleanup_error_count, 1)
        self.assertEqual(summary.exit_code(StopCriteria(target_received=1)), 1)

    async def test_disconnect_timeout_is_reported_as_cleanup_failure(self) -> None:
        receiver = BleCounterReceiver(
            scanner=_FakeScanner([object()]),
            client_factory=_DisconnectTimeoutClientFactory(),
        )

        with self.assertLogs("laptop.ble_counter_receiver", level="WARNING"):
            summary = await receiver.run(StopCriteria(target_received=1))

        self.assertEqual(summary.cleanup_error_count, 1)
        self.assertEqual(summary.exit_code(StopCriteria(target_received=1)), 1)

    async def test_run_detaches_loop_and_closed_loop_callback_is_ignored(self) -> None:
        receiver = BleCounterReceiver(
            scanner=_FakeScanner([object()]),
            client_factory=_CleanupFailureClientFactory(),
        )

        with self.assertLogs("laptop.ble_counter_receiver", level="WARNING"):
            await receiver.run(StopCriteria(target_received=1))
        self.assertIsNone(receiver._loop)

        receiver._loop = _ClosedLoop()
        receiver._make_disconnect_callback(1)(object())


class _FakeScanner:
    def __init__(self, devices: list[object]) -> None:
        self._devices = iter(devices)

    async def find_device_by_filter(self, _filter: object, timeout: float) -> object:
        del timeout
        return next(self._devices)


class _FakeService:
    def get_characteristic(self, uuid: str) -> object | None:
        return _GattCharacteristic(["notify"]) if uuid == NOTIFY_CHARACTERISTIC_UUID else None


class _FakeServices:
    def get_service(self, uuid: str) -> _FakeService | None:
        return _FakeService() if uuid == EXPECTED_SERVICE_UUID else None


class _FakeClient:
    def __init__(
        self,
        is_first: bool,
        disconnected_callback: object,
        factory: _FakeClientFactory,
    ) -> None:
        self.is_connected = False
        self.services = _FakeServices()
        self._is_first = is_first
        self._disconnected_callback = disconnected_callback
        self._factory = factory
        self.callback: object | None = None

    async def connect(self) -> None:
        self.is_connected = True

    async def start_notify(self, _uuid: str, callback: object) -> None:
        self.callback = callback
        if self._is_first:
            callback(None, bytearray(b"\x0a\x00\x00\x00"))
            self.is_connected = False
            self._disconnected_callback(self)
        else:
            assert self._factory.first_client is not None
            assert self._factory.first_client.callback is not None
            self._factory.first_client.callback(None, bytearray(b"\x0b\x00\x00\x00"))
            callback(None, bytearray(b"\x00\x00\x00\x00"))

    async def stop_notify(self, _uuid: str) -> None:
        return None

    async def disconnect(self) -> None:
        self.is_connected = False


class _FakeClientFactory:
    def __init__(self) -> None:
        self.devices: list[object] = []
        self.first_client: _FakeClient | None = None

    def __call__(self, device: object, disconnected_callback: object) -> _FakeClient:
        self.devices.append(device)
        client = _FakeClient(len(self.devices) == 1, disconnected_callback, self)
        if client._is_first:
            self.first_client = client
        return client


class _GattCharacteristic:
    def __init__(self, properties: list[str]) -> None:
        self.properties = properties


class _GattClient:
    def __init__(self, properties: list[str]) -> None:
        self.services = _GattServices(properties)


class _GattServices:
    def __init__(self, properties: list[str]) -> None:
        self._properties = properties

    def get_service(self, uuid: str) -> _FakeService | None:
        return _GattServiceWithProperties(self._properties) if uuid == EXPECTED_SERVICE_UUID else None


class _GattServiceWithProperties:
    def __init__(self, properties: list[str]) -> None:
        self._properties = properties

    def get_characteristic(self, uuid: str) -> _GattCharacteristic | None:
        return _GattCharacteristic(self._properties) if uuid == NOTIFY_CHARACTERISTIC_UUID else None


class _CleanupFailureClient:
    def __init__(self, disconnected_callback: object) -> None:
        self.is_connected = False
        self.services = _FakeServices()
        self._disconnected_callback = disconnected_callback

    async def connect(self) -> None:
        self.is_connected = True

    async def start_notify(self, _uuid: str, callback: object) -> None:
        callback(None, bytearray(b"\x01\x00\x00\x00"))

    async def stop_notify(self, _uuid: str) -> None:
        return None

    async def disconnect(self) -> None:
        self.is_connected = False
        raise RuntimeError("simulated cleanup failure")


class _CleanupFailureClientFactory:
    def __call__(self, _device: object, disconnected_callback: object) -> _CleanupFailureClient:
        return _CleanupFailureClient(disconnected_callback)


class _SlowScanner:
    def __init__(self, delay_seconds: float) -> None:
        self._delay_seconds = delay_seconds

    async def find_device_by_filter(self, _filter: object, timeout: float) -> object:
        del timeout
        await asyncio.sleep(self._delay_seconds)
        return object()


class _NoDeviceScanner:
    async def find_device_by_filter(self, _filter: object, timeout: float) -> None:
        del timeout
        return None


class _SlowConnectClient:
    def __init__(self, delay_seconds: float) -> None:
        self.is_connected = False
        self.services = _FakeServices()
        self._delay_seconds = delay_seconds

    async def connect(self) -> None:
        await asyncio.sleep(self._delay_seconds)
        self.is_connected = True

    async def stop_notify(self, _uuid: str) -> None:
        return None

    async def disconnect(self) -> None:
        self.is_connected = False


class _SlowConnectClientFactory:
    def __init__(self, delay_seconds: float) -> None:
        self._delay_seconds = delay_seconds

    def __call__(self, _device: object, disconnected_callback: object) -> _SlowConnectClient:
        del disconnected_callback
        return _SlowConnectClient(self._delay_seconds)


class _SlowCleanupClient:
    def __init__(self, delay_seconds: float) -> None:
        self.is_connected = False
        self.services = _FakeServices()
        self._delay_seconds = delay_seconds
        self._callback: object | None = None

    async def connect(self) -> None:
        self.is_connected = True

    async def start_notify(self, _uuid: str, callback: object) -> None:
        self._callback = callback
        callback(None, bytearray(b"\x01\x00\x00\x00"))

    async def stop_notify(self, _uuid: str) -> None:
        await asyncio.sleep(self._delay_seconds)

    async def disconnect(self) -> None:
        self.is_connected = False


class _SlowCleanupClientFactory:
    def __init__(self, delay_seconds: float) -> None:
        self._delay_seconds = delay_seconds

    def __call__(self, _device: object, disconnected_callback: object) -> _SlowCleanupClient:
        del disconnected_callback
        return _SlowCleanupClient(self._delay_seconds)


class _DualSlowCleanupClient(_SlowCleanupClient):
    async def disconnect(self) -> None:
        await asyncio.sleep(self._delay_seconds)
        self.is_connected = False


class _DualSlowCleanupClientFactory:
    def __init__(self, delay_seconds: float) -> None:
        self._delay_seconds = delay_seconds

    def __call__(self, _device: object, disconnected_callback: object) -> _DualSlowCleanupClient:
        del disconnected_callback
        return _DualSlowCleanupClient(self._delay_seconds)


class _WindowsCleanupLatencyClient(_SlowCleanupClient):
    async def stop_notify(self, _uuid: str) -> None:
        await asyncio.sleep(0.25)

    async def disconnect(self) -> None:
        await asyncio.sleep(0.1)
        self.is_connected = False


class _WindowsCleanupLatencyClientFactory:
    def __call__(
        self, _device: object, disconnected_callback: object
    ) -> _WindowsCleanupLatencyClient:
        del disconnected_callback
        return _WindowsCleanupLatencyClient(delay_seconds=0)


class _CancellationDelayingCleanupClient(_SlowCleanupClient):
    def __init__(self) -> None:
        super().__init__(delay_seconds=0)
        self.stop_started_at: float | None = None
        self.disconnect_started_at: float | None = None
        self.disconnect_calls = 0
        self.stop_finished = asyncio.Event()

    async def stop_notify(self, _uuid: str) -> None:
        self.stop_started_at = time.monotonic()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.5)
        finally:
            asyncio.get_running_loop().call_soon(self.stop_finished.set)

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.disconnect_started_at = time.monotonic()
        self.is_connected = False


class _CancellationDelayingCleanupClientFactory:
    def __init__(self) -> None:
        self.client = _CancellationDelayingCleanupClient()

    def __call__(
        self, _device: object, disconnected_callback: object
    ) -> _CancellationDelayingCleanupClient:
        del disconnected_callback
        return self.client


class _MissingGattCleanupClient:
    def __init__(self) -> None:
        self.is_connected = False
        self.services = _MissingGattServices()
        self.start_notify_calls = 0
        self.stop_notify_calls = 0
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.is_connected = True

    async def start_notify(self, _uuid: str, _callback: object) -> None:
        self.start_notify_calls += 1

    async def stop_notify(self, _uuid: str) -> None:
        self.stop_notify_calls += 1
        raise RuntimeError("stop_notify called without a subscription")

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.is_connected = False


class _MissingGattCleanupClientFactory:
    def __init__(self) -> None:
        self.client = _MissingGattCleanupClient()

    def __call__(self, _device: object, disconnected_callback: object) -> _MissingGattCleanupClient:
        del disconnected_callback
        return self.client


class _MissingGattServices:
    def get_service(self, _uuid: str) -> None:
        return None


class _StartNotifyFailureClient(_MissingGattCleanupClient):
    def __init__(self) -> None:
        super().__init__()
        self.services = _FakeServices()

    async def start_notify(self, _uuid: str, _callback: object) -> None:
        self.start_notify_calls += 1
        raise RuntimeError("simulated start-notify failure")


class _StartNotifyFailureClientFactory:
    def __init__(self) -> None:
        self.client = _StartNotifyFailureClient()

    def __call__(self, _device: object, disconnected_callback: object) -> _StartNotifyFailureClient:
        del disconnected_callback
        return self.client


class _SlowUnsubscribedDisconnectClient(_MissingGattCleanupClient):
    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        await asyncio.sleep(1.5)
        self.is_connected = False


class _SlowUnsubscribedDisconnectClientFactory:
    def __init__(self) -> None:
        self.client = _SlowUnsubscribedDisconnectClient()

    def __call__(
        self, _device: object, disconnected_callback: object
    ) -> _SlowUnsubscribedDisconnectClient:
        del disconnected_callback
        return self.client


class _StopInjectingClient:
    def __init__(self) -> None:
        self.is_connected = False
        self.services = _FakeServices()
        self._callback: object | None = None

    async def connect(self) -> None:
        self.is_connected = True

    async def start_notify(self, _uuid: str, callback: object) -> None:
        self._callback = callback
        callback(None, bytearray(b"\x01\x00\x00\x00"))

    async def stop_notify(self, _uuid: str) -> None:
        assert self._callback is not None
        self._callback(None, bytearray(b"\x02\x00\x00\x00"))

    async def disconnect(self) -> None:
        self.is_connected = False


class _StopInjectingClientFactory:
    def __call__(self, _device: object, disconnected_callback: object) -> _StopInjectingClient:
        del disconnected_callback
        return _StopInjectingClient()


class _StopTimeoutInjectingClient(_StopInjectingClient):
    async def stop_notify(self, _uuid: str) -> None:
        assert self._callback is not None
        asyncio.get_running_loop().call_soon(
            self._callback, None, bytearray(b"\x02\x00\x00\x00")
        )
        await asyncio.sleep(1.5)


class _StopTimeoutInjectingClientFactory:
    def __call__(self, _device: object, disconnected_callback: object) -> _StopTimeoutInjectingClient:
        del disconnected_callback
        return _StopTimeoutInjectingClient()


class _StopExceptionClient(_StopInjectingClient):
    async def stop_notify(self, _uuid: str) -> None:
        raise RuntimeError("simulated stop failure")


class _StopExceptionClientFactory:
    def __call__(self, _device: object, disconnected_callback: object) -> _StopExceptionClient:
        del disconnected_callback
        return _StopExceptionClient()


class _DisconnectTimeoutClient(_StopInjectingClient):
    async def stop_notify(self, _uuid: str) -> None:
        return None

    async def disconnect(self) -> None:
        await asyncio.sleep(1.5)
        self.is_connected = False


class _DisconnectTimeoutClientFactory:
    def __call__(self, _device: object, disconnected_callback: object) -> _DisconnectTimeoutClient:
        del disconnected_callback
        return _DisconnectTimeoutClient()


class _ClosedLoop:
    def is_closed(self) -> bool:
        return True

    def call_soon_threadsafe(self, *args: object) -> None:
        raise AssertionError("closed loop must not receive callbacks")


class StopAndSummaryTests(unittest.TestCase):
    def test_target_received_stop_criterion_uses_valid_counter_count(self) -> None:
        criteria = StopCriteria(target_received=2)

        self.assertFalse(criteria.reached(received_count=1, elapsed_seconds=999.0))
        self.assertTrue(criteria.reached(received_count=2, elapsed_seconds=0.0))

    def test_duration_bounds_a_reconnect_required_run_that_never_reconnects(self) -> None:
        criteria = StopCriteria(duration_seconds=1.0, require_reconnect=True)

        self.assertTrue(criteria.should_stop(received_count=999, elapsed_seconds=1.0, reconnect_count=0))
        self.assertEqual(
            RunSummary(999, 0, 0, 0, 0, 0, 0, 1.0).exit_code(criteria),
            1,
        )

    def test_summary_is_machine_readable_and_nonzero_for_anomalies(self) -> None:
        summary = RunSummary(
            received_count=2,
            gap_count=1,
            duplicate_count=0,
            out_of_order_count=0,
            malformed_count=0,
            queue_drop_count=0,
            reconnect_count=1,
            elapsed_seconds=1.25,
        )

        self.assertEqual(summary.exit_code(StopCriteria(target_received=2, require_reconnect=True)), 1)
        rendered = json.loads(summary.to_json())
        self.assertEqual(rendered["received_count"], 2)
        self.assertEqual(rendered["gap_count"], 1)
        self.assertEqual(rendered["reconnect_count"], 1)
        self.assertEqual(rendered["cleanup_error_count"], 0)


if __name__ == "__main__":
    unittest.main()
