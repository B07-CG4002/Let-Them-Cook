"""Regression for fixed network ACK latency at the 10 Hz source rate."""
from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from common.sensor import SensorPacket, dummy_values, encode_packet
from laptop.bridge import Bridge, BridgeConfig
from laptop.source_audit import SourceStats


DEVICE_ID = 1
BOOT_ID = 0x12345678
PACKET_COUNT = 8
SOURCE_INTERVAL_SECONDS = 0.1
ACK_DELAY_SECONDS = 0.5
FRESHNESS_SECONDS = 2.0
ACK_WINDOW = 16


class _Reader:
    def __init__(self, epoch: int) -> None:
        self.epoch = epoch


class _Writer:
    def __init__(self, epoch: int) -> None:
        self.epoch = epoch

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class DelayedAckTransport:
    """Create ACKs immediately, then delay each delivery independently.

    Delivery timers overlap when the bridge pipelines writes. The FIFO models
    an ordered network path and does not serialize or delay packet processing.
    """

    def __init__(
        self,
        delay_seconds: float,
        *,
        delivery_gate: asyncio.Event | None = None,
        gated_seq: int | None = None,
    ) -> None:
        self.delay_seconds = delay_seconds
        self.delivery_gate = delivery_gate
        self.gated_seq = gated_seq
        self.sent: list[dict] = []
        self.sent_epochs: list[int] = []
        self.acked: list[dict] = []
        self.ack_latencies: list[float] = []
        self.outstanding = 0
        self.max_outstanding = 0
        self.connection_count = 0
        self._acks: dict[int, asyncio.Queue[tuple[dict, float]]] = {}
        self._sent_per_epoch: dict[int, int] = {}
        self._delivery_tail: dict[int, asyncio.Task | None] = {}
        self._sent_changed = asyncio.Event()
        self._deliveries: set[asyncio.Task] = set()

    async def connect(self) -> tuple[_Reader, _Writer]:
        epoch = self.connection_count
        self.connection_count += 1
        self._acks[epoch] = asyncio.Queue()
        self._sent_per_epoch[epoch] = 0
        self._delivery_tail[epoch] = None
        return _Reader(epoch), _Writer(epoch)

    async def write(self, writer: _Writer, message: dict, timeout: float) -> None:
        assert timeout > 0
        copied = dict(message)
        self.sent.append(copied)
        self.sent_epochs.append(writer.epoch)
        self._sent_per_epoch[writer.epoch] += 1
        self._sent_changed.set()
        self.outstanding += 1
        self.max_outstanding = max(self.max_outstanding, self.outstanding)
        written_at = asyncio.get_running_loop().time()
        acknowledgement = {
            "v": 1,
            "type": "INGEST_ACK",
            "session_id": copied["session_id"],
            "device_id": copied["device_id"],
            "boot_id": copied["boot_id"],
            "seq": copied["seq"],
            "status": "accepted",
        }

        previous_delivery = self._delivery_tail[writer.epoch]

        async def deliver() -> None:
            due_at = written_at + self.delay_seconds
            while True:
                remaining = due_at - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
            if self.delivery_gate is not None and (
                self.gated_seq is None or copied["seq"] == self.gated_seq
            ):
                await self.delivery_gate.wait()
            if previous_delivery is not None:
                await previous_delivery
            await self._acks[writer.epoch].put((acknowledgement, written_at))

        task = asyncio.create_task(deliver())
        self._delivery_tail[writer.epoch] = task
        self._deliveries.add(task)
        task.add_done_callback(self._deliveries.discard)

    async def read(self, reader: _Reader, timeout: float) -> dict:
        acknowledgement, written_at = await asyncio.wait_for(
            self._acks[reader.epoch].get(), timeout=timeout
        )
        self.outstanding -= 1
        self.acked.append(acknowledgement)
        self.ack_latencies.append(asyncio.get_running_loop().time() - written_at)
        return acknowledgement

    async def wait_for_epoch_writes(
        self, epoch: int, count: int, *, timeout: float = 2.0
    ) -> None:
        async def wait() -> None:
            while self._sent_per_epoch.get(epoch, 0) < count:
                self._sent_changed.clear()
                if self._sent_per_epoch.get(epoch, 0) < count:
                    await self._sent_changed.wait()

        await asyncio.wait_for(wait(), timeout=timeout)

    async def aclose(self) -> None:
        tasks = tuple(self._deliveries)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class FaultingAckTransport(DelayedAckTransport):
    """Fail the first epoch only after the sender has filled its window."""

    def __init__(self, failure: str, *, window: int) -> None:
        super().__init__(0.02)
        assert failure in {"mismatch", "eof"}
        self.failure = failure
        self.window = window
        self.failed = False

    async def read(self, reader: _Reader, timeout: float) -> dict:
        if reader.epoch == 0 and not self.failed:
            await self.wait_for_epoch_writes(reader.epoch, self.window)
            self.failed = True
            if self.failure == "eof":
                raise asyncio.IncompleteReadError(partial=b"", expected=4)
            acknowledgement = await super().read(reader, timeout)
            return dict(acknowledgement, seq=acknowledgement["seq"] + 1000)
        return await super().read(reader, timeout)


class _CancellationResistantWriter(_Writer):
    def __init__(
        self, epoch: int, close_started: asyncio.Event, release_close: asyncio.Event
    ) -> None:
        super().__init__(epoch)
        self.close_started = close_started
        self.release_close = release_close
        self.transport = self
        self.aborted = False

    async def wait_closed(self) -> None:
        self.close_started.set()
        try:
            await self.release_close.wait()
        except asyncio.CancelledError:
            await self.release_close.wait()

    def abort(self) -> None:
        self.aborted = True


class RetirementCancellationTransport(FaultingAckTransport):
    def __init__(self, *, window: int) -> None:
        super().__init__("mismatch", window=window)
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def connect(self) -> tuple[_Reader, _CancellationResistantWriter]:
        reader, _ = await super().connect()
        return reader, _CancellationResistantWriter(
            reader.epoch, self.close_started, self.release_close
        )


def _window_config(window: int, *, device_id: int = DEVICE_ID) -> BridgeConfig:
    return BridgeConfig(
        ca_file="unused",
        queue_capacity=64,
        freshness=FRESHNESS_SECONDS,
        diagnostic_unprotected=True,
        expected_device_id=device_id,
        source_audit=True,
        ack_window=window,
    )


def _start_source(bridge: Bridge, *, boot_id: int = BOOT_ID) -> None:
    bridge.inbox.activate(1)
    bridge.source_audit.start(SourceStats(
        bridge.config.expected_device_id, boot_id, 0, 0, 0
    ))


def _finish_source(bridge: Bridge, *, count: int, boot_id: int = BOOT_ID) -> None:
    bridge.inbox.deactivate(preserve=True)
    bridge.source_audit.finish(SourceStats(
        bridge.config.expected_device_id, boot_id, count, count, 0
    ))


def _enqueue(bridge: Bridge, count: int, *, boot_id: int = BOOT_ID) -> None:
    device_id = bridge.config.expected_device_id
    for seq in range(count):
        packet = SensorPacket(device_id, boot_id, seq, seq * 100, dummy_values(seq))
        assert bridge.enqueue(1, encode_packet(packet))


async def _wait_until_drained(
    bridge: Bridge, expected_received: int = PACKET_COUNT, *, timeout: float = 7.0
) -> None:
    async def wait() -> None:
        while (bridge.metrics.received < expected_received
               or bridge.inbox.size
               or bridge._inflight):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout=timeout)


async def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout=timeout)


async def _stop_writer(
    bridge: Bridge, writer: asyncio.Task, transport: DelayedAckTransport
) -> None:
    writer.cancel()
    result = await asyncio.wait_for(
        asyncio.gather(writer, return_exceptions=True), timeout=1.0
    )
    if result and isinstance(result[0], Exception):
        raise result[0]
    await bridge.close_transport()
    await transport.aclose()


async def _run_delayed_ack_case() -> tuple[Bridge, DelayedAckTransport, dict]:
    config = BridgeConfig(
        ca_file="unused",
        queue_capacity=64,
        freshness=FRESHNESS_SECONDS,
        diagnostic_unprotected=True,
        expected_device_id=DEVICE_ID,
        source_audit=True,
    )
    # Existing Bridge ignores this attribute. The regression can turn green
    # once the production dataclass adopts the agreed bounded-window API.
    config.ack_window = ACK_WINDOW
    transport = DelayedAckTransport(ACK_DELAY_SECONDS)
    bridge = Bridge(config, connector=transport.connect)
    bridge._write_frame = transport.write
    bridge._read_frame = transport.read
    bridge.inbox.activate(1)
    bridge.source_audit.start(SourceStats(DEVICE_ID, BOOT_ID, 0, 0, 0))
    writer = asyncio.create_task(bridge.writer_loop())
    summary = None
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        for seq in range(PACKET_COUNT):
            remaining = started + seq * SOURCE_INTERVAL_SECONDS - loop.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
            packet = SensorPacket(
                DEVICE_ID,
                BOOT_ID,
                seq,
                seq * 100,
                dummy_values(seq),
            )
            assert bridge.enqueue(1, encode_packet(packet))

        bridge.inbox.deactivate(preserve=True)
        await _wait_until_drained(bridge)
        bridge.source_audit.finish(
            SourceStats(DEVICE_ID, BOOT_ID, PACKET_COUNT, PACKET_COUNT, 0)
        )
        summary = bridge.summary()
    finally:
        writer.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(writer, timeout=1.0)
        await bridge.close_transport()
        await transport.aclose()
    assert summary is not None
    return bridge, transport, summary


def test_fixed_ack_latency_is_overlapped_without_expiring_10hz_packets() -> None:
    bridge, transport, summary = asyncio.run(_run_delayed_ack_case())
    source = summary["source"]

    assert bridge.callback_received == PACKET_COUNT
    assert summary["received"] == source["received"] == PACKET_COUNT

    # Current stop-and-wait turns RED here: queued packets cross the two-second
    # freshness boundary even though every scheduled network ACK is delivered.
    assert summary["stale_dropped"] == 0, summary

    assert summary["sent"] == summary["acked"] == PACKET_COUNT
    assert source["generated"] == source["source_submitted"] == PACKET_COUNT
    assert source["acked"] == PACKET_COUNT
    assert source["clean"] is True
    assert summary["queue_dropped"] == 0
    assert summary["ambiguous_dropped"] == 0
    assert summary["ack_errors"] == summary["transport_errors"] == 0
    assert [message["seq"] for message in transport.sent] == list(range(PACKET_COUNT))
    assert [message["seq"] for message in transport.acked] == list(range(PACKET_COUNT))
    assert ACK_WINDOW >= transport.max_outstanding >= 4
    assert len(transport.ack_latencies) == PACKET_COUNT
    assert all(
        ACK_DELAY_SECONDS <= latency < FRESHNESS_SECONDS
        for latency in transport.ack_latencies
    )


def test_window_never_exceeds_bound_and_retires_acks_in_fifo_order() -> None:
    async def scenario() -> tuple[dict, DelayedAckTransport]:
        count = 12
        window = 4
        transport = DelayedAckTransport(0.05)
        bridge = Bridge(_window_config(window), connector=transport.connect)
        bridge._write_frame = transport.write
        bridge._read_frame = transport.read
        _start_source(bridge)
        writer = asyncio.create_task(bridge.writer_loop())
        try:
            _enqueue(bridge, count)
            _finish_source(bridge, count=count)
            await _wait_until_drained(bridge, count, timeout=3.0)
            return bridge.summary(), transport
        finally:
            await _stop_writer(bridge, writer, transport)

    summary, transport = asyncio.run(scenario())
    assert transport.max_outstanding == 4
    assert [message["seq"] for message in transport.sent] == list(range(12))
    assert [message["seq"] for message in transport.acked] == list(range(12))
    assert summary["sent"] == summary["acked"] == 12
    assert summary["ambiguous_dropped"] == summary["stale_dropped"] == 0
    assert summary["source"]["clean"] is True


@pytest.mark.parametrize("failure", ["mismatch", "eof"])
def test_ack_failure_retires_whole_epoch_as_ambiguous_without_replay(
    failure: str,
) -> None:
    async def scenario() -> tuple[dict, FaultingAckTransport]:
        count = 6
        window = 4
        transport = FaultingAckTransport(failure, window=window)
        bridge = Bridge(_window_config(window), connector=transport.connect)
        bridge._write_frame = transport.write
        bridge._read_frame = transport.read
        _start_source(bridge)
        writer = asyncio.create_task(bridge.writer_loop())
        try:
            _enqueue(bridge, count)
            _finish_source(bridge, count=count)
            await _wait_until_drained(bridge, count, timeout=4.0)
            return bridge.summary(), transport
        finally:
            await _stop_writer(bridge, writer, transport)

    summary, transport = asyncio.run(scenario())
    assert transport.connection_count == 2
    assert [message["seq"] for message in transport.sent] == list(range(6))
    assert transport.sent_epochs == [0, 0, 0, 0, 1, 1]
    assert summary["received"] == summary["sent"] == 6
    assert summary["acked"] == 2
    assert summary["ambiguous_dropped"] == 4
    assert summary["ack_errors"] == summary["transport_errors"] == 1
    assert summary["stale_dropped"] == 0
    assert summary["source"]["received"] == 6
    assert summary["source"]["acked"] == 2
    assert summary["source"]["missing_acked"] == 4


def test_cancellation_during_epoch_close_still_retires_owned_frames_once() -> None:
    async def scenario() -> tuple[dict, tuple[int, ...], list[asyncio.Task]]:
        count = window = 4
        transport = RetirementCancellationTransport(window=window)
        bridge = Bridge(_window_config(window), connector=transport.connect)
        bridge._write_frame = transport.write
        bridge._read_frame = transport.read
        _start_source(bridge)
        owner = asyncio.create_task(bridge.writer_loop(), name="window-owner")
        _enqueue(bridge, count)
        _finish_source(bridge, count=count)
        try:
            await asyncio.wait_for(transport.close_started.wait(), timeout=2.0)
            owner.cancel()
            await asyncio.sleep(0)
            transport.release_close.set()
            await asyncio.wait_for(
                asyncio.gather(owner, return_exceptions=True), timeout=1.0
            )
            await _wait_for(lambda: bridge._inflight == 0, timeout=1.0)
            before = (
                bridge.metrics.acked,
                bridge.metrics.ambiguous_dropped,
                bridge.metrics.transport_errors,
                bridge.metrics.ack_errors,
            )
            await asyncio.sleep(0.05)
            late_workers = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and not task.done()
                and task.get_name() in {"pipeline-sender", "pipeline-ack-reader"}
            ]
            return bridge.summary(), before, late_workers
        finally:
            transport.release_close.set()
            if not owner.done():
                owner.cancel()
                await asyncio.gather(owner, return_exceptions=True)
            await bridge.close_transport()
            await transport.aclose()

    summary, stable_metrics, late_workers = asyncio.run(scenario())
    assert summary["received"] == summary["sent"] == 4
    assert summary["acked"] == 0
    assert summary["ambiguous_dropped"] == 4
    assert summary["ack_errors"] == 1
    assert summary["unfinished"] is False
    assert stable_metrics == (0, 4, summary["transport_errors"], 1)
    assert late_workers == []


def test_drain_waits_for_last_delayed_ack_before_reporting_finished() -> None:
    async def scenario() -> tuple[dict, dict, int]:
        count = 4
        gate = asyncio.Event()
        transport = DelayedAckTransport(0.03, delivery_gate=gate, gated_seq=3)
        bridge = Bridge(_window_config(4), connector=transport.connect)
        bridge._write_frame = transport.write
        bridge._read_frame = transport.read
        _start_source(bridge)
        writer = asyncio.create_task(bridge.writer_loop())
        before = None
        inflight_before = None
        try:
            _enqueue(bridge, count)
            _finish_source(bridge, count=count)
            await _wait_for(
                lambda: len(transport.sent) == count and bridge.metrics.acked == count - 1
            )
            before = bridge.summary()
            inflight_before = bridge._inflight
            gate.set()
            await _wait_until_drained(bridge, count, timeout=2.0)
            return before, bridge.summary(), inflight_before
        finally:
            gate.set()
            await _stop_writer(bridge, writer, transport)

    before, after, inflight_before = asyncio.run(scenario())
    assert before["acked"] == 3
    assert before["unfinished"] is True
    assert inflight_before == 1
    assert after["acked"] == 4
    assert after["unfinished"] is False
    assert after["source"]["clean"] is True


def test_stalled_peer_does_not_consume_other_peers_window() -> None:
    async def scenario() -> tuple[dict, dict, DelayedAckTransport, DelayedAckTransport]:
        count = 6
        left_gate = asyncio.Event()
        left_transport = DelayedAckTransport(0.02, delivery_gate=left_gate)
        right_transport = DelayedAckTransport(0.02)
        left = Bridge(_window_config(4, device_id=1), connector=left_transport.connect)
        right = Bridge(_window_config(4, device_id=2), connector=right_transport.connect)
        left._write_frame, left._read_frame = left_transport.write, left_transport.read
        right._write_frame, right._read_frame = right_transport.write, right_transport.read
        _start_source(left, boot_id=BOOT_ID)
        _start_source(right, boot_id=BOOT_ID + 1)
        left_writer = asyncio.create_task(left.writer_loop())
        right_writer = asyncio.create_task(right.writer_loop())
        try:
            _enqueue(left, count, boot_id=BOOT_ID)
            _enqueue(right, count, boot_id=BOOT_ID + 1)
            _finish_source(left, count=count, boot_id=BOOT_ID)
            _finish_source(right, count=count, boot_id=BOOT_ID + 1)
            await _wait_until_drained(right, count, timeout=2.0)
            await left_transport.wait_for_epoch_writes(0, 4)
            assert right.metrics.acked == count
            assert left.metrics.acked == 0
            assert left._inflight == 4
            assert left.inbox.size == 2
            left_gate.set()
            await _wait_until_drained(left, count, timeout=2.0)
            return left.summary(), right.summary(), left_transport, right_transport
        finally:
            left_gate.set()
            await asyncio.gather(
                _stop_writer(left, left_writer, left_transport),
                _stop_writer(right, right_writer, right_transport),
            )

    left, right, left_transport, right_transport = asyncio.run(scenario())
    assert left["acked"] == right["acked"] == 6
    assert left["source"]["clean"] is right["source"]["clean"] is True
    assert left_transport.max_outstanding == right_transport.max_outstanding == 4
    assert [message["seq"] for message in right_transport.acked] == list(range(6))
