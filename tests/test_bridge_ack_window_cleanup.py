import asyncio
from contextlib import suppress

from common.sensor import SensorPacket, dummy_values, encode_packet
from laptop.bridge import Bridge, BridgeConfig


def test_cancel_during_transport_close_still_retires_every_attempted_frame():
    async def check():
        close_entered = asyncio.Event()
        release_close = asyncio.Event()
        all_written = asyncio.Event()
        messages = []

        class Writer:
            def close(self):
                pass

            def abort(self):
                pass

            transport = None

            async def wait_closed(self):
                close_entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release_close.wait()
                    raise

        writer = Writer()
        writer.transport = writer

        async def connect():
            return object(), writer

        async def write(_writer, message, timeout):
            messages.append(message)
            if len(messages) == 3:
                all_written.set()

        async def mismatched_ack(_reader, timeout):
            await all_written.wait()
            first = messages[0]
            return dict(v=1, type="INGEST_ACK", session_id=first["session_id"],
                        device_id=first["device_id"], boot_id=first["boot_id"],
                        seq=999, status="accepted")

        bridge = Bridge(BridgeConfig(ca_file="unused", ack_window=3),
                        connector=connect)
        bridge._write_frame = write
        bridge._read_frame = mismatched_ack
        bridge.inbox.activate(1)
        for seq in range(3):
            bridge.enqueue(1, encode_packet(SensorPacket(
                1, 7, seq, seq * 100, dummy_values(seq))))

        owner = asyncio.create_task(bridge.writer_loop())
        await asyncio.wait_for(close_entered.wait(), 1)
        owner.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(owner, 1)

        assert bridge.metrics.sent == 3
        assert bridge.metrics.ambiguous_dropped == 3
        assert bridge._inflight == 0
        assert bridge.metrics.cleanup_errors >= 1

        release_close.set()
        for _ in range(20):
            if not bridge._retained:
                break
            await asyncio.sleep(0)

    asyncio.run(check())
