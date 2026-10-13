"""B07 Communications BLE -> bounded TLS ingestion bridge. Results never return here."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from dataclasses import asdict, dataclass
import json
import logging
import math
import random
import secrets
import ssl
import threading
import time
from typing import Any, Optional

from common.sensor import decode_packet, dummy_values, choose_fixture, SensorPacket, encode_packet
from common.control import CONTROL_UUID, RESPONSE_UUID
from common.tls import client_context, TLS_SERVER_NAME
from common.wire import read_frame, write_frame, ProtocolError
from laptop.source_audit import SOURCE_STATS_UUID, SourceAudit, SourceStats, parse_source_stats
from laptop.controls import ControlChannel, CommandPipeline
from laptop.goodput import GoodputMeter
from laptop.ble_parameters import prefer_throughput
from laptop.ble_shutdown import disable_notifications_remotely

SERVICE_UUID = "6e1c0001-7a45-4dc4-b678-3f2d5a9c1001"
SENSOR_UUID = "6e1c0005-7a45-4dc4-b678-3f2d5a9c1001"
LOG = logging.getLogger(__name__)


@dataclass
class BridgeConfig:
    ca_file: str
    host: str = "127.0.0.1"
    port: int = 18888
    session_id: str = "ltc-comms"
    queue_capacity: int = 64
    freshness: float = 2.0
    io_timeout: float = 5.0
    scan_timeout: float = 8.0
    connect_timeout: float = 20.0
    address: Optional[str] = None
    diagnostic_unprotected: bool = False
    expected_device_id: Optional[int] = None
    source_audit: bool = False
    ack_window: int = 1
    controls_enabled: bool = False
    source_rate: Optional[int] = None
    seed: Optional[int] = None

    def __post_init__(self):
        if self.host != "127.0.0.1":
            raise ValueError("bridge must connect to its local SSH forward at 127.0.0.1")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("port must be 1..65535")
        if type(self.queue_capacity) is not int or not 1 <= self.queue_capacity <= 4096:
            raise ValueError("queue capacity must be 1..4096")
        if type(self.ack_window) is not int or not 1 <= self.ack_window <= 64:
            raise ValueError("ACK window must be 1..64")
        if self.expected_device_id is not None and (
                type(self.expected_device_id) is not int
                or self.expected_device_id not in (1, 2)):
            raise ValueError("expected device ID must be 1 or 2")
        if self.source_audit and self.expected_device_id is None:
            raise ValueError("source audit requires an expected device ID")
        if self.source_rate is not None and (type(self.source_rate) is not int
                                            or not 1 <= self.source_rate <= 200):
            raise ValueError("source rate must be integer 1..200 Hz")
        if self.controls_enabled and (not self.source_audit or self.diagnostic_unprotected):
            raise ValueError("controls require source identity and authenticated BLE bonding")
        from ultra96.protocol import validate_session
        validate_session(self.session_id)
        for value in [self.freshness, self.io_timeout, self.scan_timeout, self.connect_timeout]:
            if not math.isfinite(value) or value <= 0:
                raise ValueError("timeouts/freshness must be positive finite seconds")


@dataclass(frozen=True)
class Received:
    payload: bytes
    received_at: float
    generation: int


class RawInbox:
    """Bound callback bytes AND cross-thread loop wakeups; parse in the writer."""
    def __init__(self, capacity):
        self._items = deque()
        self._capacity = capacity
        self._lock = threading.Lock()
        self._event = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self._wake_pending = False
        self._accepting = False
        self.generation = 0
        self.dropped = 0
        self.generation_dropped = 0

    @property
    def size(self):
        with self._lock:
            return len(self._items)

    def activate(self, generation):
        with self._lock:
            self.generation_dropped += len(self._items)
            self._items.clear()
            self.generation = generation
            self._accepting = bool(generation)

    def deactivate(self, *, preserve=False):
        """Reject later callbacks, optionally retaining an accepted tail to drain."""
        with self._lock:
            if not preserve:
                self.generation_dropped += len(self._items)
                self._items.clear()
                self.generation = 0
            self._accepting = False

    def _wake(self):
        with self._lock:
            self._wake_pending = False
        self._event.set()

    def put(self, generation, data, received_at):
        with self._lock:
            if not self._accepting or not generation or generation != self.generation:
                self.generation_dropped += 1
                return False
            if len(self._items) == self._capacity:
                self._items.popleft()
                self.dropped += 1
            self._items.append(Received(bytes(data), received_at, generation))
            if not self._wake_pending:
                self._wake_pending = True
                self._loop.call_soon_threadsafe(self._wake)
            return True

    async def get(self):
        while True:
            with self._lock:
                if self._items:
                    return self._items.popleft()
                self._event.clear()
            await self._event.wait()


class StreamTracker:
    def __init__(self):
        self._last = {}
        self.gaps = self.duplicates = self.out_of_order = self.new_boots = 0

    def observe(self, packet):
        previous = self._last.get(packet.device_id)
        if previous:
            boot, seq = previous
            if boot != packet.boot_id:
                self.new_boots += 1
            else:
                delta = (packet.seq - seq) & 0xffffffff
                if delta == 0:
                    self.duplicates += 1
                    return False
                if delta >= 0x80000000:
                    self.out_of_order += 1
                    return False
                self.gaps += delta - 1
        self._last[packet.device_id] = packet.boot_id, packet.seq
        return True


@dataclass
class Metrics:
    received: int = 0
    malformed: int = 0
    stale_dropped: int = 0
    generation_dropped: int = 0
    sent: int = 0
    acked: int = 0
    duplicate_acks: int = 0
    ack_errors: int = 0
    ambiguous_dropped: int = 0
    transport_errors: int = 0
    transport_connections: int = 0
    ble_connections: int = 0
    ble_errors: int = 0
    cleanup_errors: int = 0
    identity_mismatches: int = 0
    source_stats_errors: int = 0
    disconnects: int = 0


@dataclass
class _PendingFrame:
    item: Received
    packet: SensorPacket
    sent_at: float = 0.0
    write_attempted: bool = False
    epoch: int = 0


class _PipelineTransportError(Exception):
    """The current TLS epoch cannot safely continue."""


class _PipelineCleanupError(RuntimeError):
    """An epoch worker did not retire inside the cleanup bound."""


class Bridge:
    def __init__(self, config, *, connector=None, clock=time.monotonic, evidence=None):
        self.config = config
        self.clock = clock
        self.inbox = RawInbox(config.queue_capacity)
        self.metrics = Metrics()
        self.tracker = StreamTracker()
        self.reader = self.writer = None
        self._connector = connector or self._connect_tls
        self._read_frame, self._write_frame = read_frame, write_frame
        self._retained = set()
        self._pipeline_retained = set()
        self._forward_lock = asyncio.Lock()
        self._inflight = 0
        self._transport_epoch = 0
        self.active = asyncio.Event()
        self.source_audit = (SourceAudit(config.expected_device_id)
                             if config.source_audit else None)
        self.first_received_at = None
        self.last_received_at = None
        self.max_receive_silence = 0.0
        self._notification_lock = threading.Lock()
        self.callback_received = 0
        self.last_notification_at = None
        self._observation_active = False
        self.observation_received = 0
        self.observation_max_silence = 0.0
        self._observation_last = None
        self.goodput = GoodputMeter()
        self.evidence = evidence
        self.negotiated_mtu = None
        self.source_rate_confirmed = None
        self.connection_parameters = None
        self.rng = random.Random(config.seed) if config.seed is not None else None
        self.control = (ControlChannel(config.expected_device_id, timeout=config.io_timeout,
                                       evidence=evidence, bounded=self._bounded)
                        if config.controls_enabled else None)
        self.commands = (CommandPipeline(self.control, self.forward_command)
                         if self.control is not None else None)

    def log(self, kind, **fields):
        if self.evidence is not None:
            self.evidence.record(kind, device_id=self.config.expected_device_id, **fields)

    def submit_command(self):
        if self.control is None:
            return False
        packet = SensorPacket(self.config.expected_device_id, self.control.boot_id or 0,
                              self.control.next_id(), 0, choose_fixture(self.rng), version=2)
        return self.commands.submit(packet)

    async def forward_command(self, packet, request_id):
        """Independent TLS transaction; never consumes stream sequence or ACK counts."""
        reader = writer = None
        try:
            reader, writer = await self._connector()
            await self._write_frame(writer, packet.to_message(self.config.session_id, request_id),
                                    timeout=self.config.io_timeout)
            ack = await self._read_frame(reader, timeout=self.config.io_timeout)
            self._check_ack(ack, packet, request_id=request_id)
            if ack["status"] != "accepted":
                raise ProtocolError("duplicate command ingestion; no new phone event")
        finally:
            if writer is not None:
                writer.close()
                try:
                    await self._bounded(writer.wait_closed(), 1.0)
                except BaseException:
                    if hasattr(writer, "transport"):
                        writer.transport.abort()
                    raise

    def enqueue(self, generation, data):
        received_at = self.clock()
        if not self.inbox.put(generation, data, received_at):
            return False
        try:
            packet = decode_packet(data)
            if (self.config.expected_device_id is None or
                    packet.device_id == self.config.expected_device_id):
                if self.control is not None:
                    self.control.observe_sensor(data, packet)
                self.goodput.record(packet, received_at)
                self.log("sensor", direction="ESP->laptop", version=packet.version,
                         boot_id=packet.boot_id, seq=packet.seq, uptime_ms=packet.uptime_ms,
                         values=list(packet.values), raw_hex=bytes(data).hex(), validation="decoded",
                         received_monotonic=received_at)
        except (ValueError, TypeError):
            self.log("sensor_invalid", direction="ESP->laptop", raw_hex=bytes(data).hex(),
                     validation="malformed")
        with self._notification_lock:
            self.callback_received += 1
            self.last_notification_at = received_at
            if self._observation_active:
                self.observation_max_silence = max(
                    self.observation_max_silence,
                    received_at - self._observation_last)
                self._observation_last = received_at
                self.observation_received += 1
        return True

    def begin_observation(self, now=None):
        now = self.clock() if now is None else now
        self.goodput.start(now)
        with self._notification_lock:
            self._observation_active = True
            self.observation_received = 0
            self.observation_max_silence = 0.0
            self._observation_last = now

    def finish_observation(self, now=None):
        now = self.clock() if now is None else now
        self.goodput.stop(now)
        with self._notification_lock:
            if self._observation_active:
                self.observation_max_silence = max(
                    self.observation_max_silence, now - self._observation_last)
            self._observation_active = False

    async def _bounded(self, awaitable, timeout):
        if sum(not task.done() for task in self._retained) >= 2:
            # A stalled stop-notify plus disconnect exhaust the native-operation
            # budget. Never accumulate more uncancellable WinRT work.
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise RuntimeError("native cleanup still pending; operation budget exhausted")
        task = asyncio.ensure_future(awaitable)
        self._retained.add(task)
        def done(child):
            self._retained.discard(child)
            if not child.cancelled():
                child.exception()
        task.add_done_callback(done)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except BaseException:
            task.cancel()
            raise

    async def _connect_tls(self):
        return await asyncio.wait_for(asyncio.open_connection(
            self.config.host, self.config.port, ssl=client_context(self.config.ca_file),
            server_hostname=TLS_SERVER_NAME, limit=32768,
            ssl_handshake_timeout=self.config.io_timeout), self.config.io_timeout)

    async def close_transport(self):
        writer, self.writer, self.reader = self.writer, None, None
        if writer is not None:
            writer.close()
            try:
                await self._bounded(writer.wait_closed(), 1.0)
            except asyncio.CancelledError as exc:
                self.metrics.cleanup_errors += 1
                if hasattr(writer, "transport"):
                    writer.transport.abort()
                LOG.warning("TLS cleanup failed operation=close_transport error_type=%s",
                            type(exc).__name__)
                raise
            except Exception as exc:
                self.metrics.cleanup_errors += 1
                if hasattr(writer, "transport"):
                    writer.transport.abort()
                LOG.warning("TLS cleanup failed operation=close_transport error_type=%s",
                            type(exc).__name__)

    def _check_ack(self, ack, packet, request_id=None):
        expected = dict(v=packet.version, type="INGEST_ACK", session_id=self.config.session_id,
                        device_id=packet.device_id, boot_id=packet.boot_id, seq=packet.seq)
        if packet.version == 2:
            expected["request_id"] = request_id
        if not isinstance(ack, dict) or set(ack) != set(expected) | {"status"}:
            raise ProtocolError("malformed ingestion acknowledgement")
        for key, value in expected.items():
            if type(ack[key]) is not type(value) or ack[key] != value:
                raise ProtocolError("uncorrelated ingestion acknowledgement")
        if ack["status"] not in ("accepted", "duplicate"):
            raise ProtocolError("invalid acknowledgement status")

    async def forward_one(self):
        """Single stream owner; ambiguous failed writes are never replayed."""
        async with self._forward_lock:
            item = await self.inbox.get()
            self._inflight += 1
            try:
                return await self._forward_one(item)
            finally:
                self._inflight -= 1

    def _prepare_item(self, item):
        self.metrics.received += 1
        try:
            packet = decode_packet(item.payload)
            if self.source_audit is not None:
                self.source_audit.received(packet)
            if (self.config.expected_device_id is not None
                    and packet.device_id != self.config.expected_device_id):
                self.metrics.identity_mismatches += 1
                return
            if packet.version == 1 and tuple(packet.values) != tuple(dummy_values(packet.seq)):
                raise ValueError("not a B07 Communications deterministic packet")
        except (ValueError, TypeError):
            self.metrics.malformed += 1
            return
        if self.last_received_at is not None:
            self.max_receive_silence = max(
                self.max_receive_silence, item.received_at - self.last_received_at)
        else:
            self.first_received_at = item.received_at
        self.last_received_at = item.received_at
        if not self.tracker.observe(packet):
            return None
        if item.generation != self.inbox.generation:
            self.metrics.generation_dropped += 1
            return None
        if self.clock() - item.received_at > self.config.freshness:
            self.metrics.stale_dropped += 1
            return None
        return packet

    async def _forward_one(self, item):
        packet = self._prepare_item(item)
        if packet is None:
            return
        sent = False
        try:
            if self.writer is None:
                self.reader, self.writer = await self._connector()
                self.metrics.transport_connections += 1
            # Connection/handshake may have consumed the entire freshness budget.
            if item.generation != self.inbox.generation:
                self.metrics.generation_dropped += 1
                return
            remaining = self.config.freshness - (self.clock() - item.received_at)
            if remaining <= 0:
                self.metrics.stale_dropped += 1
                return
            sent = True
            await self._write_frame(self.writer, packet.to_message(self.config.session_id),
                                    timeout=min(self.config.io_timeout, remaining))
            self.metrics.sent += 1
            try:
                ack = await self._read_frame(self.reader, timeout=self.config.io_timeout)
                self._check_ack(ack, packet)
            except Exception:
                self.metrics.ack_errors += 1
                raise
            self.metrics.acked += 1
            self.log("sensor_ack", direction="Ultra96->laptop", seq=packet.seq,
                     boot_id=packet.boot_id, validation=ack["status"])
            self.metrics.duplicate_acks += int(ack["status"] == "duplicate")
            if self.source_audit is not None:
                self.source_audit.acknowledged(packet)
        except asyncio.CancelledError:
            if sent:
                self.metrics.ambiguous_dropped += 1
            raise
        except (OSError, ValueError, EOFError, asyncio.TimeoutError, ssl.SSLError):
            self.metrics.transport_errors += 1
            if sent:
                self.metrics.ambiguous_dropped += 1
            await self.close_transport()

    async def _legacy_writer_loop(self):
        delay = 0.5
        while True:
            before = self.metrics.transport_errors
            acked_before = self.metrics.acked
            await self.forward_one()
            if self.metrics.transport_errors != before:
                await asyncio.sleep(delay)
                delay = min(5.0, delay * 2)
            elif self.metrics.acked > acked_before:
                delay = 0.5

    async def _retire_pipeline_epoch(self, state, pending, current, tasks):
        """Invalidate an epoch, close it, and retire every owned frame once."""
        state["active"] = False

        # Account for frame ownership before cleanup can suspend or be
        # cancelled. Late workers see the inactive epoch and cannot mutate it.
        frames = len(pending)
        ambiguous = len(pending)
        pending.clear()
        frame = current[0]
        if frame is not None:
            frames += 1
            ambiguous += int(frame.write_attempted)
            current[0] = None
        self.metrics.ambiguous_dropped += ambiguous
        self._inflight -= frames

        # Observe every still-running worker immediately. This set is separate
        # from native BLE cleanup accounting so it cannot exhaust _bounded's
        # native-operation budget while close_transport runs.
        for task in tasks:
            if not task.done():
                self._pipeline_retained.add(task)

                def consume(child, *, bridge=self):
                    bridge._pipeline_retained.discard(child)
                    if not child.cancelled():
                        child.exception()

                task.add_done_callback(consume)
                task.cancel()

        cancellation = None
        try:
            await self.close_transport()
        except asyncio.CancelledError as exc:
            # close_transport has already aborted the old transport. Continue
            # bounded worker observation before propagating cancellation.
            cancellation = exc
        _, unfinished = await asyncio.wait(tasks, timeout=0.2)

        if unfinished:
            self.metrics.cleanup_errors += 1
        if cancellation is not None:
            raise cancellation
        if unfinished:
            return False
        return True

    async def _pipeline_epoch(self):
        """Run one owned TLS epoch with a FIFO sender and ACK reader."""
        slots = asyncio.Semaphore(self.config.ack_window)
        pending = deque()
        pending_available = asyncio.Event()
        current = [None]
        state = {"active": True, "epoch": None}

        async def sender():
            while True:
                await slots.acquire()
                item = await self.inbox.get()
                self._inflight += 1
                frame = _PendingFrame(item=item, packet=None)
                current[0] = frame
                packet = self._prepare_item(item)
                if packet is None:
                    current[0] = None
                    self._inflight -= 1
                    slots.release()
                    continue
                frame.packet = packet
                try:
                    if self.writer is None:
                        reader, writer = await self._connector()
                        if not state["active"]:
                            writer.close()
                            return
                        self.reader, self.writer = reader, writer
                        self._transport_epoch += 1
                        state["epoch"] = self._transport_epoch
                        self.metrics.transport_connections += 1
                    if item.generation != self.inbox.generation:
                        self.metrics.generation_dropped += 1
                        current[0] = None
                        self._inflight -= 1
                        slots.release()
                        continue
                    remaining = self.config.freshness - (self.clock() - item.received_at)
                    if remaining <= 0:
                        self.metrics.stale_dropped += 1
                        current[0] = None
                        self._inflight -= 1
                        slots.release()
                        continue
                    frame.epoch = state["epoch"]
                    frame.sent_at = self.clock()
                    frame.write_attempted = True
                    await self._write_frame(
                        self.writer, packet.to_message(self.config.session_id),
                        timeout=min(self.config.io_timeout, remaining))
                    if not state["active"]:
                        return
                    self.metrics.sent += 1
                    pending.append(frame)
                    current[0] = None
                    pending_available.set()
                except asyncio.CancelledError:
                    raise
                except (OSError, ValueError, EOFError, asyncio.TimeoutError,
                        ssl.SSLError) as exc:
                    raise _PipelineTransportError() from exc

        async def receiver():
            while True:
                while not pending:
                    pending_available.clear()
                    if not pending:
                        await pending_available.wait()
                frame = pending[0]
                if frame.epoch != state["epoch"]:
                    raise _PipelineCleanupError("pending frame belongs to another TLS epoch")
                remaining = frame.sent_at + self.config.io_timeout - self.clock()
                try:
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    ack = await self._read_frame(self.reader, timeout=remaining)
                    if not state["active"]:
                        return
                    if self.clock() > frame.sent_at + self.config.io_timeout:
                        raise asyncio.TimeoutError()
                    self._check_ack(ack, frame.packet)
                except asyncio.CancelledError:
                    raise
                except (OSError, ValueError, EOFError, asyncio.TimeoutError,
                        ssl.SSLError) as exc:
                    if state["active"]:
                        self.metrics.ack_errors += 1
                    raise _PipelineTransportError() from exc
                pending.popleft()
                self.metrics.acked += 1
                self.log("sensor_ack", direction="Ultra96->laptop", seq=frame.packet.seq,
                         boot_id=frame.packet.boot_id, validation=ack["status"])
                self.metrics.duplicate_acks += int(ack["status"] == "duplicate")
                if self.source_audit is not None:
                    self.source_audit.acknowledged(frame.packet)
                self._inflight -= 1
                slots.release()

        workers = {
            asyncio.create_task(sender(), name="pipeline-sender"),
            asyncio.create_task(receiver(), name="pipeline-ack-reader"),
        }
        failure = None
        cancelled = False
        try:
            done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                error = None if task.cancelled() else task.exception()
                if failure is None and error is not None:
                    failure = error
            if failure is None:
                failure = _PipelineCleanupError("pipeline worker stopped unexpectedly")
        except asyncio.CancelledError:
            cancelled = True
        cleanup_ok = await self._retire_pipeline_epoch(
            state, pending, current, workers)
        if not cleanup_ok:
            raise _PipelineCleanupError("pipeline worker cleanup timed out")
        if cancelled:
            raise asyncio.CancelledError()
        raise failure

    async def _pipeline_writer_loop(self):
        # The pipeline owns this bridge's socket for its whole lifetime. A
        # concurrent public forward_one call waits instead of racing the FIFO.
        async with self._forward_lock:
            delay = 0.5
            while True:
                acked_before = self.metrics.acked
                try:
                    await self._pipeline_epoch()
                except _PipelineTransportError:
                    self.metrics.transport_errors += 1
                    if self.metrics.acked > acked_before:
                        delay = 0.5
                    await asyncio.sleep(delay)
                    delay = min(5.0, delay * 2)

    async def writer_loop(self):
        if self.config.ack_window == 1:
            return await self._legacy_writer_loop()
        return await self._pipeline_writer_loop()

    async def ble_loop(self, *, scanner=None, client_factory=None,
                       stop_event=None, active_event=None):
        from bleak import BleakScanner
        from laptop.ble_connection import make_ble_client
        scanner, client_factory = scanner or BleakScanner, client_factory or make_ble_client
        active_event = active_event or self.active
        generation = 0
        delay = 0.5
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            if any(not task.done() for task in self._retained):
                # Do not overlap a new native BLE connection with teardown from
                # the prior attempt. The bounded run can still stop independently.
                await asyncio.sleep(0.5)
                continue
            client = None
            connection_preference = None
            subscribed = False
            controls_subscribed = False
            normal_stop = False
            disconnected = asyncio.Event()
            generation += 1
            current = generation
            try:
                def match(device, advertisement):
                    return SERVICE_UUID in [x.lower() for x in advertisement.service_uuids or []] and (
                        not self.config.address or device.address.lower() == self.config.address.lower())
                device = await self._bounded(scanner.find_device_by_filter(
                    match, timeout=self.config.scan_timeout), self.config.scan_timeout + 0.1)
                if device is None:
                    raise OSError("expected BLE service not found")
                loop = asyncio.get_running_loop()
                def on_disconnect(_client, event=disconnected):
                    if not loop.is_closed():
                        loop.call_soon_threadsafe(event.set)
                client = client_factory(device, disconnected_callback=on_disconnect)
                await self._bounded(client.connect(), self.config.connect_timeout)
                service = client.services.get_service(SERVICE_UUID)
                characteristic = service.get_characteristic(SENSOR_UUID) if service else None
                if characteristic is None or "notify" not in characteristic.properties:
                    raise ValueError("B07 Communications sensor Notify characteristic missing")
                if client.mtu_size < 35:
                    raise ValueError("current ATT MTU cannot carry 32-byte sensor packet")
                self.negotiated_mtu = client.mtu_size
                if not self.config.diagnostic_unprotected:
                    from laptop.windows_pairing import require_authenticated_bond
                    await require_authenticated_bond(client)

                async def configure_parameters():
                    nonlocal connection_preference
                    # Store ownership inside the supervised child so cancellation
                    # after it returns cannot discard an already-created handle.
                    connection_preference = await prefer_throughput(
                        client, source_rate=self.config.source_rate)

                await self._bounded(configure_parameters(), self.config.connect_timeout)
                self.connection_parameters = connection_preference.report()
                self.log("BLE_connection_parameters", generation=current,
                         **self.connection_parameters)
                if self.source_audit is not None:
                    source = service.get_characteristic(SOURCE_STATS_UUID) if service else None
                    if source is None or "read" not in source.properties:
                        self.metrics.source_stats_errors += 1
                        self.source_audit.mark_snapshot_error()
                        raise ValueError("protected source statistics Read characteristic missing")
                    try:
                        raw_snapshot = await self._bounded(client.read_gatt_char(SOURCE_STATS_UUID),
                                                           self.config.connect_timeout)
                        snapshot = parse_source_stats(
                            raw_snapshot,
                            expected_device_id=self.config.expected_device_id)
                    except Exception:
                        self.metrics.source_stats_errors += 1
                        self.source_audit.mark_snapshot_error()
                        raise
                    if self.source_audit.start_stats is None:
                        self.source_audit.start(snapshot)
                    else:
                        # The original start boundary remains authoritative.
                        self.source_audit.mark_interruption()
                if self.control is not None:
                    for uuid, property_name in ((CONTROL_UUID, "write"), (RESPONSE_UUID, "notify")):
                        control_char = service.get_characteristic(uuid)
                        if control_char is None or property_name not in control_char.properties:
                            raise ValueError("protected B07 control characteristics missing; update firmware")
                    self.control.attach(client, snapshot.boot_id,
                                        packet_magic=bytes(raw_snapshot[:2]))
                    control_generation = self.control._generation
                    def control_notification(sender, data, gen=control_generation):
                        self.control.receive(sender, data, generation=gen)
                    await self._bounded(client.start_notify(RESPONSE_UUID, control_notification),
                                        self.config.connect_timeout)
                    controls_subscribed = True
                    if self.config.source_rate is not None:
                        await self.control.set_rate(self.config.source_rate)
                        self.source_rate_confirmed = self.config.source_rate
                self.inbox.activate(current)
                def notification(_sender, data, gen=current):
                    self.enqueue(gen, data)
                await self._bounded(client.start_notify(SENSOR_UUID, notification), self.config.connect_timeout)
                subscribed = True
                self.metrics.ble_connections += 1
                self.log("BLE_connected", direction="ESP<->laptop", generation=current,
                         mtu=client.mtu_size, authenticated=not self.config.diagnostic_unprotected)
                delay = 0.5
                active_event.set()
                if stop_event is None:
                    await disconnected.wait()
                else:
                    disconnected_wait = asyncio.create_task(disconnected.wait())
                    stop_wait = asyncio.create_task(stop_event.wait())
                    done, pending = await asyncio.wait(
                        (disconnected_wait, stop_wait),
                        return_when=asyncio.FIRST_COMPLETED)
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    normal_stop = (stop_wait in done and disconnected_wait not in done
                                   and stop_event.is_set())
                    if not normal_stop:
                        self.metrics.disconnects += 1
                        if self.source_audit is not None:
                            self.source_audit.mark_interruption()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.metrics.ble_errors += 1
                LOG.warning("BLE attempt failed: %s", exc)
            finally:
                if self.control is not None:
                    self.control.detach()
                self.log("BLE_disconnected", direction="ESP<->laptop", generation=current)
                if client is not None:
                    cleanup_cancellation = None
                    source_deadline = self.clock() + self.config.connect_timeout
                    quiesced_snapshot = None
                    remote_stop_task = None
                    if controls_subscribed and client.is_connected:
                        try:
                            await self._bounded(client.stop_notify(RESPONSE_UUID), 0.75)
                        except (Exception, asyncio.CancelledError) as exc:
                            self.metrics.cleanup_errors += 1
                            if isinstance(exc, asyncio.CancelledError):
                                cleanup_cancellation = exc
                    if normal_stop and subscribed and client.is_connected and self.source_audit is not None:
                        try:
                            async def stop_remote():
                                nonlocal remote_stop_task
                                remote_stop_task = asyncio.current_task()
                                return await disable_notifications_remotely(characteristic)

                            remote_disabled = await self._bounded(stop_remote(),
                                min(0.75, max(0.001, source_deadline - self.clock())))
                            if remote_disabled:
                                remaining = source_deadline - self.clock()
                                if remaining <= 0:
                                    raise asyncio.TimeoutError("source cleanup budget exhausted")
                                # Arduino can ACK a CCCD write before its callback.
                                # An uncached source read is the firmware mutex barrier.
                                quiesced_snapshot = parse_source_stats(await self._bounded(
                                    client.read_gatt_char(SOURCE_STATS_UUID, use_cached=False), remaining),
                                    expected_device_id=self.config.expected_device_id)
                                start = self.source_audit.start_stats
                                if start is None or quiesced_snapshot.boot_id != start.boot_id:
                                    raise ProtocolError("quiesced source boot changed")
                                expected_received = (quiesced_snapshot.submitted - start.submitted) & 0xffffffff
                                self.log("source_quiesced", snapshot=asdict(quiesced_snapshot),
                                         expected_received=expected_received)
                                drain_started = self.clock()
                                drain_deadline = min(source_deadline, drain_started + 0.5)
                                while self.source_audit.received_count < expected_received:
                                    remaining = drain_deadline - self.clock()
                                    if remaining <= 0:
                                        self.log("source_tail_drain", validation="timeout",
                                            expected_received=expected_received,
                                            received=self.source_audit.received_count,
                                            elapsed_seconds=self.clock() - drain_started)
                                        # Conservative failure: may be host backlog
                                        # or a missing BLE tail. Neither is hidden.
                                        raise asyncio.TimeoutError("source tail drain timed out")
                                    await asyncio.sleep(min(0.01, remaining))
                                self.log("source_tail_drain", validation="complete",
                                    expected_received=expected_received,
                                    received=self.source_audit.received_count,
                                    elapsed_seconds=self.clock() - drain_started)
                        except (Exception, asyncio.CancelledError) as exc:
                            self.metrics.source_stats_errors += 1
                            self.source_audit.mark_snapshot_error()
                            if isinstance(exc, asyncio.CancelledError):
                                cleanup_cancellation = exc
                            self.log("source_quiesce_failed", error_type=type(exc).__name__,
                                native_pending=bool(remote_stop_task is not None and not remote_stop_task.done()))
                            LOG.warning("BLE cleanup failed operation=quiesce_source error_type=%s",
                                        type(exc).__name__)
                    # WinRT stop_notify writes the remote CCCD and rejects an
                    # already-disconnected client. disconnect still releases
                    # local notification handlers and native service objects.
                    stopped_notifications = False
                    if (subscribed and client.is_connected and
                            (remote_stop_task is None or remote_stop_task.done())):
                        try:
                            await self._bounded(client.stop_notify(SENSOR_UUID), 0.75)
                            stopped_notifications = True
                        except (Exception, asyncio.CancelledError) as exc:
                            if isinstance(exc, asyncio.CancelledError):
                                cleanup_cancellation = exc
                            self.metrics.cleanup_errors += 1
                            LOG.warning("BLE cleanup failed operation=stop_notify error_type=%s",
                                        type(exc).__name__)
                    if (normal_stop and stopped_notifications
                            and self.source_audit is not None):
                        try:
                            remaining = source_deadline - self.clock()
                            if remaining <= 0:
                                raise asyncio.TimeoutError("source cleanup budget exhausted")
                            read_options = {"use_cached": False} if quiesced_snapshot is not None else {}
                            final = parse_source_stats(
                                await self._bounded(client.read_gatt_char(SOURCE_STATS_UUID, **read_options),
                                                    remaining),
                                expected_device_id=self.config.expected_device_id)
                            self.source_audit.finish(final)
                            if quiesced_snapshot is not None and final != quiesced_snapshot:
                                raise ProtocolError("source changed after notifications were quiesced")
                            if quiesced_snapshot is not None:
                                self.log("source_final_verified", snapshot=asdict(final), validation="unchanged")
                        except (Exception, asyncio.CancelledError) as exc:
                            self.metrics.source_stats_errors += 1
                            self.source_audit.mark_snapshot_error()
                            if isinstance(exc, asyncio.CancelledError):
                                cleanup_cancellation = exc
                            self.log("source_final_failed", error_type=type(exc).__name__)
                            LOG.warning("BLE cleanup failed operation=read_source_stats error_type=%s",
                                        type(exc).__name__)
                    self.inbox.deactivate(preserve=normal_stop and stopped_notifications)
                    active_event.clear()
                    if connection_preference is not None:
                        connection_preference.refresh()
                        self.connection_parameters = connection_preference.report()
                    try:
                        # WinRT waits before disposing GATT services. Earlier
                        # CCCD timeouts/source reads must not spend this budget.
                        await self._bounded(client.disconnect(), 1.0)
                    except (Exception, asyncio.CancelledError) as exc:
                        if isinstance(exc, asyncio.CancelledError):
                            cleanup_cancellation = exc
                        self.metrics.cleanup_errors += 1
                        LOG.warning("BLE cleanup failed operation=disconnect error_type=%s",
                                    type(exc).__name__)
                    finally:
                        if connection_preference is not None:
                            connection_preference.close()
                            self.connection_parameters = connection_preference.report()
                            self.metrics.cleanup_errors += len(
                                self.connection_parameters["cleanup_errors"])
                    if cleanup_cancellation is not None:
                        raise cleanup_cancellation
                else:
                    self.inbox.deactivate()
                    active_event.clear()
            if normal_stop:
                return
            if stop_event is not None and stop_event.is_set():
                return
            if stop_event is None:
                await asyncio.sleep(delay)
            else:
                try:
                    await asyncio.wait_for(stop_event.wait(), delay)
                    return
                except asyncio.TimeoutError:
                    pass
            delay = min(delay * 2, 5.0)

    async def dummy_loop(self, rate=10.0, *, stop_event=None, active_event=None):
        """Explicit local transport test source; never physical BLE evidence."""
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("mock rate must be positive finite")
        active_event = active_event or self.active
        self.inbox.activate(1)
        device_id = self.config.expected_device_id or 1
        boot_id = secrets.randbits(32)
        seq = 0
        if self.source_audit is not None and self.source_audit.start_stats is None:
            self.source_audit.start(SourceStats(device_id, boot_id, 0, 0, 0))
        active_event.set()
        try:
            while stop_event is None or not stop_event.is_set():
                self.enqueue(1, encode_packet(SensorPacket(device_id, boot_id, seq,
                    int(seq * 1000 / rate) & 0xffffffff, choose_fixture(self.rng), version=2)))
                seq = (seq + 1) & 0xffffffff
                if stop_event is None:
                    await asyncio.sleep(1.0 / rate)
                else:
                    try:
                        await asyncio.wait_for(stop_event.wait(), 1.0 / rate)
                    except asyncio.TimeoutError:
                        pass
        finally:
            if self.source_audit is not None and self.source_audit.end_stats is None:
                self.source_audit.finish(SourceStats(device_id, boot_id, seq, seq, 0))
            self.inbox.deactivate(preserve=True)
            active_event.clear()

    def summary(self):
        source = self.source_audit.report() if self.source_audit is not None else None
        source_issue = None
        if self.source_audit is not None and self.source_audit.start_stats is None:
            source_issue = ("source statistics unavailable or incompatible; "
                            "install updated dual-source firmware")
        elif self.source_audit is not None and self.source_audit.end_stats is None:
            source_issue = "final source statistics unavailable; capture is incomplete"
        return dict(asdict(self.metrics), queue_dropped=self.inbox.dropped,
                    callback_generation_dropped=self.inbox.generation_dropped,
                    queue_size=self.inbox.size, gaps=self.tracker.gaps,
                    duplicates=self.tracker.duplicates, out_of_order=self.tracker.out_of_order,
                    new_boots=self.tracker.new_boots,
                    unfinished=bool(self.inbox.size or self._inflight
                                    or any(not task.done()
                                           for task in self._pipeline_retained)),
                    first_received_at=self.first_received_at,
                    last_received_at=self.last_received_at,
                    max_receive_silence=self.max_receive_silence,
                    callback_received=self.callback_received,
                    observation_received=self.observation_received,
                    observation_max_silence=self.observation_max_silence,
                    sensor_goodput=self.goodput.report(self.clock()),
                    commands=self.commands.summary() if self.commands is not None else None,
                    controls=self.control.summary() if self.control is not None else None,
                    configuration={"address": self.config.address,
                                   "negotiated_att_mtu": self.negotiated_mtu,
                                   "source_rate_requested_hz": self.config.source_rate,
                                   "source_rate_confirmed_hz": self.source_rate_confirmed,
                                   "connection_parameters": self.connection_parameters,
                                   "ack_window": self.config.ack_window,
                                   "queue_capacity": self.config.queue_capacity,
                                   "freshness_seconds": self.config.freshness,
                                   "fixture_seed": self.config.seed},
                    source=source, source_issue=source_issue)

    async def run(self, duration=60.0, target=0, mock=False):
        if not math.isfinite(duration) or duration <= 0 or type(target) is not int or target < 0:
            raise ValueError("duration must be positive finite and target nonnegative")
        tasks = [asyncio.create_task(self.writer_loop()),
                 asyncio.create_task(self.dummy_loop() if mock else self.ble_loop())]
        start = self.clock()
        try:
            while self.clock() - start < duration:
                for task in tasks:
                    if task.done():
                        task.result()
                        raise RuntimeError("bridge worker exited unexpectedly")
                if target and self.metrics.acked >= target:
                    break
                await asyncio.sleep(min(0.05, duration))
        finally:
            for task in tasks:
                task.cancel()
            done, pending = await asyncio.wait(tasks, timeout=2.0)
            for task in done:
                if not task.cancelled():
                    task.result()
            for task in pending:
                self.metrics.cleanup_errors += 1
                self._retained.add(task)
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            self.inbox.activate(0)
            await self.close_transport()
        return dict(self.summary(), elapsed_seconds=round(self.clock() - start, 3), mock_input=mock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ca", required=True)
    parser.add_argument("--port", type=int, default=18888)
    parser.add_argument("--session-id", default="ltc-comms")
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--target", type=int, default=0)
    parser.add_argument("--queue-capacity", type=int, default=64)
    parser.add_argument("--freshness", type=float, default=2)
    parser.add_argument("--address")
    parser.add_argument("--mock", action="store_true", help="explicit synthetic input, not BLE evidence")
    parser.add_argument("--diagnostic-unprotected", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    async def run():
        bridge = Bridge(BridgeConfig(ca_file=args.ca, port=args.port, session_id=args.session_id,
            queue_capacity=args.queue_capacity, freshness=args.freshness, address=args.address,
            diagnostic_unprotected=args.diagnostic_unprotected))
        return await bridge.run(args.duration, args.target, args.mock)
    summary = asyncio.run(run())
    print(json.dumps(summary, sort_keys=True))
    failures = ["malformed", "ack_errors", "transport_errors", "ble_errors", "cleanup_errors",
                "queue_dropped", "stale_dropped", "gaps", "duplicates", "out_of_order"]
    return 0 if summary["acked"] > 0 and (not args.target or summary["acked"] >= args.target) and not any(
        summary[x] for x in failures) else 1


if __name__ == "__main__":
    raise SystemExit(main())
