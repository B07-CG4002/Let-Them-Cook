"""Tests for the test-only Gate D ATT MTU diagnostic probe.

Each test names the production break it catches.  Bleak itself is the only
external boundary replaced with fakes; payload assertions use hand-derived
literal byte sequences.
"""

from __future__ import annotations

import asyncio
import time
import unittest

from laptop.mtu_probe import (
    CONTROL_CHARACTERISTIC_UUID,
    DISCONNECT_ATTEMPT_RESERVE_SECONDS,
    EXPECTED_SERVICE_UUID,
    PROBE_CHARACTERISTIC_UUID,
    TOTAL_CLEANUP_GRACE_SECONDS,
    MtuProbe,
)


class _Characteristic:
    def __init__(self, properties: list[str]) -> None:
        self.properties = properties


class _Service:
    def __init__(self, control_properties: list[str], probe_properties: list[str]) -> None:
        self._control_properties = control_properties
        self._probe_properties = probe_properties

    def get_characteristic(self, uuid: str) -> _Characteristic | None:
        if uuid == CONTROL_CHARACTERISTIC_UUID:
            return _Characteristic(self._control_properties)
        if uuid == PROBE_CHARACTERISTIC_UUID:
            return _Characteristic(self._probe_properties)
        return None


class _Services:
    def __init__(self, control_properties: list[str], probe_properties: list[str]) -> None:
        self._service = _Service(control_properties, probe_properties)

    def get_service(self, uuid: str) -> _Service | None:
        return self._service if uuid == EXPECTED_SERVICE_UUID else None


class _Scanner:
    def __init__(self, device: object) -> None:
        self.device = device
        self.filter = None

    async def find_device_by_filter(self, predicate: object, timeout: float) -> object:
        del timeout
        self.filter = predicate
        return self.device


class _Advertisement:
    def __init__(self, service_uuids: list[str] | None) -> None:
        self.service_uuids = service_uuids


class _PredicateExecutingScanner:
    """A scanner double that evaluates the production advertisement predicate."""

    def __init__(self, service_uuids: list[str] | None) -> None:
        self._advertisement = _Advertisement(service_uuids)
        self.device = object()

    async def find_device_by_filter(self, predicate: object, timeout: float) -> object | None:
        del timeout
        return self.device if predicate(self.device, self._advertisement) else None


class _Client:
    def __init__(
        self,
        device: object,
        *,
        mtu_size: int = 25,
        control_properties: list[str] | None = None,
        probe_properties: list[str] | None = None,
        oversize_notification: bool = False,
        disconnect_error: Exception | None = None,
        late_notification_on_stop: bool = False,
    ) -> None:
        self.device = device
        self.mtu_size = mtu_size
        self.is_connected = False
        self.services = _Services(
            control_properties or ["write"], probe_properties or ["notify"]
        )
        self.oversize_notification = oversize_notification
        self.disconnect_error = disconnect_error
        self.late_notification_on_stop = late_notification_on_stop
        self.callback = None
        self.write_lengths: list[int] = []
        self.started_before_write = True

    async def connect(self) -> None:
        self.is_connected = True

    async def start_notify(self, _uuid: str, callback: object) -> None:
        self.callback = callback

    async def write_gatt_char(self, _uuid: str, request: bytes, response: bool) -> None:
        assert response is True
        self.started_before_write = self.callback is not None
        requested_length = int.from_bytes(request, "little")
        self.write_lengths.append(requested_length)
        if requested_length <= self.mtu_size - 3 or self.oversize_notification:
            assert self.callback is not None
            payload = bytes(index & 0xFF for index in range(requested_length))
            asyncio.get_running_loop().call_soon(self.callback, None, bytearray(payload))

    async def stop_notify(self, _uuid: str) -> None:
        if self.late_notification_on_stop:
            assert self.callback is not None
            asyncio.get_running_loop().call_soon(
                self.callback, None, bytearray(b"\x00\x01\x02\x03")
            )
        return None

    async def disconnect(self) -> None:
        self.is_connected = False
        if self.disconnect_error is not None:
            raise self.disconnect_error


class _ClientFactory:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.client: _Client | None = None

    def __call__(self, device: object) -> _Client:
        self.client = _Client(device, **self.kwargs)
        return self.client


class _SlowCleanupClient(_Client):
    def __init__(self, device: object, *, delay_seconds: float) -> None:
        super().__init__(device, mtu_size=25)
        self.delay_seconds = delay_seconds
        self.stop_started_at: float | None = None
        self.disconnect_started_at: float | None = None
        self.stop_started = asyncio.Event()
        self.stop_finished = asyncio.Event()
        self.disconnect_calls = 0

    async def stop_notify(self, _uuid: str) -> None:
        self.stop_started_at = time.monotonic()
        self.stop_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(self.delay_seconds)
        finally:
            asyncio.get_running_loop().call_soon(self.stop_finished.set)

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.disconnect_started_at = time.monotonic()
        self.is_connected = False


class _SlowCleanupClientFactory:
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.client: _SlowCleanupClient | None = None

    def __call__(self, device: object) -> _SlowCleanupClient:
        self.client = _SlowCleanupClient(device, delay_seconds=self.delay_seconds)
        return self.client


class _DualSlowCleanupClient(_Client):
    def __init__(self, device: object, *, delay_seconds: float) -> None:
        super().__init__(device, mtu_size=25)
        self.delay_seconds = delay_seconds
        self.stop_started_at: float | None = None
        self.disconnect_started_at: float | None = None

    async def stop_notify(self, _uuid: str) -> None:
        self.stop_started_at = time.monotonic()
        await asyncio.sleep(self.delay_seconds)

    async def disconnect(self) -> None:
        self.disconnect_started_at = time.monotonic()
        try:
            await asyncio.sleep(self.delay_seconds)
        except asyncio.CancelledError:
            self.is_connected = False
            raise
        self.is_connected = False


class _DualSlowCleanupClientFactory:
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.client: _DualSlowCleanupClient | None = None

    def __call__(self, device: object) -> _DualSlowCleanupClient:
        self.client = _DualSlowCleanupClient(device, delay_seconds=self.delay_seconds)
        return self.client


class MtuProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_scanner_fake_applies_exact_gate_d_advertisement_matching(self) -> None:
        """Catches accepting UUID near misses or rejecting exact UUID case variants."""
        for advertised_uuids, should_find_device in (
            ([EXPECTED_SERVICE_UUID], True),
            ([EXPECTED_SERVICE_UUID.upper()], True),
            ([EXPECTED_SERVICE_UUID[:-1] + "2"], False),
            (None, False),
        ):
            with self.subTest(advertised_uuids=advertised_uuids):
                scanner = _PredicateExecutingScanner(advertised_uuids)
                probe = MtuProbe(scanner=scanner, client_factory=_ClientFactory())
                if should_find_device:
                    summary = await probe.run()
                    self.assertEqual(summary.exit_code(), 0)
                else:
                    with self.assertRaisesRegex(
                        TimeoutError, "no peripheral advertising the Gate D test service"
                    ):
                        await probe.run()

    async def test_att_mtu_517_observes_modulo_256_boundary_payload(self) -> None:
        """Catches payload construction that fails to wrap at byte 256."""
        factory = _ClientFactory(mtu_size=517)

        summary = await MtuProbe(
            scanner=_Scanner(object()), client_factory=factory
        ).run()

        exact_514_byte_payload = bytes(range(256)) * 2 + b"\x00\x01"
        self.assertEqual(factory.client.write_lengths, [20, 514, 515])
        self.assertEqual(summary.trials[1].requested_length, 514)
        self.assertEqual(summary.trials[1].payload, exact_514_byte_payload)
        self.assertEqual(len(exact_514_byte_payload), 514)
        self.assertEqual(summary.trials[1].outcome, "exact")
        self.assertTrue(summary.trials[1].payload_exact)
        self.assertEqual(summary.trials[2].outcome, "laptop_absence")
        self.assertEqual(summary.exit_code(), 0)

    async def test_cancellation_resistant_stop_is_supervised_before_reserved_disconnect(self) -> None:
        """Catches cleanup waiting for a cancellation-resistant stop-notify task."""
        factory = _SlowCleanupClientFactory(delay_seconds=0.3)
        probe = MtuProbe(
            notification_timeout_seconds=0.01,
            scanner=_Scanner(object()),
            client_factory=factory,
        )

        started_at = time.monotonic()
        run_task = asyncio.create_task(probe.run())
        try:
            summary = await asyncio.wait_for(asyncio.shield(run_task), timeout=1.15)
        except asyncio.TimeoutError:
            run_task.cancel()
            await run_task
            self.fail("cleanup supervision did not return within the shared grace")

        client = factory.client
        self.assertLess(time.monotonic() - started_at, 1.15)
        self.assertEqual(summary.cleanup_error_count, 1)
        self.assertEqual(client.disconnect_calls, 1)
        self.assertIsNotNone(client.stop_started_at)
        self.assertIsNotNone(client.disconnect_started_at)
        self.assertLess(
            client.disconnect_started_at - client.stop_started_at,
            TOTAL_CLEANUP_GRACE_SECONDS - DISCONNECT_ATTEMPT_RESERVE_SECONDS + 0.05,
        )
        self.assertEqual(len(getattr(probe, "_detached_cleanup_tasks", ())), 1)
        await asyncio.wait_for(
            client.stop_finished.wait(), timeout=TOTAL_CLEANUP_GRACE_SECONDS
        )
        self.assertEqual(len(getattr(probe, "_detached_cleanup_tasks", ())), 0)

    async def test_two_slow_cleanup_operations_share_one_grace_window(self) -> None:
        """Catches granting each cleanup operation its own one-second deadline."""
        factory = _DualSlowCleanupClientFactory(delay_seconds=0.7)
        probe = MtuProbe(
            notification_timeout_seconds=0.01,
            scanner=_Scanner(object()),
            client_factory=factory,
        )

        started_at = time.monotonic()
        summary = await probe.run()

        self.assertLess(time.monotonic() - started_at, 1.15)
        self.assertEqual(summary.cleanup_error_count, 1)
        self.assertIsNotNone(factory.client.stop_started_at)
        self.assertIsNotNone(factory.client.disconnect_started_at)

    async def test_outer_cleanup_cancellation_retains_child_until_it_finishes(self) -> None:
        """Catches losing a cancellation-resistant cleanup child on outer cancellation."""
        factory = _SlowCleanupClientFactory(delay_seconds=0.3)
        probe = MtuProbe(
            notification_timeout_seconds=0.01,
            scanner=_Scanner(object()),
            client_factory=factory,
        )
        run_task = asyncio.create_task(probe.run())
        while factory.client is None:
            await asyncio.sleep(0)
        await factory.client.stop_started.wait()

        run_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await run_task

        self.assertEqual(len(getattr(probe, "_detached_cleanup_tasks", ())), 1)
        await asyncio.wait_for(
            factory.client.stop_finished.wait(), timeout=TOTAL_CLEANUP_GRACE_SECONDS
        )
        self.assertEqual(len(getattr(probe, "_detached_cleanup_tasks", ())), 0)

    async def test_run_uses_scanned_device_and_delivers_exact_safe_and_boundary_payloads(self) -> None:
        """Catches changing the scan/device handoff or accepting truncated/corrupt payloads."""
        device = object()
        scanner = _Scanner(device)
        factory = _ClientFactory(mtu_size=25)

        summary = await MtuProbe(scanner=scanner, client_factory=factory).run()

        self.assertIsNotNone(scanner.filter)
        self.assertIs(factory.client.device, device)
        self.assertTrue(factory.client.started_before_write)
        self.assertEqual(factory.client.write_lengths, [20, 22, 23])
        self.assertEqual(summary.windows_att_mtu, 25)
        self.assertEqual(summary.value_boundary, 22)
        self.assertEqual(summary.trials[0].payload, b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10\x11\x12\x13")
        self.assertEqual(summary.trials[1].payload, b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10\x11\x12\x13\x14\x15")
        self.assertEqual(summary.trials[2].outcome, "laptop_absence")
        self.assertEqual(summary.exit_code(), 0)

    async def test_primary_gatt_error_keeps_disconnect_failure_in_machine_summary(self) -> None:
        """Catches dropping cleanup evidence whenever GATT validation also fails."""
        factory = _ClientFactory(
            control_properties=["read"],
            disconnect_error=RuntimeError("disconnect failed"),
        )

        summary = await MtuProbe(scanner=_Scanner(object()), client_factory=factory).run()

        self.assertEqual(
            summary.error,
            "required Gate D write control characteristic is missing",
        )
        self.assertEqual(summary.cleanup_error_count, 1)
        self.assertEqual(summary.exit_code(), 1)

    async def test_oversize_notification_is_an_anomaly_not_evidence_of_rejection(self) -> None:
        """Catches treating a boundary-plus-one notification as a successful rejection."""
        factory = _ClientFactory(mtu_size=25, oversize_notification=True)

        summary = await MtuProbe(scanner=_Scanner(object()), client_factory=factory).run()

        self.assertEqual(summary.trials[2].outcome, "unexpected_notification")
        self.assertEqual(summary.exit_code(), 1)

    async def test_late_oversize_notification_during_cleanup_is_an_anomaly(self) -> None:
        """Catches accepting a timed-out oversize trial when cleanup receives a late notification."""
        factory = _ClientFactory(mtu_size=25, late_notification_on_stop=True)

        summary = await MtuProbe(scanner=_Scanner(object()), client_factory=factory).run()

        self.assertEqual(summary.trials[2].outcome, "laptop_absence")
        self.assertEqual(summary.unexpected_notification_count, 1)
        self.assertEqual(summary.exit_code(), 1)

    async def test_cleanup_failure_is_exposed_in_summary_and_exit_status(self) -> None:
        """Catches silently hiding stop-notify or disconnect cleanup failures."""
        factory = _ClientFactory(disconnect_error=RuntimeError("disconnect failed"))

        summary = await MtuProbe(scanner=_Scanner(object()), client_factory=factory).run()

        self.assertEqual(summary.cleanup_error_count, 1)
        self.assertEqual(summary.exit_code(), 1)


if __name__ == "__main__":
    unittest.main()
