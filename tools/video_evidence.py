"""Matched sensor/ACK examples from one saved capture; phone reception is filmed separately."""
from __future__ import annotations

import json
import math
from pathlib import Path

from common.sensor import SensorPacket, decode_packet
from ultra96.protocol import validate_session


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"non-finite JSON value: {value}")


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def _json(text):
    result = json.loads(text, object_pairs_hook=_object, parse_constant=_invalid_constant,
                        parse_float=_finite_float)
    if not isinstance(result, dict):
        raise ValueError("expected a JSON object")
    return result


def _integer(value, low, high, field):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"invalid {field}")
    return value


def _identity(row, session):
    """Old evidence omits session/request ID; never silently ignore explicit conflicts."""
    if "session_id" in row and row["session_id"] != session:
        raise ValueError("packet session_id disagrees with this capture's report")
    if row.get("request_id") is not None:
        raise ValueError("sensor telemetry cannot carry a command request_id")
    device = _integer(row.get("device_id"), 1, 2, "device_id")
    boot = _integer(row.get("boot_id"), 0, 0xffffffff, "boot_id")
    seq = _integer(row.get("seq"), 0, 0xffffffff, "seq")
    return session, device, boot, seq, None


def show_packet_examples(folder: Path) -> bool:
    """Print one sensor/ACK pair per device, using only the explicitly selected folder.

    Current sensor_ack records contain device/boot/seq/status but omit version,
    session and request_id. Session comes from report.json; these event types are
    telemetry, whose v2 request_id is null. Those provenance limits are printed.
    This is a packet illustration, not a whole-capture or phone-delivery audit.
    """
    folder = Path(folder).expanduser().resolve()
    print(f"Packet examples from: {folder}")
    try:
        report = _json((folder / "report.json").read_text(encoding="utf-8-sig"))
        session = validate_session(report.get("session_id"))
        run = report.get("run")
        if isinstance(run, dict) and "session_id" in run and run["session_id"] != session:
            raise ValueError("report session_id disagrees with run metadata")
        sensors, acknowledgements = {}, {}
        with (folder / "packets.jsonl").open(encoding="utf-8-sig") as stream:
            for number, line in enumerate(stream, 1):
                try:
                    row = _json(line)
                    kind = row.get("type")
                    if not isinstance(kind, str) or not kind:
                        raise ValueError("evidence record has no event type")
                    if kind not in ("sensor", "sensor_ack"):
                        continue
                    identity = _identity(row, session)
                    if kind == "sensor":
                        packet = SensorPacket(identity[1], identity[2], identity[3],
                                              row.get("uptime_ms"), row.get("values"),
                                              version=row.get("version"))
                        if row.get("validation") != "decoded" or row.get("direction") != "ESP->laptop":
                            raise ValueError("sensor record is not a decoded ESP-to-laptop sample")
                        if "raw_hex" in row:
                            if not isinstance(row["raw_hex"], str) or decode_packet(bytes.fromhex(row["raw_hex"])) != packet:
                                raise ValueError("raw packet disagrees with decoded sensor fields")
                        previous = sensors.get(identity)
                        if previous is not None and any(previous.get(key) != row.get(key)
                                for key in ("version", "uptime_ms", "values")):
                            raise ValueError("conflicting sensor payloads for one identity")
                        sensors.setdefault(identity, row)
                    else:
                        if row.get("validation") not in ("accepted", "duplicate") or row.get("direction") != "Ultra96->laptop":
                            raise ValueError("sensor ACK has an invalid status or direction")
                        if "version" in row:
                            _integer(row["version"], 1, 2, "ACK version")
                        acknowledgements.setdefault(identity, []).append(row)
                except (ValueError, TypeError) as error:
                    raise ValueError(f"packets.jsonl line {number}: {error}") from error
        examples = {}
        for identity, sensor in sensors.items():
            for ack in acknowledgements.get(identity, ()):
                if "version" in ack and ack["version"] != sensor["version"]:
                    raise ValueError("sensor and ACK versions disagree for one identity")
                examples.setdefault(identity[1], (identity, sensor, ack))
        missing = [str(device) for device in (1, 2) if device not in examples]
        if missing:
            raise ValueError("no matching sensor/sensor_ack pair for device " + ", ".join(missing))
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        print(f"PACKET EXAMPLES NOT PASSED: {error}")
        return False

    print("Session context comes from this folder's report.json; current event records omit session_id.")
    print("These sensor events are telemetry: request_id=null is inferred from the protocol, not logged.")
    print("Current sensor_ack records omit protocol version; explicit versions are checked when present.")
    for device in (1, 2):
        identity, sensor, ack = examples[device]
        print("Matched identity: " + json.dumps(dict(zip(
            ("session_id", "device_id", "boot_id", "seq", "request_id"), identity)), ensure_ascii=False))
        print("Sensor: " + json.dumps(sensor, ensure_ascii=False, sort_keys=True))
        print("ACK: " + json.dumps(ack, ensure_ascii=False, sort_keys=True))
    print("Examples show board ingestion ACKs, not phone receipts or a complete capture audit.")
    return True
