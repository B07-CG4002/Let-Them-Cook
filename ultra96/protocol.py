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
# Read-only board monitor on the gateway port: one update every MONITOR_INTERVAL
# seconds, each carrying at most MAX_MONITOR_EVENTS recent events.
MONITOR_INTERVAL = 0.25
MAX_MONITOR_EVENTS = 100
_BASE = {"v", "type", "session_id"}
_TRACE = {"device_id", "boot_id", "seq", "request_id"}
_FIELDS = {
    "SENSOR_BATCH": _BASE | _TRACE | {"uptime_ms", "values"},
    "INGEST_ACK": _BASE | _TRACE | {"status"},
    "SUBSCRIBE": _BASE,
    "SUBSCRIBED": _BASE,
    "GESTURE_RESULT": _BASE | _TRACE | {"result_id", "gesture", "confidence"},
    "MONITOR": _BASE,
    "MONITORING": _BASE,
    "MONITOR_UPDATE": _BASE | {"update", "uptime_seconds", "model", "subscriber", "identities",
                               "identity_limit", "devices", "service", "events", "events_dropped"},
}
# Monitor envelopes stay at version 1. Results stay at version 3 under sensor protocol
# v4 too: both phone apps accept only v=3 results. Only SENSOR_BATCH and INGEST_ACK
# follow the selected sensor version.
RESULT_VERSION = VERSION
_SENSOR_KINDS = ("SENSOR_BATCH", "INGEST_ACK")
_VERSIONS = {"SENSOR_BATCH": VERSION, "INGEST_ACK": VERSION, "GESTURE_RESULT": RESULT_VERSION,
             "MONITOR": 1, "MONITORING": 1, "MONITOR_UPDATE": 1}
# SUBSCRIBE and SUBSCRIBED come in two versions. Version 1 (results only) is what every
# installed phone app sends. Version 2 opts in to HAND_EVENT frames as well, in the same
# ordered stream; it names the hand-event format, and SUBSCRIBED answers in the version asked.
HAND_EVENT_FORMAT = 1
_SUBSCRIPTION_KINDS = ("SUBSCRIBE", "SUBSCRIBED")
_SUBSCRIPTION_FIELDS = {1: _BASE, 2: _BASE | {"event_format"}}
_MONITOR_DEVICE = {"accepted", "duplicates", "results", "commands", "refused", "sent", "unsent"}
_MONITOR_SERVICE = {"rejected", "client_limit", "idle_evicted", "subscribers", "replaced",
                    "model_samples", "model_errors", "model_slow_calls", "model_results_late"}
_MONITOR_REFUSALS = ("unknown_identity", "duplicate")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")


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


def _count(value, low=0):
    return type(value) is int and value >= low


def _seconds(value):
    """A finite JSON number (not a boolean) of at least zero."""
    if type(value) is int:
        return value >= 0
    return type(value) is float and math.isfinite(value) and value >= 0


def _monitor_event(event):
    """One event array of a MONITOR_UPDATE; its last item is the uptime t."""
    if not isinstance(event, list) or not event or not _seconds(event[-1]):
        return False
    kind = event[0]
    if kind in ("result", "command", "refused"):
        if (len(event) != (6 if kind == "refused" else 7) or not _integer(event[1], 1, 2)
                or not _integer(event[2], 0, 0xFFFFFFFF) or not _integer(event[3], 0, 0xFFFFFFFF)):
            return False
        if kind == "refused":
            return event[4] in _MONITOR_REFUSALS
        return valid_gesture(event[4]) and valid_confidence(event[5])
    if kind == "subscriber" and len(event) == 5 and _count(event[2], 1):
        if event[1] == "claimed":
            return event[3] is None
        return (event[1] == "ended" and isinstance(event[3], str)
                and _TOKEN.fullmatch(event[3]) is not None)
    return False


def _monitor_update(message):
    for field in ("update", "identities", "events_dropped"):
        if not _count(message[field]):
            raise ProtocolError("invalid monitor " + field)
    if not _count(message["identity_limit"], 1):
        raise ProtocolError("invalid monitor identity_limit")
    if not _seconds(message["uptime_seconds"]):
        raise ProtocolError("invalid monitor uptime_seconds")
    model = message["model"]
    if not isinstance(model, str) or not 1 <= len(model) <= 128 or not model.isprintable():
        raise ProtocolError("monitor model must be 1..128 printable characters")
    if message["subscriber"] is not None and not _count(message["subscriber"], 1):
        raise ProtocolError("invalid monitor subscriber")
    devices = message["devices"]
    if not isinstance(devices, dict) or set(devices) != {"1", "2"}:
        raise ProtocolError("monitor devices must be exactly \"1\" and \"2\"")
    for counters in devices.values():
        if (not isinstance(counters, dict) or set(counters) != _MONITOR_DEVICE
                or any(not _count(value) for value in counters.values())):
            raise ProtocolError("invalid monitor device counters")
    service = message["service"]
    if (not isinstance(service, dict) or set(service) != _MONITOR_SERVICE | {"model_max_call_ms"}
            or any(not _count(service[key]) for key in _MONITOR_SERVICE)
            or not _seconds(service["model_max_call_ms"])):
        raise ProtocolError("invalid monitor service counters")
    events = message["events"]
    if not isinstance(events, list) or len(events) > MAX_MONITOR_EVENTS:
        raise ProtocolError("monitor events must be a list of at most {}".format(MAX_MONITOR_EVENTS))
    if any(not _monitor_event(event) for event in events):
        raise ProtocolError("invalid monitor event")


def validate_session(session_id):
    if (not isinstance(session_id, str) or not 1 <= len(session_id) <= 128
            or any(ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF
                   for character in session_id)):
        raise ProtocolError("session_id must be a nonempty string of at most 128 characters")
    return session_id


def validate_message(message, expected_type=None, session_id=None, *, sensor_version=VERSION):
    """Validate one protocol-version-3 message (monitor envelopes are version 1).

    sensor_version (3, or an opt-in 4) sets the version of SENSOR_BATCH and INGEST_ACK and
    the number of sample values; GESTURE_RESULT is version 3 whatever it is.
    SUBSCRIBE and SUBSCRIBED are version 1, or version 2 with event_format 1 (hand events
    asked for), in either sensor mode.
    request_id is always present in data messages: null for samples and model
    results, uint32 equal to seq for commands.
    """
    if type(sensor_version) is not int or sensor_version not in (3,4):
        raise ProtocolError("sensor_version must be 3 or 4")
    if not isinstance(message, dict):
        raise ProtocolError("message must be an object")
    kind = message.get("type")
    if not isinstance(kind, str) or kind not in _FIELDS:
        raise ProtocolError("unknown message type or unexpected fields")
    if expected_type is not None and kind != expected_type:
        raise ProtocolError("unexpected message type")
    version = message.get("v")
    if kind in _SUBSCRIPTION_KINDS:
        if type(version) is not int or version not in _SUBSCRIPTION_FIELDS:
            raise ProtocolError("unsupported protocol version")
        fields = _SUBSCRIPTION_FIELDS[version]
    else:
        if type(version) is not int or version != (sensor_version if kind in _SENSOR_KINDS else _VERSIONS[kind]):
            raise ProtocolError("unsupported protocol version")
        fields = _FIELDS[kind]
    if set(message) != fields:
        raise ProtocolError("unknown message type or unexpected fields")
    validate_session(message["session_id"])
    if session_id is not None and message["session_id"] != session_id:
        raise ProtocolError("wrong configured session")
    if "event_format" in fields and (type(message["event_format"]) is not int
                                     or message["event_format"] != HAND_EVENT_FORMAT):
        raise ProtocolError("unsupported event_format")
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
        if (not isinstance(values, list) or len(values) != (CHANNEL_COUNT if sensor_version == 3 else 12)
                or any(not _integer(value, -32768, 32767) for value in values)):
            raise ProtocolError("values must contain exactly {} int16 integers".format(11 if sensor_version == 3 else 12))
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
    elif kind == "MONITOR_UPDATE":
        _monitor_update(message)
    return message


def subscribed(subscribe):
    """The SUBSCRIBED answer to a valid SUBSCRIBE: the same version and, for version 2, event_format."""
    answer = {"v": subscribe["v"], "type": "SUBSCRIBED", "session_id": subscribe["session_id"]}
    if subscribe["v"] == 2:
        answer["event_format"] = HAND_EVENT_FORMAT
    return answer


def validate_subscribed(message, subscribe):
    """A client's check of the service's answer: exactly the SUBSCRIBED of the SUBSCRIBE it sent.

    validate_message alone accepts SUBSCRIBED of either version; a client that asked at
    version 1 must not accept a version-2 answer (or the reverse).
    """
    validate_message(message, "SUBSCRIBED", subscribe["session_id"])
    if message != subscribed(subscribe):
        raise ProtocolError("SUBSCRIBED does not answer the SUBSCRIBE that was sent")
    return message


def trace_fields(message):
    return {key: message[key] for key in ("session_id", "device_id", "boot_id", "seq", "request_id")}


def result_identity(message):
    trace = "{device_id}:{boot_id}:{seq}".format(**message)
    return "cmd:" + trace if message.get("request_id") is not None else trace
