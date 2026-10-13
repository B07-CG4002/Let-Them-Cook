"""BLE link-loss cleanup boundaries; all clients are in-process fakes."""
import asyncio
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bleak.exc import BleakError
from common.control import CONTROL_UUID, RESPONSE_UUID
from laptop.bridge import Bridge, BridgeConfig, SENSOR_UUID, SERVICE_UUID
from laptop.source_audit import SOURCE_STATS_UUID


class FakeScanner:
    def __init__(self):
        self.scans = 0

    async def find_device_by_filter(self, predicate, timeout):
        self.scans += 1
        if self.scans > 1:
            await asyncio.Event().wait()
        device = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
        assert predicate(device, SimpleNamespace(service_uuids=[SERVICE_UUID]))
        return device


class FakeClient:
    mtu_size = 517

    def __init__(self, device, disconnected_callback):
        self.on_disconnect = disconnected_callback
        self.is_connected = False
        self.stop_calls = 0
        self.disconnect_calls = 0
        self.stop_error = None
        self.disconnect_error = None
        self.stop_started = asyncio.Event()
        self.stop_gate = None
        self.disconnect_started = asyncio.Event()
        self.disconnect_gate = None
        characteristic = SimpleNamespace(properties=["notify"])
        service = SimpleNamespace(get_characteristic=lambda uuid:
                                  characteristic if uuid == SENSOR_UUID else None)
        self.services = SimpleNamespace(get_service=lambda uuid:
                                        service if uuid == SERVICE_UUID else None)

    async def connect(self):
        self.is_connected = True

    async def start_notify(self, uuid, callback):
        self.notification = callback

    async def stop_notify(self, uuid):
        self.stop_calls += 1
        self.stop_started.set()
        # Match installed Bleak WinRT: a gone link raises before its CCCD write.
        if not self.is_connected:
            raise BleakError("Not connected")
        if self.stop_gate is not None:
            await self.stop_gate.wait()
        if self.stop_error:
            raise self.stop_error

    async def disconnect(self):
        self.disconnect_calls += 1
        self.is_connected = False
        self.disconnect_started.set()
        if self.disconnect_gate is not None:
            await self.disconnect_gate.wait()
        if self.disconnect_error:
            raise self.disconnect_error

    def lose_link(self):
        self.is_connected = False
        self.on_disconnect(self)


class DisconnectCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def start_bridge(self):
        bridge = Bridge(BridgeConfig(ca_file="unused", diagnostic_unprotected=True))
        scanner = FakeScanner()
        clients = []

        def factory(*args, **kwargs):
            client = FakeClient(*args, **kwargs)
            clients.append(client)
            return client

        worker = asyncio.create_task(bridge.ble_loop(scanner=scanner, client_factory=factory))
        self.addAsyncCleanup(self.cancel_worker, worker)
        await self.wait_until(lambda: bridge.metrics.ble_connections == 1)
        return bridge, worker, clients[0], scanner

    async def wait_until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0)
        await asyncio.wait_for(wait(), 1)

    async def cancel_worker(self, worker):
        if not worker.done():
            worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(worker), 1)

    async def test_link_loss_skips_remote_notification_stop_but_releases_local_resources(self):
        bridge, worker, client, scanner = await self.start_bridge()
        client.lose_link()
        # The next scan proves the preceding cleanup finished before teardown.
        await self.wait_until(lambda: scanner.scans == 2)
        self.assertEqual(client.stop_calls, 0)
        self.assertEqual(bridge.metrics.cleanup_errors, 0)
        self.assertEqual(bridge.inbox.generation, 0)
        self.assertFalse(worker.done())  # Recoverable loss leaves reconnect loop running.
        await self.cancel_worker(worker)
        self.assertEqual(client.disconnect_calls, 1)
        self.assertEqual(bridge.metrics.cleanup_errors, 0)

    async def test_connected_cancellation_stops_notifications_then_disconnects(self):
        bridge, worker, client, _ = await self.start_bridge()
        await self.cancel_worker(worker)
        self.assertEqual(client.stop_calls, 1)
        self.assertEqual(client.disconnect_calls, 1)
        self.assertEqual(bridge.metrics.cleanup_errors, 0)
        self.assertEqual(bridge.inbox.generation, 0)

    async def test_cancellation_during_link_loss_cleanup_exits_instead_of_reconnecting(self):
        bridge, worker, client, _ = await self.start_bridge()
        client.disconnect_gate = asyncio.Event()
        client.lose_link()
        await asyncio.wait_for(client.disconnect_started.wait(), 1)
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(worker), 0.2)
        self.assertEqual(client.disconnect_calls, 1)
        self.assertEqual(bridge.inbox.generation, 0)

    async def test_second_cancellation_during_stop_still_attempts_local_disconnect(self):
        bridge, worker, client, _ = await self.start_bridge()
        client.stop_gate = asyncio.Event()
        worker.cancel()
        await asyncio.wait_for(client.stop_started.wait(), 1)
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(worker), 0.2)
        self.assertEqual(client.stop_calls, 1)
        self.assertEqual(client.disconnect_calls, 1)
        self.assertEqual(bridge.inbox.generation, 0)

    async def test_actual_cleanup_failures_identify_operation_and_type_without_message(self):
        bridge, worker, client, _ = await self.start_bridge()
        client.stop_error = BleakError("private-stop-detail")
        client.disconnect_error = RuntimeError("private-disconnect-detail")
        with self.assertLogs("laptop.bridge", level="WARNING") as captured:
            await self.cancel_worker(worker)
        self.assertEqual(client.stop_calls, 1)
        self.assertEqual(client.disconnect_calls, 1)
        self.assertEqual(bridge.metrics.cleanup_errors, 2)
        self.assertTrue(any("operation=stop_notify error_type=BleakError" in line
                            for line in captured.output))
        self.assertTrue(any("operation=disconnect error_type=RuntimeError" in line
                            for line in captured.output))
        self.assertNotIn("private-", "\n".join(captured.output))

    async def test_both_notification_timeouts_leave_time_to_release_native_resources(self):
        """Slow CCCD writes must not cancel WinRT's delayed service disposal."""
        bridge = Bridge(BridgeConfig(ca_file="unused", expected_device_id=1,
                                     source_audit=True, controls_enabled=True))
        stop = asyncio.Event()
        clients = []

        class ProtectedClient(FakeClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                properties = {SENSOR_UUID: ["notify"], SOURCE_STATS_UUID: ["read"],
                              CONTROL_UUID: ["write"], RESPONSE_UUID: ["notify"]}
                service = SimpleNamespace(get_characteristic=lambda uuid:
                    SimpleNamespace(properties=properties[uuid]) if uuid in properties else None)
                self.services = SimpleNamespace(get_service=lambda uuid: service)
                self.stop_gate = asyncio.Event()
                self.resources_released = False
                clients.append(self)

            async def read_gatt_char(self, uuid):
                return struct.pack("<4sB3xIIII", b"W7S1", 1, 7, 0, 0, 0)

            async def disconnect(self):
                self.disconnect_calls += 1
                # Installed Bleak WinRT waits 100 ms before closing services.
                await asyncio.sleep(0.1)
                self.resources_released = True
                self.is_connected = False

        async def authenticated(_client):
            pass

        with patch("laptop.windows_pairing.require_authenticated_bond", authenticated):
            worker = asyncio.create_task(bridge.ble_loop(scanner=FakeScanner(),
                client_factory=ProtectedClient, stop_event=stop))
            try:
                await self.wait_until(lambda: bridge.metrics.ble_connections == 1)
                stop.set()
                await asyncio.wait_for(worker, 3)
            finally:
                if not worker.done():
                    worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        self.assertTrue(clients[0].resources_released,
                        "notification timeouts consumed the native disconnect budget")
        self.assertEqual(clients[0].stop_calls, 2)
        self.assertEqual(clients[0].disconnect_calls, 1)
        self.assertEqual(bridge.metrics.cleanup_errors, 2)
        self.assertFalse(any(not task.done() for task in bridge._retained))
