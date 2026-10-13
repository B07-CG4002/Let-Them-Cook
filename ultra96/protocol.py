"""Exact B07 Communications message schemas, separate from generic JSON framing."""
from common.wire import ProtocolError

GESTURES = ("REST", "FIST", "OPEN", "POINT")
_BASE = {"v", "type", "session_id"}
_TRACE = {"device_id", "boot_id", "seq"}
_FIELDS = {
    "SENSOR_BATCH": _BASE | _TRACE | {"uptime_ms", "values"},
    "INGEST_ACK": _BASE | _TRACE | {"status"},
    "SUBSCRIBE": _BASE,
    "SUBSCRIBED": _BASE,
    "GESTURE_RESULT": _BASE | _TRACE | {"result_id", "gesture", "confidence"},
}


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def validate_session(session_id):
    if (not isinstance(session_id, str) or not 1 <= len(session_id) <= 128
            or any(ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF
                   for character in session_id)):
        raise ProtocolError("session_id must be a nonempty string of at most 128 characters")
    return session_id


def validate_message(message, expected_type=None, session_id=None):
    """Validate legacy v1 sentinels or v2 dummy data and request correlation.

    Subscription envelopes stay v1; upgraded subscribers accept both result
    versions. V2 request_id is required: null for telemetry, uint32 for commands.
    """
    if not isinstance(message, dict):
        raise ProtocolError("message must be an object")
    kind = message.get("type")
    if not isinstance(kind, str) or kind not in _FIELDS:
        raise ProtocolError("unknown message type or unexpected fields")
    if expected_type is not None and kind != expected_type:
        raise ProtocolError("unexpected message type")
    version = message.get("v")
    if type(version) is not int or version not in (1, 2):
        raise ProtocolError("unsupported protocol version")
    if version == 2 and kind in ("SUBSCRIBE", "SUBSCRIBED"):
        raise ProtocolError("subscription envelopes require protocol version 1")
    fields = _FIELDS[kind] | ({"request_id"} if version == 2 else set())
    if set(message) != fields:
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
        if version == 2 and message["request_id"] is not None:
            if not _integer(message["request_id"], 1, 0xFFFFFFFF):
                raise ProtocolError("invalid request_id")
            if message["seq"] != message["request_id"]:
                raise ProtocolError("command seq must equal request_id")
    if kind == "SENSOR_BATCH":
        if not _integer(message["uptime_ms"], 0, 0xFFFFFFFF):
            raise ProtocolError("invalid uptime_ms")
        values = message["values"]
        if (not isinstance(values, list) or len(values) != 8
                or any(not _integer(value, -32768, 32767) for value in values)):
            raise ProtocolError("values must contain exactly eight int16 integers")
        expected = [message["seq"] % 2000 - 1000 + 10 * i for i in range(8)]
        if version == 1 and values != expected:
            raise ProtocolError("wrong B07 Communications dummy sentinels")
    elif kind == "INGEST_ACK":
        if message["status"] not in ("accepted", "duplicate"):
            raise ProtocolError("invalid acknowledgement status")
    elif kind == "GESTURE_RESULT":
        trace_id = result_identity(message)
        valid_gesture = (message["gesture"] == GESTURES[message["seq"] % 4]
                         if version == 1 else message["gesture"] in GESTURES)
        if message["result_id"] != trace_id or not valid_gesture:
            raise ProtocolError("inconsistent dummy result")
        if type(message["confidence"]) not in (float, int) or message["confidence"] != 1.0:
            raise ProtocolError("dummy confidence must be 1.0")
    return message


def trace_fields(message):
    fields = {key: message[key] for key in ("session_id", "device_id", "boot_id", "seq")}
    if message.get("v") == 2:
        fields["request_id"] = message["request_id"]
    return fields


def result_identity(message):
    trace = "{device_id}:{boot_id}:{seq}".format(**message)
    return "cmd:" + trace if message.get("request_id") is not None else trace
