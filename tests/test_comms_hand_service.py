"""The HAND_EVENT path through the board service: model port, model bridge, hand gate, subscription.

No hardware. The service tests run a real TLS CommsServer in this process. Its model
is a model_bridge.Bridge whose runner connections are socket pairs attached with
Bridge.attach (Windows has no AF_UNIX), and a scripted runner sends hand_hello,
hand_event and compat result lines. Its events come from Wen Ree's EventEncoder
(src.hand_events), as the worker makes them.
"""
import ast
import asyncio
import json
import os
from pathlib import Path
import queue
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time

import pytest

from common import sensor, sensor_v4
from common.hand_contract import make_hello
from common.wire import read_frame, write_frame
from src.hand_events import EventEncoder
from ultra96 import model_bridge
from ultra96.model import ModelPort, ModelStartError, load_model
from ultra96.model_bridge import Bridge

ROOT = Path(__file__).resolve().parents[1]
SESSION = "ltc-comms"
LEFT, RIGHT = 1, 2
EPOCHS = ("{:032x}".format(0xE1), "{:032x}".format(0xE2))


def wait_for(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


async def until(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def blocking(function, *args):
    return await asyncio.get_running_loop().run_in_executor(None, function, *args)


# --- ultra96.server.ResultQueue: a full queue drops hand events first ---------------------------

def test_a_full_queue_drops_hand_events_first_so_results_fare_as_for_a_version_1_subscriber():
    from ultra96.server import ResultQueue

    def result(seq):
        return dict(v=3, type="GESTURE_RESULT", session_id=SESSION, device_id=LEFT, boot_id=7, seq=seq,
                    request_id=None, result_id="1:7:%d" % seq, gesture="CHOP", confidence=0.9)

    def hand(seq):
        return dict(type="HAND_EVENT", session_id=SESSION, device_id=RIGHT, event_seq=seq, kind="pulse")

    async def scenario():
        events = []
        queue = ResultQueue(observer=lambda event, **fields: events.append((event, fields.get("reason"))),
                            hand_events=True)
        for seq in range(1, 21):
            queue.put(result(seq))
        for seq in range(1, 13):
            queue.put(hand(seq))
        queue.put(result(21))  # full: the oldest hand event gives way, no result is lost
        assert (queue.dropped, queue.hand_dropped) == (0, 1)
        for seq in range(22, 33):
            queue.put(result(seq))  # each pushes out one more hand event
        assert (queue.dropped, queue.hand_dropped) == (0, 12)
        queue.put(hand(13))  # only results are queued: the new hand event is dropped itself
        assert (queue.dropped, queue.hand_dropped) == (0, 13)
        queue.put(result(33))  # and a new result drops the oldest result, as on version 1
        assert (queue.dropped, queue.hand_dropped) == (1, 13)
        taken = [(await queue.get())["seq"] for _ in range(32)]
        assert taken == list(range(2, 34))  # arrival order kept
        assert ("hand_event_dropped", "drop_new") in events
        assert sum(1 for event in events if event == ("hand_event_dropped", "drop_oldest")) == 12
        assert sum(1 for event in events if event[0] == "result_drop_oldest") == 1

    asyncio.run(scenario())


def test_a_version_1_queue_drops_its_oldest_result_as_before():
    from ultra96.server import ResultQueue

    async def scenario():
        queue = ResultQueue()
        for seq in range(1, 34):
            queue.put(dict(type="GESTURE_RESULT", device_id=LEFT, seq=seq))
        assert (queue.dropped, queue.hand_dropped) == (1, 0)
        assert [(await queue.get())["seq"] for _ in range(32)] == list(range(2, 34))

    asyncio.run(scenario())


# --- ultra96.protocol: SUBSCRIBE and SUBSCRIBED versions 1 and 2 ----------------------------------

def test_subscription_versions_1_and_2_have_exact_fields_in_either_sensor_mode():
    from common.wire import ProtocolError
    from ultra96.protocol import subscribed, validate_message
    for kind in ("SUBSCRIBE", "SUBSCRIBED"):
        first = dict(v=1, type=kind, session_id=SESSION)
        second = dict(v=2, type=kind, session_id=SESSION, event_format=1)
        for version in (3, 4):
            assert validate_message(first, kind, SESSION, sensor_version=version) is first
            assert validate_message(second, kind, SESSION, sensor_version=version) is second
        for bad in (dict(first, event_format=1), {key: value for key, value in second.items() if key != "event_format"},
                    dict(second, event_format=2), dict(second, event_format=True), dict(second, event_format=1.0),
                    dict(second, event_format="1"), dict(second, extra=1), dict(second, v=3), dict(first, v=True),
                    dict(second, session_id="elsewhere")):
            with pytest.raises(ProtocolError):
                validate_message(bad, kind, SESSION, sensor_version=4)
    assert subscribed(dict(v=1, type="SUBSCRIBE", session_id=SESSION)) == dict(
        v=1, type="SUBSCRIBED", session_id=SESSION)
    assert subscribed(dict(v=2, type="SUBSCRIBE", session_id=SESSION, event_format=1)) == dict(
        v=2, type="SUBSCRIBED", session_id=SESSION, event_format=1)


def test_a_client_accepts_only_the_subscribed_of_the_version_it_sent():
    from common.wire import ProtocolError
    from ultra96.protocol import validate_subscribed
    first = dict(v=1, type="SUBSCRIBE", session_id=SESSION)
    second = dict(v=2, type="SUBSCRIBE", session_id=SESSION, event_format=1)
    answers = (dict(first, type="SUBSCRIBED"), dict(second, type="SUBSCRIBED"))
    assert validate_subscribed(answers[0], first) is answers[0]
    assert validate_subscribed(answers[1], second) is answers[1]
    # validate_message takes either version; a client must not take the other one's answer.
    for answer, request in ((answers[1], first), (answers[0], second), (dict(answers[1], event_format=2), second),
                            (dict(answers[0], session_id="elsewhere"), first), (dict(answers[0], v=3), first)):
        with pytest.raises(ProtocolError):
            validate_subscribed(answer, request)


# --- ultra96.model: the optional start_hand_events(emit_hand) ------------------------------------

class HandModel:
    """A model with the optional hand entry; it records the order of its start calls."""
    name = "hand-model"

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def start(self, emit):
        self.calls.append("start")
        self.emit = emit

    def start_hand_events(self, emit_hand):
        self.calls.append("start_hand_events")
        self.emit_hand = emit_hand
        if self.fail is not None:
            raise self.fail

    def on_sample(self, device_id, boot_id, seq, uptime_ms, values):
        pass


def test_the_model_port_gives_emit_hand_right_after_start_only_to_a_hand_consumer():
    async def check():
        loop = asyncio.get_running_loop()
        model = HandModel()
        ModelPort(model, print, {}, deliver_hand=print).start(loop)
        assert model.calls == ["start", "start_hand_events"]
        # Without a hand consumer (sensor version 3) it is never called, and no metric appears.
        plain, metrics = HandModel(), {}
        ModelPort(plain, print, metrics).start(loop)
        assert plain.calls == ["start"] and not any(key.startswith("model_hand") for key in metrics)
        failing = HandModel(fail=RuntimeError("no"))
        with pytest.raises(ModelStartError, match=r"start_hand_events\(emit_hand\) raised RuntimeError"):
            ModelPort(failing, print, {}, deliver_hand=print).start(loop)
        assert failing.emit_hand("hello", {}, 1) is False  # the port closed
    asyncio.run(check())


def test_emit_hand_from_a_runner_thread_keeps_the_call_order_with_results_and_copies_payloads():
    async def check():
        delivered, metrics = [], {}
        model = HandModel()
        port = ModelPort(model, lambda *result: delivered.append(("result",) + result), metrics,
                         deliver_hand=lambda *message: delivered.append(message))
        port.start(asyncio.get_running_loop())

        def runner():
            for seq in range(50):
                payload = {"type": "HAND_EVENT", "event_seq": seq}
                assert model.emit_hand("event", payload, 3) is True
                payload["event_seq"] = -1  # changed after the call: the service has its own copy
                assert model.emit("CHOP", 0.9, 1, 7, seq) is True
            assert model.emit_hand("closed", None, 3) is True

        thread = threading.Thread(target=runner)
        thread.start()
        await blocking(thread.join, 5)
        await until(lambda: len(delivered) == 101)
        expected = []
        for seq in range(50):
            expected += [("event", {"type": "HAND_EVENT", "event_seq": seq}, 3),
                         ("result", "CHOP", 0.9, 1, 7, seq)]
        assert delivered == expected + [("closed", None, 3)]
        assert metrics["model_hand_rejected"] == metrics["model_hand_late"] == 0
    asyncio.run(check())


def test_malformed_emit_hand_calls_and_calls_after_close_are_refused_and_counted():
    async def check():
        delivered, metrics = [], {}
        model = HandModel()
        port = ModelPort(model, print, metrics, deliver_hand=lambda *message: delivered.append(message))
        port.start(asyncio.get_running_loop())
        for call in (("other", {}, 1), ("event", [], 1), ("hello", None, 1), ("closed", {}, 1),
                     ("event", {}, True), ("event", {}, -1), ("event", {}, "1"),
                     ("event", {"lock": threading.Lock()}, 1)):
            assert model.emit_hand(*call) is False
        assert metrics["model_hand_rejected"] == 8 and delivered == []
        port.close()
        assert model.emit_hand("event", {}, 1) is False
        assert metrics["model_hand_late"] == 1 and delivered == []
    asyncio.run(check())


def test_load_model_accepts_the_bridge_and_refuses_an_async_start_hand_events(tmp_path):
    assert load_model("ultra96.model_bridge") is model_bridge
    name = "comms_hand_async_model"
    (tmp_path / (name + ".py")).write_text(
        "def start(emit):\n    pass\n\n\ndef on_sample(*args):\n    pass\n\n\n"
        "async def start_hand_events(emit_hand):\n    pass\n", encoding="utf-8")
    try:
        with pytest.raises(TypeError, match="start_hand_events"):
            load_model(name, tmp_path)
    finally:
        sys.modules.pop(name, None)
        sys.path.remove(str(tmp_path))


# --- ultra96.model_bridge: hand lines ---------------------------------------------------------------

class Calls:
    """A bridge's emit and emit_hand, recorded in call order."""

    def __init__(self, answer=True):
        self.queue = queue.Queue()
        self.answer = answer

    def emit(self, *fields):
        self.queue.put(("result",) + fields)
        return self.answer

    def emit_hand(self, kind, payload, connection):
        self.queue.put((kind, payload, connection))
        return self.answer

    def get(self):
        return self.queue.get(timeout=5)


def line(message):
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


class Runner:
    """The model runner's end of one bridge connection; reads the bridge's hello."""

    def __init__(self, bridge):
        service_end, self.sock = socket.socketpair()
        self.sock.settimeout(5)
        bridge.attach(service_end)
        self.lines = self.sock.makefile("r", encoding="utf-8")
        self.hello = self.read()

    def read(self):
        return json.loads(self.lines.readline())

    def send(self, *messages):
        self.sock.sendall(b"".join(line(message) for message in messages))

    def close(self):
        self.lines.close()
        self.sock.close()


def bridge_for(version, calls=None, hand=True):
    bridge = Bridge()
    bridge.configure_sensor_version(version)
    calls = Calls() if calls is None else calls
    bridge.start(calls.emit)
    if hand:
        bridge.start_hand_events(calls.emit_hand)
    return bridge, calls


def result_line(gesture, device, boot, seq, confidence=0.9):
    return dict(type="result", gesture=gesture, confidence=confidence, device_id=device, boot_id=boot, seq=seq)


def test_the_v4_hello_offers_hand_events_and_the_v3_hello_is_unchanged():
    for version, channels in ((3, sensor.CHANNELS), (4, sensor_v4.CHANNELS)):
        bridge, _ = bridge_for(version)
        runner = Runner(bridge)
        expected = dict(type="hello", format=1, channels=list(channels))
        if version == 4:
            expected["hand_events"] = 1
        assert runner.hello == expected
        runner.close()
        bridge.close()


def test_hand_lines_reach_emit_hand_in_order_with_their_connection_number():
    bridge, calls = bridge_for(4)
    runner = Runner(bridge)
    hello = make_hello(SESSION, EPOCHS[0])
    event = {"type": "HAND_EVENT", "nested": [1, {"x": None}]}  # the bridge does not check the schema
    runner.send(dict(type="hand_hello", hello=hello), dict(type="hand_event", event=event),
                result_line("CHOP", 1, 7, 1), dict(type="hand_event", event=event, extra=1))
    assert [calls.get() for _ in range(4)] == [
        ("hello", hello, 1), ("event", event, 1), ("result", "CHOP", 0.9, 1, 7, 1), ("event", event, 1)]
    # Non-object payloads, missing payloads and unknown types are bad lines, as before.
    runner.send(dict(type="hand_event", event=[event]), dict(type="hand_event"),
                dict(type="hand_hello", hello="hi"), dict(type="hand_other", event=event),
                dict(type=["hand_event"], event=event), result_line("CHOP", 1, 7, 2))
    assert calls.get() == ("result", "CHOP", 0.9, 1, 7, 2)
    assert {key: bridge.counts[key] for key in ("bad_lines", "hand_hellos", "hand_events", "hand_refused",
                                                "results")} == dict(bad_lines=5, hand_hellos=1, hand_events=2,
                                                                    hand_refused=0, results=2)
    # The connection ends: emit_hand hears "closed" for its number, after its last line.
    runner.close()
    assert calls.get() == ("closed", None, 1)
    second = Runner(bridge)
    second.send(dict(type="hand_event", event=event))
    assert calls.get() == ("event", event, 2)
    # A new runner replaces the second one: "closed" names the replaced connection. (On Linux
    # the bridge's shutdown wakes the old reader at once; on Windows only the peer's close does.)
    third = Runner(bridge)
    second.close()
    third.send(dict(type="hand_event", event=event))
    assert sorted((calls.get(), calls.get()), key=repr) == sorted(
        [("closed", None, 2), ("event", event, 3)], key=repr)
    bridge.close()
    third.close()
    assert calls.get() == ("closed", None, 3)


def test_hand_lines_are_bad_lines_in_v3_mode_and_refused_when_nobody_takes_them():
    bridge, calls = bridge_for(3)
    runner = Runner(bridge)
    runner.send(dict(type="hand_hello", hello={}), dict(type="hand_event", event={}), result_line("WASH", 2, 1, 2))
    assert calls.get() == ("result", "WASH", 0.9, 2, 1, 2)
    assert bridge.counts["bad_lines"] == 2 and bridge.counts["hand_events"] == 0
    runner.close()
    wait_for(lambda: bridge._current is None)
    time.sleep(0.05)
    assert calls.queue.empty()  # no "closed" either: version 3 has no hand path
    bridge.close()
    # Version 4, but the model never registered emit_hand: refused, not bad lines.
    bridge, calls = bridge_for(4, hand=False)
    runner = Runner(bridge)
    runner.send(dict(type="hand_event", event={}), result_line("WASH", 2, 1, 3))
    assert calls.get() == ("result", "WASH", 0.9, 2, 1, 3)
    assert bridge.counts["hand_refused"] == 1 and bridge.counts["bad_lines"] == 0
    runner.close()
    bridge.close()
    # emit_hand says no (the service is stopping): refused as well.
    bridge, calls = bridge_for(4, Calls(answer=False))
    runner = Runner(bridge)
    runner.send(dict(type="hand_event", event={}))
    assert calls.get() == ("event", {}, 1)
    wait_for(lambda: bridge.counts["hand_refused"] == 1)
    runner.close()
    bridge.close()


# --- the service end to end, with real TLS -------------------------------------------------------

@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    from tools.generate_comms_pki import generate_pki
    folder = tmp_path_factory.mktemp("comms-hand-service-pki")
    generate_pki(folder)
    return folder


class BridgeModel:
    """The service's model: a model_bridge.Bridge whose runners attach over socket pairs."""
    name = "hand-bridge"

    def __init__(self):
        self.bridge = Bridge()

    def configure_sensor_version(self, version):
        self.bridge.configure_sensor_version(version)

    def start(self, emit):
        self.bridge.start(emit)

    def start_hand_events(self, emit_hand):
        self.bridge.start_hand_events(emit_hand)

    def on_sample(self, device_id, boot_id, seq, uptime_ms, values):
        self.bridge.on_sample(device_id, boot_id, seq, uptime_ms, values)


class Script:
    """The worker's side of one runner connection: its hello and Wen Ree's EventEncoder."""

    def __init__(self, epoch):
        self.hello = make_hello(SESSION, epoch)
        self.encoder = EventEncoder(self.hello)
        self.elapsed = {LEFT: 0.0, RIGHT: 0.0}

    def event(self, kind, device, sample, gesture, confidence=0.93, reason=None):
        self.elapsed[device] += 0.2
        record = dict(type=kind, device_id=device, elapsed_s=self.elapsed[device], gesture=gesture,
                      confidence=confidence, **{key: sample[key] for key in ("boot_id", "seq", "uptime_ms")})
        if reason is not None:
            record["reason"] = reason
        (event,) = self.encoder.consume(record)
        return event


async def start_server(pki, **kwargs):
    from common.tls import server_context
    from ultra96.server import CommsServer
    server = CommsServer(server_context(pki / "server-cert.pem", pki / "server-key.pem"),
                         ingest_port=0, gateway_port=0, **kwargs)
    await server.start()
    return server


async def connect(pki, port):
    from common.tls import TLS_SERVER_NAME, client_context
    return await asyncio.open_connection("127.0.0.1", port, ssl=client_context(pki / "ca-cert.pem"),
                                         server_hostname=TLS_SERVER_NAME)


async def disconnect(writer):
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 2)
    except (ConnectionError, OSError, ssl.SSLError, asyncio.TimeoutError):
        writer.transport.abort()


async def subscribe(pki, server, version):
    reader, writer = await connect(pki, server.gateway_port)
    request = dict(v=version, type="SUBSCRIBE", session_id=SESSION)
    if version == 2:
        request["event_format"] = 1
    await write_frame(writer, request)
    assert await read_frame(reader) == dict(request, type="SUBSCRIBED")
    return reader, writer


async def frame_body(reader, timeout=5.0):
    """One frame's raw JSON bytes, as the phone receives them."""
    async def receive():
        length = struct.unpack("!I", await reader.readexactly(4))[0]
        return await reader.readexactly(length)
    return await asyncio.wait_for(receive(), timeout)


def sample(version, device, boot, seq):
    codec = sensor_v4 if version == 4 else sensor
    fixtures = codec.DUMMY_FIXTURES
    return codec.SensorPacket(device, boot, seq, 1000 + 20 * seq, fixtures[seq % len(fixtures)]).to_message(SESSION)


class Glove:
    """The laptop's ingest connection, and the runner that must see each accepted sample."""

    def __init__(self, reader, writer, version):
        self.reader, self.writer, self.version = reader, writer, version

    async def send(self, runner, device, boot, seq):
        message = sample(self.version, device, boot, seq)
        await write_frame(self.writer, message)
        assert (await read_frame(self.reader))["status"] == "accepted"
        forwarded = await blocking(runner.read)
        assert (forwarded["type"], forwarded["device_id"], forwarded["seq"]) == ("sample", device, seq)
        return message


async def scenario(pki, *, subscription, hand_lines=True, version=4):
    """Two gloves, four samples; the runner answers each with its hand events and compat results.

    LEFT starts CHOP, RIGHT pulses PICKUP; then on one LEFT sample CHOP ends and POURING
    starts (and the runner resends that start: a duplicate); then RIGHT is IDLE.
    """
    events = []
    model = BridgeModel()
    server = await start_server(pki, model=model, sensor_version=version,
                                observer=lambda event, **fields: events.append((event, fields)))
    writers, runner = [], None
    try:
        runner = await blocking(Runner, model.bridge)
        phone, phone_writer = await subscribe(pki, server, subscription)
        writers.append(phone_writer)
        laptop, laptop_writer = await connect(pki, server.ingest_port)
        writers.append(laptop_writer)
        glove = Glove(laptop, laptop_writer, version)
        script = Script(EPOCHS[0])
        sent, results, lines = [], [], []

        def hand(event):
            sent.append(event)
            lines.append(dict(type="hand_event", event=event))

        def result(gesture, device, boot, seq):
            results.append((gesture, device, boot, seq))
            lines.append(result_line(gesture, device, boot, seq))

        async def answer():
            chosen = [message for message in lines if hand_lines or message["type"] == "result"]
            lines.clear()
            await blocking(runner.send, *chosen)

        if hand_lines:
            await blocking(runner.send, dict(type="hand_hello", hello=script.hello))
        left = await glove.send(runner, LEFT, 11, 0)
        right = await glove.send(runner, RIGHT, 22, 0)
        hand(script.event("state_start", LEFT, left, "CHOP"))
        result("CHOP", LEFT, 11, 0)
        hand(script.event("action", RIGHT, right, "PICKUP"))
        result("PICKUP", RIGHT, 22, 0)
        await answer()
        left = await glove.send(runner, LEFT, 11, 1)
        hand(script.event("state_end", LEFT, left, "CHOP", confidence=None, reason="confirmed_transition"))
        hand(script.event("state_start", LEFT, left, "POURING"))
        result("POURING", LEFT, 11, 1)
        lines.append(dict(type="hand_event", event=sent[-1]))  # resent: the same event_id
        await answer()
        right = await glove.send(runner, RIGHT, 22, 1)
        hand(script.event("idle", RIGHT, right, "IDLE"))
        result("IDLE", RIGHT, 22, 1)
        await answer()
        expected = len(results) + (len(sent) if hand_lines and server._hand_gate is not None
                                   and subscription == 2 else 0)
        frames = [await frame_body(phone) for _ in range(expected)]
        with pytest.raises(asyncio.TimeoutError):
            await frame_body(phone, timeout=0.3)
        await disconnect(phone_writer)
        await until(lambda: server._subscriber is None)
        return dict(server=server, bridge=model.bridge, frames=frames, sent=sent, results=results,
                    events=events, devices={device: dict(counts) for device, counts in server.monitor.devices.items()})
    finally:
        for writer in writers:
            await disconnect(writer)
        if runner is not None:
            runner.close()
        await server.close()


def gesture_results(run):
    frames = [json.loads(body) for body in run["frames"]]
    return [(frame["gesture"], frame["device_id"], frame["boot_id"], frame["seq"])
            for frame in frames if frame["type"] == "GESTURE_RESULT"]


def test_a_v2_subscriber_gets_results_and_hand_events_in_order_exactly_as_the_runner_sent_them(pki):
    run = asyncio.run(scenario(pki, subscription=2))
    sent = [(json.dumps(event, separators=(",", ":"))).encode("utf-8") for event in run["sent"]]
    results = iter(run["results"])
    events = iter(sent)
    # The runner's line order: hand event, then its compat result; both transition events first.
    expected_order = ["H", "R", "H", "R", "H", "H", "R", "H", "R"]
    kinds = ["H" if json.loads(body)["type"] == "HAND_EVENT" else "R" for body in run["frames"]]
    assert kinds == expected_order
    for body, kind in zip(run["frames"], kinds):
        if kind == "H":
            assert body == next(events)  # byte for byte what the runner put in its hand_event line
        else:
            frame = json.loads(body)
            assert (frame["v"], frame["type"], frame["result_id"]) == (
                3, "GESTURE_RESULT", "{}:{}:{}".format(frame["device_id"], frame["boot_id"], frame["seq"]))
            assert (frame["gesture"], frame["device_id"], frame["boot_id"], frame["seq"]) == next(results)
    transition = [json.loads(body) for body in sent[2:4]]
    assert [event["kind"] for event in transition] == ["state_end", "state_start"]
    assert transition[0]["source"] == transition[1]["source"]
    metrics = run["server"].metrics
    assert (metrics["hand_hellos"], metrics["hand_events_accepted"], metrics["hand_events_duplicate"],
            metrics["hand_events_published"], metrics["hand_events_refused"],
            metrics["hand_events_unrequested"], metrics["hand_events_no_subscriber"]) == (1, 5, 1, 5, 0, 0, 0)
    assert metrics["model_results"] == 4 and metrics["model_results_duplicate"] == 0
    assert run["bridge"].counts["hand_events"] == 6 and run["bridge"].counts["bad_lines"] == 0
    names = [event for event, _ in run["events"]]
    assert names.count("hand_event_published") == 5 and names.count("hand_event_write_complete") == 5
    assert [fields["reason"] for event, fields in run["events"] if event == "hand_event_refused"] == ["duplicate"]
    claimed = [fields for event, fields in run["events"] if event == "subscriber_claimed"]
    assert claimed[0]["hand_events"] is True


def test_a_v1_subscriber_gets_exactly_the_gesture_results_and_never_a_hand_event(pki):
    run = asyncio.run(scenario(pki, subscription=1))
    assert all(json.loads(body)["type"] == "GESTURE_RESULT" for body in run["frames"])
    assert gesture_results(run) == run["results"]
    metrics = run["server"].metrics
    assert (metrics["hand_events_accepted"], metrics["hand_events_unrequested"],
            metrics["hand_events_published"]) == (5, 5, 0)
    claimed = [fields for event, fields in run["events"] if event == "subscriber_claimed"]
    assert "hand_events" not in claimed[0]


def result_path(run):
    """The result observer events, without the timing fields (queue size and age)."""
    return sorted((event, tuple(sorted((key, value) for key, value in fields.items()
                                       if key not in ("queue_size", "age_seconds"))))
                  for event, fields in run["events"] if not event.startswith("hand_"))


def test_hand_events_change_neither_the_monitor_counts_nor_the_result_evidence_or_metrics(pki):
    with_hands = asyncio.run(scenario(pki, subscription=2))
    without = asyncio.run(scenario(pki, subscription=2, hand_lines=False))
    assert gesture_results(with_hands) == gesture_results(without) == without["results"]
    assert with_hands["devices"] == without["devices"]
    assert with_hands["devices"][LEFT]["sent"] == 2 and with_hands["devices"][RIGHT]["results"] == 2
    assert result_path(with_hands) == result_path(without)
    timing = ("model_max_call_ms", "model_slow_calls")
    result_metrics = [{key: value for key, value in run["server"].metrics.items()
                       if not key.startswith(("hand_", "model_hand")) and key not in timing}
                      for run in (with_hands, without)]
    assert result_metrics[0] == result_metrics[1]
    assert without["server"].metrics["hand_events_accepted"] == 0


def test_a_v1_subscriber_of_a_v4_service_sees_the_same_bytes_and_evidence_as_without_hand_events(pki):
    # The installed phone app subscribes at version 1: the runner's hand lines must change
    # nothing it receives, nothing the monitor counts and no result evidence, in v4 or v3 mode.
    with_hands = asyncio.run(scenario(pki, subscription=1))
    without = asyncio.run(scenario(pki, subscription=1, hand_lines=False))
    version_3 = asyncio.run(scenario(pki, subscription=1, version=3))
    assert with_hands["frames"] == without["frames"] == version_3["frames"]
    assert len(with_hands["frames"]) == len(with_hands["results"]) == 4
    assert with_hands["devices"] == without["devices"] == version_3["devices"]
    assert result_path(with_hands) == result_path(without) == result_path(version_3)
    timing = ("model_max_call_ms", "model_slow_calls")
    result_metrics = [{key: value for key, value in run["server"].metrics.items()
                       if not key.startswith(("hand_", "model_hand")) and key not in timing}
                      for run in (with_hands, without, version_3)]
    assert result_metrics[0] == result_metrics[1] == result_metrics[2]
    assert with_hands["server"].metrics["hand_events_unrequested"] == 5


def test_only_new_stream_samples_reach_the_gate_and_a_new_boot_resets_only_that_hand(pki):
    async def check():
        events = []
        model = BridgeModel()
        server = await start_server(pki, model=model, sensor_version=4,
                                    observer=lambda event, **fields: events.append((event, fields)))
        writers, runner = [], None
        try:
            phone, phone_writer = await subscribe(pki, server, 2)
            writers.append(phone_writer)
            laptop, laptop_writer = await connect(pki, server.ingest_port)
            writers.append(laptop_writer)
            runner = await blocking(Runner, model.bridge)
            glove = Glove(laptop, laptop_writer, 4)
            script = Script(EPOCHS[0])
            await blocking(runner.send, dict(type="hand_hello", hello=script.hello))
            left = await glove.send(runner, LEFT, 11, 0)
            right = await glove.send(runner, RIGHT, 22, 0)
            # A retransmission and a command are accepted by the service but never reach the gate.
            await write_frame(laptop_writer, left)
            assert (await read_frame(laptop))["status"] == "duplicate"
            await write_frame(laptop_writer, dict(sample(4, LEFT, 11, 1), request_id=1))
            assert (await read_frame(laptop))["status"] == "accepted"
            assert json.loads(await frame_body(phone))["request_id"] == 1  # the command's echo
            gate = server._hand_gate
            assert [[message["seq"] for message, _ in gate._replay[device]] for device in (LEFT, RIGHT)] == [[0], [0]]
            chop, wash = script.event("state_start", LEFT, left, "CHOP"), script.event("state_start", RIGHT, right, "WASH")
            await blocking(runner.send, dict(type="hand_event", event=chop), dict(type="hand_event", event=wash))
            assert [json.loads(await frame_body(phone)) for _ in range(2)] == [chop, wash]
            # The left glove reboots: its first sample of the new boot ends only the left state.
            rebooted = await glove.send(runner, LEFT, 12, 0)
            assert gate.active_state(LEFT) is None and gate.active_state(RIGHT)["state_id"] == wash["state_id"]
            assert [(fields["device_id"], fields["reason"]) for event, fields in events
                    if event == "hand_state_ended"] == [(LEFT, "source_reset")]
            # The worker's own end of the reset state still reaches the phone.
            end = script.event("state_end", LEFT, rebooted, "CHOP", confidence=None, reason="source_reset")
            await blocking(runner.send, dict(type="hand_event", event=end))
            assert json.loads(await frame_body(phone)) == end
            metrics = server.metrics
            assert (metrics["hand_resets"], metrics["hand_gate_errors"], metrics["hand_events_published"],
                    metrics["hand_events_refused"]) == (1, 0, 3, 0)
        finally:
            for writer in writers:
                await disconnect(writer)
            if runner is not None:
                runner.close()
            await server.close()
    asyncio.run(check())


def test_the_phone_simulator_refuses_a_version_2_subscribed_to_its_version_1_subscribe(pki):
    from common.tls import server_context
    from common.wire import ProtocolError
    from laptop.phone_simulator import receive_results

    async def check():
        async def peer(reader, writer):
            await read_frame(reader)
            await write_frame(writer, dict(v=2, type="SUBSCRIBED", session_id=SESSION, event_format=1))
            await disconnect(writer)
        server = await asyncio.start_server(peer, "127.0.0.1", 0,
                                            ssl=server_context(pki / "server-cert.pem", pki / "server-key.pem"))
        try:
            with pytest.raises(ProtocolError, match="SUBSCRIBE that was sent"):
                await receive_results("127.0.0.1", server.sockets[0].getsockname()[1], pki / "ca-cert.pem", count=1)
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(check())


def test_a_subscribe_with_an_unknown_event_format_gets_no_subscribed(pki):
    async def check():
        server = await start_server(pki, sensor_version=4)
        try:
            reader, writer = await connect(pki, server.gateway_port)
            await write_frame(writer, dict(v=2, type="SUBSCRIBE", session_id=SESSION, event_format=2))
            assert await asyncio.wait_for(reader.read(), 2) == b""
            await disconnect(writer)
            assert server.metrics["subscribers"] == 0 and server.metrics["rejected"] == 1
        finally:
            await server.close()
    asyncio.run(check())


def test_a_version_3_service_has_no_gate_and_answers_a_v2_subscribe_with_results_only(pki):
    run = asyncio.run(scenario(pki, subscription=2, version=3))
    assert run["server"]._hand_gate is None
    assert gesture_results(run) == run["results"]
    assert all(json.loads(body)["type"] == "GESTURE_RESULT" for body in run["frames"])
    # The runner's hand lines (one hello, five events, one resend) are bad lines there.
    assert run["bridge"].counts["bad_lines"] == 7 and run["bridge"].counts["hand_events"] == 0
    assert not any(key.startswith(("hand_", "model_hand")) for key in run["server"].metrics)
    claimed = [fields for event, fields in run["events"] if event == "subscriber_claimed"]
    assert claimed[0]["hand_events"] is False


def test_the_runner_closing_ends_its_states_and_a_new_hello_starts_fresh(pki):
    async def check():
        events = []
        model = BridgeModel()
        server = await start_server(pki, model=model, sensor_version=4,
                                    observer=lambda event, **fields: events.append((event, fields)))
        writers, runners = [], []
        try:
            phone, phone_writer = await subscribe(pki, server, 2)
            writers.append(phone_writer)
            laptop, laptop_writer = await connect(pki, server.ingest_port)
            writers.append(laptop_writer)
            glove = Glove(laptop, laptop_writer, 4)
            first = await blocking(Runner, model.bridge)
            runners.append(first)
            script = Script(EPOCHS[0])
            await blocking(first.send, dict(type="hand_hello", hello=script.hello))
            left = await glove.send(first, LEFT, 11, 0)
            chop = script.event("state_start", LEFT, left, "CHOP")
            await blocking(first.send, dict(type="hand_event", event=chop))
            assert json.loads(await frame_body(phone)) == chop
            assert server._hand_gate.active_state(LEFT)["state_id"] == chop["state_id"]
            # The worker's connection ends: the service ends its states (worker_disconnect).
            await blocking(first.close)
            await until(lambda: server._hand_gate.active_state(LEFT) is None)
            assert [(fields["device_id"], fields["reason"]) for event, fields in events
                    if event == "hand_state_ended"] == [(LEFT, "worker_disconnect")]
            # A new connection and epoch: its event_seq starts at 1 again and is accepted.
            second = await blocking(Runner, model.bridge)
            runners.append(second)
            script = Script(EPOCHS[1])
            await blocking(second.send, dict(type="hand_hello", hello=script.hello))
            left = await glove.send(second, LEFT, 11, 1)
            pulse = script.event("action", LEFT, left, "PICKUP")
            assert pulse["event_seq"] == 1
            # The old epoch's event, resent on the new connection, is refused.
            await blocking(second.send, dict(type="hand_event", event=chop), dict(type="hand_event", event=pulse))
            assert json.loads(await frame_body(phone)) == pulse
            with pytest.raises(asyncio.TimeoutError):
                await frame_body(phone, timeout=0.3)
            metrics = server.metrics
            assert (metrics["hand_hellos"], metrics["hand_events_accepted"], metrics["hand_events_refused"],
                    metrics["hand_events_published"]) == (2, 2, 1, 2)
            assert model.bridge.counts["connections"] == 2
            assert [fields["connection"] for event, fields in events if event == "hand_hello_accepted"] == [1, 2]
            assert [fields["reason"] for event, fields in events if event == "hand_event_refused"] == ["invalid"]
        finally:
            for writer in writers:
                await disconnect(writer)
            for runner in runners:
                runner.close()
            await server.close()
    asyncio.run(check())


def test_hand_events_without_a_subscriber_are_counted_and_the_event_log_takes_every_hand_event(pki, tmp_path):
    from ultra96.diagnostics import BoundedEventLog

    async def check(log):
        model = BridgeModel()
        server = await start_server(pki, model=model, sensor_version=4, observer=log)
        runner = None
        try:
            laptop, laptop_writer = await connect(pki, server.ingest_port)
            runner = await blocking(Runner, model.bridge)
            script = Script(EPOCHS[0])
            await blocking(runner.send, dict(type="hand_hello", hello=script.hello),
                           dict(type="hand_hello", hello={"type": "hello"}))
            left = await Glove(laptop, laptop_writer, 4).send(runner, LEFT, 11, 0)
            event = script.event("action", LEFT, left, "THROW")
            await blocking(runner.send, dict(type="hand_event", event=event), dict(type="hand_event", event={"bad": 1}))
            await until(lambda: server.metrics["hand_events_no_subscriber"] == 1
                        and server.metrics["hand_events_refused"] == 1)
            await disconnect(laptop_writer)
            return dict(server.metrics)
        finally:
            if runner is not None:
                runner.close()
            await server.close()

    log = BoundedEventLog(tmp_path / "events")
    metrics = asyncio.run(check(log))
    state = log.close()
    # Every hand observer event passed the log's field rules: nothing dropped, nothing rejected.
    assert state["complete"] is True and state["callback_error_count"] == 0
    records = [json.loads(text) for text in (tmp_path / "events" / "events.jsonl").read_text().splitlines()]
    names = [record["event"] for record in records]
    assert {"hand_hello_accepted", "hand_hello_refused", "hand_event_published", "hand_event_refused",
            "hand_event_dropped"} <= set(names)
    dropped = [record for record in records if record["event"] == "hand_event_dropped"]
    assert [(record["reason"], record["kind"], record["hand_seq"], record["boot_id"], record["seq"])
            for record in dropped] == [("no_subscriber", "pulse", 1, 11, 0)]
    assert metrics["hand_hellos_refused"] == 1 and metrics["hand_hellos"] == 1


def test_hand_event_frames_in_the_queue_never_count_as_results():
    from ultra96.server import ResultQueue

    class Counter:
        def __init__(self):
            self.counts = []

        def count(self, device, fate):
            self.counts.append((device, fate))

    async def check():
        observed, monitor = [], Counter()
        results = ResultQueue(observer=lambda event, **fields: observed.append((event, fields)),
                              subscriber_id=1, monitor=monitor, hand_events=True)
        hand = dict(v=4, type="HAND_EVENT", session_id=SESSION, device_id=LEFT, event_seq=3, kind="pulse",
                    source=dict(boot_id=11, seq=4, uptime_ms=1000))
        for _ in range(33):  # the queue holds 32: the oldest is pushed out
            results.put(hand)
        assert results.hand_dropped == 1 and results.dropped == 0
        results.put(hand, received_at=time.monotonic() - 3)  # pushes out one more; itself too old to send
        for _ in range(31):
            await results.get_timed()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(results.get_timed(), 0.1)  # the old one is dropped as stale
        assert (results.hand_dropped, results.dropped, results.stale) == (3, 0, 0)
        results.put(hand)
        results.retire("peer_eof")
        assert monitor.counts == []  # the board monitor's per-glove counters never see them
        reasons = [fields.get("reason") for event, fields in observed if event == "hand_event_dropped"]
        assert reasons == ["drop_oldest", "drop_oldest", "stale_dequeue", "abandoned_peer_eof"]
        assert {event for event, _ in observed} == {"hand_event_enqueued", "hand_event_dropped"}
        enqueued = [fields for event, fields in observed if event == "hand_event_enqueued"][0]
        assert {key: enqueued[key] for key in ("device_id", "hand_seq", "kind", "boot_id", "seq")} == dict(
            device_id=LEFT, hand_seq=3, kind="pulse", boot_id=11, seq=4)
    asyncio.run(check())


# --- what a v3 board service loads, and what the deploy copies -----------------------------------

def test_a_version_3_service_imports_nothing_of_the_hand_path():
    code = ("import ssl, sys\n"
            "from ultra96.server import CommsServer\n"
            "for version in (3, 4):\n"
            "    server = CommsServer(ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER), sensor_version=version)\n"
            "    print(version, server._hand_gate is not None, sorted(name for name in ("
            "'ultra96.hand_gate', 'ultra96.hand_admission', 'common.hand_contract') if name in sys.modules))\n")
    output = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True,
                            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), timeout=60)
    assert output.returncode == 0, output.stderr
    assert output.stdout.splitlines() == [
        "3 False []",
        "4 True ['common.hand_contract', 'ultra96.hand_admission', 'ultra96.hand_gate']"]


def test_the_deploy_copies_every_module_the_v4_gate_imports():
    from tools import deploy_service
    names = {name for name, _ in deploy_service.source_files()}
    pending, seen = ["ultra96/hand_gate.py"], set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        assert path in names
        for node in ast.walk(ast.parse((ROOT / path).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                top = module.split(".")[0]
                assert top not in ("src", "tools", "laptop", "phone", "hardware"), (path, module)
                if top in ("common", "ultra96"):
                    pending.append(module.replace(".", "/") + ".py")
    assert {"ultra96/hand_gate.py", "ultra96/hand_admission.py", "common/hand_contract.py",
            "common/wire.py"} <= seen
