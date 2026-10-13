"""Bounded authenticated BLE command/file transactions, independent of uplinks.

One transaction owns the response slot. Commands are never retried after an
ambiguous timeout; only an identical file chunk can be retried. A BLE generation
change fails the transaction and all packets accepted for the old boot.
"""
from __future__ import annotations

import asyncio
from collections import deque
import os
import secrets
import struct
import sys
import threading
import time

from common.control import (COMMAND, SET_RATE, FILE_BEGIN, FILE_CHUNK, FILE_END,
    FILE_ABORT, RESPONSE_FLAG, CONTROL_UUID, MIN_CONTROL_MTU, MAX_FILE_SIZE,
    ControlFrame, encode_control, decode_control, chunk_size, file_metadata,
    parse_file_metadata, transformed_values)
from common.sensor import SensorPacket, encode_packet, decode_packet
from common.wire import ProtocolError


class ControlChannel:
    def __init__(self, device_id, *, timeout=3.0, evidence=None, bounded=None):
        if device_id not in (1, 2) or timeout <= 0:
            raise ValueError("invalid control device/timeout")
        self.device_id, self.timeout, self.evidence = device_id, timeout, evidence
        self.client = self.boot_id = None
        self._generation = 0
        self._serial = asyncio.Lock()
        self._pending = None
        self._recent = deque(maxlen=64)
        self._notifications = deque()
        self._notification_lock = threading.Lock()
        self._wake_pending = False
        self._loop = asyncio.get_running_loop()
        self._next_id = secrets.randbelow(0x7ffffffe) + 1
        self.errors = self.duplicates = self.notification_dropped = 0
        self._native_tasks = set()
        self._bounded = bounded or self._bounded_native

    async def _bounded_native(self, awaitable, timeout):
        """Timeouts must not await a native operation which ignores cancellation."""
        if any(not task.done() for task in self._native_tasks):
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise RuntimeError("native control write still pending")
        task = asyncio.ensure_future(awaitable)
        self._native_tasks.add(task)
        def completed(child):
            self._native_tasks.discard(child)
            if not child.cancelled():
                child.exception()
        task.add_done_callback(completed)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except BaseException:
            task.cancel()
            raise

    def next_id(self):
        identity = self._next_id
        if identity > 0xffffffff:
            raise RuntimeError("control request identity space exhausted; restart capture")
        self._next_id += 1
        return identity

    def log(self, kind, **fields):
        if self.evidence is not None:
            self.evidence.record(kind, device_id=self.device_id, **fields)

    def attach(self, client, boot_id):
        if any(not task.done() for task in self._native_tasks):
            raise RuntimeError("native control write still pending")
        if client.mtu_size < MIN_CONTROL_MTU:
            raise ValueError("controls require negotiated ATT MTU >= 64")
        self.detach()
        self.client, self.boot_id = client, boot_id
        self.log("control_connected", boot_id=boot_id, mtu=client.mtu_size)

    def detach(self):
        self._generation += 1
        self.client = self.boot_id = None
        if self._pending is not None and not self._pending[1].done():
            self._pending[1].set_exception(ConnectionError("BLE control connection ended"))

    def receive(self, _sender, data, *, generation=None):
        """Bound both response storage and cross-thread event-loop wakeups."""
        with self._notification_lock:
            if len(self._notifications) >= 32:
                self.notification_dropped += 1
                return
            self._notifications.append((self._generation if generation is None else generation,
                                        bytes(data)))
            if not self._wake_pending and not self._loop.is_closed():
                self._wake_pending = True
                self._loop.call_soon_threadsafe(self._deliver)

    def _deliver(self):
        with self._notification_lock:
            items = list(self._notifications)
            self._notifications.clear()
            self._wake_pending = False
        for generation, data in items:
            if generation != self._generation or self.client is None:
                self.errors += 1
                continue
            try:
                response = decode_control(data, mtu=self.client.mtu_size)
                identity = (response.opcode, response.request_id, response.offset)
                if identity in self._recent:
                    self.duplicates += 1
                    self.log("control_duplicate", request_id=response.request_id,
                             opcode=response.opcode, offset=response.offset)
                    continue
                if self._pending is None or self._pending[1].done():
                    raise ProtocolError("unsolicited control response")
                request, future = self._pending
                if (response.device_id != self.device_id or
                        response.request_id != request.request_id or
                        response.opcode != request.opcode | RESPONSE_FLAG):
                    raise ProtocolError("uncorrelated control response")
                if response.status:
                    raise ProtocolError(f"ESP rejected control status={response.status}")
                self._recent.append(identity)
                future.set_result(response)
            except (ValueError, TypeError) as exc:
                self.errors += 1
                self.log("control_invalid", validation=str(exc))
                if self._pending is not None and not self._pending[1].done():
                    self._pending[1].set_exception(exc)

    async def _exchange(self, request):
        client = self.client
        if client is None:
            raise ConnectionError("BLE control unavailable")
        if any(not task.done() for task in self._native_tasks):
            raise RuntimeError("native control write still pending")
        generation = self._generation
        response = self._loop.create_future()
        self._pending = (request, response)
        consumed = False
        try:
            await self._bounded(self._native_write(client,
                encode_control(request, mtu=client.mtu_size)), self.timeout)
            if generation != self._generation:
                raise ConnectionError("BLE disconnected during control write")
            result = await asyncio.wait_for(response, self.timeout)
            consumed = True
            return result
        except asyncio.TimeoutError:
            # Let a cancellable ATT operation retire before a byte-identical
            # chunk retry. An uncooperative operation stays tracked and rejects
            # the retry, so no second native write can overlap it.
            await asyncio.sleep(0)
            raise
        finally:
            if not response.done():
                response.cancel()
            elif not response.cancelled():
                error = response.exception()
                if not consumed and error is None:
                    result = response.result()
                    identity = (result.opcode, result.request_id, result.offset)
                    if identity in self._recent:
                        self._recent.remove(identity)
            self._pending = None

    async def _native_write(self, client, encoded):
        task = asyncio.current_task()
        self._native_tasks.add(task)
        try:
            await client.write_gatt_char(CONTROL_UUID, encoded, response=True)
        finally:
            self._native_tasks.discard(task)

    async def command(self, packet, *, expected_generation=None):
        generation = self._generation if expected_generation is None else expected_generation
        async with self._serial:
            if generation != self._generation:
                raise ConnectionError("command belongs to a disconnected BLE generation")
            if (packet.version != 2 or packet.device_id != self.device_id or
                    packet.boot_id != self.boot_id or packet.uptime_ms != 0):
                raise ValueError("command requires current boot, device, v2 and uptime zero")
            self.log("command_original", direction="laptop->ESP", request_id=packet.seq,
                     boot_id=packet.boot_id, values=list(packet.values),
                     expected_values=list(transformed_values(packet.values)))
            response = await self._exchange(ControlFrame(COMMAND, self.device_id,
                packet.seq, payload=encode_packet(packet)))
            modified = decode_packet(response.payload)
            if (response.offset != 0 or modified.version != 2 or
                    modified.device_id != packet.device_id or modified.boot_id != packet.boot_id
                    or modified.seq != packet.seq
                    or modified.values != transformed_values(packet.values)):
                raise ProtocolError("ESP command transformation or identity mismatch")
            self.log("command_modified", direction="ESP->laptop", request_id=packet.seq,
                     boot_id=modified.boot_id, uptime_ms=modified.uptime_ms,
                     values=list(modified.values), validation="verified")
            return modified

    async def set_rate(self, rate):
        generation = self._generation
        if type(rate) is not int or not 1 <= rate <= 200:
            raise ValueError("source rate must be integer 1..200 Hz")
        async with self._serial:
            if generation != self._generation:
                raise ConnectionError("rate operation belongs to a disconnected BLE generation")
            payload = struct.pack("<H", rate)
            response = await self._exchange(ControlFrame(SET_RATE, self.device_id,
                self.next_id(), payload=payload))
            if response.payload != payload or response.offset != 0:
                raise ProtocolError("ESP did not acknowledge requested source rate")
            self.log("source_rate", rate_hz=rate, validation="acknowledged")

    async def transfer_file(self, data, *, transfer_id=None, retries=2):
        if not isinstance(data, bytes) or len(data) > MAX_FILE_SIZE:
            raise ValueError("file must be bytes, at most 65536 bytes")
        transfer_id = self.next_id() if transfer_id is None else transfer_id
        generation = self._generation
        async with self._serial:
            if self.client is None or generation != self._generation:
                raise ConnectionError("BLE control unavailable")
            size = chunk_size(self.client.mtu_size)
            metadata = file_metadata(data)
            sender_length, sender_digest = parse_file_metadata(metadata)
            self.log("file_begin", direction="laptop->ESP", transfer_id=transfer_id,
                     sender_bytes=sender_length, sender_sha256=sender_digest.hex())
            started = time.monotonic()
            try:
                begin = await self._exchange(ControlFrame(FILE_BEGIN, self.device_id,
                    transfer_id, payload=metadata))
                if begin.offset != 0:
                    raise ProtocolError("file begin offset mismatch")
                for offset in range(0, len(data), size):
                    payload = data[offset:offset + size]
                    request = ControlFrame(FILE_CHUNK, self.device_id, transfer_id,
                                           offset, payload)
                    for attempt in range(retries + 1):
                        if generation != self._generation:
                            raise ConnectionError("file interrupted by BLE disconnect")
                        try:
                            ack = await self._exchange(request)
                            break
                        except asyncio.TimeoutError:
                            if attempt == retries:
                                raise
                    if ack.offset != offset + len(payload):
                        raise ProtocolError("file chunk next-offset mismatch")
                final = await self._exchange(ControlFrame(FILE_END, self.device_id,
                                                         transfer_id, len(data)))
                if final.offset != len(data) or final.payload != metadata:
                    raise ProtocolError("receiver file length or SHA-256 mismatch")
                length, digest = parse_file_metadata(final.payload)
                elapsed = time.monotonic() - started
                result = {"transfer_id": transfer_id, "bytes": length,
                          "sha256": digest.hex(), "elapsed_seconds": elapsed,
                          "sender_bytes": sender_length, "sender_sha256": sender_digest.hex(),
                          "receiver_bytes": length, "receiver_sha256": digest.hex(),
                          "file_payload_kbps": length * 8 / max(elapsed, 1e-9) / 1000,
                          "verified": True}
                self.log("file_complete", direction="laptop->ESP", **result)
                return result
            except BaseException as failure:
                self.log("file_failed", transfer_id=transfer_id,
                         validation=f"{type(failure).__name__}: {failure}")
                if generation == self._generation and self.client is not None:
                    self.log("file_abort_requested", transfer_id=transfer_id)
                    try:
                        await self._exchange(ControlFrame(FILE_ABORT, self.device_id, transfer_id))
                        self.log("file_abort_confirmed", transfer_id=transfer_id)
                    except (Exception, asyncio.CancelledError):
                        self.log("file_abort_unconfirmed", transfer_id=transfer_id)
                raise

    def summary(self):
        return {"response_errors": self.errors, "duplicate_responses": self.duplicates,
                "notification_dropped": self.notification_dropped,
                "native_pending": sum(not task.done() for task in self._native_tasks)}


class CommandPipeline:
    def __init__(self, channel, forward, *, capacity=8):
        self.channel, self.forward = channel, forward
        self.queue = asyncio.Queue(maxsize=capacity)
        self.accepted = self.rejected = self.completed = self.failed = 0
        self._accepting = True

    def submit(self, packet):
        if not self._accepting or self.channel.client is None or self.queue.full():
            self.rejected += 1
            self.channel.log("command_rejected", request_id=packet.seq, validation="busy/disconnected")
            return False
        self.queue.put_nowait((packet, self.channel._generation))
        self.accepted += 1
        self.channel.log("command_accepted", request_id=packet.seq)
        return True

    async def run(self):
        while True:
            packet, generation = await self.queue.get()
            try:
                if generation != self.channel._generation:
                    raise ConnectionError("accepted command belongs to a disconnected BLE generation")
                modified = await self.channel.command(packet, expected_generation=generation)
                await self.forward(modified, packet.seq)
                self.completed += 1
                self.channel.log("command_ingested", direction="laptop->Ultra96",
                                 request_id=packet.seq, validation="board_ACK")
            except asyncio.CancelledError:
                self.failed += 1
                self.channel.log("command_failed", request_id=packet.seq, validation="cancelled")
                raise
            except Exception as exc:
                self.failed += 1
                self.channel.log("command_failed", request_id=packet.seq,
                                 validation=f"{type(exc).__name__}: {exc}")
            finally:
                self.queue.task_done()

    async def join(self):
        await self.queue.join()

    async def stop(self):
        self._accepting = False
        while not self.queue.empty():
            packet, _generation = self.queue.get_nowait()
            self.failed += 1
            self.queue.task_done()
            self.channel.log("command_failed", request_id=packet.seq, validation="shutdown")

    def summary(self):
        return {"accepted": self.accepted, "rejected": self.rejected,
                "completed": self.completed, "failed": self.failed,
                "pending": self.accepted - self.completed - self.failed}


def dispatch_key(key, submit):
    if key in ("1", "2"):
        return submit(int(key))
    return None


async def keyboard_loop(submit, stop, *, stream=None):
    """Read individual 1/2 keys without blocking the asyncio event loop."""
    stream = stream or sys.stdin
    if not stream.isatty():
        raise ValueError("--keyboard requires an interactive terminal")
    if os.name == "nt":
        import msvcrt
        while not stop.is_set():
            for _ in range(16):
                if not msvcrt.kbhit():
                    break
                key = msvcrt.getwch()
                if key == "\x03":
                    raise KeyboardInterrupt
                dispatch_key(key, submit)
            await asyncio.sleep(0.02)
    else:
        import select
        import termios
        import tty
        descriptor = stream.fileno()
        previous = termios.tcgetattr(descriptor)
        try:
            tty.setcbreak(descriptor)
            while not stop.is_set():
                for _ in range(16):
                    if not select.select([descriptor], [], [], 0)[0]:
                        break
                    dispatch_key(os.read(descriptor, 1).decode("utf-8", errors="ignore"), submit)
                await asyncio.sleep(0.02)
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)
