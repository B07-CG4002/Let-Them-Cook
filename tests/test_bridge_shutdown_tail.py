"""Keep queued native notifications observable across the remote CCCD stop."""
import asyncio
from collections import deque
import struct
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from common.sensor import SensorPacket, encode_packet
from laptop.bridge import Bridge, BridgeConfig, SENSOR_UUID, SERVICE_UUID
from laptop.source_audit import SOURCE_STATS_UUID


async def capture_tail(mode="delayed"):
    events, order, messages = [], [], deque()
    stopped, release_native = asyncio.Event(), asyncio.Event()
    bridge = Bridge(BridgeConfig(ca_file="unused", diagnostic_unprotected=True,
        expected_device_id=1, source_audit=True,
        connect_timeout=0.1 if mode == "budget" else 20), evidence=SimpleNamespace(
            record=lambda kind, **fields: events.append((kind, fields))))

    class Writer:
        def close(self): pass
        async def wait_closed(self): pass

    async def connect(): return object(), Writer()
    async def write(_writer, message, timeout): messages.append(message)
    async def read(_reader, timeout):
        message = messages.popleft()
        return {key: value for key, value in message.items() if key not in ("uptime_ms", "values")} | {
            "type": "INGEST_ACK", "status": "accepted"}
    bridge._connector, bridge._write_frame, bridge._read_frame = connect, write, read

    class Scanner:
        @staticmethod
        async def find_device_by_filter(predicate, timeout):
            return SimpleNamespace(address="AA:BB")

    class Client:
        mtu_size = 517
        is_connected = False
        callback = None
        tail_task = None
        reads = 0
        remote_pending = False

        def __init__(self, _device, disconnected_callback):
            sensor = SimpleNamespace(properties=["notify"])
            source = SimpleNamespace(properties=["read"])
            service = SimpleNamespace(get_characteristic=lambda uuid:
                sensor if uuid == SENSOR_UUID else source if uuid == SOURCE_STATS_UUID else None)
            self.services = SimpleNamespace(get_service=lambda uuid: service)

        async def connect(self): self.is_connected = True

        async def read_gatt_char(self, uuid, **kwargs):
            self.reads += 1
            order.append("source_read")
            if self.reads == 1:
                seq, boot = 0, 7
            else:
                if mode == "budget":
                    await asyncio.sleep(0.06)
                    order.append("barrier_read_complete" if self.reads == 2 else "final_read_complete")
                seq = 3 if mode == "drift" and self.reads >= 3 else 2
                boot = 8 if mode == "wrong_boot" else 7
                if "remote_none" in order:
                    assert kwargs.get("use_cached") is False
            return struct.pack("<4sB3xIIII", b"W7S1", 1, boot, seq, seq, 0)

        async def start_notify(self, uuid, callback):
            self.callback = callback
            callback(None, encode_packet(SensorPacket(1, 7, 0, 0, (0,) * 8, version=2)))

        async def stop_notify(self, uuid):
            assert not self.remote_pending, "a pending native CCCD stop must not be retried"
            order.append("stop_notify")
            self.callback = None

        async def disconnect(self):
            order.append("disconnect")
            self.is_connected = False

    client = Client(None, None)

    async def disable_remote(_characteristic):
        order.append("remote_none")
        if mode == "rejected":
            raise OSError("CCCD rejected")
        if mode == "resistant":
            client.remote_pending = True
            try:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release_native.wait()
            finally:
                client.remote_pending = False
            return True
        async def delayed_tail():
            await asyncio.sleep(0 if mode == "budget" else 0.05)
            if client.callback is not None and mode != "lost":
                order.append("tail_delivered")
                client.callback(None, encode_packet(SensorPacket(1, 7, 1, 10, (0,) * 8, version=2)))
        client.tail_task = asyncio.create_task(delayed_tail())
        return True

    writer = asyncio.create_task(bridge.writer_loop())
    with patch("laptop.bridge.disable_notifications_remotely", disable_remote, create=True):
        worker = asyncio.create_task(bridge.ble_loop(scanner=Scanner,
            client_factory=lambda *_args, **_kwargs: client, stop_event=stopped))
        try:
            await asyncio.wait_for(bridge.active.wait(), 1)
            if mode == "capacity":
                for _ in range(2):
                    bridge._retained.add(asyncio.create_task(release_native.wait()))
            stopped.set()
            await asyncio.wait_for(worker, 2)
            deadline = asyncio.get_running_loop().time() + 0.2
            while bridge.inbox.size and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0)
        finally:
            release_native.set()
            if not worker.done(): worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            if client.tail_task is not None:
                await client.tail_task
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            await asyncio.gather(*bridge._retained, return_exceptions=True)
            await bridge.close_transport()
    return bridge, events, order


def test_shutdown_delivers_late_callback_before_handler_removal_and_final_source_read():
    async def run():
        bridge, events, order = await capture_tail()
        source = bridge.summary()["source"]
        assert source["clean"] is True, source
        assert source["generated"] == source["received"] == source["acked"] == 2
        assert order.index("remote_none") < order.index("tail_delivered") < order.index("stop_notify")
        assert order[-2:] == ["source_read", "disconnect"]
        assert any(kind == "source_tail_drain" and fields["validation"] == "complete"
                   for kind, fields in events)
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["lost", "rejected", "wrong_boot", "drift"])
def test_shutdown_cannot_hide_missing_tail_rejected_cccd_or_source_boundary_change(mode):
    async def run():
        bridge, events, order = await capture_tail(mode)
        assert bridge.summary()["source"]["clean"] is False
        assert bridge.metrics.source_stats_errors > 0
        assert "stop_notify" in order and order[-1] == "disconnect"
        assert any(kind in ("source_quiesce_failed", "source_final_failed") for kind, _ in events)
    asyncio.run(run())


def test_cancellation_resistant_remote_stop_is_not_retried_and_shutdown_remains_bounded():
    async def run():
        bridge, events, order = await capture_tail("resistant")
        assert order.count("remote_none") == 1
        assert "stop_notify" not in order
        assert order[-1] == "disconnect"
        assert bridge.summary()["source"]["clean"] is False
        assert any(kind == "source_quiesce_failed" for kind, _ in events)
    asyncio.run(run())


def test_native_operation_capacity_rejects_remote_stop_before_it_is_started():
    async def run():
        bridge, events, order = await capture_tail("capacity")
        assert "remote_none" not in order
        assert bridge.metrics.source_stats_errors > 0
    asyncio.run(run())


def test_quiesced_and_final_source_reads_share_one_cleanup_deadline():
    async def run():
        bridge, events, order = await capture_tail("budget")
        assert "barrier_read_complete" in order
        assert "final_read_complete" not in order
        assert order[-1] == "disconnect"
        assert bridge.source_audit.end_stats is None
        assert bridge.metrics.source_stats_errors == 1
    asyncio.run(run())
