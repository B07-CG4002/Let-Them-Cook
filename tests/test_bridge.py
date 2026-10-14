import asyncio
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from common.sensor import DUMMY_FIXTURES, SENSOR_MTU_FLOOR, SensorPacket, encode_packet
from laptop.bridge import Bridge, BridgeConfig, RawInbox, StreamTracker, SENSOR_UUID
from tools.ssh_tunnel import tunnel_command, supervise


def values(seq):
    return DUMMY_FIXTURES[seq % len(DUMMY_FIXTURES)]


def payload(seq=0, boot=7):
    return encode_packet(SensorPacket(1, boot, seq, 100 * seq, values(seq)))


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_copies_and_evicts_oldest_and_ignores_old_generation(self):
        inbox = RawInbox(2)
        inbox.activate(1)
        data = bytearray(payload(0))
        inbox.put(1, data, 0)
        data[0] = 0
        inbox.put(1, payload(1), 0)
        inbox.put(1, payload(2), 0)
        self.assertEqual(inbox.dropped, 1)
        self.assertEqual((await inbox.get()).payload, payload(1))
        inbox.activate(2)
        inbox.put(1, payload(3), 0)
        inbox.put(2, payload(4), 0)
        self.assertEqual((await inbox.get()).payload, payload(4))
        self.assertEqual(inbox.generation_dropped, 2)

    async def test_tracker_wrap_duplicate_old_and_new_boot(self):
        tracker = StreamTracker()
        for seq in [0xfffffffe, 0xffffffff, 0, 2, 2, 1]:
            tracker.observe(SensorPacket(1, 7, seq, 0, values(seq)))
        tracker.observe(SensorPacket(1, 8, 0, 0, values(0)))
        self.assertEqual((tracker.gaps, tracker.duplicates, tracker.out_of_order, tracker.new_boots),
                         (1, 1, 1, 1))

    async def test_stale_after_connect_is_dropped_without_write(self):
        clock = [0.0]
        async def connect():
            clock[0] = 3.0
            return object(), FakeWriter()
        bridge = Bridge(BridgeConfig(ca_file="unused"), connector=connect, clock=lambda: clock[0])
        bridge.inbox.activate(1)
        bridge.inbox.put(1, payload(), 0)
        await bridge.forward_one()
        self.assertEqual(bridge.metrics.stale_dropped, 1)
        self.assertEqual(bridge.metrics.sent, 0)
        await bridge.close_transport()

    async def test_invalid_ack_drops_ambiguous_item_and_closes_connection(self):
        bridge = Bridge(BridgeConfig(ca_file="unused"), connector=AsyncMock(return_value=(object(), FakeWriter())))
        bridge.inbox.activate(1)
        bridge.inbox.put(1, payload(), bridge.clock())
        bridge._write_frame = AsyncMock()
        bridge._read_frame = AsyncMock(return_value=dict(v=3, type="INGEST_ACK", session_id="ltc-comms",
            device_id=1, boot_id=7, seq=999, request_id=None, status="accepted"))
        await bridge.forward_one()
        self.assertEqual(bridge.metrics.ack_errors, 1)
        self.assertEqual(bridge.metrics.ambiguous_dropped, 1)
        self.assertEqual(bridge.inbox.size, 0)
        self.assertIsNone(bridge.writer)

    async def test_valid_ack_and_packets_that_are_not_version_3(self):
        writer = FakeWriter()
        connector = AsyncMock(return_value=(object(), writer))
        bridge = Bridge(BridgeConfig(ca_file="unused"), connector=connector)
        bridge.inbox.activate(1)
        bridge._write_frame = AsyncMock()
        bridge._read_frame = AsyncMock(return_value=dict(v=3, type="INGEST_ACK", session_id="ltc-comms",
            device_id=1, boot_id=7, seq=0, request_id=None, status="accepted"))
        bridge.inbox.put(1, payload(), bridge.clock())
        await bridge.forward_one()
        self.assertEqual(bridge.metrics.acked, 1)
        # A 32-byte packet from version-2 firmware, a 38-byte packet with version byte 2
        # and a truncated version-3 packet are malformed and never forwarded.
        old_firmware = struct.pack("<2sBBIII8h", b"LC", 2, 1, 7, 1, 100, *([0] * 8))
        wrong_version = bytearray(payload(1))
        wrong_version[2] = 2
        for raw in (old_firmware, bytes(wrong_version), payload(1)[:-1]):
            bridge.inbox.put(1, raw, bridge.clock())
            await bridge.forward_one()
        self.assertEqual(bridge.metrics.malformed, 3)
        self.assertEqual(bridge._write_frame.await_count, 1)
        await bridge.close_transport()

    async def test_ble_mtu_floor_is_the_38_byte_packet_plus_att_header(self):
        self.assertEqual(SENSOR_MTU_FLOOR, 41)
        for mtu in (40, 41):
            stop = asyncio.Event()
            bridge = Bridge(BridgeConfig(ca_file="unused", diagnostic_unprotected=True))

            class Scanner:
                @staticmethod
                async def find_device_by_filter(predicate, timeout):
                    return SimpleNamespace(address="AA:BB:CC:DD:EE:FF")

            class Client:
                mtu_size = mtu
                is_connected = False

                def __init__(self, _device, disconnected_callback):
                    sensor = SimpleNamespace(properties=["notify"])
                    service = SimpleNamespace(
                        get_characteristic=lambda uuid: sensor if uuid == SENSOR_UUID else None)
                    self.services = SimpleNamespace(get_service=lambda uuid: service)

                async def connect(self):
                    self.is_connected = True
                    stop.set()  # One attempt only.

                async def disconnect(self):
                    self.is_connected = False

            async def refuse(_client, *, source_rate):
                raise OSError("connection parameters are not part of this test")

            with patch("laptop.bridge.prefer_throughput", refuse, create=True):
                await asyncio.wait_for(bridge.ble_loop(scanner=Scanner, client_factory=Client,
                                                       stop_event=stop), 2)
            # MTU 40 fails at the floor; MTU 41 passes it and fails later, at the patched step.
            self.assertEqual(bridge.metrics.ble_errors, 1)
            self.assertEqual(bridge.negotiated_mtu, None if mtu == 40 else 41)

    async def test_disconnect_during_connect_discards_old_generation(self):
        bridge = None
        async def connect():
            bridge.inbox.activate(2)
            return object(), FakeWriter()
        bridge = Bridge(BridgeConfig(ca_file="unused"), connector=connect)
        bridge.inbox.activate(1)
        bridge.inbox.put(1, payload(), bridge.clock())
        await bridge.forward_one()
        self.assertEqual(bridge.metrics.generation_dropped, 1)
        self.assertEqual(bridge.metrics.sent, 0)
        await bridge.close_transport()


class FakeWriter:
    def close(self): pass
    async def wait_closed(self): pass


class TunnelTests(unittest.IsolatedAsyncioTestCase):
    def test_only_loopback_local_forward_strict_trust(self):
        cmd = tunnel_command("yanjie@stujump.comp.nus.edu.sg", "xilinx@board.example", 18888, 8888)
        self.assertIn("127.0.0.1:18888:127.0.0.1:8888", cmd)
        self.assertIn("StrictHostKeyChecking=yes", cmd)
        self.assertIn("ExitOnForwardFailure=yes", cmd)
        self.assertNotIn("-R", cmd)
        for port in [0, 65536, True]:
            with self.assertRaises(ValueError):
                tunnel_command("user@jump", "user@board", port, 8888)
        for host in ["-oProxyCommand=bad", "user@host with space"]:
            with self.assertRaises(ValueError):
                tunnel_command(host, "user@board", 18888, 8888)

    async def test_supervisor_terminates_only_owned_child(self):
        stop = asyncio.Event()
        class Process:
            returncode = None
            terminated = False
            async def wait(self):
                await stop.wait()
                return 0
            def terminate(self): self.terminated = True; self.returncode = 0
            def kill(self): raise AssertionError("unnecessary kill")
        proc = Process()
        async def spawn(*args, **kwargs):
            asyncio.get_running_loop().call_soon(stop.set)
            return proc
        await supervise(["ssh", "-N"], stop, spawn=spawn)
        self.assertTrue(proc.terminated)


class BleLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_matching_reconnect_and_old_callback_isolation(self):
        from types import SimpleNamespace
        from laptop.bridge import SERVICE_UUID, SENSOR_UUID
        bridge = Bridge(BridgeConfig(ca_file="unused", diagnostic_unprotected=True))
        second_ready = asyncio.Event()
        callbacks = []
        class Scanner:
            @staticmethod
            async def find_device_by_filter(predicate, timeout):
                device = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
                assert not predicate(device, SimpleNamespace(service_uuids=["wrong"]))
                assert predicate(device, SimpleNamespace(service_uuids=[SERVICE_UUID.upper()]))
                return device
        class Client:
            mtu_size = 517
            def __init__(self, device, disconnected_callback):
                self.callback = disconnected_callback
                self.number = len(callbacks)
                callbacks.append(disconnected_callback)
                self.is_connected = False
                characteristic = SimpleNamespace(properties=["notify"])
                self.services = SimpleNamespace(get_service=lambda uuid:
                    SimpleNamespace(get_characteristic=lambda u: characteristic if u == SENSOR_UUID else None)
                    if uuid == SERVICE_UUID else None)
            async def connect(self): self.is_connected = True
            async def start_notify(self, uuid, cb):
                cb(None, payload(self.number))
                if self.number == 0:
                    self.callback(self)
                else:
                    callbacks[0](self)  # old client's delayed native disconnect callback
                    second_ready.set()
            async def stop_notify(self, uuid): pass
            async def disconnect(self): self.is_connected = False
        worker = asyncio.create_task(bridge.ble_loop(scanner=Scanner, client_factory=Client))
        await asyncio.wait_for(second_ready.wait(), 2)
        await asyncio.sleep(0.02)
        self.assertEqual(bridge.metrics.ble_connections, 2)
        self.assertEqual(bridge.inbox.generation, 2)
        self.assertFalse(worker.done())
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError): await worker
        self.assertEqual(bridge.inbox.generation, 0)

    async def test_forward_lock_allows_only_one_inflight_frame(self):
        writer = FakeWriter()
        bridge = Bridge(BridgeConfig(ca_file="unused"), connector=AsyncMock(return_value=(object(), writer)))
        bridge.inbox.activate(1)
        for seq in range(3): bridge.inbox.put(1, payload(seq), bridge.clock())
        inflight = [0, 0]
        sent = []
        async def write(_writer, message, timeout):
            inflight[0] += 1
            inflight[1] = max(inflight)
            sent.append(message["seq"])
        async def read(_reader, timeout):
            await asyncio.sleep(0)
            inflight[0] -= 1
            return dict(v=3, type="INGEST_ACK", session_id="ltc-comms", device_id=1,
                        boot_id=7, seq=sent[-1], request_id=None, status="accepted")
        bridge._write_frame, bridge._read_frame = write, read
        await asyncio.gather(*(bridge.forward_one() for _ in range(3)))
        self.assertEqual(sent, [0, 1, 2])
        self.assertEqual(inflight[1], 1)
        self.assertEqual(bridge.metrics.acked, 3)
        await bridge.close_transport()

    async def test_native_cleanup_timeout_retains_child_then_consumes_completion(self):
        bridge = Bridge(BridgeConfig(ca_file="unused"))
        release = asyncio.Event()
        cancelled = asyncio.Event()
        async def native():
            try: await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                raise RuntimeError("late native result")
        with self.assertRaises(asyncio.TimeoutError):
            await bridge._bounded(native(), 0.02)
        await asyncio.wait_for(cancelled.wait(), 1)
        self.assertEqual(len(bridge._retained), 1)
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
            if not bridge._retained: break
        self.assertFalse(bridge._retained)


class ReviewRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_tls_close_aborts_transport(self):
        bridge = Bridge(BridgeConfig(ca_file="unused"))
        class Writer:
            aborted = False
            def __init__(self): self.transport = self
            def close(self): pass
            def abort(self): self.aborted = True
            async def wait_closed(self): await asyncio.Event().wait()
        writer = Writer()
        bridge.writer = writer
        await bridge.close_transport()
        self.assertTrue(writer.aborted)
        self.assertEqual(bridge.metrics.cleanup_errors, 1)

    async def test_cancellation_resistant_native_tasks_are_capped(self):
        bridge = Bridge(BridgeConfig(ca_file="unused"))
        release = asyncio.Event()
        async def native():
            try: await asyncio.Event().wait()
            except asyncio.CancelledError: await release.wait()
        for _ in range(2):
            with self.assertRaises(asyncio.TimeoutError):
                await bridge._bounded(native(), 0.01)
        try:
            with self.assertRaises(RuntimeError):
                await bridge._bounded(native(), 0.01)
            self.assertEqual(len(bridge._retained), 2)
        finally:
            release.set()
            for _ in range(10): await asyncio.sleep(0)

    async def test_stale_item_does_not_reset_failed_transport_backoff(self):
        from unittest.mock import patch
        bridge = Bridge(BridgeConfig(ca_file="unused"))
        attempts = [0]
        sleeps = []
        async def forward():
            attempts[0] += 1
            if attempts[0] in (1, 3): bridge.metrics.transport_errors += 1
            if attempts[0] == 4: raise asyncio.CancelledError()
        async def sleep(delay): sleeps.append(delay)
        bridge.forward_one = forward
        with patch("laptop.bridge.asyncio.sleep", sleep):
            with self.assertRaises(asyncio.CancelledError): await bridge.writer_loop()
        self.assertEqual(sleeps, [0.5, 1.0])
