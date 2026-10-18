"""Exact B07 Communications message schemas, separate from generic JSON framing."""
import math
import re

from common.sensor import CHANNEL_COUNT, VERSION
from common.wire import ProtocolError

# Labels of the built-in dummy model and of command echo results. Model labels are
# not listed here: Communications carries any label that satisfies GESTURE_PATTERN.
DUMMY_GESTURES = ("REST", "FIST", "OPEN", "POINT")
MAX_GESTURE_LENGTH = 32
GESTURE_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,%d}" % MAX_GESTURE_LENGTH)
_BASE = {"v", "type", "session_id"}
_TRACE = {"device_id", "boot_id", "seq", "request_id"}
_FIELDS = {
    "SENSOR_BATCH": _BASE | _TRACE | {"uptime_ms", "values"},
    "INGEST_ACK": _BASE | _TRACE | {"status"},
    "SUBSCRIBE": _BASE,
    "SUBSCRIBED": _BASE,
    "GESTURE_RESULT": _BASE | _TRACE | {"result_id", "gesture", "confidence"},
}
# Subscription envelopes never changed and stay at version 1.
_VERSIONS = {"SENSOR_BATCH": VERSION, "INGEST_ACK": VERSION, "GESTURE_RESULT": VERSION,
             "SUBSCRIBE": 1, "SUBSCRIBED": 1}


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def valid_gesture(value):
    """1..32 characters from [A-Za-z0-9_-]; the same rule on every receiver."""
    return isinstance(value, str) and GESTURE_PATTERN.fullmatch(value) is not None


def valid_confidence(value):
    """A finite JSON number (not a boolean) in [0, 1]."""
    if type(value) is int:  # never float() a huge integer
        return 0 <= value <= 1
    return type(value) is float and math.isfinite(value) and 0 <= value <= 1


def validate_session(session_id):
    if (not isinstance(session_id, str) or not 1 <= len(session_id) <= 128
            or any(ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF
                   for character in session_id)):
        raise ProtocolError("session_id must be a nonempty string of at most 128 characters")
    return session_id


def validate_message(message, expected_type=None, session_id=None):
    """Validate one protocol-version-3 message (subscription envelopes are version 1).

    request_id is always present in data messages: null for samples and model
    results, uint32 equal to seq for commands.
    """
    if not isinstance(message, dict):
        raise ProtocolError("message must be an object")
    kind = message.get("type")
    if not isinstance(kind, str) or kind not in _FIELDS:
        raise ProtocolError("unknown message type or unexpected fields")
    if expected_type is not None and kind != expected_type:
        raise ProtocolError("unexpected message type")
    version = message.get("v")
    if type(version) is not int or version != _VERSIONS[kind]:
        raise ProtocolError("unsupported protocol version")
    if set(message) != _FIELDS[kind]:
        raise ProtocolError("unknown message type or unexpected fields")
    validate_session(message["session_id"])
    if session_id is not None and message["session_id"] != session_id:
        raise ProtocolError("wrong configured session")
    if kind in ("SENSOR_BATCH", "INGEST_ACK", "GESTURE_RESULT"):
        if not _integer(message["device_id"], 1, 2):
            raise ProtocolError("invalid device_id")
        for field in ("boot_id", "seq"):
            if not _integer(message[field], 0, 0xFFFFFFFF):
                raise ProtocolError("invalid " + field)
        if message["request_id"] is not None:
            if not _integer(message["request_id"], 1, 0xFFFFFFFF):
                raise ProtocolError("invalid request_id")
            if message["seq"] != message["request_id"]:
                raise ProtocolError("command seq must equal request_id")
    if kind == "SENSOR_BATCH":
        if not _integer(message["uptime_ms"], 0, 0xFFFFFFFF):
            raise ProtocolError("invalid uptime_ms")
        values = message["values"]
        if (not isinstance(values, list) or len(values) != CHANNEL_COUNT
                or any(not _integer(value, -32768, 32767) for value in values)):
            raise ProtocolError("values must contain exactly eleven int16 integers")
    elif kind == "INGEST_ACK":
        if message["status"] not in ("accepted", "duplicate"):
            raise ProtocolError("invalid acknowledgement status")
    elif kind == "GESTURE_RESULT":
        if message["result_id"] != result_identity(message):
            raise ProtocolError("result_id does not match the result identity")
        if not valid_gesture(message["gesture"]):
            raise ProtocolError("gesture must be 1..32 characters from A-Z a-z 0-9 _ -")
        if not valid_confidence(message["confidence"]):
            raise ProtocolError("confidence must be a finite number in [0, 1]")
    return message


def trace_fields(message):
    return {key: message[key] for key in ("session_id", "device_id", "boot_id", "seq", "request_id")}


def result_identity(message):
    trace = "{device_id}:{boot_id}:{seq}".format(**message)
    return "cmd:" + trace if message.get("request_id") is not None else trace
