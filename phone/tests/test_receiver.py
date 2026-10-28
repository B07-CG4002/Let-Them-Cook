"""Standalone Android receiver tests; no BLE, SSH or external services."""
import asyncio
import copy
import json
import contextlib
import io
from pathlib import Path
import ssl
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from phone.receiver import ProtocolError, encode_frame, read_frame, receive, tls_context, validate_result, validate_subscribed, validate_session
from phone.receiver import HandStateTracker, subscribe_message, validate_hand_event
# Wen Ree's contract and encoder (read-only) produce the events a real worker sends.
from common.hand_contract import ProtocolError as ContractError, make_hello, validate_event
from src.hand_events import EventEncoder


MODEL_LABELS = ("PICKUPHOLD", "PLACEDOWN", "CHOP", "WASH", "THROW", "FIREEXT")
EPOCH = "0123456789abcdef0123456789abcdef"
NEW_EPOCH = "fedcba9876543210fedcba9876543210"
SUBSCRIBED_V1 = dict(v=1, type="SUBSCRIBED", session_id="ltc-comms")
SUBSCRIBED_V2 = dict(v=2, type="SUBSCRIBED", session_id="ltc-comms", event_format=1)
SUBSCRIBE_V1 = dict(v=1, type="SUBSCRIBE", session_id="ltc-comms")
SUBSCRIBE_V2 = dict(v=2, type="SUBSCRIBE", session_id="ltc-comms", event_format=1)
END_REASONS = ("confirmed_transition", "prediction_timeout", "source_reset", "connection_reset",
               "worker_disconnect", "manual_reset")


def result():
    # A version-3 model result: request_id null, a model label and a real confidence.
    return dict(v=3, type="GESTURE_RESULT", session_id="ltc-comms", device_id=1,
                boot_id=4294967295, seq=3, request_id=None, result_id="1:4294967295:3",
                gesture="PICKUPHOLD", confidence=0.73)


def command_result():
    return dict(result(), request_id=3, result_id="cmd:1:4294967295:3")


def hand_hello(epoch=EPOCH, **settings):
    # The worker's hello (format 2). The phone never sees it; the service checks events against it.
    return make_hello("ltc-comms", epoch, **settings)


def hand_record(kind="state_start", gesture="CHOP", device=1, time=0.0, seq=1, confidence=0.95, **extra):
    # A local runtime record as the worker hands it to the EventEncoder.
    record = dict(type=kind, gesture=gesture, device_id=device, elapsed_s=time,
                  boot_id=10 + device, seq=seq, uptime_ms=seq * 20)
    if kind != "state_end":
        record["confidence"] = confidence
    record.update(extra)
    return record


def one_event(encoder, *args, **kwargs):
    [event] = encoder.consume(hand_record(*args, **kwargs))
    return event


def every_kind():
    """One event of every kind, from Wen Ree's EventEncoder."""
    encoder = EventEncoder(hand_hello())
    events = dict(
        state_start=one_event(encoder, "state_start", "CHOP", seq=1),
        state_refresh=one_event(encoder, "prediction", "CHOP", time=0.25, seq=2),
        state_end=one_event(encoder, "state_end", "CHOP", time=0.5, seq=3, reason="prediction_timeout"),
        pulse=one_event(encoder, "action", "THROW", device=2, seq=1),
        idle=one_event(encoder, "idle", "IDLE", device=2, time=0.25, seq=2))
    # The first reset still names the hand's last sample; the second has none left.
    [events["reset"]] = encoder.reset("source_reset", device=1)
    [events["bare_reset"]] = encoder.reset("manual_reset", device=1)
    return events


def restamped(event, seq, **changes):
    # The same event under another event_seq, with a matching event_id (and state_id for a start).
    identity = "{}:{}:{}".format(event["worker_epoch"], event["device_id"], seq)
    changed = dict(copy.deepcopy(event), event_seq=seq, event_id=identity)
    if event["kind"] == "state_start":
        changed["state_id"] = identity
    changed.update(changes)
    return changed


def stream_of(*messages, eof=False):
    stream = asyncio.StreamReader()
    for message in messages:
        stream.feed_data(encode_frame(message))
    if eof:
        stream.feed_eof()
    return stream


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ContractTests(unittest.IsolatedAsyncioTestCase):
    def assert_rejected(self, item, **context):
        with self.subTest(**context), self.assertRaises(ProtocolError):
            validate_result(item, "ltc-comms")

    def test_result_and_subscription_contracts(self):
        self.assertEqual(validate_session("a" * 128), "a" * 128)
        for invalid in ["", "a" * 129, "line\nbreak", "\ud800"]:
            with self.assertRaises(ProtocolError):
                validate_session(invalid)
        self.assertEqual(validate_result(result(), "ltc-comms"), result())
        validate_subscribed(dict(v=1, type="SUBSCRIBED", session_id="ltc-comms"), "ltc-comms")
        # The subscription envelopes did not move to version 3.
        for version in (2, 3, True):
            with self.subTest(subscribed=version), self.assertRaises(ProtocolError):
                validate_subscribed(dict(v=version, type="SUBSCRIBED", session_id="ltc-comms"), "ltc-comms")
        for field, value in [("v", True), ("v", 3.0), ("seq", -1), ("boot_id", 2**32),
                             ("device_id", 0), ("device_id", 3),
                             ("result_id", "1:2:3"), ("result_id", "cmd:1:4294967295:3"),
                             ("session_id", "elsewhere"), ("type", "SUBSCRIBED"), ("extra", 1)]:
            self.assert_rejected(dict(result(), **{field: value}), field=field, value=value)
        for field in result():
            item = result(); del item[field]
            self.assert_rejected(item, missing=field)

    def test_only_version_3_results_are_accepted(self):
        for version in (0, 1, 2, 4):
            self.assert_rejected(dict(result(), v=version), version=version)
        # The exact version-1 and version-2 dummy results of the earlier protocol.
        old = dict(v=1, type="GESTURE_RESULT", session_id="ltc-comms", device_id=1, boot_id=4294967295,
                   seq=3, result_id="1:4294967295:3", gesture="POINT", confidence=1.0)
        self.assert_rejected(old, shape="version 1")
        self.assert_rejected(dict(old, v=2, request_id=None), shape="version 2")

    def test_any_well_formed_label_and_real_confidence_is_accepted(self):
        for gesture in MODEL_LABELS + ("REST", "FIST", "OPEN", "POINT", "a", "Z" * 32, "left_chop-2"):
            item = dict(result(), gesture=gesture)
            with self.subTest(gesture=gesture):
                self.assertEqual(validate_result(item, "ltc-comms"), item)
        for confidence in (0, 1, 0.0, 1.0, 0.5, 1e-05, 0.999999, -0.0):
            item = dict(result(), confidence=confidence)
            with self.subTest(confidence=confidence):
                self.assertEqual(validate_result(item, "ltc-comms"), item)

    def test_gesture_rule(self):
        for gesture in ["", "A" * 33, "PICK UP", "PICK.UP", "PICKUP\n", "\nPICKUP", "CHOP\t",
                        "caf\u00e9", "\uff21", "\u0661", None, 5, ["CHOP"]]:
            self.assert_rejected(dict(result(), gesture=gesture), gesture=gesture)

    def test_confidence_rule(self):
        for confidence in [-0.01, 1.01, 1.5, -1, 2, float("inf"), float("-inf"), float("nan"),
                           True, False, None, "0.5", 10**12]:
            self.assert_rejected(dict(result(), confidence=confidence), confidence=confidence)

    def test_command_echo_correlation(self):
        self.assertEqual(validate_result(command_result(), "ltc-comms"), command_result())
        self.assertEqual(validate_result(dict(command_result(), request_id=4294967295, seq=4294967295,
                                              result_id="cmd:1:4294967295:4294967295"), "ltc-comms")["seq"],
                         4294967295)
        for request in [True, 0, -1, 2, 3.0, 2**32, "3"]:
            self.assert_rejected(dict(command_result(), request_id=request), request_id=request)
        # A command result without the cmd: prefix, and a model result with it.
        self.assert_rejected(dict(command_result(), result_id="1:4294967295:3"), prefix="missing")
        self.assert_rejected(dict(result(), result_id="cmd:1:4294967295:3"), prefix="unexpected")

    async def test_wire_numbers_reach_the_confidence_rule(self):
        def frame(confidence):
            body = ('{"v":3,"type":"GESTURE_RESULT","session_id":"ltc-comms","device_id":2,"boot_id":7,'
                    '"seq":9,"request_id":null,"result_id":"2:7:9","gesture":"CHOP","confidence":'
                    + confidence + '}').encode("utf-8")
            stream = asyncio.StreamReader()
            stream.feed_data(len(body).to_bytes(4, "big") + body); stream.feed_eof()
            return stream
        for token, value in [("0.73", 0.73), ("7.3e-1", 0.73), ("1", 1), ("0", 0), ("1e-05", 1e-05)]:
            with self.subTest(token=token):
                message = validate_result(await read_frame(frame(token)), "ltc-comms")
                self.assertEqual(message["confidence"], value)
        # 1e999 overflows to Infinity and NaN is not JSON: the frame itself is refused.
        # true decodes to a boolean, which the confidence rule refuses.
        for token in ["1e999", "-1e999", "NaN", "Infinity", "true"]:
            with self.subTest(token=token), self.assertRaises(ProtocolError):
                validate_result(await read_frame(frame(token)), "ltc-comms")
        for token in ["1.01", "-0.5", "\"0.5\"", "null"]:
            with self.subTest(token=token), self.assertRaises(ProtocolError):
                validate_result(await read_frame(frame(token)), "ltc-comms")

    async def test_split_and_coalesced_frames(self):
        stream = asyncio.StreamReader()
        data = encode_frame(result())
        async def feed():
            for value in data:
                stream.feed_data(bytes([value]))
                await asyncio.sleep(0)
            stream.feed_data(data)
        task = asyncio.create_task(feed())
        self.assertEqual(await read_frame(stream), result())
        self.assertEqual(await read_frame(stream), result())
        await task

    async def test_reject_bad_frames(self):
        payloads = [b'{"v":1,"v":1}', b'{"v":NaN}', b'{"v":1e999}', b'[]', b'{} trailing', b'\xff']
        frames = [len(data).to_bytes(4, "big") + data for data in payloads]
        frames += [b'\0\0\0\0', (16385).to_bytes(4, "big"), b'\0\0', b'\0\0\0\x02{']
        for data in frames:
            stream = asyncio.StreamReader(); stream.feed_data(data); stream.feed_eof()
            with self.subTest(data=data), self.assertRaises(ProtocolError):
                await read_frame(stream)

    async def test_incomplete_frame_deadline(self):
        stream = asyncio.StreamReader(); stream.feed_data(b'\0')
        with self.assertRaises(asyncio.TimeoutError):
            await read_frame(stream, timeout=0.02)

    async def test_startup_grace_ends_at_first_byte_of_partial_prefix_or_body(self):
        for partial in [b'\0', b'\0\0\0\x02{']:
            stream = asyncio.StreamReader(); stream.feed_data(partial)
            with self.subTest(partial=partial):
                started = asyncio.get_running_loop().time()
                with self.assertRaises(asyncio.TimeoutError):
                    await read_frame(stream, timeout=0.05, first_byte_timeout=0.5)
                self.assertLess(asyncio.get_running_loop().time() - started, 0.3)

    async def test_prefix_and_body_share_one_deadline_after_startup_byte(self):
        stream = asyncio.StreamReader(); stream.feed_data(b'\0')
        loop = asyncio.get_running_loop()
        prefix = loop.call_later(0.12, stream.feed_data, b'\0\0\x02')
        body = loop.call_later(0.28, stream.feed_data, b'{}')
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await read_frame(stream, timeout=0.2, first_byte_timeout=0.5)
        finally:
            prefix.cancel(); body.cancel()

    async def test_startup_idle_budget_is_bounded(self):
        stream = asyncio.StreamReader()
        with self.assertRaises(asyncio.TimeoutError):
            await read_frame(stream, timeout=0.5, first_byte_timeout=0.05)

    async def test_startup_eof_remains_a_protocol_error(self):
        for partial in [b'', b'\0', b'\0\0\0\x02{']:
            stream = asyncio.StreamReader(); stream.feed_data(partial); stream.feed_eof()
            with self.subTest(partial=partial), self.assertRaises(ProtocolError):
                await read_frame(stream, first_byte_timeout=0.5)

    async def test_cancellation_propagates_during_startup_idle_or_partial_frame(self):
        for partial in [b'', b'\0']:
            stream = asyncio.StreamReader(); stream.feed_data(partial)
            task = asyncio.create_task(read_frame(stream, first_byte_timeout=0.5))
            await asyncio.sleep(0.02)
            task.cancel()
            with self.subTest(partial=partial), self.assertRaises(asyncio.CancelledError):
                await task

    async def test_excessive_integer_is_a_protocol_error_on_every_supported_python(self):
        body = b'{"v":' + b'9' * 5000 + b'}'
        stream = asyncio.StreamReader()
        stream.feed_data(len(body).to_bytes(4, "big") + body)
        with self.assertRaises(ProtocolError):
            await read_frame(stream)


class HandSubscriptionTests(unittest.TestCase):
    def test_subscribe_versions(self):
        self.assertEqual(subscribe_message("ltc-comms"), SUBSCRIBE_V1)
        self.assertEqual(subscribe_message("ltc-comms", hand_events=True), SUBSCRIBE_V2)
        self.assertEqual(encode_frame(SUBSCRIBE_V2)[4:],
                         b'{"v":2,"type":"SUBSCRIBE","session_id":"ltc-comms","event_format":1}')

    def test_subscribed_must_answer_the_subscribe_that_was_sent(self):
        self.assertEqual(validate_subscribed(SUBSCRIBED_V1, "ltc-comms"), SUBSCRIBED_V1)
        self.assertEqual(validate_subscribed(SUBSCRIBED_V2, "ltc-comms", hand_events=True), SUBSCRIBED_V2)
        with self.assertRaises(ProtocolError):
            validate_subscribed(SUBSCRIBED_V2, "ltc-comms")
        refused = [SUBSCRIBED_V1, dict(SUBSCRIBED_V1, v=2), dict(SUBSCRIBED_V2, v=1), dict(SUBSCRIBED_V2, v=True),
                   dict(SUBSCRIBED_V2, event_format=2), dict(SUBSCRIBED_V2, event_format=True),
                   dict(SUBSCRIBED_V2, event_format=1.0), dict(SUBSCRIBED_V2, event_format=None),
                   dict(SUBSCRIBED_V2, session_id="elsewhere"), dict(SUBSCRIBED_V2, type="SUBSCRIBE"),
                   dict(SUBSCRIBED_V2, extra=1)]
        for message in refused:
            with self.subTest(message=message), self.assertRaises(ProtocolError):
                validate_subscribed(message, "ltc-comms", hand_events=True)


class HandEventContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = every_kind()

    def assert_accepted(self, message, **context):
        with self.subTest(**context):
            self.assertIs(validate_hand_event(message, "ltc-comms"), message)

    def assert_refused(self, message, **context):
        # The phone refuses it, and so does Wen Ree's validator with the worker's hello.
        with self.subTest(**context):
            with self.assertRaises(ProtocolError):
                validate_hand_event(message, "ltc-comms")
            with self.assertRaises(ContractError):
                validate_event(message, hand_hello())

    def test_every_kind_from_the_encoder_is_accepted(self):
        self.assertEqual(self.events["reset"]["source"], dict(boot_id=11, seq=3, uptime_ms=60))
        self.assertIsNone(self.events["bare_reset"]["source"])
        for kind, event in self.events.items():
            validate_event(event, hand_hello())
            self.assert_accepted(event, kind=kind)

    def test_exactly_the_fifteen_fields(self):
        for kind, event in self.events.items():
            self.assertEqual(len(event), 15)
            for field in event:
                item = dict(event)
                del item[field]
                self.assert_refused(item, kind=kind, missing=field)
            self.assert_refused(dict(event, extra=None), kind=kind, extra=True)

    def test_envelope(self):
        start = self.events["state_start"]
        for field, value in [("v", 3), ("v", 5), ("v", True), ("v", 4.0), ("type", "GESTURE_RESULT"),
                             ("type", "hand_event"), ("event_format", 0), ("event_format", 2),
                             ("event_format", True), ("event_format", 1.0), ("event_format", None),
                             ("session_id", "elsewhere"), ("session_id", None), ("session_id", ["ltc-comms"])]:
            self.assert_refused(dict(start, **{field: value}), field=field, value=value)
        with self.assertRaises(ProtocolError):
            validate_hand_event(start, "elsewhere")

    def test_worker_epoch_is_32_lowercase_hex(self):
        start = self.events["state_start"]
        for epoch in [EPOCH.upper(), EPOCH[:-1], EPOCH + "0", "g" * 32, EPOCH[:-1] + chr(10), None, 7]:
            identity = "{}:1:1".format(epoch)
            self.assert_refused(dict(start, worker_epoch=epoch, event_id=identity, state_id=identity), epoch=epoch)

    def test_device_and_event_seq(self):
        pulse = self.events["pulse"]
        for device in [0, 3, True, 2.0, "2", None]:
            self.assert_refused(dict(pulse, device_id=device), device=device)
        for seq in [0, -1, 2**53, True, 1.0, "1", None]:
            self.assert_refused(dict(pulse, event_seq=seq, event_id="{}:2:{}".format(EPOCH, seq)), seq=seq)
        top = restamped(pulse, 2**53 - 1)
        validate_event(top, hand_hello())
        self.assert_accepted(top, seq="2^53-1")

    def test_event_id_is_epoch_device_seq(self):
        pulse = self.events["pulse"]
        self.assertEqual(pulse["event_id"], EPOCH + ":2:1")
        for identity in [EPOCH + ":2:2", EPOCH + ":1:1", NEW_EPOCH + ":2:1", EPOCH + ":2:01", EPOCH + ":02:1",
                         EPOCH.upper() + ":2:1", EPOCH + ":2:1 ", EPOCH + ":2", None, 1]:
            self.assert_refused(dict(pulse, event_id=identity), event_id=identity)

    def test_kind_and_gesture_pairing(self):
        events = self.events
        for gesture in ("PICKUP", "DROP", "THROW"):
            self.assert_accepted(dict(events["pulse"], gesture=gesture), gesture=gesture)
        for kind in ("state_start", "state_refresh", "state_end"):
            for gesture in ("CHOP", "WASH", "POURING"):
                self.assert_accepted(dict(events[kind], gesture=gesture), kind=kind, gesture=gesture)
        cases = [("pulse", "kind", "action"), ("pulse", "kind", None), ("pulse", "kind", ["pulse"]),
                 ("pulse", "kind", "PULSE"), ("pulse", "gesture", "CHOP"), ("pulse", "gesture", "IDLE"),
                 ("pulse", "gesture", None), ("pulse", "gesture", ["THROW"]), ("pulse", "gesture", "throw"),
                 ("state_start", "gesture", "PICKUP"), ("state_refresh", "gesture", "IDLE"),
                 ("state_end", "gesture", "DROP"), ("state_end", "gesture", None), ("idle", "gesture", "CHOP"),
                 ("idle", "gesture", None), ("reset", "gesture", "IDLE"), ("reset", "gesture", ""),
                 ("bare_reset", "gesture", "CHOP")]
        for kind, field, value in cases:
            self.assert_refused(dict(events[kind], **{field: value}), kind=kind, field=field, value=value)

    def test_confidence_only_on_predictions(self):
        for kind in ("pulse", "state_start", "state_refresh", "idle"):
            for value in [None, True, False, -0.01, 1.01, float("nan"), float("inf"), "0.9", [0.9]]:
                self.assert_refused(dict(self.events[kind], confidence=value), kind=kind, confidence=value)
        for kind in ("state_end", "reset", "bare_reset"):
            for value in [0, 0.0, 0.9, 1, False]:
                self.assert_refused(dict(self.events[kind], confidence=value), kind=kind, confidence=value)

    def test_reason(self):
        for kind in ("pulse", "state_start", "state_refresh", "idle"):
            for value in ["confirmed_transition", "lease_expired", "", None, ["confirmed"]]:
                self.assert_refused(dict(self.events[kind], reason=value), kind=kind, reason=value)
        for kind in ("state_end", "reset", "bare_reset"):
            for reason in END_REASONS:
                self.assert_accepted(dict(self.events[kind], reason=reason), kind=kind, reason=reason)
            for value in ["confirmed", "lease_expired", "confirmed_idle", "", None, ["manual_reset"]]:
                self.assert_refused(dict(self.events[kind], reason=value), kind=kind, reason=value)

    def test_source(self):
        good = self.events["pulse"]["source"]
        for kind in ("pulse", "state_start", "state_refresh", "state_end", "idle"):
            self.assert_refused(dict(self.events[kind], source=None), kind=kind, source=None)
        self.assert_accepted(dict(self.events["pulse"], source=dict(boot_id=2**32 - 1, seq=0, uptime_ms=0)), edge=True)
        bad = [dict(good, extra=1), dict(boot_id=1, seq=2), dict(good, seq=-1), dict(good, seq=2**32),
               dict(good, uptime_ms=True), dict(good, boot_id=1.0), dict(good, seq="2"), dict(good, seq=None),
               [11, 1, 20], "11:1"]
        for kind in ("pulse", "reset"):
            for source in bad:
                self.assert_refused(dict(self.events[kind], source=source), kind=kind, source=source)
        # A reset may name its last sample or none.
        self.assert_accepted(dict(self.events["bare_reset"], source=good), reset="with source")
        self.assert_accepted(dict(self.events["reset"], source=None), reset="without source")

    def test_state_id(self):
        prefix = EPOCH + ":1:"
        for kind in ("state_refresh", "state_end"):
            event = self.events[kind]
            seq = event["event_seq"]
            self.assertEqual(event["state_id"], prefix + "1")
            self.assert_accepted(dict(event, state_id=prefix + str(seq)), kind=kind, state="itself")
            for state_id in [None, 1, prefix, prefix + "0", prefix + "01", prefix + str(seq + 1), prefix + "1.0",
                             prefix + "+1", prefix + " 1", prefix + chr(0xFF11), prefix + chr(0x0661),
                             prefix + "1" + chr(10), EPOCH + ":2:1", NEW_EPOCH + ":1:1", EPOCH.upper() + ":1:1", "1"]:
                self.assert_refused(dict(event, state_id=state_id), kind=kind, state_id=state_id)
        # The largest state number fits; a 17-digit one does not.
        refresh = self.events["state_refresh"]
        self.assert_accepted(restamped(refresh, 2**53 - 1, state_id=prefix + str(2**53 - 1)), state="2^53-1")
        self.assert_refused(restamped(refresh, 2**53 - 1, state_id=prefix + "1" + "0" * 16), state="17 digits")
        # A state_start names itself.
        start = self.events["state_start"]
        self.assert_refused(dict(start, state_id=None), start="null")
        self.assert_refused(restamped(start, 5, state_id=prefix + "1"), start="earlier state")
        for kind in ("pulse", "idle", "reset", "bare_reset"):
            for state_id in [self.events[kind]["event_id"], ""]:
                self.assert_refused(dict(self.events[kind], state_id=state_id), kind=kind, state_id=state_id)

    def test_lease(self):
        for kind in ("state_start", "state_refresh"):
            for value in (100, 2000):
                self.assert_accepted(dict(self.events[kind], lease_ms=value), kind=kind, lease=value)
            for value in [0, 99, 2001, None, True, 1000.0, "1000"]:
                self.assert_refused(dict(self.events[kind], lease_ms=value), kind=kind, lease=value)
        for kind in ("state_end", "pulse", "idle", "reset", "bare_reset"):
            for value in [1000, 1, False, 0.0, None]:
                self.assert_refused(dict(self.events[kind], lease_ms=value), kind=kind, lease=value)

    def test_threshold_and_exact_lease_are_left_to_the_service(self):
        # Only the hello states the threshold and the lease: the service checks those.
        for confidence in (0, 1, 0.0, 0.5, 1.0):
            self.assert_accepted(dict(self.events["pulse"], confidence=confidence), confidence=confidence)
        with self.assertRaises(ContractError):
            validate_event(dict(self.events["pulse"], confidence=0.5), hand_hello())
        other_lease = dict(self.events["state_start"], lease_ms=1500)
        self.assert_accepted(other_lease, lease=1500)
        with self.assertRaises(ContractError):
            validate_event(other_lease, hand_hello())

    async def test_wire_events_decode_up_to_the_largest_event_seq(self):
        top = restamped(self.events["state_refresh"], 2**53 - 1)
        self.assertEqual(validate_hand_event(await read_frame(stream_of(top, eof=True)), "ltc-comms"), top)
        # 2^53 still decodes and then fails the event rule.
        over = restamped(self.events["state_refresh"], 2**53)
        with self.assertRaises(ProtocolError):
            validate_hand_event(await read_frame(stream_of(over, eof=True)), "ltc-comms")
        # The decoder still refuses integers longer than 16 digits as a framing error.
        with self.assertRaises(ProtocolError):
            await read_frame(stream_of(restamped(self.events["state_refresh"], 10**16), eof=True))
        # Results keep their uint32 rule: an 11-digit boot_id now reaches validate_result, which refuses it.
        wide = dict(result(), boot_id=10**10, result_id="1:10000000000:3")
        message = await read_frame(stream_of(wide, eof=True))
        with self.assertRaises(ProtocolError):
            validate_result(message, "ltc-comms")


class HandStateTrackerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.tracker = HandStateTracker("ltc-comms", clock=self.clock)
        self.encoder = EventEncoder(hand_hello())

    def event(self, *args, **kwargs):
        return one_event(self.encoder, *args, **kwargs)

    def apply(self, event, at=None):
        if at is not None:
            self.clock.now = at
        return self.tracker.apply(event)

    def hand(self, device):
        return self.tracker.snapshot()[device - 1]

    def ends(self):
        return [(end["device_id"], end["gesture"], end["reason"]) for end in self.tracker.ends]

    def test_both_hands_are_independent(self):
        left = self.event("state_start", "CHOP", device=1, seq=1)
        right_pulse = self.event("action", "PICKUP", device=2, seq=1)
        right_state = self.event("state_start", "WASH", device=2, time=0.25, seq=2)
        # Each hand counts its own events from 1.
        self.assertEqual((left["event_seq"], right_pulse["event_seq"], right_state["event_seq"]), (1, 1, 2))
        self.assertEqual([self.apply(left), self.apply(right_pulse), self.apply(right_state)],
                         ["state_started", "pulse", "state_started"])
        self.assertEqual(self.tracker.snapshot(), [
            dict(device_id=1, hand="LEFT", active_gesture="CHOP", state_id=left["event_id"], expires_in_ms=1000,
                 last_pulse=None, last_pulse_event_id=None, accepted=1, duplicate=0, ignored=0),
            dict(device_id=2, hand="RIGHT", active_gesture="WASH", state_id=right_state["event_id"],
                 expires_in_ms=1000, last_pulse="PICKUP", last_pulse_event_id=right_pulse["event_id"],
                 accepted=2, duplicate=0, ignored=0)])
        # Ending the right hand's state leaves the left hand's alone.
        right_end = self.event("state_end", "WASH", device=2, time=0.5, seq=3, reason="prediction_timeout")
        self.assertEqual(self.apply(right_end, at=0.25), "state_ended")
        self.assertEqual((self.hand(1)["active_gesture"], self.hand(1)["expires_in_ms"]), ("CHOP", 750))
        self.assertIsNone(self.hand(2)["active_gesture"])
        self.assertEqual(self.ends(), [(2, "WASH", "prediction_timeout")])
        json.dumps(self.tracker.snapshot(), allow_nan=False)

    def test_duplicates_are_ignored_and_do_not_extend_a_lease(self):
        start = self.event("state_start", "CHOP", seq=1)
        refresh = self.event("prediction", "CHOP", time=0.25, seq=2)
        pulse = self.event("action", "DROP", device=2, seq=1)
        self.assertEqual([self.apply(start), self.apply(start)], ["state_started", "duplicate"])
        self.assertEqual(self.apply(refresh, at=0.25), "refreshed")
        # A retransmitted refresh does not move the deadline (still 1.25 s).
        self.assertEqual(self.apply(refresh, at=0.75), "duplicate")
        self.assertEqual(self.hand(1)["expires_in_ms"], 500)
        self.assertEqual([self.apply(pulse), self.apply(pulse)], ["pulse", "duplicate"])
        self.assertEqual([(hand["accepted"], hand["duplicate"], hand["ignored"]) for hand in self.tracker.snapshot()],
                         [(2, 2, 0), (1, 1, 0)])

    def test_out_of_order_is_ignored_per_hand(self):
        first = self.event("action", "PICKUP", seq=1)
        second = self.event("action", "THROW", time=0.25, seq=2)
        third = self.event("action", "DROP", time=0.5, seq=3)
        right = self.event("action", "PICKUP", device=2, seq=1)
        self.assertEqual([self.apply(first), self.apply(third), self.apply(second), self.apply(right)],
                         ["pulse", "pulse", "out_of_order", "pulse"])
        left = self.hand(1)
        self.assertEqual((left["last_pulse"], left["last_pulse_event_id"]), ("DROP", third["event_id"]))
        self.assertEqual((left["accepted"], left["ignored"]), (2, 1))
        self.assertEqual(self.hand(2)["last_pulse"], "PICKUP")

    def test_new_worker_epoch_ends_both_states_and_restarts_counters(self):
        self.apply(self.event("state_start", "CHOP", seq=1))
        self.apply(self.event("state_start", "WASH", device=2, seq=1))
        late = self.event("action", "THROW", device=2, time=0.25, seq=2)
        fresh = EventEncoder(hand_hello(NEW_EPOCH))
        right = one_event(fresh, "action", "PICKUP", device=2, seq=5)
        left = one_event(fresh, "state_start", "POURING", device=1, seq=6)
        self.assertEqual((right["event_seq"], left["event_seq"]), (1, 1))
        self.assertEqual(self.apply(right, at=0.25), "pulse")
        self.assertEqual(self.ends(), [(1, "CHOP", "connection_reset"), (2, "WASH", "connection_reset")])
        # event_seq 1 of the new epoch is not a duplicate of the old epoch's 1.
        self.assertEqual(self.apply(left), "state_started")
        # A late event of the retired epoch changes nothing.
        self.assertEqual(self.apply(late), "stale_epoch")
        self.assertEqual(self.hand(1)["active_gesture"], "POURING")
        self.assertEqual(self.hand(2)["last_pulse"], "PICKUP")
        self.assertEqual(self.hand(2)["ignored"], 1)

    def test_lease_expiry_without_resurrection(self):
        start = self.event("state_start", "CHOP", seq=1)
        refresh = self.event("prediction", "CHOP", time=0.25, seq=2)
        later = self.event("prediction", "CHOP", time=0.5, seq=3)
        end = self.event("state_end", "CHOP", time=0.75, seq=4, reason="prediction_timeout")
        self.assertEqual(self.apply(start), "state_started")
        self.assertEqual(self.apply(refresh, at=0.75), "refreshed")
        self.clock.now = 1.5
        self.assertEqual((self.hand(1)["active_gesture"], self.hand(1)["expires_in_ms"]), ("CHOP", 250))
        # The state ends at its deadline on this receiver's own clock.
        self.clock.now = 1.75
        self.assertIsNone(self.hand(1)["active_gesture"])
        self.assertEqual(list(self.tracker.ends), [dict(device_id=1, state_id=start["event_id"], gesture="CHOP",
                                                        reason="lease_expired")])
        # Neither a late refresh nor the late end brings it back.
        self.assertEqual(self.apply(later, at=2.0), "orphan_refresh")
        self.assertEqual(self.apply(end), "orphan_end")
        left = self.hand(1)
        self.assertIsNone(left["active_gesture"])
        self.assertEqual((left["accepted"], left["ignored"]), (2, 2))

    def test_expiry_is_also_checked_before_each_event(self):
        self.apply(self.event("state_start", "CHOP", seq=1))
        self.assertEqual(self.apply(self.event("action", "PICKUP", device=2, seq=1), at=1.0), "pulse")
        self.assertEqual(self.ends(), [(1, "CHOP", "lease_expired")])

    def test_orphan_refresh_and_end(self):
        self.event("state_start", "CHOP", seq=1)  # encoded, then lost on the way
        refresh = self.event("prediction", "CHOP", time=0.25, seq=2)
        end = self.event("state_end", "CHOP", time=0.5, seq=3, reason="confirmed_transition")
        wash = self.event("state_start", "WASH", time=0.5, seq=4)
        # The start was lost: its refresh and end name a state this receiver never saw.
        self.assertEqual([self.apply(refresh), self.apply(end)], ["orphan_refresh", "orphan_end"])
        self.assertIsNone(self.hand(1)["active_gesture"])
        self.assertEqual(self.apply(wash), "state_started")
        # An end naming the earlier state does not clear the newer one.
        self.assertEqual(self.apply(restamped(end, 5)), "orphan_end")
        self.assertEqual(self.hand(1)["active_gesture"], "WASH")
        # A refresh must match the active gesture as well as its state_id.
        self.assertEqual(self.apply(restamped(refresh, 6, state_id=wash["state_id"])), "orphan_refresh")
        self.assertEqual(self.apply(restamped(refresh, 7, state_id=wash["state_id"], gesture="WASH"), at=0.5),
                         "refreshed")
        left = self.hand(1)
        self.assertEqual((left["active_gesture"], left["expires_in_ms"]), ("WASH", 1000))
        self.assertEqual((left["accepted"], left["ignored"]), (2, 4))

    def test_transition_pair_on_one_source_sample(self):
        start = self.event("state_start", "CHOP", seq=1)
        end = self.event("state_end", "CHOP", time=0.25, seq=5, reason="confirmed_transition")
        pouring = self.event("state_start", "POURING", time=0.25, seq=5)
        self.assertEqual(end["source"], pouring["source"])
        self.assertNotEqual(end["event_id"], pouring["event_id"])
        self.assertEqual([self.apply(start), self.apply(end, at=0.25), self.apply(pouring)],
                         ["state_started", "state_ended", "state_started"])
        left = self.hand(1)
        self.assertEqual((left["active_gesture"], left["state_id"], left["accepted"]),
                         ("POURING", pouring["event_id"], 3))
        self.assertEqual(self.ends(), [(1, "CHOP", "confirmed_transition")])

    def test_idle_and_reset_clear_the_state(self):
        self.apply(self.event("state_start", "CHOP", seq=1))
        self.assertEqual(self.apply(self.event("idle", "IDLE", time=0.25, seq=2)), "idle")
        self.apply(self.event("state_start", "WASH", time=0.5, seq=3))
        self.apply(self.event("state_start", "POURING", device=2, seq=1))
        [reset] = self.encoder.reset("source_reset", device=1)
        self.assertEqual(self.apply(reset), "reset")
        self.assertEqual(self.ends(), [(1, "CHOP", "confirmed_idle"), (1, "WASH", "source_reset")])
        self.assertIsNone(self.hand(1)["active_gesture"])
        self.assertEqual(self.hand(2)["active_gesture"], "POURING")

    def test_own_reconnect_resets_states_but_keeps_counts(self):
        self.apply(self.event("state_start", "CHOP", seq=1))
        self.apply(self.event("action", "PICKUP", device=2, seq=1))
        refresh = self.event("prediction", "CHOP", time=0.25, seq=2)
        self.tracker.reset()
        self.assertEqual(self.ends(), [(1, "CHOP", "subscription_reset")])
        self.assertEqual([(hand["active_gesture"], hand["accepted"]) for hand in self.tracker.snapshot()],
                         [(None, 1), (None, 1)])
        # The state is not revived after the reconnect, and the same worker epoch carries on.
        self.assertEqual(self.apply(refresh), "orphan_refresh")
        self.assertEqual(self.apply(self.event("action", "THROW", time=0.5, seq=3)), "pulse")
        self.assertEqual(self.hand(1)["last_pulse"], "THROW")

    def test_invalid_event_changes_nothing(self):
        start = self.event("state_start", "CHOP", seq=1)
        self.apply(start)
        before = self.tracker.snapshot()
        for bad in [dict(start, session_id="elsewhere"), restamped(start, 2, lease_ms=0), dict(type="HAND_EVENT"),
                    dict(result())]:
            with self.subTest(bad=bad), self.assertRaises(ProtocolError):
                self.tracker.apply(bad)
        self.assertEqual(self.tracker.snapshot(), before)

    def test_clock_running_back_is_held(self):
        self.apply(self.event("state_start", "CHOP", seq=1), at=0.5)
        self.clock.now = 0.25
        self.assertEqual(self.hand(1)["expires_in_ms"], 1000)

    def test_memory_stays_bounded(self):
        # 71 worker epochs: only the latest 64 retired ones and the latest 64 ends are kept.
        epochs = ["{:032x}".format(number) for number in range(1, 72)]
        for epoch in epochs:
            start = one_event(EventEncoder(hand_hello(epoch)), "state_start", "CHOP")
            self.assertEqual(self.apply(start), "state_started")
        self.assertEqual(list(self.tracker.retired), epochs[-65:-1])
        self.assertEqual(len(self.tracker.ends), 64)
        self.assertEqual(self.tracker.ends[-1]["state_id"], epochs[-2] + ":1:1")
        # A remembered retired epoch is still refused and changes nothing.
        late = one_event(EventEncoder(hand_hello(epochs[-2])), "action", "PICKUP", device=2)
        self.assertEqual(self.apply(late), "stale_epoch")
        self.assertEqual((self.hand(1)["state_id"], self.hand(2)["last_pulse"]), (epochs[-1] + ":1:1", None))


class ReceiverCloseTests(unittest.IsolatedAsyncioTestCase):
    async def receiver_with_writer(self, writer):
        stream = asyncio.StreamReader()
        stream.feed_data(encode_frame(dict(v=1, type="SUBSCRIBED", session_id="ltc-comms")))
        stream.feed_data(encode_frame(result()))
        args = SimpleNamespace(ca="unused", port=19999, session="ltc-comms", count=1, duration=None)
        with patch("phone.receiver.tls_context", return_value=object()), \
             patch("phone.receiver.asyncio.open_connection", AsyncMock(return_value=(stream, writer))), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return await receive(args)

    async def test_stalled_tls_shutdown_aborts_socket_after_deadline(self):
        writer = ClosingWriter(stall=True)
        summary = await self.receiver_with_writer(writer)
        self.assertEqual(summary["received"], 1)
        self.assertTrue(writer.closed)
        self.assertTrue(writer.aborted, "timed-out close must release its socket")

    async def test_cancellation_during_tls_shutdown_aborts_socket_and_propagates(self):
        writer = ClosingWriter(stall=True)
        task = asyncio.create_task(self.receiver_with_writer(writer))
        await asyncio.wait_for(writer.closing.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(writer.aborted, "cancelled close must release its socket")

    async def test_clean_tls_shutdown_preserves_graceful_close(self):
        writer = ClosingWriter(stall=False)
        self.assertEqual((await self.receiver_with_writer(writer))["received"], 1)
        self.assertTrue(writer.closed)
        self.assertFalse(writer.aborted)


class ClosingWriter:
    """TLS shutdown boundary: no network mock participates in schema validation."""
    def __init__(self, stall):
        self.stall = stall
        self.closed = self.aborted = False
        self.closing = asyncio.Event()
        self.transport = SimpleNamespace(abort=self.abort)

    def write(self, data):
        pass

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    def abort(self):
        self.aborted = True

    async def wait_closed(self):
        self.closing.set()
        if self.stall:
            await asyncio.Event().wait()


class RecordingWriter(ClosingWriter):
    """Keeps what the receiver sends, so tests can read its SUBSCRIBE."""
    def __init__(self):
        super().__init__(stall=False)
        self.data = bytearray()

    def write(self, data):
        self.data += data

    def frames(self):
        frames, data = [], bytes(self.data)
        while data:
            size = int.from_bytes(data[:4], "big")
            frames.append(json.loads(data[4:4 + size].decode("utf-8")))
            data = data[4 + size:]
        return frames


class HandReceiverTests(unittest.IsolatedAsyncioTestCase):
    async def run_receiver(self, streams, count=1, **options):
        """Real framing and validation; only the TLS transport is replaced."""
        writers = [RecordingWriter() for _ in streams]
        output, errors = io.StringIO(), io.StringIO()
        args = SimpleNamespace(ca="unused", port=19999, session="ltc-comms", count=count, duration=None, **options)
        tracker = HandStateTracker("ltc-comms", clock=FakeClock()) if options.get("hand_events") else None
        with patch("phone.receiver.tls_context", return_value=object()), \
             patch("phone.receiver.asyncio.open_connection", AsyncMock(side_effect=list(zip(streams, writers)))), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            summary = await asyncio.wait_for(receive(args, tracker=tracker), 5)
        return SimpleNamespace(summary=summary, lines=[json.loads(line) for line in output.getvalue().splitlines()],
                               errors=errors.getvalue(), sent=[writer.frames() for writer in writers])

    async def test_default_receiver_is_unchanged_and_fails_on_hand_event(self):
        # Without --hand-events (also when args has no such attribute, as for older callers).
        for options in ({}, dict(hand_events=False)):
            pulse = every_kind()["pulse"]
            run = await self.run_receiver([stream_of(SUBSCRIBED_V1, pulse), stream_of(SUBSCRIBED_V1, result())],
                                          **options)
            with self.subTest(options=options):
                self.assertEqual(run.summary, dict(received=1, reconnects=1))
                self.assertEqual(run.lines, [result()])
                self.assertEqual(run.sent, [[SUBSCRIBE_V1], [SUBSCRIBE_V1]])
                self.assertEqual(run.errors.splitlines(), ["subscribed session=ltc-comms",
                                                           "reconnect reason=ProtocolError",
                                                           "subscribed session=ltc-comms"])

    async def test_default_receiver_refuses_subscribed_version_2(self):
        run = await self.run_receiver([stream_of(SUBSCRIBED_V2, result()), stream_of(SUBSCRIBED_V1, result())])
        self.assertEqual(run.summary, dict(received=1, reconnects=1))
        self.assertEqual(run.lines, [result()])

    async def test_hand_receiver_needs_subscribed_version_2(self):
        run = await self.run_receiver([stream_of(SUBSCRIBED_V1, result()), stream_of(SUBSCRIBED_V2, result())],
                                      hand_events=True)
        self.assertEqual((run.summary["received"], run.summary["reconnects"]), (1, 1))
        self.assertEqual(run.lines, [result()])
        self.assertEqual(run.sent, [[SUBSCRIBE_V2], [SUBSCRIBE_V2]])
        self.assertIn("hand_events subscription not confirmed", run.errors)
        self.assertEqual(run.errors.splitlines()[-1], "subscribed session=ltc-comms hand_events=1")

    async def test_service_closing_on_version_2_subscribe_is_reported(self):
        # A service from before HAND_EVENT closes the connection instead of answering.
        run = await self.run_receiver([stream_of(eof=True), stream_of(SUBSCRIBED_V2, result())], hand_events=True)
        self.assertEqual((run.summary["received"], run.summary["reconnects"]), (1, 1))
        self.assertEqual(run.errors.splitlines()[:2], ["reconnect reason=ProtocolError",
                                                       "hand_events subscription not confirmed"])

    async def test_one_stream_carries_results_and_hand_events(self):
        events = every_kind()
        broken = dict(events["pulse"], event_id=EPOCH + ":2:9")
        stranger = dict(events["idle"], session_id="elsewhere")
        right = dict(result(), device_id=2, boot_id=12, seq=1, result_id="2:12:1", gesture="THROW", confidence=0.9)
        # The repeated result() is still deduplicated by result_id, as on version 1.
        stream = stream_of(SUBSCRIBED_V2, events["state_start"], result(), events["pulse"], broken, events["pulse"],
                           result(), stranger, events["state_refresh"], right)
        run = await self.run_receiver([stream], count=2, hand_events=True)
        # Invalid hand events are counted and skipped; the results around them still arrive.
        self.assertEqual(run.lines, [events["state_start"], result(), events["pulse"], events["state_refresh"], right])
        self.assertEqual(run.summary, dict(received=2, reconnects=0, hand_events=3, hand_invalid=2, hands=[
            dict(device_id=1, hand="LEFT", active_gesture="CHOP", state_id=events["state_start"]["event_id"],
                 expires_in_ms=1000, last_pulse=None, last_pulse_event_id=None, accepted=2, duplicate=0, ignored=0),
            dict(device_id=2, hand="RIGHT", active_gesture=None, state_id=None, expires_in_ms=None,
                 last_pulse="THROW", last_pulse_event_id=events["pulse"]["event_id"],
                 accepted=1, duplicate=1, ignored=0)]))
        self.assertEqual(run.sent, [[SUBSCRIBE_V2]])
        self.assertEqual(run.errors.count("hand_event_invalid reason="), 2)
        self.assertNotIn("reconnect", run.errors)

    async def test_other_frames_still_fail_a_version_2_subscription(self):
        pulse = every_kind()["pulse"]
        for bad in [dict(v=1, type="MONITOR_UPDATE", session_id="ltc-comms"), dict(result(), v=2),
                    dict(pulse, type="hand_event")]:
            run = await self.run_receiver([stream_of(SUBSCRIBED_V2, bad), stream_of(SUBSCRIBED_V2, result())],
                                          hand_events=True)
            with self.subTest(bad=bad):
                self.assertEqual((run.summary["received"], run.summary["reconnects"]), (1, 1))
                self.assertEqual(run.summary["hand_invalid"], 0)
                self.assertEqual(run.lines, [result()])


class ReceiverStartupTests(unittest.IsolatedAsyncioTestCase):
    @contextlib.asynccontextmanager
    async def running_receiver(self, streams, count=1):
        """Keep framing real; scale its budgets and replace only TLS transport."""
        writers = [ClosingWriter(stall=False) for _ in streams]
        output, errors = io.StringIO(), io.StringIO()
        statistics = dict(received=0, reconnects=0)
        args = SimpleNamespace(ca="unused", port=19999, session="ltc-comms", count=count, duration=None)

        async def fast_frame(reader, timeout=5.0, first_byte_timeout=None, allow_idle=False):
            options = {} if first_byte_timeout is None else dict(first_byte_timeout=first_byte_timeout * 0.02)
            if allow_idle:
                options['allow_idle'] = True
            return await read_frame(reader, timeout=timeout * 0.02, **options)

        with patch("phone.receiver.tls_context", return_value=object()), \
             patch("phone.receiver.asyncio.open_connection", AsyncMock(side_effect=list(zip(streams, writers)))), \
             patch("phone.receiver.read_frame", fast_frame), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            task = asyncio.create_task(receive(args, statistics))
            try:
                yield task, statistics, output, errors, writers
            finally:
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def subscribed_stream(self):
        stream = asyncio.StreamReader()
        stream.feed_data(encode_frame(dict(v=1, type="SUBSCRIBED", session_id="ltc-comms")))
        return stream

    async def test_cold_start_waits_beyond_frame_deadline_without_reconnecting(self):
        stream = self.subscribed_stream()
        async with self.running_receiver([stream]) as (task, statistics, output, errors, _):
            # Scaled five-second frame deadline is 0.1 s; BLE is still starting.
            await asyncio.sleep(0.25)
            self.assertEqual(statistics["reconnects"], 0, errors.getvalue())
            stream.feed_data(encode_frame(result()))
            self.assertEqual(await asyncio.wait_for(task, 0.3), dict(received=1, reconnects=0))
            self.assertEqual(json.loads(output.getvalue()), result())

    async def test_later_clean_idle_resumes_on_same_subscription(self):
        stream = self.subscribed_stream(); stream.feed_data(encode_frame(result()))
        async with self.running_receiver([stream], count=2) as (task, statistics, output, _, _):
            await asyncio.sleep(0.25)
            self.assertEqual(statistics, dict(received=1, reconnects=0))
            next_result = dict(result(), seq=4, result_id='1:4294967295:4', gesture='CHOP', confidence=0.41)
            stream.feed_data(encode_frame(next_result))
            await asyncio.wait_for(task, 0.3)
            self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()], [result(), next_result])

    async def test_first_result_clean_idle_has_no_frame_deadline(self):
        stream = self.subscribed_stream()
        async with self.running_receiver([stream]) as (_, statistics, _, _, writers):
            await asyncio.sleep(0.7)
            self.assertFalse(writers[0].closing.is_set())
            self.assertEqual(statistics, dict(received=0, reconnects=0))

    async def test_partial_result_still_expires_after_long_clean_idle(self):
        stream = self.subscribed_stream()
        async with self.running_receiver([stream]) as (_, statistics, _, _, writers):
            await asyncio.sleep(0.25)
            stream.feed_data(b'\x00')
            await asyncio.wait_for(writers[0].closing.wait(), 0.3)
            self.assertEqual(statistics, dict(received=0, reconnects=1))

    async def test_subscribed_response_retains_frame_deadline(self):
        stream = asyncio.StreamReader()
        async with self.running_receiver([stream]) as (_, statistics, _, _, writers):
            await asyncio.wait_for(writers[0].closing.wait(), 0.4)
            self.assertEqual(statistics, dict(received=0, reconnects=1))

    async def test_invalid_result_does_not_consume_startup_grace(self):
        first = self.subscribed_stream()
        # A result from a service still on protocol version 2.
        invalid = result(); invalid["v"] = 2
        first.feed_data(encode_frame(invalid))
        second = self.subscribed_stream()
        async with self.running_receiver([first, second]) as (task, statistics, output, errors, _):
            # The invalid result forces reconnect. The replacement subscription
            # is still allowed to wait for its first valid result beyond 0.1 s.
            await asyncio.sleep(0.8)
            self.assertEqual(statistics, dict(received=0, reconnects=1), errors.getvalue())
            second.feed_data(encode_frame(result()))
            self.assertEqual(await asyncio.wait_for(task, 0.3), dict(received=1, reconnects=1))
            self.assertEqual(json.loads(output.getvalue()), result())

    async def test_reconnect_after_valid_result_also_allows_clean_idle(self):
        first = self.subscribed_stream(); first.feed_data(encode_frame(result())); first.feed_eof()
        second = self.subscribed_stream()
        async with self.running_receiver([first, second], count=0) as (_, statistics, _, _, writers):
            # First connection ends at EOF; the replacement remains subscribed.
            await asyncio.sleep(0.85)
            self.assertFalse(writers[1].closing.is_set())
            self.assertEqual(statistics, dict(received=1, reconnects=1))


class ReceiverDurationTests(unittest.TestCase):
    def test_cli_duration_bounds_first_result_idle_and_closes_connection(self):
        from phone.receiver import main
        writers = []

        async def open_stream(*args, **kwargs):
            stream = asyncio.StreamReader()
            stream.feed_data(encode_frame(dict(v=1, type="SUBSCRIBED", session_id="ltc-comms")))
            writer = ClosingWriter(stall=False)
            writers.append(writer)
            return stream, writer

        errors = io.StringIO()
        with patch("phone.receiver.tls_context", return_value=object()), \
             patch("phone.receiver.asyncio.open_connection", open_stream), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            exit_code = main(["--ca", "unused", "--count", "1", "--duration", "0.05"])
        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(errors.getvalue().splitlines()[-1]), dict(received=0, reconnects=0))
        self.assertEqual(len(writers), 1)
        self.assertTrue(writers[0].closed)

    def test_cli_hand_events_prints_events_and_a_per_hand_summary(self):
        from phone.receiver import main
        encoder = EventEncoder(hand_hello(lease_ms=100))
        wash = one_event(encoder, "state_start", "WASH", device=2, seq=1)
        pickup = one_event(encoder, "action", "PICKUP", device=1, seq=1)
        writers = []

        async def open_stream(*args, **kwargs):
            writer = RecordingWriter()
            writers.append(writer)
            return stream_of(SUBSCRIBED_V2, wash, pickup), writer

        output, errors = io.StringIO(), io.StringIO()
        with patch("phone.receiver.tls_context", return_value=object()), \
             patch("phone.receiver.asyncio.open_connection", open_stream), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            exit_code = main(["--ca", "unused", "--hand-events", "--duration", "0.3"])
        self.assertEqual(exit_code, 0)
        self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()], [wash, pickup])
        self.assertEqual(writers[0].frames(), [SUBSCRIBE_V2])
        # The 100 ms lease ran out before the run ended, so the summary shows no ongoing state.
        self.assertEqual(json.loads(errors.getvalue().splitlines()[-1]), dict(
            received=0, reconnects=0, hand_events=2, hand_invalid=0, hands=[
                dict(device_id=1, hand="LEFT", active_gesture=None, state_id=None, expires_in_ms=None,
                     last_pulse="PICKUP", last_pulse_event_id=pickup["event_id"], accepted=1, duplicate=0, ignored=0),
                dict(device_id=2, hand="RIGHT", active_gesture=None, state_id=None, expires_in_ms=None,
                     last_pulse=None, last_pulse_event_id=None, accepted=1, duplicate=0, ignored=0)]))


class TlsReceiverTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        from tools.generate_comms_pki import generate_pki
        cls.directory = tempfile.TemporaryDirectory(prefix="comms-phone-test-")
        cls.folder = Path(cls.directory.name)
        generate_pki(cls.folder / "pki")
        generate_pki(cls.folder / "wrong")
        cls.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_context.load_cert_chain(cls.folder / "pki/server-cert.pem", cls.folder / "pki/server-key.pem")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    async def test_actual_tls_receiver_recovers_and_prints_result(self):
        attempts = []
        handlers = set()
        async def handle(reader, writer):
            task = asyncio.current_task(); handlers.add(task)
            try:
                attempts.append(await read_frame(reader))
                # SUBSCRIBED stays at version 1; a version-3 envelope forces one reconnect.
                writer.write(encode_frame(dict(v=3 if len(attempts) == 1 else 1, type="SUBSCRIBED", session_id="ltc-comms")))
                if len(attempts) > 1:
                    writer.write(encode_frame(result()))
                await writer.drain()
                await reader.read()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                handlers.discard(task)
        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=self.server_context)
        args = SimpleNamespace(ca=self.folder / "pki/ca-cert.pem", port=server.sockets[0].getsockname()[1],
                               session="ltc-comms", count=1, duration=4)
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                summary = await asyncio.wait_for(receive(args), 5)
            self.assertEqual(json.loads(output.getvalue()), result())
            self.assertEqual(summary, dict(received=1, reconnects=1))
            self.assertEqual(attempts, [dict(v=1, type="SUBSCRIBE", session_id="ltc-comms")] * 2)
        finally:
            server.close(); await server.wait_closed()
            if handlers:
                await asyncio.gather(*handlers)

    async def test_actual_tls_hand_event_subscription(self):
        encoder = EventEncoder(hand_hello())
        chop = one_event(encoder, "state_start", "CHOP", seq=1)
        pickup = one_event(encoder, "action", "PICKUP", device=2, seq=1)
        chop_end = one_event(encoder, "state_end", "CHOP", time=0.25, seq=2, reason="confirmed_transition")
        pouring = one_event(encoder, "state_start", "POURING", time=0.25, seq=2)
        pouring_refresh = one_event(encoder, "prediction", "POURING", time=0.5, seq=3)
        drop = one_event(encoder, "action", "DROP", device=2, time=0.25, seq=2)
        broken = dict(pickup, event_id=EPOCH + ":2:9")
        right = dict(result(), device_id=2, boot_id=12, seq=2, result_id="2:12:2", gesture="DROP", confidence=0.9)
        connections = [
            # Both hands, a duplicate, an invalid event, a result and a CHOP-to-POURING
            # transition on one source sample; then the service side closes.
            [chop, pickup, pickup, broken, result(), chop_end, pouring],
            # After the reconnect the old POURING state is not revived by its refresh.
            [pouring_refresh, drop, right]]
        attempts = []
        handlers = set()

        async def handle(reader, writer):
            task = asyncio.current_task(); handlers.add(task)
            try:
                attempts.append(await read_frame(reader))
                writer.write(encode_frame(SUBSCRIBED_V2))
                for message in connections[len(attempts) - 1]:
                    writer.write(encode_frame(message))
                await writer.drain()
                if len(attempts) > 1:
                    await reader.read()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                handlers.discard(task)
        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=self.server_context)
        args = SimpleNamespace(ca=self.folder / "pki/ca-cert.pem", port=server.sockets[0].getsockname()[1],
                               session="ltc-comms", count=2, duration=4, hand_events=True)
        tracker = HandStateTracker("ltc-comms", clock=FakeClock())
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                summary = await asyncio.wait_for(receive(args, tracker=tracker), 5)
            self.assertEqual(attempts, [SUBSCRIBE_V2] * 2)
            self.assertEqual(chop_end["source"], pouring["source"])
            self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()],
                             [chop, pickup, result(), chop_end, pouring, drop, right])
            self.assertEqual(summary, dict(received=2, reconnects=1, hand_events=5, hand_invalid=1, hands=[
                dict(device_id=1, hand="LEFT", active_gesture=None, state_id=None, expires_in_ms=None,
                     last_pulse=None, last_pulse_event_id=None, accepted=3, duplicate=0, ignored=1),
                dict(device_id=2, hand="RIGHT", active_gesture=None, state_id=None, expires_in_ms=None,
                     last_pulse="DROP", last_pulse_event_id=drop["event_id"], accepted=2, duplicate=1, ignored=0)]))
            self.assertEqual([(end["gesture"], end["reason"]) for end in tracker.ends],
                             [("CHOP", "confirmed_transition"), ("POURING", "subscription_reset")])
        finally:
            server.close(); await server.wait_closed()
            if handlers:
                await asyncio.gather(*handlers)

    async def test_actual_tls_rejects_wrong_ca_and_wrong_name(self):
        async def handle(reader, writer):
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=self.server_context)
        port = server.sockets[0].getsockname()[1]
        try:
            for ca, identity in [("wrong", "ultra96.week7.internal"), ("pki", "wrong-host.internal")]:
                with self.subTest(ca=ca, identity=identity), self.assertRaises(ssl.SSLCertVerificationError):
                    await asyncio.open_connection("127.0.0.1", port, ssl=tls_context(self.folder / ca / "ca-cert.pem"), server_hostname=identity)
        finally:
            server.close(); await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
