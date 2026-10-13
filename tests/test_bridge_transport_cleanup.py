"""TLS teardown must abort failed transports without swallowing cancellation."""
import asyncio
import logging

import pytest

from common.sensor import SensorPacket, dummy_values, encode_packet
from laptop.bridge import Bridge, BridgeConfig


class Writer:
    def __init__(self, failure=None):
        self.transport = self
        self.failure = failure
        self.started = asyncio.Event()
        self.closed = False
        self.aborted = False

    def close(self):
        self.closed = True

    def abort(self):
        self.aborted = True

    async def wait_closed(self):
        self.started.set()
        if self.failure is not None:
            raise self.failure
        await asyncio.Event().wait()


def test_cancelled_tls_close_aborts_and_propagates(caplog):
    async def check():
        bridge = Bridge(BridgeConfig(ca_file="unused"))
        writer = Writer()
        bridge.reader, bridge.writer = object(), writer
        task = asyncio.create_task(bridge.close_transport())
        await writer.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert writer.closed and writer.aborted
        assert bridge.reader is None and bridge.writer is None
        assert bridge.metrics.cleanup_errors == 1
        assert not any(not child.done() for child in bridge._retained)
    with caplog.at_level(logging.WARNING, logger="laptop.bridge"):
        asyncio.run(check())
    assert "operation=close_transport" in caplog.text
    assert "error_type=CancelledError" in caplog.text


@pytest.mark.parametrize("failure", [ConnectionResetError("private transport detail"),
                                    asyncio.TimeoutError("private transport detail")])
def test_reset_or_timeout_aborts_clears_refs_and_logs_type_only(failure, caplog):
    async def check():
        bridge = Bridge(BridgeConfig(ca_file="unused"))
        writer = Writer(failure)
        bridge.reader, bridge.writer = object(), writer
        await bridge.close_transport()
        await asyncio.sleep(0)
        assert writer.closed and writer.aborted
        assert bridge.reader is None and bridge.writer is None
        assert bridge.metrics.cleanup_errors == 1
        assert not any(not child.done() for child in bridge._retained)
    with caplog.at_level(logging.WARNING, logger="laptop.bridge"):
        asyncio.run(check())
    assert "operation=close_transport" in caplog.text
    assert "error_type=" + type(failure).__name__ in caplog.text
    assert "private transport detail" not in caplog.text


def test_writer_cancelled_during_fault_cleanup_stops_without_second_cancel():
    async def check():
        bridge = Bridge(BridgeConfig(ca_file="unused"))
        writer = Writer()
        bridge.writer = writer
        async def failed_write(*args, **kwargs):
            raise ConnectionResetError("synthetic failed tunnel")
        bridge._write_frame = failed_write
        bridge.inbox.activate(1)
        packet = SensorPacket(1, 7, 0, 0, dummy_values(0))
        bridge.inbox.put(1, encode_packet(packet), bridge.clock())
        worker = asyncio.create_task(bridge.writer_loop())
        try:
            await writer.started.wait()
            worker.cancel()
            await asyncio.wait((worker,), timeout=0.2)
            assert worker.cancelled(), "writer swallowed cancellation and continued after TLS cleanup"
            assert writer.aborted
            assert bridge.metrics.transport_errors == 1
            assert bridge.metrics.cleanup_errors == 1
        finally:
            if not worker.done():
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(check())
