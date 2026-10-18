"""Read-only board monitor on the gateway port: per-glove counters and recent events."""
import asyncio
import contextlib
import io
import json
import logging
import math
import ssl
import struct
from types import SimpleNamespace

import pytest

from common.wire import ProtocolError, encode_frame, read_frame, write_frame
from ultra96.protocol import DUMMY_GESTURES, MAX_MONITOR_EVENTS, MONITOR_INTERVAL, validate_message

SESSION = "ltc-comms"
MONITOR = {"v": 1, "type": "MONITOR", "session_id": SESSION}
MONITORING = {"v": 1, "type": "MONITORING", "session_id": SESSION}
ZERO = dict(accepted=0, duplicates=0, results=0, commands=0, refused=0, sent=0, unsent=0)


def sample(device, seq, request_id=None, boot=9):
    return {"v": 3, "type": "SENSOR_BATCH", "session_id": SESSION, "device_id": device,
            "boot_id": boot, "seq": seq, "uptime_ms": seq * 100,
            "values": [(seq + 10 * i) % 32768 for i in range(11)], "request_id": request_id}


def result(seq, device=1):
    return dict(v=3, type="GESTURE_RESULT", session_id=SESSION, device_id=device, boot_id=7,
                seq=seq, request_id=None, result_id="{}:7:{}".format(device, seq),
                gesture=DUMMY_GESTURES[seq % 4], confidence=1.0)


def started(server):
    """_ingest is driven directly here, so start the model as CommsServer.start() would."""
    server.model_start_seconds = server.model.start(asyncio.get_running_loop())
    return server


def reader_for(*messages, eof=False):
    reader = asyncio.StreamReader()
    for message in messages:
        reader.feed_data(encode_frame(message))
    if eof:
        reader.feed_eof()
    return reader


class Writer:
    """A connection stand-in that records frames; results may block or fail in drain."""
    def __init__(self, reader=None, block_results=False, fail_type=None):
        self.reader = reader
        self.block_results = block_results
        self.fail_type = fail_type
        self.messages = []
        self.result_started = asyncio.Event()
        self.subscribed = asyncio.Event()

    def write(self, data):
        length = struct.unpack("!I", data[:4])[0]
        self.messages.append(json.loads(data[4:4 + length]))

    async def drain(self):
        kind = self.messages[-1]["type"]
        if kind == "SUBSCRIBED":
            self.subscribed.set()
        if kind == self.fail_type:
            raise ConnectionResetError("write failed")
        if kind == "GESTURE_RESULT":
            self.result_started.set()
            if self.block_results:
                await asyncio.Event().wait()

    def close(self):
        if self.reader is not None:
            self.reader.feed_eof()


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    from tools.generate_comms_pki import generate_pki
    folder = tmp_path_factory.mktemp("comms-monitor-pki")
    generate_pki(folder)
    return folder


async def start_server(pki, **kwargs):
    from common.tls import server_context
    from ultra96.server import CommsServer
    server = CommsServer(server_context(pki / "server-cert.pem", pki / "server-key.pem"),
                         ingest_port=0, gateway_port=0, **kwargs)
    await server.start()
    return server


async def connect(pki, port):
    from common.tls import client_context
    return await asyncio.open_connection("127.0.0.1", port, ssl=client_context(pki / "ca-cert.pem"),
                                         server_hostname="ultra96.week7.internal")


async def disconnect(writer):
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, ssl.SSLError):
        pass


async def eof(reader):
    """Read until the server closes the connection; True when it did within a few seconds."""
    try:
        while await asyncio.wait_for(reader.read(65536), 3):
            pass
    except (ConnectionError, ssl.SSLError):
        pass
    return True


async def attach(pki, port):
    reader, writer = await connect(pki, port)
    await write_frame(writer, MONITOR)
    assert await read_frame(reader) == MONITORING
    return reader, writer


async def subscribe(pki, server):
    reader, writer = await connect(pki, server.gateway_port)
    await write_frame(writer, {"v": 1, "type": "SUBSCRIBE", "session_id": SESSION})
    assert (await read_frame(reader))["type"] == "SUBSCRIBED"
    return reader, writer


async def next_update(reader):
    return validate_message(await read_frame(reader, timeout=2.0), "MONITOR_UPDATE", SESSION)


async def updates_until(reader, condition, events, timeout=5.0):
    """Read updates, collecting their events, until condition(update) holds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        update = await next_update(reader)
        events.extend(update["events"])
        if condition(update):
            return update
        assert loop.time() < deadline, update


async def until(condition, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline
        await asyncio.sleep(0.01)


def default_metrics(pki):
    from common.tls import server_context
    from ultra96.server import CommsServer
    return CommsServer(server_context(pki / "server-cert.pem", pki / "server-key.pem")).metrics


def test_monitor_receives_monitoring_then_periodic_updates_that_validate(pki):
    async def check():
        server = await start_server(pki)
        try:
            reader, writer = await attach(pki, server.gateway_port)
            loop = asyncio.get_running_loop()
            updates, times = [], []
            for _ in range(4):
                updates.append(await next_update(reader))
                times.append(loop.time())
            assert [update["update"] for update in updates] == [0, 1, 2, 3]
            first = updates[0]
            assert (first["model"], first["subscriber"], first["identities"],
                    first["identity_limit"]) == ("dummy", None, 0, 128)
            assert first["devices"] == {"1": ZERO, "2": ZERO}
            assert first["events"] == [] and first["events_dropped"] == 0
            assert first["service"]["subscribers"] == 0
            assert first["service"]["model_max_call_ms"] == 0.0
            uptimes = [update["uptime_seconds"] for update in updates]
            assert uptimes == sorted(uptimes) and uptimes[0] >= 0
            # One update per interval, not a burst.
            assert times[-1] - times[0] >= 2 * MONITOR_INTERVAL
            assert server.metrics["subscribers"] == 0
            await disconnect(writer)
            await until(lambda: not server._tasks)
            assert server.metrics["rejected"] == 0
        finally:
            await server.close()
    asyncio.run(check())


@pytest.mark.parametrize("observed", [False, True])
def test_per_glove_counters_and_events_follow_the_result_path(pki, observed, caplog):
    from ultra96.diagnostics import _EVENTS
    caplog.set_level(logging.INFO, logger="ultra96.server")
    seen = []
    observer = (lambda event, **fields: seen.append((event, fields))) if observed else None

    async def check():
        server = await start_server(pki, observer=observer)
        try:
            monitor, monitor_writer = await attach(pki, server.gateway_port)
            phone, phone_writer = await subscribe(pki, server)
            left, left_writer = await connect(pki, server.ingest_port)
            right, right_writer = await connect(pki, server.ingest_port)
            for seq in range(5):
                await write_frame(left_writer, sample(1, seq))
                assert (await read_frame(left))["status"] == "accepted"
            for seq in range(3):
                await write_frame(right_writer, sample(2, seq))
                assert (await read_frame(right))["status"] == "accepted"
            await write_frame(left_writer, sample(1, 4))
            assert (await read_frame(left))["status"] == "duplicate"
            await write_frame(right_writer, sample(2, 7, request_id=7))
            assert (await read_frame(right))["status"] == "accepted"
            received = [await read_frame(phone) for _ in range(9)]
            # The phone's results are exactly what it gets without a monitor.
            assert [result["result_id"] for result in received] == [
                "1:9:0", "1:9:1", "1:9:2", "1:9:3", "1:9:4", "2:9:0", "2:9:1", "2:9:2", "cmd:2:9:7"]
            assert all(validate_message(result, "GESTURE_RESULT", SESSION) for result in received)
            labels = {(result["device_id"], result["seq"]): result["gesture"] for result in received}

            events = []
            update = await updates_until(monitor, lambda update: update["devices"]["2"]["sent"] == 4,
                                         events)
            assert update["devices"] == {
                "1": dict(ZERO, accepted=5, duplicates=1, results=5, sent=5),
                "2": dict(ZERO, accepted=4, results=3, commands=1, sent=4)}
            assert (update["subscriber"], update["identities"]) == (1, 2)
            assert update["service"]["subscribers"] == 1 and update["service"]["replaced"] == 0
            assert update["service"]["model_samples"] == 8  # commands never reach the model
            published = [event for event in events if event[0] in ("result", "command")]
            assert [tuple(event[:4]) for event in published] == (
                [("result", 1, 9, seq) for seq in range(5)]
                + [("result", 2, 9, seq) for seq in range(3)] + [("command", 2, 9, 7)])
            for event in published:
                assert event[4] == labels[(event[1], event[3])] and event[4] in DUMMY_GESTURES
                assert event[5] == 1.0
            assert ["subscriber", "claimed", 1, None] in [event[:4] for event in events]
            times = [event[-1] for event in events]
            assert times == sorted(times) and times[-1] <= update["uptime_seconds"]

            # The phone leaves; a later result has no subscriber and is unsent.
            await disconnect(phone_writer)
            await until(lambda: server._subscriber is None)
            await write_frame(left_writer, sample(1, 5))
            assert (await read_frame(left))["status"] == "accepted"
            update = await updates_until(
                monitor, lambda update: update["devices"]["1"]["unsent"] == 1, events)
            assert update["subscriber"] is None
            assert ["subscriber", "ended", 1, "peer_eof"] in [event[:4] for event in events]
            for counters in update["devices"].values():
                assert counters["results"] + counters["commands"] == counters["sent"] + counters["unsent"]
            assert server.monitor.devices[1] == dict(update["devices"]["1"])
            for writer in (left_writer, right_writer, monitor_writer):
                await disconnect(writer)
        finally:
            await server.close()
        assert server.metrics["rejected"] == 0

    asyncio.run(check())
    # The per-sample INFO line is gone; nothing else in the hot path logs.
    assert not [record for record in caplog.records if "accepted session" in record.getMessage()]
    if observed:
        # The monitor adds no observer event and no field that carries labels or values.
        assert {event for event, _ in seen} <= _EVENTS
        assert all("gesture" not in fields and "values" not in fields for _, fields in seen)


def test_monitor_does_not_touch_the_subscriber_and_a_second_monitor_replaces_the_first(pki):
    async def check():
        server = await start_server(pki)
        writers = []
        try:
            phone, phone_writer = await subscribe(pki, server)
            writers.append(phone_writer)
            owner = server._subscriber
            first, first_writer = await attach(pki, server.gateway_port)
            writers.append(first_writer)
            await next_update(first)
            second, second_writer = await attach(pki, server.gateway_port)
            writers.append(second_writer)
            assert await eof(first)
            updates = [await next_update(second) for _ in range(2)]
            assert [update["update"] for update in updates] == [0, 1]
            assert server._subscriber is owner
            assert (server.metrics["subscribers"], server.metrics["replaced"]) == (1, 0)
            # The phone still gets every result.
            laptop, laptop_writer = await connect(pki, server.ingest_port)
            writers.append(laptop_writer)
            await write_frame(laptop_writer, sample(1, 0))
            await read_frame(laptop)
            assert (await read_frame(phone))["result_id"] == "1:9:0"
            await until(lambda: len(server._sockets) == 3)
            assert server.metrics["rejected"] == 0
            assert set(server.metrics) == set(default_metrics(pki))
            # The service's close ends the monitor session.
            await asyncio.wait_for(server.close(), timeout=3)
            assert await eof(second)
            assert server.monitor._attached is None
        finally:
            await asyncio.gather(*(disconnect(writer) for writer in writers))
            await server.close()
    asyncio.run(check())


def test_bad_monitor_frames_close_the_connection_and_count_as_rejected(pki):
    bad = [dict(MONITOR, session_id="wrong"), dict(MONITOR, v=2), dict(MONITOR, v=True),
           dict(MONITOR, extra=1), {"v": 1, "type": "MONITOR"}, dict(MONITORING)]

    async def check():
        server = await start_server(pki)
        try:
            for count, frame in enumerate(bad, 1):
                reader, writer = await connect(pki, server.gateway_port)
                await write_frame(writer, frame)
                assert await eof(reader)
                await disconnect(writer)
                await until(lambda: server.metrics["rejected"] == count)
            # MONITOR is a gateway message only: the ingest port refuses it.
            reader, writer = await connect(pki, server.ingest_port)
            await write_frame(writer, MONITOR)
            assert await eof(reader)
            await disconnect(writer)
            await until(lambda: server.metrics["rejected"] == len(bad) + 1)
            assert server.monitor._attached is None
            assert server.metrics["subscribers"] == 0
        finally:
            await server.close()
    asyncio.run(check())


def test_any_inbound_byte_or_eof_ends_the_send_only_monitor(pki):
    async def check():
        server = await start_server(pki)
        try:
            reader, writer = await attach(pki, server.gateway_port)
            writer.write(b"x")
            await writer.drain()
            assert await eof(reader)
            await disconnect(writer)
            await until(lambda: not server._sockets and server.monitor._attached is None)
            reader, writer = await attach(pki, server.gateway_port)
            await disconnect(writer)
            await until(lambda: not server._sockets and server.monitor._attached is None)
            assert server.metrics["rejected"] == 0
        finally:
            await server.close()
    asyncio.run(check())


def test_service_stdout_stays_the_lifecycle_lines_with_a_monitor(pki):
    from ultra96.server import _run
    args = SimpleNamespace(cert=str(pki / "server-cert.pem"), key=str(pki / "server-key.pem"),
                           session_id=SESSION, ingest_port=0, gateway_port=0)
    output = io.StringIO()

    async def check():
        with contextlib.redirect_stdout(output):
            service = asyncio.create_task(_run(args))
            await until(lambda: '"listening"' in output.getvalue(), timeout=5)
            listening = json.loads(output.getvalue().splitlines()[1])
            monitor, monitor_writer = await attach(pki, listening["gateway_port"])
            laptop, laptop_writer = await connect(pki, listening["ingest_port"])
            await write_frame(laptop_writer, sample(1, 0))
            await read_frame(laptop)
            await updates_until(monitor, lambda update: update["devices"]["1"]["unsent"] == 1, [])
            await disconnect(laptop_writer)
            await disconnect(monitor_writer)
            service.cancel()
            with pytest.raises(asyncio.CancelledError):
                await service
    asyncio.run(check())
    lines = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [line["event"] for line in lines] == ["model_ready", "listening", "stopped"]
    assert set(lines[-1]["metrics"]) == set(default_metrics(pki))
    assert lines[-1]["metrics"]["accepted"] == 1


@pytest.mark.parametrize("observed", [False, True])
def test_every_published_result_is_sent_or_unsent_once_through_overflow_and_close(observed):
    from ultra96.server import CommsServer
    from ultra96.monitor import _Attachment

    async def check():
        events = []
        server = started(CommsServer(ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER), session_id=SESSION,
            observer=(lambda event, **fields: events.append(event)) if observed else None))
        async def ingest(*messages):
            with pytest.raises(asyncio.IncompleteReadError):
                await server._ingest(reader_for(*messages, eof=True), Writer())
        await ingest(sample(1, 0))
        reader = reader_for(dict(v=1, type="SUBSCRIBE", session_id=SESSION))
        writer = Writer(reader, block_results=True)
        gateway = asyncio.create_task(server._gateway(reader, writer))
        server._tasks.add(gateway)
        await asyncio.wait_for(writer.subscribed.wait(), 1)
        await ingest(sample(1, 1))
        await asyncio.wait_for(writer.result_started.wait(), 1)
        await ingest(sample(1, 1), *(sample(1, seq) for seq in range(2, 37)))
        await ingest(sample(2, 0), sample(2, 1, request_id=1))
        await asyncio.wait_for(server.close(), 1)
        # Glove 1: no subscriber 1, cancelled in its write 1, dropped oldest 5 (two of them
        # by glove 2's results), abandoned 30. Glove 2: abandoned 2. None was sent.
        assert server.monitor.devices == {
            1: dict(ZERO, accepted=37, duplicates=1, results=37, unsent=37),
            2: dict(ZERO, accepted=2, results=1, commands=1, unsent=2)}
        assert server.metrics["result_drops"] == 5
        # Refused model results count per glove whether or not a monitor is attached.
        server.monitor._attached = _Attachment(None)
        server._model_result("REST", 1.0, 1, 9, 99)
        server._model_result("REST", 1.0, 1, 9, 3)
        assert server.monitor.devices[1]["refused"] == 2
        assert [event[0] for event in server.monitor._attached.events] == ["refused", "refused"]
        assert [event[4] for event in server.monitor._attached.events] == [
            "unknown_identity", "duplicate"]
    asyncio.run(check())


def test_result_queue_counts_stale_failed_and_written_results_for_its_monitor():
    import time
    from ultra96.monitor import ServiceMonitor
    from ultra96.server import CommsServer, ResultQueue

    class OldQueue(ResultQueue):
        """Hands out one result that expires between the dequeue and the write."""
        served = False

        async def get_timed(self, now=None):
            if self.served:
                await asyncio.Event().wait()
            self.served = True
            return time.monotonic() - 3, result(4)

    async def check():
        monitor = ServiceMonitor()
        queue = ResultQueue(subscriber_id=1, monitor=monitor)
        queue.put(result(0, device=2), received_at=100.0)
        queue.put(result(1, device=2), received_at=103.0)
        assert (await queue.get_timed(now=103.0))[1]["seq"] == 1
        assert monitor.devices[2]["unsent"] == 1  # result 0 was stale at dequeue
        queue.put(result(2, device=1))
        queue.retire("disconnected")
        assert monitor.devices[1]["unsent"] == 1
        server = CommsServer(ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER), session_id=SESSION)
        for fail_type, key in ((None, "sent"), ("GESTURE_RESULT", "unsent")):
            queue = ResultQueue(subscriber_id=1, monitor=server.monitor)
            queue.put(result(3))
            sender = asyncio.create_task(server._send_results(Writer(fail_type=fail_type), queue))
            await until(lambda: server.monitor.devices[1][key] == 1)
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        writer = Writer()
        sender = asyncio.create_task(server._send_results(writer, OldQueue(monitor=server.monitor)))
        await until(lambda: server.monitor.devices[1]["unsent"] == 2)
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        assert writer.messages == []
        assert server.monitor.devices[1]["sent"] == 1
        # Without a monitor the queue behaves as before.
        plain = ResultQueue()
        for seq in range(40):
            plain.put({"seq": seq}, received_at=100.0)
        plain.retire("disconnected")
        assert plain.dropped == 8
    asyncio.run(check())


def state(**changes):
    return dict(dict(model="dummy", subscriber=None, identities=0, identity_limit=128,
                     metrics={}), **changes)


def decode(frame):
    assert struct.unpack("!I", frame[:4])[0] == len(frame) - 4 <= 16384
    return validate_message(json.loads(frame[4:].decode("utf-8")), "MONITOR_UPDATE", SESSION)


def test_events_are_kept_only_while_attached_and_overflow_counts_as_dropped():
    from ultra96.monitor import EVENT_CAPACITY, ServiceMonitor, _Attachment
    monitor = ServiceMonitor()
    monitor.refused(1, 9, 0, "unknown_identity")
    monitor.subscriber("claimed", 1)
    assert monitor.devices[1]["refused"] == 1
    attached = monitor._attached = _Attachment(None)
    for seq in range(EVENT_CAPACITY + 10):
        monitor.refused(2, 9, seq, "duplicate")
    assert len(attached.events) == EVENT_CAPACITY and attached.dropped == 10
    assert monitor.devices[2]["refused"] == EVENT_CAPACITY + 10
    update = decode(monitor._update(attached, 0, SESSION, state()))
    assert update["events_dropped"] == 10
    assert len(update["events"]) == MAX_MONITOR_EVENTS
    # Oldest first: the ten oldest were pushed out of the full buffer.
    assert [event[3] for event in update["events"]] == list(range(10, 10 + MAX_MONITOR_EVENTS))
    assert len(attached.events) == EVENT_CAPACITY - MAX_MONITOR_EVENTS
    # The update reads the service counters from the metrics and cleans an odd model name.
    update = decode(monitor._update(attached, 1, SESSION, state(
        model="bad\nname" + "x" * 200, subscriber=3, identities=2,
        metrics={"rejected": 4, "model_max_call_ms": 1.25, "accepted": 99})))
    assert update["model"].startswith("bad?name") and len(update["model"]) == 128
    assert update["service"]["rejected"] == 4 and update["service"]["model_max_call_ms"] == 1.25
    assert (update["subscriber"], update["identities"], update["update"]) == (3, 2, 1)


def test_frames_stay_within_the_frame_limit_with_100_long_gesture_events(monkeypatch):
    import ultra96.monitor as module
    monitor = module.ServiceMonitor()
    attached = monitor._attached = module._Attachment(None)
    session = "é" * 128
    for seq in range(150):
        monitor.published(dict(result(seq, device=2), boot_id=0xFFFFFFFF, seq=0xFFFFFFFF - seq,
                               gesture="G" * 32, confidence=0.1234567890123456789))
    monitor.started -= 10**6  # a service that has run for days
    state_ = state(model="é" * 128, subscriber=10**9, identities=128,
                   metrics={key: 10**12 for key in module.SERVICE_COUNTERS})
    frame = monitor._update(attached, 10**9, session, state_)
    assert len(frame) - 4 <= 16384
    update = validate_message(json.loads(frame[4:].decode("utf-8")), "MONITOR_UPDATE", session)
    assert len(update["events"]) == MAX_MONITOR_EVENTS
    assert update["events"][0][:6] == ["result", 2, 0xFFFFFFFF, 0xFFFFFFFF, "G" * 32,
                                       0.1234567890123456789]
    assert len(attached.events) == 50

    # An update that would not fit sends fewer events and keeps the rest, in order.
    real = module.encode_frame

    def limited(message):
        if len(message["events"]) > 30:
            raise ProtocolError("frame length outside 1..16384")
        return real(message)

    monkeypatch.setattr(module, "encode_frame", limited)
    remaining = [event[3] for event in attached.events]
    sent = []
    while attached.events:
        events = validate_message(json.loads(monitor._update(attached, 0, session, state_)[4:]),
                                  "MONITOR_UPDATE", session)["events"]
        assert 0 < len(events) <= 30
        sent.extend(event[3] for event in events)
    assert sent == remaining


def test_sender_skips_ticks_behind_a_slow_reader_and_ends_on_a_slow_write(monkeypatch):
    import ultra96.monitor as module

    class Transport:
        size = 20000

        def get_write_buffer_size(self):
            return self.size

    class Writer:
        def __init__(self):
            self.transport = Transport()
            self.frames = []
            self.block = False

        def write(self, data):
            self.frames.append(data)

        async def drain(self):
            if self.block:
                await asyncio.Event().wait()

    async def check():
        monitor = module.ServiceMonitor()
        writer = Writer()
        attached = monitor._attached = module._Attachment(writer)
        for seq in range(1000):
            monitor.refused(1, 9, seq, "duplicate")
        sender = asyncio.create_task(monitor._send_updates(attached, SESSION, state))
        await asyncio.sleep(3 * MONITOR_INTERVAL)
        assert writer.frames == []  # never queued behind a full buffer
        writer.transport.size = 0
        await until(lambda: writer.frames)
        await asyncio.sleep(0.05)
        # Back-to-back frames while events are pending, at most four per tick.
        assert len(writer.frames) == 4
        assert [decode(frame)["update"] for frame in writer.frames] == [0, 1, 2, 3]
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)

        monkeypatch.setattr(module, "WRITE_TIMEOUT", 0.05)
        writer = Writer()
        writer.transport.size = 0
        writer.block = True
        attached = monitor._attached = module._Attachment(writer)
        loop = asyncio.get_running_loop()
        began = loop.time()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(monitor._send_updates(attached, SESSION, state), 1)
        assert loop.time() - began < 0.5  # the write deadline, not the outer one
        assert len(writer.frames) == 1
    asyncio.run(check())


def test_a_peer_that_stops_reading_below_the_high_water_mark_ends_within_the_write_timeout(
        monkeypatch):
    import ultra96.monitor as module

    class Transport:
        def __init__(self):
            self.size = 0
            self.held = 0

        def get_write_buffer_size(self):
            # A stalled peer (held=None) keeps its bytes; a slow one drains after `held` checks.
            if self.size and self.held is not None:
                if self.held == 0:
                    self.size = 0
                else:
                    self.held -= 1
            return self.size

    class Writer:
        """Between the low and high marks of a real transport, so drain() never waits."""
        def __init__(self, held):
            self.transport = Transport()
            self.held = held
            self.frames = []

        def write(self, data):
            self.frames.append(data)
            self.transport.size, self.transport.held = 20000, self.held

        async def drain(self):
            pass

        def close(self):
            pass

    async def check():
        monkeypatch.setattr(module, "WRITE_TIMEOUT", 0.4)
        monitor = module.ServiceMonitor()
        loop = asyncio.get_running_loop()
        writer = Writer(held=None)
        began = loop.time()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(monitor.serve(asyncio.StreamReader(), writer, SESSION, state), 3)
        assert 0.4 < loop.time() - began < 0.4 + 3 * MONITOR_INTERVAL
        assert len(writer.frames) == 1  # MONITORING only: no update was queued behind it
        assert monitor._attached is None

        # A slow reader that still drains below the mark keeps its session.
        writer = Writer(held=2)
        attached = monitor._attached = module._Attachment(writer)
        sender = asyncio.create_task(monitor._send_updates(attached, SESSION, state))
        await asyncio.sleep(4 * 0.4)
        assert not sender.done()
        assert len(writer.frames) >= 2
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
    asyncio.run(check())


def valid_update(**changes):
    update = {"v": 1, "type": "MONITOR_UPDATE", "session_id": SESSION, "update": 0,
              "uptime_seconds": 1.5, "model": "dummy", "subscriber": 1, "identities": 2,
              "identity_limit": 128, "devices": {"1": dict(ZERO), "2": dict(ZERO, sent=3)},
              "service": {"rejected": 0, "client_limit": 0, "idle_evicted": 0, "subscribers": 1,
                          "replaced": 0, "model_samples": 3, "model_errors": 0,
                          "model_slow_calls": 0, "model_results_late": 0,
                          "model_max_call_ms": 0.25},
              "events": [["result", 1, 9, 0, "REST", 1.0, 0.5], ["command", 2, 9, 7, "FIST", 1, 1],
                         ["refused", 1, 9, 5, "unknown_identity", 0.75],
                         ["refused", 2, 9, 5, "duplicate", 0.75],
                         ["subscriber", "claimed", 1, None, 0.0],
                         ["subscriber", "ended", 1, "peer_eof", 1.25]],
              "events_dropped": 0}
    update.update(changes)
    return update


def nested(path, value):
    update = valid_update()
    target = update
    for key in path[:-1]:
        target = target[key]
    if value is KeyError:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return update


def test_monitor_messages_validate_and_keep_the_existing_rules():
    for message in (MONITOR, MONITORING, valid_update(), valid_update(subscriber=None, events=[])):
        assert validate_message(dict(message)) == message
    assert validate_message(valid_update(), "MONITOR_UPDATE", SESSION)
    with pytest.raises(ProtocolError, match="unexpected message type"):
        validate_message(MONITOR, "SUBSCRIBE", SESSION)
    for message in (MONITOR, MONITORING, valid_update()):
        with pytest.raises(ProtocolError):
            validate_message(message, "SENSOR_BATCH", SESSION)
    with pytest.raises(ProtocolError, match="wrong configured session"):
        validate_message(MONITOR, "MONITOR", "other")
    assert encode_frame(valid_update())


@pytest.mark.parametrize("update", [
    valid_update(update=True), valid_update(update=-1), valid_update(update=1.0),
    valid_update(identities=False), valid_update(identity_limit=0),
    valid_update(events_dropped=True), valid_update(subscriber=0), valid_update(subscriber=True),
    valid_update(uptime_seconds=math.nan), valid_update(uptime_seconds=math.inf),
    valid_update(uptime_seconds=-0.5), valid_update(uptime_seconds=True),
    valid_update(model=""), valid_update(model="x" * 129), valid_update(model="a\nb"),
    valid_update(model=7), valid_update(extra=1), valid_update(v=2), valid_update(v=True),
    {key: value for key, value in valid_update().items() if key != "service"},
    nested(["devices", "2"], KeyError), nested(["devices", "3"], dict(ZERO)),
    nested(["devices", "1", "sent"], True), nested(["devices", "1", "sent"], -1),
    nested(["devices", "1", "sent"], 1.0), nested(["devices", "1", "extra"], 0),
    nested(["devices", "1", "unsent"], KeyError), valid_update(devices=[]),
    nested(["service", "rejected"], True), nested(["service", "extra"], 0),
    nested(["service", "model_errors"], KeyError),
    nested(["service", "model_max_call_ms"], math.nan),
    nested(["service", "model_max_call_ms"], math.inf),
    nested(["service", "model_max_call_ms"], -1.0),
    valid_update(events=[["refused", 1, 9, 0, "duplicate", 0.0]] * (MAX_MONITOR_EVENTS + 1)),
    valid_update(events={}), valid_update(events=[[]]), valid_update(events=["result"]),
    valid_update(events=[["result", 1, 9, 0, "bad label!", 1.0, 0.5]]),
    valid_update(events=[["result", 1, 9, 0, "", 1.0, 0.5]]),
    valid_update(events=[["result", 1, 9, 0, "G" * 33, 1.0, 0.5]]),
    valid_update(events=[["result", 1, 9, 0, "REST", 1.5, 0.5]]),
    valid_update(events=[["result", 1, 9, 0, "REST", math.nan, 0.5]]),
    valid_update(events=[["result", 1, 9, 0, "REST", True, 0.5]]),
    valid_update(events=[["result", 3, 9, 0, "REST", 1.0, 0.5]]),
    valid_update(events=[["result", True, 9, 0, "REST", 1.0, 0.5]]),
    valid_update(events=[["result", 1, 2 ** 32, 0, "REST", 1.0, 0.5]]),
    valid_update(events=[["result", 1, 9, -1, "REST", 1.0, 0.5]]),
    valid_update(events=[["result", 1, 9, 0, "REST", 1.0]]),
    valid_update(events=[["result", 1, 9, 0, "REST", 1.0, math.nan]]),
    valid_update(events=[["result", 1, 9, 0, "REST", 1.0, -1]]),
    valid_update(events=[["result", 1, 9, 0, "REST", 1.0, 0.5, 0]]),
    valid_update(events=[["refused", 1, 9, 0, "other", 0.5]]),
    valid_update(events=[["refused", 1, 9, 0, "duplicate", "0.5"]]),
    valid_update(events=[["subscriber", "claimed", 1, "peer_eof", 0.5]]),
    valid_update(events=[["subscriber", "ended", 1, None, 0.5]]),
    valid_update(events=[["subscriber", "ended", 1, "bad reason", 0.5]]),
    valid_update(events=[["subscriber", "ended", 0, "peer_eof", 0.5]]),
    valid_update(events=[["subscriber", "left", 1, None, 0.5]]),
    valid_update(events=[["unknown", 0.5]]),
])
def test_validate_message_rejects_malformed_monitor_updates(update):
    with pytest.raises(ProtocolError):
        validate_message(update, "MONITOR_UPDATE", SESSION)


def test_a_sender_stops_once_its_connection_is_no_longer_attached_without_a_cancel():
    """Python 3.10's wait_for can swallow a cancellation that arrives just after the drain
    finished; the sender must still end once it is replaced or its session has ended."""
    import ultra96.monitor as module

    class Transport:
        def get_write_buffer_size(self):
            return 0

        def abort(self):
            pass

    class Writer:
        def __init__(self):
            self.transport = Transport()
            self.frames = []

        def write(self, data):
            self.frames.append(data)

        async def drain(self):
            pass

        def close(self):
            pass

    async def check():
        monitor = module.ServiceMonitor()
        attached = monitor._attached = module._Attachment(Writer())
        sender = asyncio.create_task(monitor._send_updates(attached, SESSION, state))
        await until(lambda: attached.writer.frames)
        monitor._attached = module._Attachment(Writer())  # replaced; no cancel is sent
        await asyncio.wait_for(sender, 2 * MONITOR_INTERVAL + 0.5)
        written = len(attached.writer.frames)
        await asyncio.sleep(2 * MONITOR_INTERVAL)
        assert len(attached.writer.frames) == written
    asyncio.run(check())
