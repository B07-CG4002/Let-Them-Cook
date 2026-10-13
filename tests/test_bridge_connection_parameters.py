"""Connection preference lifetime stays inside the physical BLE attempt."""
import asyncio
import struct
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from common.control import CONTROL_UUID, RESPONSE_UUID, ControlFrame, decode_control, encode_control
from laptop.bridge import Bridge, BridgeConfig, SENSOR_UUID, SERVICE_UUID
from laptop.source_audit import SOURCE_STATS_UUID


async def capture(mode="normal", cleanup_error=False):
    order, evidence = [], []
    stop = asyncio.Event()
    bridge = Bridge(BridgeConfig(ca_file="unused", expected_device_id=1,
        source_audit=True, controls_enabled=True, source_rate=25),
        evidence=SimpleNamespace(record=lambda kind, **fields: evidence.append((kind, fields))))

    class Preference:
        closed = 0
        interval = 60.0

        def report(self):
            return {"requested": True, "request_status": "SUCCESS", "request_accepted": True,
                    "actual_interval_ms": self.interval, "released": bool(self.closed),
                    "cleanup_errors": ["device:OSError"] if cleanup_error and self.closed else []}

        def refresh(self):
            order.append("refresh")
            self.interval = 30.0

        def close(self):
            order.append("preference_close")
            self.closed += 1

    preference = Preference()

    class Scanner:
        @staticmethod
        async def find_device_by_filter(predicate, timeout):
            device = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
            assert predicate(device, SimpleNamespace(service_uuids=[SERVICE_UUID]))
            return device

    class Client:
        mtu_size = 517
        is_connected = False

        def __init__(self, _device, disconnected_callback):
            properties = {SENSOR_UUID: ["notify"], SOURCE_STATS_UUID: ["read"],
                          CONTROL_UUID: ["write"], RESPONSE_UUID: ["notify"]}
            service = SimpleNamespace(get_characteristic=lambda uuid:
                SimpleNamespace(properties=properties[uuid]) if uuid in properties else None)
            self.services = SimpleNamespace(get_service=lambda uuid: service)

        async def connect(self):
            self.is_connected = True
            order.append("connect")

        async def read_gatt_char(self, uuid):
            order.append("source_read")
            if mode == "source_error":
                stop.set()
                raise OSError("synthetic source read error")
            return struct.pack("<4sB3xIIII", b"W7S1", 1, 7, 0, 0, 0)

        async def start_notify(self, uuid, callback):
            order.append("response_subscribe" if uuid == RESPONSE_UUID else "sensor_subscribe")
            if uuid == RESPONSE_UUID:
                self.response = callback

        async def write_gatt_char(self, uuid, data, response):
            request = decode_control(data, mtu=self.mtu_size)
            self.response(None, encode_control(ControlFrame(request.opcode | 128, 1,
                request.request_id, payload=request.payload), mtu=self.mtu_size))

        async def stop_notify(self, uuid):
            order.append("stop_notify")

        async def disconnect(self):
            order.append("disconnect")
            self.is_connected = False
            if mode == "disconnect_error":
                raise OSError("synthetic disconnect error")

    async def authenticated(_client):
        order.append("authenticated")

    async def prefer(_client, *, source_rate):
        assert source_rate == 25
        order.append("prefer")
        return preference

    with patch("laptop.windows_pairing.require_authenticated_bond", authenticated), \
            patch("laptop.bridge.prefer_throughput", prefer, create=True):
        worker = asyncio.create_task(bridge.ble_loop(scanner=Scanner, client_factory=Client,
                                                    stop_event=stop))
        try:
            if mode != "source_error":
                await asyncio.wait_for(bridge.active.wait(), 1)
                if mode == "cancel":
                    worker.cancel()
                else:
                    stop.set()
            if mode == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(worker, 1)
            else:
                await asyncio.wait_for(worker, 1)
        finally:
            if not worker.done():
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    return bridge, preference, order, evidence


def test_preference_follows_authentication_precedes_sources_and_reports_observed_parameters():
    async def run():
        bridge, preference, order, evidence = await capture()
        assert preference.closed == 1
        assert order.index("authenticated") < order.index("prefer") < order.index("source_read")
        assert order.index("prefer") < order.index("response_subscribe")
        assert order.index("refresh") < order.index("disconnect") < order.index("preference_close")
        report = bridge.summary()["configuration"]["connection_parameters"]
        assert report["request_accepted"] is True
        assert report["actual_interval_ms"] == 30.0
        assert report["released"] is True
        parameter_events = [fields for kind, fields in evidence if kind == "BLE_connection_parameters"]
        assert len(parameter_events) == 1
        assert parameter_events[0]["request_accepted"] is True
        assert parameter_events[0]["actual_interval_ms"] == 60.0
        assert bridge.metrics.cleanup_errors == 0
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["cancel", "source_error", "disconnect_error"])
def test_preference_is_released_after_disconnect_on_cancellation_or_failure(mode):
    async def run():
        bridge, preference, order, _ = await capture(mode)
        assert preference.closed == 1
        assert order.index("refresh") < order.index("disconnect") < order.index("preference_close")
        assert bridge.summary()["configuration"]["connection_parameters"]["released"] is True
    asyncio.run(run())


def test_preference_cleanup_failure_is_not_reported_as_clean():
    async def run():
        bridge, preference, _, _ = await capture(cleanup_error=True)
        assert preference.closed == 1
        assert bridge.metrics.cleanup_errors == 1
        assert bridge.summary()["configuration"]["connection_parameters"]["cleanup_errors"] == ["device:OSError"]
    asyncio.run(run())
