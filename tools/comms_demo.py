"""Explain dummy packets, display checked sender traces, and audit saved demo logs."""
import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import sys
import time

from common.sensor import SensorPacket, decode_packet, dummy_values, encode_packet
from common.wire import encode_frame
from laptop.bridge import Bridge, BridgeConfig
from ultra96.protocol import validate_message, validate_session


def packet_example():
    """A reproducible teaching example, explicitly separate from live evidence."""
    packet = SensorPacket(1, 7, 42, 4200, dummy_values(42))
    batch = packet.to_message("ltc-comms")
    trace = dict(session_id="ltc-comms", device_id=1, boot_id=7, seq=42)
    ack = dict(v=1, type="INGEST_ACK", **trace, status="accepted")
    result = dict(v=1, type="GESTURE_RESULT", **trace, result_id="1:7:42", gesture="OPEN", confidence=1.0)
    messages = dict(sensor_batch=batch, ingest_ack=ack, gesture_result=result,
                    subscribe=dict(v=1, type="SUBSCRIBE", session_id="ltc-comms"),
                    subscribed=dict(v=1, type="SUBSCRIBED", session_id="ltc-comms"))
    frames = {}
    for name, message in messages.items():
        validate_message(message)
        wire = encode_frame(message)
        frames[name] = dict(length_prefix_hex=wire[:4].hex(), body_bytes=len(wire)-4,
                            json_utf8=wire[4:].decode("utf8"))
    return dict(kind="illustrative example; not captured hardware evidence",
                ble_bytes=32, ble_hex=encode_packet(packet).hex(),
                sensor_batch=batch, ingest_ack=ack, gesture_result=result, frames=frames)


class DemoBridge(Bridge):
    """Observe the existing sender after ACK validation; never subscribe to results."""
    def __init__(self, config, emit, *, clock=time.monotonic):
        super().__init__(config, clock=clock)
        self.emit = emit
        self.started = self.clock()
        self.first_ack = self.last_ack = None
        self.maximum_ack_gap = 0.0

    def _check_ack(self, ack, packet):
        super()._check_ack(ack, packet)
        now = self.clock()
        if self.first_ack is None:
            self.first_ack = now
        if self.last_ack is not None:
            self.maximum_ack_gap = max(self.maximum_ack_gap, now - self.last_ack)
        self.last_ack = now
        trace = "{}:{}:{}".format(packet.device_id, packet.boot_id, packet.seq)
        self.emit(dict(event="packet", stage="decoded packet after ingestion ACK validation",
            session_id=self.config.session_id, result_id=trace,
            ble_hex=encode_packet(packet).hex(), sensor_batch=packet.to_message(self.config.session_id)))
        self.emit(dict(event="ack", session_id=self.config.session_id,
                       result_id=trace, status=ack["status"], elapsed_seconds=round(now-self.started, 6)))

    def ack_timing(self):
        gap = None if self.last_ack is None else max(self.maximum_ack_gap, self.clock()-self.last_ack)
        return dict(first_activity_seconds=None if self.first_ack is None else round(self.first_ack-self.started, 6),
                    max_gap_seconds=None if gap is None else round(gap, 6),
                    maximum_silence_seconds=5.0, activity_met=gap is not None and gap <= 5.0)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non-finite JSON value")


def _load_rows(path):
    # UTF-8 BOM is accepted for Windows PowerShell capture. Never execute log text.
    with Path(path).open(encoding="utf-8-sig") as stream:
        total = 0
        for index in range(200001):
            line = stream.readline(65537)
            if not line:
                return
            total += len(line)
            if index == 200000 or len(line) > 65536 or total > 64*1024*1024:
                raise ValueError("log exceeds bounded audit capacity")
            if not line.strip():
                continue
            try:
                value = json.loads(line, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            except RecursionError as exc:
                raise ValueError("excessively nested JSON") from exc
            if not isinstance(value, dict):
                raise ValueError("log record must be an object")
            yield value


def _trace(trace):
    if not isinstance(trace, str):
        raise ValueError("missing trace ID")
    parts = trace.split(":")
    if len(parts) != 3 or any(not part.isascii() or not part.isdecimal() for part in parts):
        raise ValueError("invalid trace ID")
    values = [int(part) for part in parts]
    if (values[0] not in (1, 2) or any(not 0 <= v <= 0xffffffff for v in values[1:])
            or trace != ":".join(str(v) for v in values)):
        raise ValueError("invalid trace ID")
    return dict(device_id=values[0], boot_id=values[1], seq=values[2])


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_logs(path, *, phone_results=None, minimum_count=100, session_id="ltc-comms"):
    """Recompute identity correlation; a file cannot prove remote/Phone provenance."""
    if type(minimum_count) is not int or minimum_count < 1:
        raise ValueError("minimum count must be positive")
    validate_session(session_id)
    accepted, results = set(), set()
    ack_count = result_count = duplicate_acks = duplicate_results = repeated_acks = 0
    interruption_events = 0
    summary = None

    def add_result(message):
        nonlocal result_count, duplicate_results
        validate_message(message, "GESTURE_RESULT", session_id)
        result_count += 1
        trace = message["result_id"]
        duplicate_results += int(trace in results)
        results.add(trace)
        if len(results) > 65536:
            raise ValueError("result ledger exceeds capacity")

    for row in _load_rows(path):
        if summary is not None:
            raise ValueError("unexpected record after final summary")
        event = row.get("event")
        if event == "summary":
            if type(row.get("passed")) is not bool or not isinstance(row.get("bridge"), dict):
                raise ValueError("invalid runner summary")
            if row.get("session_id") != session_id:
                raise ValueError("wrong or missing summary session")
            summary = row
            continue
        if event in ("context", "fault"):
            interruption_events += int(event == "fault")
            continue  # Never echo arbitrary context or fault text.
        if row.get("session_id") != session_id:
            raise ValueError("wrong session")
        if event == "subscribed":
            continue
        if event in ("subscriber_reconnect", "subscriber_fatal"):
            interruption_events += 1
            continue
        trace = row.get("result_id")
        fields = _trace(trace)
        if event == "ack":
            validate_message(dict(v=1, type="INGEST_ACK", session_id=session_id,
                                 **fields, status=row.get("status")))
            ack_count += 1
            if row["status"] == "duplicate":
                duplicate_acks += 1
            else:
                repeated_acks += int(trace in accepted)
                accepted.add(trace)
            if len(accepted) > 65536:
                raise ValueError("ACK ledger exceeds capacity")
        elif event == "result":
            if phone_results is not None:
                raise ValueError("cannot combine desktop subscriber events with a Phone result file")
            add_result(dict(v=1, type="GESTURE_RESULT", session_id=session_id, **fields,
                result_id=trace, gesture=row.get("gesture"), confidence=row.get("confidence")))
        elif event == "packet":
            batch = validate_message(row.get("sensor_batch"), "SENSOR_BATCH", session_id)
            packet = decode_packet(bytes.fromhex(row.get("ble_hex", "")))
            if packet.to_message(session_id) != batch or any(batch[k] != v for k, v in fields.items()):
                raise ValueError("packet bytes, decoded fields and trace disagree")
        else:
            raise ValueError("unknown demo event")
    if phone_results is not None:
        for row in _load_rows(phone_results):
            add_result(row)
    missing, unexpected = accepted - results, results - accepted
    exact = not (missing or unexpected or duplicate_acks or duplicate_results or repeated_acks)
    complete = summary is not None
    count_consistent = bool(complete and type(summary["bridge"].get("acked")) is int
                            and summary["bridge"]["acked"] == ack_count)
    recorded_passed = summary["passed"] if complete else None
    passed = bool(exact and len(accepted) >= minimum_count and len(results) >= minimum_count
                  and complete and count_consistent and recorded_passed and not interruption_events)
    return dict(audit_passed=passed,
        evidence_kind="offline log correlation; no live connection or physical provenance verified",
        session_id=session_id, minimum_count=minimum_count, acks=ack_count,
        accepted_unique=len(accepted), results=result_count, result_unique=len(results),
        matched=len(accepted & results), exact_id_match=bool(exact and accepted),
        missing_results=len(missing), unexpected_results=len(unexpected),
        missing_sample=sorted(missing)[:5], unexpected_sample=sorted(unexpected)[:5],
        duplicate_acks=duplicate_acks, repeated_accepted_acks=repeated_acks,
        duplicate_results=duplicate_results, complete_summary=complete,
        interruption_events=interruption_events,
        recorded_ack_count_consistent=count_consistent, recorded_runner_passed=recorded_passed,
        source_sha256=_digest(path), phone_results_sha256=_digest(phone_results) if phone_results else None)


async def _sender(args):
    def emit(event):
        print(json.dumps(event, allow_nan=False, separators=(",", ":")), flush=True)
    bridge = DemoBridge(BridgeConfig(ca_file=args.ca, port=args.port,
        session_id=args.session_id, address=args.address), emit)
    summary = await bridge.run(duration=args.duration, target=args.target, mock=False)
    timing = bridge.ack_timing()
    minimum = args.target or math.ceil(args.duration * 9)
    errors = ("malformed", "ack_errors", "transport_errors", "ble_errors", "cleanup_errors",
              "queue_dropped", "stale_dropped", "gaps", "duplicates", "out_of_order",
              "ambiguous_dropped", "duplicate_acks", "generation_dropped", "new_boots")
    passed = bool(summary["acked"] >= max(1, minimum) and summary["ble_connections"] == 1
                  and summary["transport_connections"] == 1 and timing["activity_met"]
                  and not any(summary[k] for k in errors))
    emit(dict(event="summary", passed=passed, source="real BLE", bridge=summary,
        topology="ingestion only; Phone delivery requires its separate receiver and audit",
        session_id=args.session_id, minimum_count=minimum, ack_timing=timing))
    return 0 if passed else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("packet", help="print an illustrative packet and its framed messages; no network")
    sender = sub.add_parser("sender", help="protected real BLE sender with packet/ACK display; no subscriber")
    sender.add_argument("--ca", required=True)
    sender.add_argument("--port", type=int, default=18888)
    sender.add_argument("--session-id", default="ltc-comms")
    sender.add_argument("--address")
    sender.add_argument("--duration", type=float, default=60)
    sender.add_argument("--target", type=int, default=100)
    audit = sub.add_parser("audit", help="offline exact trace check of saved runner or sender/Phone logs")
    audit.add_argument("log", type=Path)
    audit.add_argument("--phone-results", type=Path)
    audit.add_argument("--minimum-count", type=int, default=100)
    audit.add_argument("--session-id", default="ltc-comms")
    args = parser.parse_args(argv)
    try:
        if args.command == "sender":
            return asyncio.run(_sender(args))
        if args.command == "packet":
            result = packet_example()
        else:
            result = audit_logs(args.log, phone_results=args.phone_results,
                                minimum_count=args.minimum_count, session_id=args.session_id)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0 if args.command == "packet" or result["audit_passed"] else 1
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Do not echo a malformed input line, certificates or arbitrary exception text.
        print("demo_error=" + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
