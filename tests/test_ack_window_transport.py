"""Real TLS coverage for the bounded ACK window under propagation latency."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import suppress

import pytest

from common.sensor import SensorPacket, dummy_values, encode_packet
from common.tls import TLS_SERVER_NAME, client_context, server_context
from common.wire import read_frame, write_frame
from laptop.bridge import Bridge, BridgeConfig
from laptop.source_audit import SourceStats
from ultra96.server import CommsServer


class DelayedDownstreamProxy:
    """Forward raw TLS immediately upstream and delay downstream bytes in FIFO."""

    def __init__(self, target_port: int, delay_seconds: float) -> None:
        self.target_port = target_port
        self.delay_seconds = delay_seconds
        self.port = 0
        self._server = None
        self._handlers: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _copy(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        while payload := await reader.read(65536):
            writer.write(payload)
            await writer.drain()

    async def _delayed_copy(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        queue: asyncio.Queue[tuple[float, bytes] | None] = asyncio.Queue(maxsize=64)

        async def receive() -> None:
            while payload := await reader.read(65536):
                await queue.put((
                    asyncio.get_running_loop().time() + self.delay_seconds,
                    payload,
                ))
            await queue.put(None)

        async def send() -> None:
            while True:
                item = await queue.get()
                if item is None:
                    return
                due_at, payload = item
                while True:
                    remaining = due_at - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(remaining)
                writer.write(payload)
                await writer.drain()

        await asyncio.gather(receive(), send())

    async def _handle(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        self._handlers.add(task)
        upstream_writer = None
        self._writers.add(client_writer)
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", self.target_port
            )
            self._writers.add(upstream_writer)
            workers = {
                asyncio.create_task(self._copy(client_reader, upstream_writer)),
                asyncio.create_task(self._delayed_copy(upstream_reader, client_writer)),
            }
            done, pending = await asyncio.wait(
                workers, return_when=asyncio.FIRST_COMPLETED
            )
            for child in pending:
                child.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            for child in done:
                if not child.cancelled():
                    error = child.exception()
                    if error is not None:
                        raise error
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            for writer in (client_writer, upstream_writer):
                if writer is not None:
                    self._writers.discard(writer)
                    writer.close()
                    with suppress(Exception):
                        await writer.wait_closed()
            self._handlers.discard(task)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for writer in tuple(self._writers):
            writer.close()
        handlers = tuple(self._handlers)
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)


@pytest.fixture(scope="module")
def latency_pki(tmp_path_factory):
    from tools.generate_comms_pki import generate_pki

    folder = tmp_path_factory.mktemp("ack-window-pki")
    generate_pki(folder)
    return folder


async def _subscribe(pki, server: CommsServer):
    reader, writer = await asyncio.open_connection(
        "127.0.0.1",
        server.gateway_port,
        ssl=client_context(pki / "ca-cert.pem"),
        server_hostname=TLS_SERVER_NAME,
    )
    await write_frame(
        writer, {"v": 1, "type": "SUBSCRIBE", "session_id": "ltc-comms"}
    )
    assert await read_frame(reader) == {
        "v": 1,
        "type": "SUBSCRIBED",
        "session_id": "ltc-comms",
    }
    return reader, writer


def _bridge(pki, proxy_port: int, device_id: int) -> Bridge:
    return Bridge(BridgeConfig(
        ca_file=str(pki / "ca-cert.pem"),
        port=proxy_port,
        queue_capacity=64,
        freshness=2.0,
        io_timeout=3.0,
        expected_device_id=device_id,
        source_audit=True,
        ack_window=4,
    ))


async def _stop_bridge(bridge: Bridge, task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await bridge.close_transport()


def test_real_tls_server_processes_window_ahead_of_delayed_acks_and_results_exactly(
    latency_pki,
) -> None:
    async def scenario() -> tuple[int, dict, list[dict], dict]:
        server = CommsServer(
            server_context(
                latency_pki / "server-cert.pem", latency_pki / "server-key.pem"
            ),
            ingest_port=0,
            gateway_port=0,
        )
        await server.start()
        proxy = DelayedDownstreamProxy(server.ingest_port, delay_seconds=0.35)
        await proxy.start()
        phone_reader, phone_writer = await _subscribe(latency_pki, server)
        bridges = {device: _bridge(latency_pki, proxy.port, device) for device in (1, 2)}
        writers = {
            device: asyncio.create_task(bridge.writer_loop())
            for device, bridge in bridges.items()
        }
        async def collect_results() -> list[dict]:
            return [await read_frame(phone_reader) for _ in range(12)]

        results_task = asyncio.create_task(collect_results())
        maximum_server_lead = 0
        try:
            for device, bridge in bridges.items():
                bridge.inbox.activate(1)
                bridge.source_audit.start(SourceStats(device, 1000 + device, 0, 0, 0))
            loop = asyncio.get_running_loop()
            started = loop.time()
            for seq in range(6):
                remaining = started + seq * 0.1 - loop.time()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                for device, bridge in bridges.items():
                    packet = SensorPacket(
                        device, 1000 + device, seq, seq * 100, dummy_values(seq)
                    )
                    assert bridge.enqueue(1, encode_packet(packet))
                maximum_server_lead = max(
                    maximum_server_lead,
                    server.metrics["accepted"]
                    - sum(bridge.metrics.acked for bridge in bridges.values()),
                )
            for device, bridge in bridges.items():
                bridge.inbox.deactivate(preserve=True)
                bridge.source_audit.finish(SourceStats(device, 1000 + device, 6, 6, 0))

            async def drain() -> None:
                nonlocal maximum_server_lead
                while any(
                    bridge.metrics.acked < 6 or bridge.inbox.size or bridge._inflight
                    for bridge in bridges.values()
                ):
                    maximum_server_lead = max(
                        maximum_server_lead,
                        server.metrics["accepted"]
                        - sum(bridge.metrics.acked for bridge in bridges.values()),
                    )
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(drain(), timeout=6.0)
            results = await asyncio.wait_for(results_task, timeout=2.0)
            return (
                maximum_server_lead,
                {device: bridge.summary() for device, bridge in bridges.items()},
                results,
                dict(server.metrics),
            )
        finally:
            if not results_task.done():
                results_task.cancel()
                await asyncio.gather(results_task, return_exceptions=True)
            await asyncio.gather(*(
                _stop_bridge(bridges[device], writers[device]) for device in (1, 2)
            ))
            phone_writer.close()
            with suppress(Exception):
                await phone_writer.wait_closed()
            await proxy.close()
            await server.close()

    lead, summaries, results, metrics = asyncio.run(
        asyncio.wait_for(scenario(), timeout=10.0)
    )
    assert lead >= 4
    assert metrics["accepted"] == 12
    assert metrics["duplicates"] == metrics["rejected"] == 0
    for device in (1, 2):
        summary = summaries[device]
        assert summary["received"] == summary["sent"] == summary["acked"] == 6
        assert summary["stale_dropped"] == summary["ambiguous_dropped"] == 0
        assert summary["ack_errors"] == summary["transport_errors"] == 0
        assert summary["source"]["clean"] is True
    sequences = defaultdict(list)
    for result in results:
        sequences[result["device_id"]].append(result["seq"])
        assert result["result_id"] == (
            f'{result["device_id"]}:{result["boot_id"]}:{result["seq"]}'
        )
    assert dict(sequences) == {1: list(range(6)), 2: list(range(6))}
