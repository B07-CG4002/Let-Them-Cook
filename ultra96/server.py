"""TLS-only B07 Communications service. Ingestion ACKs and Phone results use separate ports.

Each newly accepted stream sample goes to the model (ultra96.model); its results go to the
one subscribed phone. Sensor protocol defaults to v3; v4 requires explicit selection.
"""
import argparse
import asyncio
from collections import OrderedDict
import errno
import json
import logging
import math
import os
import random
import signal
import socket
import ssl
import sys
import threading
import time
import traceback

from common.tls import server_context
from common.wire import ProtocolError, STREAM_LIMIT, read_frame, write_frame
from common.sensor import VERSION
from ultra96.model import DummyModel, ModelPort, ModelStartError, load_model
from ultra96.monitor import ServiceMonitor
from ultra96.protocol import (DUMMY_GESTURES, result_identity, trace_fields, validate_message,
                              validate_session)

LOG = logging.getLogger(__name__)
_EXPECTED_ERRORS = (ProtocolError, asyncio.IncompleteReadError, asyncio.TimeoutError,
                    ConnectionError, OSError, ssl.SSLError)
# How long an ingest connection may wait for the next frame to begin. A gated
# glove sends nothing while the player is still, and closing its stream would
# lose the first samples after the pause to a TLS reconnect. Once a frame has
# begun, the rest of it must still arrive within the usual five seconds.
INGEST_IDLE_TIMEOUT = 600.0
INGEST_IDLE_TIMEOUT_RANGE = (5.0, 86400.0)
# Both listeners share eight client slots. When they are all taken, the ingest
# connection that has waited longest for its next frame is closed to admit the
# new client, provided it has waited at least this long (the old idle limit).
# The SSH tunnel's sshd can hold the socket of a laptop that vanished for many
# minutes. A live glove that is only still reconnects when it next sends, and
# loses the frames then in flight, as it did under the old five-second limit.
# Connections that are streaming are never displaced; then the client is refused.
MAX_CLIENTS = 8
INGEST_EVICTABLE_AFTER = 5.0


async def _close_writer(writer):
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
    except (asyncio.TimeoutError, ConnectionError, OSError, ssl.SSLError):
        writer.transport.abort()


class ResultQueue:
    """Live-only bounded queue with monotonic age, shared by one subscriber."""
    def __init__(self, *, observer=None, subscriber_id=None, monitor=None):
        self._queue = asyncio.Queue(maxsize=32)
        self.dropped = 0
        self.stale = 0
        self.observer = observer
        self.observer_errors = 0
        self.subscriber_id = subscriber_id
        self.retirement_reason = None
        self.monitor = monitor

    def count(self, fate, message):
        """Per-glove sent/unsent for the board monitor, with or without an observer."""
        if self.monitor is not None:
            self.monitor.count(message.get("device_id"), fate)

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
            self.count("unsent", old)
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
                self.count("unsent", message)
                self.observe("result_drop_stale", message, received_at, reason="dequeue")
                continue
            return received_at, message

    def retire(self, reason):
        """Account unsent queued IDs after the sender has relinquished ownership."""
        while not self._queue.empty():
            received_at, message = self._queue.get_nowait()
            self.count("unsent", message)
            self.observe("result_abandoned", message, received_at, reason=reason)


class CommsServer:
    def __init__(self, ssl_context, session_id="ltc-comms", ingest_port=8888, gateway_port=9999,
                 *, observer=None, rng=None, max_namespaces=128, max_commands=4096, model=None,
                 ingest_idle_timeout=INGEST_IDLE_TIMEOUT, sensor_version=3):
        if (not isinstance(ssl_context, ssl.SSLContext)
                or ssl_context.minimum_version < ssl.TLSVersion.TLSv1_2):
            raise ValueError("server requires TLS >=1.2")
        if (not isinstance(ingest_idle_timeout, (int, float)) or isinstance(ingest_idle_timeout, bool)
                or not math.isfinite(ingest_idle_timeout) or ingest_idle_timeout <= 0):
            raise ValueError("ingest idle timeout must be positive finite seconds")
        if type(sensor_version) is not int or sensor_version not in (3,4):
            raise ValueError("sensor_version must be 3 or 4")
        self.sensor_version = sensor_version
        self.ingest_idle_timeout = float(ingest_idle_timeout)
        self.ssl_context = ssl_context
        self.session_id = validate_session(session_id)
        self.ingest_port = ingest_port
        self.gateway_port = gateway_port
        self._listeners = []
        self._accept_tasks = set()
        self._sockets = set()
        self._tasks = set()
        self._writers = set()
        # Ingest client task -> monotonic time it began waiting for its next frame.
        self._waiting_ingest = {}
        self._subscriber = None
        self.observer = observer
        # Always-on per-glove counters for the read-only board monitor (ultra96.monitor).
        self.monitor = ServiceMonitor()
        self._subscriber_generation = 0
        # Stream high-water marks are never evicted: an old identity cannot reach
        # the model again after leaving the bounded fingerprint cache. Command IDs
        # need not be ordered across relay restarts, so commands use a separate
        # bounded ledger that rejects capacity exhaustion.
        if (type(max_namespaces) is not int or max_namespaces < 1 or
                type(max_commands) is not int or max_commands < 1):
            raise ValueError("identity capacities must be positive integers")
        self._high_water = {}
        self._fingerprints = OrderedDict()
        self._commands = {}
        self._max_namespaces = max_namespaces
        self._max_commands = max_commands
        # Model result IDs already sent; the phone also de-duplicates the last 4096.
        self._emitted = OrderedDict()
        self._rng = random.SystemRandom() if rng is None else rng
        self._closing = False
        self._accept_failure = None
        self._failed = asyncio.Event()
        self.metrics = {"accepted": 0, "duplicates": 0, "rejected": 0,
                        "subscribers": 0, "replaced": 0, "disconnected_results": 0,
                        "result_drops": 0, "result_stale": 0, "client_limit": 0,
                        "idle_evicted": 0, "accept_retries": 0, "accept_failures": 0}
        if observer is not None:
            self.metrics["observer_errors"] = 0
        selected_model = DummyModel(self._rng) if model is None else model
        configure = getattr(selected_model, "configure_sensor_version", None)
        if callable(configure):
            configure(sensor_version)
        converter = None
        if sensor_version == 4:
            from common.sensor_v4 import physical_values as converter
        self.model = ModelPort(selected_model, self._model_result, self.metrics,
                               physical_converter=converter)
        self.model_start_seconds = None

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
        self.monitor.started = time.monotonic()
        # The model is ready before any listener can deliver a sample.
        self.model_start_seconds = self.model.start(asyncio.get_running_loop())
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
            if not self._closing and len(self._sockets) >= MAX_CLIENTS:
                try:
                    await self._evict_idle_ingest()
                except BaseException:
                    connection.close()
                    raise
            if self._closing or len(self._sockets) >= MAX_CLIENTS:
                self.metrics["client_limit"] += 1
                connection.close()
                continue
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._sockets.add(connection)
            task = asyncio.create_task(self._client(connection, gateway))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _evict_idle_ingest(self):
        """Close the ingest connection that has waited longest for a frame, if one may go."""
        now = time.monotonic()
        idle = [(since, task) for task, since in self._waiting_ingest.items()
                if now - since >= INGEST_EVICTABLE_AFTER and not task.done()]
        if not idle:
            return
        since, task = min(idle, key=lambda item: item[0])
        del self._waiting_ingest[task]
        self.metrics["idle_evicted"] += 1
        LOG.warning("client slots full; closing an ingest connection idle for %.1f s",
                    now - since)
        task.cancel()
        # Its cleanup closes TLS within about a second and frees the slot.
        await asyncio.wait({task}, timeout=2.0)

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
        task = asyncio.current_task()
        while not self._closing:
            # While it waits for a frame, a full server may close this connection.
            self._waiting_ingest[task] = time.monotonic()
            try:
                frame = await read_frame(reader, idle_timeout=self.ingest_idle_timeout)
            finally:
                self._waiting_ingest.pop(task, None)
            message = validate_message(frame, "SENSOR_BATCH", self.session_id, sensor_version=self.sensor_version)
            duplicate = self._duplicate(message)
            if duplicate:
                self.metrics["duplicates"] += 1
                self.monitor.count(message["device_id"], "duplicates")
                self._observe("result_duplicate", **trace_fields(message))
            else:
                self.metrics["accepted"] += 1
                self.monitor.count(message["device_id"], "accepted")
                self._observe("result_accepted", **trace_fields(message))
                if message["request_id"] is None:
                    # Runs before the ACK is written; the model must return immediately.
                    self.model.on_sample(message["device_id"], message["boot_id"], message["seq"],
                                         message["uptime_ms"], message["values"])
                else:
                    # Commands test the control path; they never reach the model.
                    result = dict(v=self.sensor_version, type="GESTURE_RESULT", **trace_fields(message))
                    result.update(result_id=result_identity(message),
                                  gesture=self._rng.choice(DUMMY_GESTURES), confidence=1.0)
                    self.monitor.published(result)
                    self._publish(result)
            ack = dict(v=message["v"], type="INGEST_ACK", **trace_fields(message))
            ack["status"] = "duplicate" if duplicate else "accepted"
            await write_frame(writer, ack)

    def _publish(self, result):
        if self._subscriber is None:
            self.metrics["disconnected_results"] += 1
            self.monitor.count(result["device_id"], "unsent")
            self._observe("result_no_subscriber", **trace_fields(result))
        else:
            self._subscriber[1].put(result)

    def _model_result(self, gesture, confidence, device_id, boot_id, seq):
        """Loop thread: one already-normalized model result for an accepted stream sample."""
        identity = dict(session_id=self.session_id, device_id=device_id, boot_id=boot_id,
                        seq=seq, request_id=None)
        high_water = self._high_water.get((device_id, boot_id))
        if high_water is None or seq > high_water:
            self.metrics["model_results_rejected"] += 1
            self.monitor.refused(device_id, boot_id, seq, "unknown_identity")
            self._observe("model_result_refused", reason="unknown_identity", **identity)
            self.model.warn("identity", "model result names a sample this service has not accepted")
            return
        result_id = "{}:{}:{}".format(device_id, boot_id, seq)
        if result_id in self._emitted:
            self.metrics["model_results_duplicate"] += 1
            self.monitor.refused(device_id, boot_id, seq, "duplicate")
            self._observe("model_result_refused", reason="duplicate", **identity)
            self.model.warn("duplicate", "model emitted a second result for one sample")
            return
        self._emitted[result_id] = None
        if len(self._emitted) > 4096:
            self._emitted.popitem(last=False)
        result = dict(v=self.sensor_version, type="GESTURE_RESULT", result_id=result_id, gesture=gesture,
                      confidence=confidence, **identity)
        validate_message(result, "GESTURE_RESULT", self.session_id, sensor_version=self.sensor_version)
        self.metrics["model_results"] += 1
        self.monitor.published(result)
        self._observe("model_result", **identity)
        self._publish(result)

    def _duplicate(self, message):
        """Record acceptance atomically before the next await; no outage replay.

        State is scoped to this server process/configured session. A fresh server
        must use a fresh session when old inputs might be replayed. Telemetry seq
        must increase within a boot (no wrap); command request IDs must not repeat
        within a boot except exact retries. Capacity failures close ingestion.
        """
        trace = (message["device_id"], message["boot_id"], message["seq"])
        fingerprint = (message["uptime_ms"], tuple(message["values"]))
        command = message["request_id"] is not None
        ledger = self._commands if command else self._fingerprints
        if trace in ledger:
            if ledger[trace] != fingerprint:
                raise ProtocolError("conflicting payload for existing identity")
            return True
        if command:
            if len(ledger) >= self._max_commands:
                raise ProtocolError("command identity capacity exhausted; start a new session")
        else:
            namespace = trace[:2]
            if namespace in self._high_water:
                if message["seq"] <= self._high_water[namespace]:
                    raise ProtocolError("stale stream identity outside replay window")
            elif len(self._high_water) >= self._max_namespaces:
                raise ProtocolError("stream identity capacity exhausted; start a new session")
            self._high_water[namespace] = message["seq"]
        ledger[trace] = fingerprint
        if not command and len(ledger) > 4096:
            ledger.popitem(last=False)
        return False

    async def _gateway(self, reader, writer):
        frame = await read_frame(reader)
        if isinstance(frame, dict) and frame.get("type") == "MONITOR":
            # A read-only board monitor: it never claims or replaces the subscriber.
            validate_message(frame, "MONITOR", self.session_id)
            await self.monitor.serve(reader, writer, self.session_id, self._monitor_status)
            return
        validate_message(frame, "SUBSCRIBE", self.session_id)
        self._subscriber_generation += 1
        queue = ResultQueue(observer=self._observe if self.observer is not None else None,
                            subscriber_id=self._subscriber_generation, monitor=self.monitor)
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
            self.monitor.subscriber("claimed", queue.subscriber_id)
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
                self.monitor.subscriber("ended", queue.subscriber_id, reason)
                self._observe("subscriber_ended", session_id=self.session_id,
                              subscriber_id=queue.subscriber_id, reason=reason, **error_fields)

    def _monitor_status(self):
        """Service-wide fields of a board monitor update, read by the monitor's own task."""
        subscriber = None if self._subscriber is None else self._subscriber[1].subscriber_id
        return dict(model=self.model.name, subscriber=subscriber,
                    identities=len(self._high_water), identity_limit=self._max_namespaces,
                    metrics=self.metrics)

    async def _send_results(self, writer, queue):
        while True:
            received_at, result = await queue.get_timed()
            remaining = 2.0 - (time.monotonic() - received_at)
            if remaining <= 0:
                queue.stale += 1
                queue.count("unsent", result)
                queue.observe("result_drop_stale", result, received_at, reason="before_write")
                continue
            # A drain deadline bounds our own backlog. Bytes already handed to
            # TCP/TLS cannot be retracted, so receiver rendering age is separate.
            queue.observe("result_send_started", result, received_at)
            try:
                await write_frame(writer, result, timeout=remaining)
            except asyncio.CancelledError:
                queue.count("unsent", result)
                queue.observe("result_send_cancelled", result, received_at, ambiguous=True)
                raise
            except Exception as exc:
                queue.count("unsent", result)
                queue.observe("result_send_failed", result, received_at,
                              ambiguous=True, error_type=type(exc).__name__)
                raise
            queue.count("sent", result)
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
        self.model.close()


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


def _model_failed(spec, exc):
    print(json.dumps({"event": "model_failed", "model": spec or "dummy",
                      "error_type": type(exc.__cause__ or exc).__name__}), flush=True)


async def _run_service(args, observer=None):
    spec = getattr(args, "model", None)
    # Only loading the model and its start(emit) are model failures; a bad session,
    # certificate or port is reported as itself.
    try:
        model = load_model(spec, getattr(args, "model_path", None))
    except (Exception, SystemExit) as exc:
        _model_failed(spec, exc)
        if isinstance(exc, SystemExit):  # exit nonzero even for a bare sys.exit() in the model
            raise ModelStartError("importing the model raised SystemExit") from exc
        raise
    service = CommsServer(server_context(args.cert, args.key), session_id=args.session_id,
                          ingest_port=args.ingest_port, gateway_port=args.gateway_port,
                          observer=observer, model=model,
                          ingest_idle_timeout=getattr(args, "ingest_idle_timeout",
                                                      INGEST_IDLE_TIMEOUT),
                          sensor_version=getattr(args, "sensor_version", 3))
    try:
        await service.start()
    except ModelStartError as exc:
        _model_failed(spec, exc)
        raise
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    handles_sigterm = False
    waiters = ()
    try:
        print(json.dumps({"event": "model_ready", "model": service.model.name,
                          "seconds": round(service.model_start_seconds, 3)}), flush=True)
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


def _ingest_idle_timeout(value):
    low, high = INGEST_IDLE_TIMEOUT_RANGE
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "ingest idle timeout must be {:g}..{:g} seconds".format(low, high)) from exc
    if not math.isfinite(parsed) or not low <= parsed <= high:
        raise argparse.ArgumentTypeError(
            "ingest idle timeout must be {:g}..{:g} seconds".format(low, high))
    return parsed


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sensor-version", type=int, choices=(3,4), default=3)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--session-id", default="ltc-comms")
    parser.add_argument("--ingest-port", type=int, default=8888)
    parser.add_argument("--gateway-port", type=int, default=9999)
    parser.add_argument("--event-log-dir", help="new directory for bounded result-path diagnostics")
    parser.add_argument("--model", metavar="MODULE",
                        help="importable module with start(emit) and on_sample(...); "
                             "default: the built-in dummy model")
    parser.add_argument("--model-path", metavar="DIR",
                        help="directory put first on the import path before loading --model")
    parser.add_argument("--ingest-idle-timeout", metavar="SECONDS", type=_ingest_idle_timeout,
                        default=INGEST_IDLE_TIMEOUT,
                        help="how long an ingest connection may wait for its next frame "
                             "(5..86400, default 600); a gated glove is silent while idle")
    return parser


def main():
    args = _parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass
    except BaseException as exc:
        if _model_threads_left():
            traceback.print_exc()
            _exit_now(exc.code if isinstance(exc, SystemExit) and type(exc.code) is int else 1)
        raise
    if _model_threads_left():
        _exit_now(0)


def _model_threads_left():
    """Non-daemon threads a model started; they would keep a stopped service alive."""
    return [thread for thread in threading.enumerate()
            if thread is not threading.main_thread() and not thread.daemon and thread.is_alive()]


def _exit_now(code):
    # The service has closed its ports and printed its metrics; nothing else may hold it.
    print(json.dumps({"event": "exit_with_model_threads",
                      "threads": len(_model_threads_left())}), flush=True)
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    main()
