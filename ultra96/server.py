"""TLS-only B07 Communications service. Ingestion ACKs and Phone results use separate ports."""
import argparse
import asyncio
from collections import OrderedDict
import errno
import json
import logging
import os
import random
import signal
import socket
import ssl
import time

from common.tls import server_context
from common.wire import ProtocolError, STREAM_LIMIT, read_frame, write_frame
from ultra96.protocol import GESTURES, result_identity, trace_fields, validate_message, validate_session

LOG = logging.getLogger(__name__)
_EXPECTED_ERRORS = (ProtocolError, asyncio.IncompleteReadError, asyncio.TimeoutError,
                    ConnectionError, OSError, ssl.SSLError)


async def _close_writer(writer):
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
    except (asyncio.TimeoutError, ConnectionError, OSError, ssl.SSLError):
        writer.transport.abort()


class ResultQueue:
    """Live-only bounded queue with monotonic age, shared by one subscriber."""
    def __init__(self, *, observer=None, subscriber_id=None):
        self._queue = asyncio.Queue(maxsize=32)
        self.dropped = 0
        self.stale = 0
        self.observer = observer
        self.observer_errors = 0
        self.subscriber_id = subscriber_id
        self.retirement_reason = None

    def observe(self, event, message, received_at=None, **fields):
        if self.observer is None:
            return
        fields.update({key: message[key] for key in
                       ("session_id", "device_id", "boot_id", "seq", "request_id") if key in message})
        fields.update(subscriber_id=self.subscriber_id, queue_size=self._queue.qsize())
        if received_at is not None:
            fields["age_seconds"] = max(0.0, time.monotonic() - received_at)
        try:
            self.observer(event, **fields)
        except Exception:
            self.observer_errors += 1

    def put(self, message, received_at=None):
        if self._queue.full():
            old_at, old = self._queue.get_nowait()
            self.dropped += 1
            self.observe("result_drop_oldest", old, old_at)
        received_at = time.monotonic() if received_at is None else received_at
        self._queue.put_nowait((received_at, message))
        self.observe("result_enqueued", message, received_at)

    async def get(self, now=None):
        _, message = await self.get_timed(now=now)
        return message

    async def get_timed(self, now=None):
        while True:
            received_at, message = await self._queue.get()
            current = time.monotonic() if now is None else now
            if current - received_at >= 2.0:
                self.stale += 1
                self.observe("result_drop_stale", message, received_at, reason="dequeue")
                continue
            return received_at, message

    def retire(self, reason):
        """Account unsent queued IDs after the sender has relinquished ownership."""
        while not self._queue.empty():
            received_at, message = self._queue.get_nowait()
            self.observe("result_abandoned", message, received_at, reason=reason)


class CommsServer:
    def __init__(self, ssl_context, session_id="ltc-comms", ingest_port=8888, gateway_port=9999,
                 *, observer=None, rng=None, max_v2_namespaces=128, max_v2_commands=4096):
        if (not isinstance(ssl_context, ssl.SSLContext)
                or ssl_context.minimum_version < ssl.TLSVersion.TLSv1_2):
            raise ValueError("server requires TLS >=1.2")
        self.ssl_context = ssl_context
        self.session_id = validate_session(session_id)
        self.ingest_port = ingest_port
        self.gateway_port = gateway_port
        self._listeners = []
        self._accept_tasks = set()
        self._sockets = set()
        self._tasks = set()
        self._writers = set()
        self._subscriber = None
        self.observer = observer
        self._subscriber_generation = 0
        self._recent = OrderedDict()
        # V2 stream high-water marks are never evicted: an old identity cannot
        # draw another random event after leaving the bounded fingerprint cache.
        # Command IDs need not be ordered across relay restarts, so commands use
        # a separate bounded ledger that rejects capacity exhaustion.
        if (type(max_v2_namespaces) is not int or max_v2_namespaces < 1 or
                type(max_v2_commands) is not int or max_v2_commands < 1):
            raise ValueError("v2 identity capacities must be positive integers")
        self._v2_high_water = {}
        self._v2_recent = OrderedDict()
        self._v2_commands = {}
        self._max_v2_namespaces = max_v2_namespaces
        self._max_v2_commands = max_v2_commands
        self._rng = random.SystemRandom() if rng is None else rng
        self._closing = False
        self._accept_failure = None
        self._failed = asyncio.Event()
        self.metrics = {"accepted": 0, "duplicates": 0, "rejected": 0,
                        "subscribers": 0, "replaced": 0, "disconnected_results": 0,
                        "result_drops": 0, "result_stale": 0, "client_limit": 0,
                        "accept_retries": 0, "accept_failures": 0}
        if observer is not None:
            self.metrics["observer_errors"] = 0

    def _observe(self, event, **fields):
        """Observers must be nonblocking; diagnostic failures never stop forwarding."""
        if self.observer is not None:
            try:
                self.observer(event, **fields)
            except Exception:
                self.metrics["observer_errors"] = self.metrics.get("observer_errors", 0) + 1

    async def start(self):
        if self._listeners or self._closing:
            raise RuntimeError("server is already started or closed")
        try:
            for gateway, port in ((False, self.ingest_port), (True, self.gateway_port)):
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.setblocking(False)
                self._listeners.append(listener)
                if os.name == "posix":
                    # Match asyncio's POSIX listener default: stopped clients
                    # in TIME_WAIT must not prevent an immediate service restart.
                    # Windows has different reuse semantics and does not need it.
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", port))
                listener.listen(8)
                task = asyncio.create_task(self._accept(listener, gateway))
                self._accept_tasks.add(task)
                task.add_done_callback(self._accept_done)
            self.ingest_port = self._listeners[0].getsockname()[1]
            self.gateway_port = self._listeners[1].getsockname()[1]
        except BaseException:
            await self.close()
            raise

    async def _accept(self, listener, gateway):
        loop = asyncio.get_running_loop()
        retry_delay = 0.1
        while not self._closing:
            try:
                connection, _ = await loop.sock_accept(listener)
            except OSError as error:
                # sock_accept propagates aborted queued peers and temporary
                # resource exhaustion; unlike asyncio.start_server it supplies
                # no automatic accept retry. Avoid spinning on a readable socket.
                if (not isinstance(error, ConnectionAbortedError)
                        and error.errno not in (errno.ECONNABORTED, errno.EMFILE,
                            errno.ENFILE, errno.ENOBUFS, errno.ENOMEM)
                        and getattr(error, "winerror", None) not in (10024, 10053, 10055)):
                    raise
                self.metrics["accept_retries"] += 1
                await asyncio.sleep(retry_delay)
                retry_delay = min(1.0, retry_delay * 2)
                continue
            retry_delay = 0.1
            connection.setblocking(False)
            if self._closing or len(self._sockets) >= 8:
                self.metrics["client_limit"] += 1
                connection.close()
                continue
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._sockets.add(connection)
            task = asyncio.create_task(self._client(connection, gateway))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _accept_done(self, task):
        if task.cancelled():
            error = RuntimeError("listener task cancelled unexpectedly")
        else:
            error = task.exception() or RuntimeError("listener task returned unexpectedly")
        if self._closing:
            return
        self.metrics["accept_failures"] += 1
        if self._accept_failure is None:
            self._accept_failure = error
        self._failed.set()
        LOG.error("listener task failed: %s", type(error).__name__)

    async def wait_failure(self):
        """Service hosts await this so a dead listener cannot look healthy."""
        await self._failed.wait()
        raise RuntimeError("B07 Communications listener failed") from self._accept_failure

    async def _client(self, connection, gateway):
        writer = None
        try:
            loop = asyncio.get_running_loop()
            reader = asyncio.StreamReader(limit=STREAM_LIMIT)
            protocol = asyncio.StreamReaderProtocol(reader)
            transport, _ = await loop.connect_accepted_socket(lambda: protocol, connection,
                ssl=self.ssl_context, ssl_handshake_timeout=5.0)
            writer = asyncio.StreamWriter(transport, protocol, reader, loop)
            transport.set_write_buffer_limits(high=32768, low=16384)
            self._writers.add(writer)
            if gateway:
                await self._gateway(reader, writer)
            else:
                await self._ingest(reader, writer)
        except asyncio.IncompleteReadError:
            pass  # read_frame preserves this only for EOF between complete frames.
        except _EXPECTED_ERRORS:
            self.metrics["rejected"] += 1
        finally:
            try:
                if writer is not None:
                    await _close_writer(writer)
            finally:
                # close() can cancel a client that was already awaiting TLS
                # shutdown here. Always release transport/socket ownership,
                # even when that cancellation interrupts graceful closure.
                try:
                    if writer is not None:
                        writer.transport.abort()
                finally:
                    self._writers.discard(writer)
                    try:
                        connection.close()
                    finally:
                        self._sockets.discard(connection)

    async def _ingest(self, reader, writer):
        while not self._closing:
            message = validate_message(await read_frame(reader), "SENSOR_BATCH", self.session_id)
            trace = (message["device_id"], message["boot_id"], message["seq"])
            duplicate = (self._v2_duplicate(message) if message["v"] == 2
                         else trace in self._recent)
            if duplicate:
                self.metrics["duplicates"] += 1
                self._observe("result_duplicate", **trace_fields(message))
            else:
                if message["v"] == 1:
                    self._recent[trace] = None
                    if len(self._recent) > 4096:
                        self._recent.popitem(last=False)
                self.metrics["accepted"] += 1
                self._observe("result_accepted", **trace_fields(message))
                LOG.info("accepted session=%s device=%d boot=%d seq=%d",
                         self.session_id, *trace)
                result = dict(v=message["v"], type="GESTURE_RESULT", **trace_fields(message))
                result.update(result_id=result_identity(message),
                              gesture=(self._rng.choice(GESTURES) if message["v"] == 2
                                       else GESTURES[message["seq"] % 4]), confidence=1.0)
                if self._subscriber is None:
                    self.metrics["disconnected_results"] += 1
                    self._observe("result_no_subscriber", **trace_fields(message))
                else:
                    self._subscriber[1].put(result)
            ack = dict(v=message["v"], type="INGEST_ACK", **trace_fields(message))
            ack["status"] = "duplicate" if duplicate else "accepted"
            await write_frame(writer, ack)

    def _v2_duplicate(self, message):
        """Record acceptance atomically before the next await; no outage replay.

        State is scoped to this server process/configured session. A fresh server
        must use a fresh session when old inputs might be replayed. Telemetry seq
        must increase within a boot (no wrap); command request IDs must not repeat
        within a boot except exact retries. Capacity failures close ingestion.
        """
        trace = (message["device_id"], message["boot_id"], message["seq"])
        fingerprint = (message["uptime_ms"], tuple(message["values"]))
        command = message["request_id"] is not None
        ledger = self._v2_commands if command else self._v2_recent
        if trace in ledger:
            if ledger[trace] != fingerprint:
                raise ProtocolError("conflicting payload for existing v2 identity")
            return True
        if command:
            if len(ledger) >= self._max_v2_commands:
                raise ProtocolError("v2 command identity capacity exhausted; start a new session")
        else:
            namespace = trace[:2]
            if namespace in self._v2_high_water:
                if message["seq"] <= self._v2_high_water[namespace]:
                    raise ProtocolError("stale v2 stream identity outside replay window")
            elif len(self._v2_high_water) >= self._max_v2_namespaces:
                raise ProtocolError("v2 stream identity capacity exhausted; start a new session")
            self._v2_high_water[namespace] = message["seq"]
        ledger[trace] = fingerprint
        if not command and len(ledger) > 4096:
            ledger.popitem(last=False)
        return False

    async def _gateway(self, reader, writer):
        validate_message(await read_frame(reader), "SUBSCRIBE", self.session_id)
        self._subscriber_generation += 1
        queue = ResultQueue(observer=self._observe if self.observer is not None else None,
                            subscriber_id=self._subscriber_generation)
        owner = (writer, queue)
        previous = self._subscriber
        self._subscriber = owner
        self.metrics["subscribers"] += 1
        sender = monitor = None
        reason = "disconnected"
        error_fields = {}
        try:
            self._observe("subscriber_claimed", session_id=self.session_id,
                          subscriber_id=queue.subscriber_id)
            if previous is not None:
                self.metrics["replaced"] += 1
                previous[1].retirement_reason = "replaced"
                self._observe("subscriber_replaced", session_id=self.session_id,
                              subscriber_id=queue.subscriber_id,
                              previous_subscriber_id=previous[1].subscriber_id)
                previous[0].close()
            await write_frame(writer, {"v": 1, "type": "SUBSCRIBED", "session_id": self.session_id})
            self._observe("subscribed_write_complete", session_id=self.session_id,
                          subscriber_id=queue.subscriber_id, phone_receipt_confirmed=False)
            sender = asyncio.create_task(self._send_results(writer, queue))
            # After SUBSCRIBE the connection is receive-only; EOF or extra bytes end ownership.
            monitor = asyncio.create_task(reader.read(1))
            finished, _ = await asyncio.wait((sender, monitor), return_when=asyncio.FIRST_COMPLETED)
            for task in finished:
                task.result()
            reason = ("peer_eof" if monitor.result() == b"" else "unexpected_input") \
                if monitor in finished else "sender_stopped"
        except asyncio.CancelledError:
            reason = "server_shutdown" if self._closing else "cancelled"
            raise
        except Exception as exc:
            reason = "error"
            error_fields["error_type"] = type(exc).__name__
            raise
        finally:
            if self._subscriber is owner:
                self._subscriber = None
            for task in (sender, monitor):
                if task is not None:
                    task.cancel()
            try:
                await asyncio.gather(*(task for task in (sender, monitor) if task is not None),
                                     return_exceptions=True)
            finally:
                reason = queue.retirement_reason or reason
                queue.retire(reason)
                self.metrics["result_drops"] += queue.dropped
                self.metrics["result_stale"] += queue.stale
                self._observe("subscriber_ended", session_id=self.session_id,
                              subscriber_id=queue.subscriber_id, reason=reason, **error_fields)

    async def _send_results(self, writer, queue):
        while True:
            received_at, result = await queue.get_timed()
            remaining = 2.0 - (time.monotonic() - received_at)
            if remaining <= 0:
                queue.stale += 1
                queue.observe("result_drop_stale", result, received_at, reason="before_write")
                continue
            # A drain deadline bounds our own backlog. Bytes already handed to
            # TCP/TLS cannot be retracted, so receiver rendering age is separate.
            queue.observe("result_send_started", result, received_at)
            try:
                await write_frame(writer, result, timeout=remaining)
            except asyncio.CancelledError:
                queue.observe("result_send_cancelled", result, received_at, ambiguous=True)
                raise
            except Exception as exc:
                queue.observe("result_send_failed", result, received_at,
                              ambiguous=True, error_type=type(exc).__name__)
                raise
            queue.observe("result_write_complete", result, received_at,
                          phone_receipt_confirmed=False)

    async def close(self):
        self._closing = True
        for task in self._accept_tasks:
            task.cancel()
        await asyncio.gather(*self._accept_tasks, return_exceptions=True)
        self._accept_tasks.clear()
        for listener in self._listeners:
            listener.close()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._listeners.clear()
        self._subscriber = None
        self._recent.clear()


async def _run(args):
    observer = None
    if getattr(args, "event_log_dir", None) is not None:
        from ultra96.diagnostics import BoundedEventLog
        observer = BoundedEventLog(args.event_log_dir)
    try:
        await _run_service(args, observer)
    finally:
        if observer is not None:
            try:
                state = observer.close(timeout=2.0)
            except Exception as exc:
                state = {"complete": False, "error_type": type(exc).__name__}
            print(json.dumps({"event": "observation_stopped", "state": state}), flush=True)


async def _run_service(args, observer=None):
    service = CommsServer(server_context(args.cert, args.key), session_id=args.session_id,
                          ingest_port=args.ingest_port, gateway_port=args.gateway_port,
                          observer=observer)
    await service.start()
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    handles_sigterm = False
    waiters = ()
    try:
        try:
            loop.add_signal_handler(signal.SIGTERM, stopped.set)
            handles_sigterm = True
        except (NotImplementedError, RuntimeError):
            # Windows event loops and non-main-thread hosts do not support this
            # API. asyncio.run retains the existing Ctrl+C cancellation path.
            pass
        print(json.dumps({"event": "listening", "host": "127.0.0.1", "tls": True,
                          "ingest_port": service.ingest_port, "gateway_port": service.gateway_port}), flush=True)
        failure = asyncio.create_task(service.wait_failure())
        termination = asyncio.create_task(stopped.wait())
        waiters = (failure, termination)
        finished, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if failure in finished:
            await failure  # Preserve a fatal listener's nonzero CLI exit.
    finally:
        for task in waiters:
            task.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        try:
            await service.close()
            print(json.dumps({"event": "stopped", "metrics": service.metrics}), flush=True)
        finally:
            if handles_sigterm:
                loop.remove_signal_handler(signal.SIGTERM)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--session-id", default="ltc-comms")
    parser.add_argument("--ingest-port", type=int, default=8888)
    parser.add_argument("--gateway-port", type=int, default=9999)
    parser.add_argument("--event-log-dir", help="new directory for bounded result-path diagnostics")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
