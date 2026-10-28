"""Standalone B07 Communications Android/Termux receiver (Python 3.8+, standard library).

Connects only to this Phone's loopback SSH forward. TLS identity remains the
Ultra96 service identity. No Laptop connection, replay request or plaintext mode.
With --hand-events it subscribes with version 2 and also receives the per-hand
HAND_EVENT frames of a sensor-version-4 service; without it nothing changes.
"""
import argparse
import asyncio
from collections import OrderedDict, deque
import json
import math
from pathlib import Path
import re
import ssl
import sys
import time

MAX_FRAME_BYTES = 16384
TLS_IDENTITY = "ultra96.week7.internal"
# GESTURE_RESULT carries protocol version 3; the subscription envelopes stay at 1.
RESULT_VERSION = 3
SUBSCRIPTION_VERSION = 1
RESULT_FIELDS = frozenset({"v", "type", "session_id", "device_id", "boot_id", "seq", "request_id",
                           "result_id", "gesture", "confidence"})
# Any model label of 1..32 characters from A-Z a-z 0-9 _ - (the same rule as the
# Ultra96 and the iPhone). Used with fullmatch: '$' would also match before "\n".
GESTURE_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,32}")

# HAND_EVENT: Wen Ree's envelope (common/hand_contract.py), carried unchanged by
# the service. Only a version-2 subscription receives it; the installed app
# subscribes with version 1 and never sees one.
HAND_SUBSCRIPTION_VERSION = 2
HAND_EVENT_VERSION = 4
HAND_EVENT_FORMAT = 1
MAX_EVENT_SEQ = 2**53 - 1
HAND_EVENT_FIELDS = frozenset({"v", "type", "event_format", "session_id", "worker_epoch", "device_id",
                               "event_seq", "event_id", "kind", "gesture", "confidence", "source",
                               "state_id", "lease_ms", "reason"})
SOURCE_FIELDS = frozenset({"boot_id", "seq", "uptime_ms"})
HANDS = {1: "LEFT", 2: "RIGHT"}
ONGOING_GESTURES = ("CHOP", "WASH", "POURING")
# The gestures each kind may carry; a reset carries none.
KIND_GESTURES = {"pulse": ("PICKUP", "DROP", "THROW"), "state_start": ONGOING_GESTURES,
                 "state_refresh": ONGOING_GESTURES, "state_end": ONGOING_GESTURES,
                 "idle": ("IDLE",), "reset": (None,)}
PREDICTION_KINDS = ("pulse", "state_start", "state_refresh", "idle")
STATE_KINDS = ("state_start", "state_refresh", "state_end")
END_REASONS = ("confirmed_transition", "prediction_timeout", "source_reset", "connection_reset",
               "worker_disconnect", "manual_reset")
EPOCH_PATTERN = re.compile(r"[0-9a-f]{32}")
# The event_seq part of a state_id: canonical ASCII decimal, no leading zero, at most 2^53-1.
STATE_NUMBER_PATTERN = re.compile(r"[1-9][0-9]{0,15}")
# What the tracker reports for each event: accepted ones are printed, the others only counted.
ACCEPTED_OUTCOMES = frozenset({"pulse", "state_started", "refreshed", "state_ended", "idle", "reset"})
IGNORED_OUTCOMES = frozenset({"out_of_order", "orphan_refresh", "orphan_end", "stale_epoch"})
RETIRED_EPOCHS = 64


class ProtocolError(ValueError):
    pass


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value):
    raise ProtocolError("non-finite JSON number")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ProtocolError("non-finite JSON number")
    return number


def _bounded_integer(value):
    # All integer fields in the Phone contract fit uint32, except the HAND_EVENT
    # event_seq (at most 2^53-1, 16 digits). Bound conversion explicitly so
    # Python 3.8 and versions with an integer-digit cap agree; the field rules
    # then refuse anything outside each field's own range.
    if len(value.lstrip("-")) > 16:
        raise ProtocolError("integer outside B07 Communications field bounds")
    return int(value)


def encode_frame(message):
    if not isinstance(message, dict):
        raise ProtocolError("JSON root must be an object")
    try:
        data = json.dumps(message, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as error:
        raise ProtocolError("invalid JSON") from error
    if not 1 <= len(data) <= MAX_FRAME_BYTES:
        raise ProtocolError("frame outside 1..16384 bytes")
    return len(data).to_bytes(4, "big") + data


async def read_frame(reader, timeout=5.0, first_byte_timeout=None, allow_idle=False):
    # A subscribed connection may be quiet indefinitely. Once a byte arrives,
    # the remaining prefix and body must complete within one shared frame budget.
    first = b""
    if allow_idle or first_byte_timeout is not None:
        try:
            first = await asyncio.wait_for(reader.readexactly(1),
                                           None if allow_idle else first_byte_timeout)
        except asyncio.IncompleteReadError as error:
            raise ProtocolError("malformed or incomplete frame") from error

    async def read():
        try:
            size = int.from_bytes(first + await reader.readexactly(4 - len(first)), "big")
            if not 1 <= size <= MAX_FRAME_BYTES:
                raise ProtocolError("frame outside 1..16384 bytes")
            message = json.loads((await reader.readexactly(size)).decode("utf-8"),
                                 object_pairs_hook=_unique_object, parse_constant=_reject_constant,
                                 parse_float=_finite_float, parse_int=_bounded_integer)
            if not isinstance(message, dict):
                raise ProtocolError("JSON root must be an object")
            return message
        except (asyncio.IncompleteReadError, UnicodeError, ValueError, RecursionError) as error:
            raise ProtocolError("malformed or incomplete frame") from error
    return await asyncio.wait_for(read(), timeout)


def _header(message, fields, kind, session, version):
    if (not isinstance(message, dict) or set(message) != fields or
            type(message.get("v")) is not int or message["v"] != version or
            message.get("type") != kind or message.get("session_id") != session):
        raise ProtocolError("unexpected schema or session")
    validate_session(message["session_id"])


def validate_session(session):
    if (not isinstance(session, str) or not 1 <= len(session) <= 128 or
            any(ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF for character in session)):
        raise ProtocolError("session must contain 1..128 Unicode characters without controls")
    return session


def subscribe_message(session, hand_events=False):
    if not hand_events:
        return dict(v=SUBSCRIPTION_VERSION, type="SUBSCRIBE", session_id=session)
    return dict(v=HAND_SUBSCRIPTION_VERSION, type="SUBSCRIBE", session_id=session, event_format=HAND_EVENT_FORMAT)


def validate_subscribed(message, session, hand_events=False):
    # SUBSCRIBED must answer the SUBSCRIBE that was sent: version 1, or version 2 with event_format 1.
    if not hand_events:
        _header(message, {"v", "type", "session_id"}, "SUBSCRIBED", session, SUBSCRIPTION_VERSION)
        return message
    _header(message, {"v", "type", "session_id", "event_format"}, "SUBSCRIBED", session, HAND_SUBSCRIPTION_VERSION)
    if type(message["event_format"]) is not int or message["event_format"] != HAND_EVENT_FORMAT:
        raise ProtocolError("SUBSCRIBED must confirm event_format 1")
    return message


def valid_gesture(value):
    return isinstance(value, str) and GESTURE_PATTERN.fullmatch(value) is not None


def valid_confidence(value):
    # A JSON number, never a boolean: type() excludes bool. Integers 0 and 1 are allowed.
    if type(value) is int:
        return 0 <= value <= 1
    return type(value) is float and math.isfinite(value) and 0.0 <= value <= 1.0


def validate_result(message, session):
    _header(message, RESULT_FIELDS, "GESTURE_RESULT", session, RESULT_VERSION)
    for field in ("device_id", "boot_id", "seq"):
        if type(message[field]) is not int or not 0 <= message[field] < 2**32:
            raise ProtocolError("invalid uint32 " + field)
    if message["device_id"] not in (1, 2):
        raise ProtocolError("invalid device_id")
    # request_id is null for model results; a command echo carries request_id == seq.
    expected_id = "{}:{}:{}".format(message["device_id"], message["boot_id"], message["seq"])
    if message["request_id"] is not None:
        request = message["request_id"]
        if type(request) is not int or not 1 <= request < 2**32 or request != message["seq"]:
            raise ProtocolError("invalid request correlation")
        expected_id = "cmd:" + expected_id
    if message["result_id"] != expected_id:
        raise ProtocolError("result_id does not match the result identity")
    if not valid_gesture(message["gesture"]):
        raise ProtocolError("gesture must be 1..32 characters from A-Z a-z 0-9 _ -")
    if not valid_confidence(message["confidence"]):
        raise ProtocolError("confidence must be a finite number in [0, 1]")
    return message


def _uint32(value):
    return type(value) is int and 0 <= value < 2**32


def validate_hand_event(message, session):
    """Check a HAND_EVENT on the phone, where the worker's hello is not known.

    The same rules as Wen Ree's validate_event in common/hand_contract.py,
    except two that need the hello: confidence strictly above its threshold
    and lease_ms equal to its lease (here only 100..2000). The Ultra96
    service checks both before it publishes an event. Returns the message;
    raises ProtocolError.
    """
    _header(message, HAND_EVENT_FIELDS, "HAND_EVENT", session, HAND_EVENT_VERSION)
    if type(message["event_format"]) is not int or message["event_format"] != HAND_EVENT_FORMAT:
        raise ProtocolError("event_format must be 1")
    epoch = message["worker_epoch"]
    if not isinstance(epoch, str) or EPOCH_PATTERN.fullmatch(epoch) is None:
        raise ProtocolError("worker_epoch must be 32 lowercase hexadecimal digits")
    device, sequence = message["device_id"], message["event_seq"]
    if type(device) is not int or device not in HANDS:
        raise ProtocolError("invalid device_id")
    if type(sequence) is not int or not 1 <= sequence <= MAX_EVENT_SEQ:
        raise ProtocolError("event_seq must be an integer in 1..2^53-1")
    if message["event_id"] != "{}:{}:{}".format(epoch, device, sequence):
        raise ProtocolError("event_id does not match worker_epoch:device_id:event_seq")
    kind = message["kind"]
    if not isinstance(kind, str) or kind not in KIND_GESTURES:
        raise ProtocolError("unknown event kind")
    if message["gesture"] not in KIND_GESTURES[kind]:
        raise ProtocolError("gesture is incompatible with event kind")
    if kind in PREDICTION_KINDS:
        if not valid_confidence(message["confidence"]):
            raise ProtocolError("prediction confidence must be a finite number in [0, 1]")
        if message["reason"] != "confirmed":
            raise ProtocolError("prediction reason must be confirmed")
    elif message["confidence"] is not None or message["reason"] not in END_REASONS:
        raise ProtocolError("state_end and reset carry confidence null and an end reason")
    # Every event names the sample it came from; a reset may have none.
    source = message["source"]
    if source is None:
        if kind != "reset":
            raise ProtocolError("only a reset may have a null source")
    elif (not isinstance(source, dict) or set(source) != SOURCE_FIELDS or
            not all(_uint32(value) for value in source.values())):
        raise ProtocolError("source must be exactly boot_id, seq and uptime_ms as uint32")
    state_id = message["state_id"]
    if kind in STATE_KINDS:
        # A state of this hand and epoch, started at or before this event.
        prefix = "{}:{}:".format(epoch, device)
        number = state_id[len(prefix):] if isinstance(state_id, str) and state_id.startswith(prefix) else ""
        if STATE_NUMBER_PATTERN.fullmatch(number) is None or int(number) > sequence:
            raise ProtocolError("state_id must name a state of this hand and epoch")
        if kind == "state_start" and state_id != message["event_id"]:
            raise ProtocolError("a state_start names itself: state_id must equal event_id")
    elif state_id is not None:
        raise ProtocolError("only state events carry a state_id")
    lease = message["lease_ms"]
    if kind in ("state_start", "state_refresh"):
        if type(lease) is not int or not 100 <= lease <= 2000:
            raise ProtocolError("state_start and state_refresh carry lease_ms in 100..2000")
    elif type(lease) is not int or lease != 0:
        raise ProtocolError("lease_ms must be 0 outside state_start and state_refresh")
    return message


class _Hand:
    def __init__(self, device_id):
        self.device_id = device_id
        self.high_water = 0      # highest event_seq applied in the current worker_epoch
        self.active = None       # the ongoing state: dict(state_id, gesture, deadline)
        self.last_pulse = None   # dict(gesture, event_id) of the last accepted pulse
        self.accepted = 0
        self.duplicate = 0
        self.ignored = 0


class HandStateTracker:
    """Per-hand state from HAND_EVENT frames, on this receiver's own monotonic clock.

    Mirrors Wen Ree's EventConsumer (src/hand_events.py) without the hello.
    Within a worker_epoch each hand keeps the highest event_seq applied: an
    equal one is a duplicate, a lower one is out of order, and both are
    ignored. An event of a new worker_epoch ends both hands' states. A refresh
    never creates or revives a state and an end clears only the state it
    names. A state that sees no refresh within lease_ms ends (lease_expired).
    Accepted pulses are requests for the game, not executed actions.
    """

    def __init__(self, session, clock=time.monotonic):
        self.session = validate_session(session)
        self.clock = clock
        self.now = clock()
        self.epoch = None
        self.retired = OrderedDict()   # earlier worker epochs of this stream, bounded
        self.ends = deque(maxlen=64)   # the latest ended states, for logs and tests
        self.hands = {device: _Hand(device) for device in HANDS}

    def _time(self):
        # A monotonic clock never runs back; hold the last reading if it does.
        self.now = max(self.now, self.clock())
        return self.now

    def _end(self, hand, reason):
        if hand.active is not None:
            self.ends.append(dict(device_id=hand.device_id, state_id=hand.active["state_id"],
                                  gesture=hand.active["gesture"], reason=reason))
            hand.active = None

    def expire(self):
        now = self._time()
        for hand in self.hands.values():
            if hand.active is not None and now >= hand.active["deadline"]:
                self._end(hand, "lease_expired")
        return now

    def reset(self, reason="subscription_reset"):
        """Forget every state and high-water mark (the phone's own subscription ended).

        The counters stay, so the summary covers the whole run.
        """
        self._time()
        for hand in self.hands.values():
            self._end(hand, reason)
            hand.high_water = 0
        self.epoch = None

    def apply(self, message):
        """Apply one HAND_EVENT and return its outcome; ProtocolError if it is invalid."""
        validate_hand_event(message, self.session)
        now = self.expire()
        hand = self.hands[message["device_id"]]
        epoch = message["worker_epoch"]
        if epoch != self.epoch:
            if epoch in self.retired:
                hand.ignored += 1
                return "stale_epoch"
            if self.epoch is not None:
                # A new worker connection: its counters start again at 1.
                self.retired[self.epoch] = None
                while len(self.retired) > RETIRED_EPOCHS:
                    self.retired.popitem(last=False)
                for each in self.hands.values():
                    self._end(each, "connection_reset")
                    each.high_water = 0
            self.epoch = epoch
        sequence = message["event_seq"]
        if sequence == hand.high_water:
            hand.duplicate += 1
            return "duplicate"
        if sequence < hand.high_water:
            hand.ignored += 1
            return "out_of_order"
        hand.high_water = sequence
        kind = message["kind"]
        active = hand.active
        if kind == "pulse":
            self._end(hand, "confirmed_transition")
            hand.last_pulse = dict(gesture=message["gesture"], event_id=message["event_id"])
            outcome = "pulse"
        elif kind == "state_start":
            self._end(hand, "confirmed_transition")
            hand.active = dict(state_id=message["state_id"], gesture=message["gesture"],
                               deadline=now + message["lease_ms"] / 1000.0)
            outcome = "state_started"
        elif kind == "state_refresh":
            if (active is None or active["state_id"] != message["state_id"] or
                    active["gesture"] != message["gesture"]):
                hand.ignored += 1
                return "orphan_refresh"
            active["deadline"] = now + message["lease_ms"] / 1000.0
            outcome = "refreshed"
        elif kind == "state_end":
            if active is None or active["state_id"] != message["state_id"]:
                hand.ignored += 1
                return "orphan_end"
            self._end(hand, message["reason"])
            outcome = "state_ended"
        else:
            self._end(hand, "confirmed_idle" if kind == "idle" else message["reason"])
            outcome = kind
        hand.accepted += 1
        return outcome

    def snapshot(self):
        """Both hands as JSON-ready dicts: the ongoing state, the last pulse and the counts."""
        now = self.expire()
        hands = []
        for device in sorted(self.hands):
            hand = self.hands[device]
            active, pulse = hand.active, hand.last_pulse
            hands.append(dict(
                device_id=device, hand=HANDS[device],
                active_gesture=None if active is None else active["gesture"],
                state_id=None if active is None else active["state_id"],
                expires_in_ms=None if active is None else int(math.ceil((active["deadline"] - now) * 1000)),
                last_pulse=None if pulse is None else pulse["gesture"],
                last_pulse_event_id=None if pulse is None else pulse["event_id"],
                accepted=hand.accepted, duplicate=hand.duplicate, ignored=hand.ignored))
        return hands


def tls_context(ca_file):
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _hand_event(message, tracker, statistics):
    # An invalid HAND_EVENT is counted and skipped: its frame was complete, and
    # the GESTURE_RESULT frames on the same subscription must not be lost.
    try:
        outcome = tracker.apply(message)
    except ProtocolError as error:
        statistics["hand_invalid"] += 1
        print("hand_event_invalid reason=" + str(error), file=sys.stderr, flush=True)
        return False
    if outcome in ACCEPTED_OUTCOMES:
        print(json.dumps(message, allow_nan=False, separators=(",", ":")), flush=True)
        statistics["hand_events"] += 1
    statistics["hands"] = tracker.snapshot()
    return True


async def receive(args, statistics=None, tracker=None):
    context = tls_context(args.ca)
    hand_events = bool(getattr(args, "hand_events", False))
    if statistics is None:
        statistics = dict(received=0, reconnects=0)
    if hand_events:
        if tracker is None:
            tracker = HandStateTracker(args.session)
        statistics.update(hand_events=0, hand_invalid=0, hands=tracker.snapshot())
    seen = OrderedDict()
    count = 0
    reconnects = 0
    backoff = 0.5
    started = asyncio.get_running_loop().time()
    while args.duration is None or asyncio.get_running_loop().time() - started < args.duration:
        writer = None
        subscribed = False
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(
                "127.0.0.1", args.port, ssl=context, server_hostname=TLS_IDENTITY,
                ssl_handshake_timeout=5.0, limit=MAX_FRAME_BYTES + 4), 5.0)
            writer.write(encode_frame(subscribe_message(args.session, hand_events)))
            await asyncio.wait_for(writer.drain(), 5.0)
            validate_subscribed(await read_frame(reader), args.session, hand_events)
            subscribed = True
            print("subscribed session=" + args.session + (" hand_events=1" if hand_events else ""),
                  file=sys.stderr, flush=True)
            while args.duration is None or asyncio.get_running_loop().time() - started < args.duration:
                frame = await read_frame(reader, allow_idle=True)
                # Version 2 carries both types in one ordered stream. On version 1
                # a HAND_EVENT fails the result rules like any other frame.
                if hand_events and frame.get("type") == "HAND_EVENT":
                    if _hand_event(frame, tracker, statistics):
                        backoff = 0.5
                    continue
                message = validate_result(frame, args.session)
                if message["result_id"] in seen:
                    continue
                seen[message["result_id"]] = None
                if len(seen) > 4096:
                    seen.popitem(last=False)
                # Print each live message immediately; there is no disconnected backlog.
                print(json.dumps(message, allow_nan=False, separators=(",", ":")), flush=True)
                count += 1
                statistics["received"] = count
                backoff = 0.5
                if args.count and count >= args.count:
                    if hand_events:
                        statistics["hands"] = tracker.snapshot()
                    return statistics
        except (OSError, ProtocolError, asyncio.TimeoutError) as error:
            reconnects += 1
            statistics["reconnects"] = reconnects
            # Avoid logging credential-bearing command lines or certificate contents.
            print("reconnect reason=" + type(error).__name__, file=sys.stderr, flush=True)
            if hand_events:
                if writer is not None and not subscribed:
                    # A service from before HAND_EVENT closes on a version-2 SUBSCRIBE.
                    print("hand_events subscription not confirmed", file=sys.stderr, flush=True)
                # Events may have been missed while this subscription was down.
                tracker.reset()
                statistics["hands"] = tracker.snapshot()
        finally:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), 1.0)
                except asyncio.CancelledError:
                    writer.transport.abort()
                    raise
                except (OSError, asyncio.TimeoutError):
                    writer.transport.abort()
        await asyncio.sleep(backoff)
        backoff = min(5.0, backoff * 2)
    if hand_events:
        statistics["hands"] = tracker.snapshot()
    return statistics


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ca", required=True, type=Path, help="Verified B07 Communications public CA PEM")
    parser.add_argument("--port", type=int, default=19999, help="This Phone's localhost forward port")
    parser.add_argument("--session", default="ltc-comms")
    parser.add_argument("--count", type=int, default=0, help="Stop after N unique results; 0 continues")
    parser.add_argument("--duration", type=float, help="Overall test duration in seconds")
    parser.add_argument("--hand-events", action="store_true",
                        help="Subscribe with version 2 and also print accepted HAND_EVENT frames "
                             "(a sensor-version-4 service); --count still counts results")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or args.count < 0:
        parser.error("invalid port, count or session")
    try:
        validate_session(args.session)
    except ProtocolError as error:
        parser.error(str(error))
    if args.duration is not None and not 0 < args.duration < float("inf"):
        parser.error("duration must be finite and positive")
    tracker = HandStateTracker(args.session) if args.hand_events else None
    try:
        async def run():
            statistics = dict(received=0, reconnects=0)
            if args.duration is None:
                return await receive(args, statistics, tracker)
            # Bound the whole run, including reconnect sleep and TLS startup.
            try:
                return await asyncio.wait_for(receive(args, statistics, tracker), args.duration)
            except asyncio.TimeoutError:
                return statistics
        summary = asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    except (OSError, ssl.SSLError, ValueError) as error:
        print("configuration_error=" + type(error).__name__, file=sys.stderr)
        return 1
    if tracker is not None:
        # The per-hand summary as the run ended, after any lease that ran out meanwhile.
        summary["hands"] = tracker.snapshot()
    print(json.dumps(summary, separators=(",", ":")), file=sys.stderr)
    return 0 if not args.count or summary["received"] >= args.count else 1


if __name__ == "__main__":
    raise SystemExit(main())
