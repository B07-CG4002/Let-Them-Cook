"""Correlate input ACKs and independent results through existing local TLS forwards.

Starts no server or SSH process. Default input is synthetic; --ble uses the
existing protected BLE bridge. Remote provenance is an operator assertion,
never something a successful connection to a local port establishes.
"""
import argparse
import asyncio
import json
import math
import secrets
import ssl
import time
from types import SimpleNamespace

from common.sensor import SensorPacket, dummy_values, encode_packet
from common.tls import TLS_SERVER_NAME, client_context
from common.wire import STREAM_LIMIT, ProtocolError, read_frame, write_frame
from laptop.bridge import Bridge, BridgeConfig, StreamTracker
from ultra96.protocol import validate_message


async def read_live_frame(reader, idle_timeout, frame_timeout=5.0):
    """Allow quiet startup while keeping the five-second partial-frame deadline."""
    first = await asyncio.wait_for(reader.readexactly(1), idle_timeout)
    class PrefixedReader:
        def __init__(self):
            self.prefix = first

        async def readexactly(self, count):
            prefix, self.prefix = self.prefix, b""
            try:
                return prefix + await reader.readexactly(count - len(prefix))
            except asyncio.IncompleteReadError as exc:
                # Preserve the consumed byte so partial header EOF stays invalid.
                raise asyncio.IncompleteReadError(prefix + exc.partial, count) from exc
    return await read_frame(PrefixedReader(), timeout=frame_timeout)


class Correlation:
    """Exact, bounded acceptance ledger; overflow invalidates the audit."""
    def __init__(self, capacity=65536, start=None):
        self.capacity = capacity
        self.accepted_ids = set()
        self.result_ids = set()
        self.acks = self.results = self.duplicate_acks = 0
        self.duplicate_results = self.repeated_accepted_acks = self.overflow = 0
        self.first_result = self.last_result = None
        self.start = time.monotonic() if start is None else start
        self.activity = {name: dict(first=None, last=None, max_gap=0.0)
                         for name in ("ack", "result")}

    def _activity(self, name):
        activity = self.activity[name]
        now = time.monotonic()
        if activity["last"] is not None:
            activity["max_gap"] = max(activity["max_gap"], now - activity["last"])
        if activity["first"] is None:
            activity["first"] = now
        activity["last"] = now

    def timing(self, end):
        report = {}
        for name, activity in self.activity.items():
            first, last = activity["first"], activity["last"]
            report[name] = dict(
                first_activity_seconds=None if first is None else round(first - self.start, 6),
                last_activity_seconds=None if last is None else round(last - self.start, 6),
                active_span_seconds=0.0 if first is None else round(last - first, 6),
                max_gap_seconds=None if last is None else round(
                    max(activity["max_gap"], end - last), 6))
        return report

    def _add(self, collection, trace):
        if len(collection) < self.capacity:
            collection.add(trace)
        else:
            self.overflow += 1

    def ack(self, trace, status):
        self._activity("ack")
        self.acks += 1
        if status == "duplicate":
            self.duplicate_acks += 1
        elif trace in self.accepted_ids:
            self.repeated_accepted_acks += 1
        else:
            self._add(self.accepted_ids, trace)

    def result(self, trace):
        self._activity("result")
        self.results += 1
        self.first_result = self.first_result or trace
        self.last_result = trace
        if trace in self.result_ids:
            self.duplicate_results += 1
        else:
            self._add(self.result_ids, trace)

    def summary(self):
        missing = self.accepted_ids - self.result_ids
        unexpected = self.result_ids - self.accepted_ids
        return dict(acks=self.acks, accepted_acks=self.acks - self.duplicate_acks,
            unique_accepted_acks=len(self.accepted_ids),
            duplicate_acks=self.duplicate_acks, results=self.results,
            unique_results=len(self.result_ids), matched=len(self.accepted_ids & self.result_ids),
            missing_results=len(missing), unexpected_results=len(unexpected),
            missing_result_sample=sorted(missing)[:20], unexpected_result_sample=sorted(unexpected)[:20],
            duplicate_results=self.duplicate_results, repeated_accepted_acks=self.repeated_accepted_acks,
            overflow=self.overflow, capacity_per_side=self.capacity,
            first_result=self.first_result, last_result=self.last_result,
            exact_match=not (missing or unexpected or self.duplicate_results
                            or self.repeated_accepted_acks or self.overflow))


class ObservedBridge(Bridge):
    """Observe only ACKs already checked by the deployed bridge's contract."""
    def __init__(self, config, ledger, emit):
        super().__init__(config)
        self.ledger, self.emit = ledger, emit
        self.synthetic_boot_id = secrets.randbits(32)

    def _check_ack(self, ack, packet):
        super()._check_ack(ack, packet)
        trace = "{}:{}:{}".format(packet.device_id, packet.boot_id, packet.seq)
        self.ledger.ack(trace, ack["status"])
        self.emit("ack", result_id=trace, status=ack["status"])

    async def dummy_loop(self, rate=10.0):
        self.inbox.activate(1)
        seq = 0
        while True:
            packet = SensorPacket(1, self.synthetic_boot_id, seq,
                                  (seq * 100) & 0xffffffff, dummy_values(seq))
            self.inbox.put(1, encode_packet(packet), self.clock())
            seq = (seq + 1) & 0xffffffff
            await asyncio.sleep(1.0 / rate)


async def rehearse_remote(ca_file, duration=40.0, target=100, ble=False,
                         ingest_port=18888, gateway_port=19999, session_id="ltc-comms",
                         confirmed_remote_topology=False, on_event=None, address=None):
    """Use two independently supplied forwards; collect JSON-safe exact evidence.

    Faults remain failures even when later recovery succeeds. Server-side logs,
    SSH ownership/provenance and physical encryption must be collected separately.
    The 65536-ID ledger covers a 600-second 10 Hz soak with ample margin; a longer
    run that exhausts it explicitly fails instead of claiming approximate matching.
    """
    if not math.isfinite(duration) or duration <= 0 or type(target) is not int or target < 0:
        raise ValueError("duration must be positive finite and target nonnegative")
    if type(gateway_port) is not int or not 1 <= gateway_port <= 65535:
        raise ValueError("gateway port must be 1..65535")
    start = time.monotonic()
    def emit(event, **fields):
        if on_event is not None:
            on_event(dict(event=event, session_id=session_id,
                          elapsed_seconds=round(time.monotonic() - start, 6), **fields))
    ledger = Correlation(start=start)
    bridge = ObservedBridge(BridgeConfig(ca_file=ca_file, port=ingest_port,
        session_id=session_id, address=address), ledger, emit)
    subscriber = dict(connections=0, transport_errors=0, cleanup_errors=0,
                      fatal_error=None, last_transport_error=None)
    tracker = StreamTracker()
    context = client_context(ca_file)
    reader = writer = None
    consumer = producer = None
    bridge_summary = None

    async def close_viewer():
        nonlocal writer, reader
        old, writer, reader = writer, None, None
        if old is None:
            return
        old.close()
        try:
            await asyncio.wait_for(old.wait_closed(), 1.0)
        except (OSError, asyncio.TimeoutError):
            subscriber["cleanup_errors"] += 1
            old.transport.abort()
        except asyncio.CancelledError:
            old.transport.abort()
            raise

    async def subscribe():
        nonlocal reader, writer
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1",
            gateway_port, ssl=context, server_hostname=TLS_SERVER_NAME,
            limit=STREAM_LIMIT, ssl_handshake_timeout=5.0), 5.0)
        writer.transport.set_write_buffer_limits(high=32768, low=16384)
        await write_frame(writer, dict(v=1, type="SUBSCRIBE", session_id=session_id))
        validate_message(await read_frame(reader), "SUBSCRIBED", session_id)
        subscriber["connections"] += 1
        emit("subscribed", connection=subscriber["connections"])

    async def receive():
        delay = 0.5
        while True:
            try:
                if reader is None:
                    await subscribe()
                # Idle wait covers BLE setup, but a frame still has only five
                # seconds once its first byte arrives. A fault closes this reader
                # before reconnection; no partially consumed frame is resumed.
                result = validate_message(await read_live_frame(reader, max(30.0, duration + 5.0)),
                                          "GESTURE_RESULT", session_id)
                ledger.result(result["result_id"])
                tracker.observe(SimpleNamespace(**result))
                emit("result", result_id=result["result_id"], gesture=result["gesture"],
                     confidence=result["confidence"])
                delay = 0.5
            except (ssl.SSLCertVerificationError, ProtocolError) as exc:
                subscriber["fatal_error"] = type(exc).__name__
                emit("subscriber_fatal", error_type=type(exc).__name__)
                return
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
                subscriber["transport_errors"] += 1
                subscriber["last_transport_error"] = type(exc).__name__
                emit("subscriber_reconnect", error_type=type(exc).__name__, delay_seconds=delay)
                await close_viewer()
                await asyncio.sleep(delay)
                delay = min(5.0, delay * 2)

    try:
        # No input worker exists until strict TLS plus SUBSCRIBED has completed.
        try:
            await subscribe()
        except (ssl.SSLError, ProtocolError, OSError, asyncio.TimeoutError,
                asyncio.IncompleteReadError) as exc:
            subscriber["fatal_error"] = type(exc).__name__
            emit("subscriber_fatal", error_type=type(exc).__name__)
        if subscriber["fatal_error"] is None:
            consumer = asyncio.create_task(receive())
            producer = asyncio.create_task(bridge.run(duration=duration, target=target, mock=not ble))
            done, _ = await asyncio.wait((producer, consumer), return_when=asyncio.FIRST_COMPLETED)
            if producer in done:
                bridge_summary = producer.result()
                deadline = time.monotonic() + 2.0
                while not ledger.accepted_ids.issubset(ledger.result_ids) and not consumer.done():
                    if time.monotonic() >= deadline:
                        break
                    await asyncio.sleep(0.025)
            if consumer.done():
                consumer.result()
    finally:
        for task in (consumer, producer):
            if task is not None and not task.done():
                task.cancel()
        tasks = [task for task in (consumer, producer) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await close_viewer()
    if bridge_summary is None:
        bridge_summary = dict(bridge.summary(), mock_input=not ble)
    subscriber.update(gaps=tracker.gaps, duplicates=tracker.duplicates,
                      out_of_order=tracker.out_of_order, new_boots=tracker.new_boots)
    correlation = ledger.summary()
    timing = ledger.timing(time.monotonic())
    minimum_count = math.ceil(duration * 10.0 * 0.90) if target == 0 else target
    count_met = (bridge_summary["acked"] >= minimum_count
                 and correlation["unique_results"] >= minimum_count)
    activity_met = all(item["max_gap_seconds"] is not None and item["max_gap_seconds"] <= 5.0
                       for item in timing.values())
    sustained = dict(enabled=target == 0, expected_rate_hz=10.0, minimum_fraction=0.90,
        minimum_count=minimum_count, count_met=count_met, maximum_silence_seconds=5.0,
        activity_met=activity_met, coverage_met=count_met and activity_met)
    clean_connections = (bridge_summary["transport_connections"] == 1
        and bridge_summary["ble_connections"] == (1 if ble else 0)
        and subscriber["connections"] == 1)
    failures = ("malformed", "ack_errors", "transport_errors", "ble_errors", "cleanup_errors",
                "queue_dropped", "stale_dropped", "gaps", "duplicates", "out_of_order",
                "ambiguous_dropped", "duplicate_acks", "generation_dropped", "new_boots")
    passed = bool(bridge_summary["acked"] > 0 and (not target or bridge_summary["acked"] >= target)
        and clean_connections and (target != 0 or sustained["coverage_met"])
        and correlation["exact_match"] and not any(bridge_summary[key] for key in failures)
        and not any(subscriber[key] for key in ("transport_errors", "cleanup_errors", "fatal_error",
                                                "gaps", "duplicates", "out_of_order", "new_boots")))
    return dict(passed=passed, source="real BLE" if ble else "synthetic",
        topology=("operator-confirmed SSH forwards to actual Ultra96; independent desktop subscriber"
                  if confirmed_remote_topology else "existing local forwards; remote provenance unverified"),
        remote_provenance_operator_confirmed=confirmed_remote_topology,
        endpoints=dict(host="127.0.0.1", ingest_port=ingest_port, gateway_port=gateway_port),
        session_id=session_id, protected_policy=bool(ble),
        synthetic_boot_id=None if ble else bridge.synthetic_boot_id,
        elapsed_seconds=round(time.monotonic() - start, 3), bridge=bridge_summary,
        subscriber=subscriber, correlation=correlation, timing=timing,
        sustained=sustained, clean_connections=clean_connections)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ca", required=True)
    parser.add_argument("--ingest-port", type=int, default=18888)
    parser.add_argument("--gateway-port", type=int, default=19999)
    parser.add_argument("--session-id", default="ltc-comms")
    parser.add_argument("--duration", type=float, default=40)
    parser.add_argument("--target", type=int, default=100, help="0 runs for the full duration")
    parser.add_argument("--ble", action="store_true", help="use protected real BLE instead of synthetic input")
    parser.add_argument("--address", help="optional exact BLE device address")
    parser.add_argument("--confirmed-remote-topology", action="store_true",
                        help="operator confirms both forwards terminate on the actual Ultra96")
    args = parser.parse_args()
    def display(event):
        print(json.dumps(event, sort_keys=True), flush=True)
    summary = asyncio.run(rehearse_remote(args.ca, args.duration, args.target, args.ble,
        args.ingest_port, args.gateway_port, args.session_id, args.confirmed_remote_topology,
        on_event=display, address=args.address))
    print(json.dumps(dict(event="summary", **summary), sort_keys=True), flush=True)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
