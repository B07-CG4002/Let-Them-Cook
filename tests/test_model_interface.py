"""The board's model interface: start(emit), on_sample(...) and a thread-safe emit (no hardware)."""
import asyncio
import contextlib
from fractions import Fraction
import io
import json
import logging
import numbers
from pathlib import Path
import random
import ssl
import subprocess
import sys
import threading
import time
import types
from types import SimpleNamespace
import uuid

import pytest

from common.sensor import SCALES
from common.wire import encode_frame, read_frame, write_frame
from ultra96 import model as model_module
from ultra96.model import DummyModel, ModelStartError, load_model, normalize_result
from ultra96.protocol import DUMMY_GESTURES, validate_message
from ultra96.server import CommsServer, ResultQueue, _run

ROOT = Path(__file__).resolve().parents[1]


def sensor(seq=0, request_id=None, device=1, boot=7, values=None):
    return dict(v=3, type="SENSOR_BATCH", session_id="ltc-comms", device_id=device,
                boot_id=boot, seq=seq, uptime_ms=(1000 + seq) & 0xFFFFFFFF,
                values=list(values) if values is not None else [seq % 100 + i for i in range(11)],
                request_id=request_id)


class Writer:
    def __init__(self):
        self.messages = []

    def write(self, data):
        self.messages.append(json.loads(data[4:]))

    async def drain(self):
        pass


class Recorder:
    """A model that records its calls and only emits when a test tells it to."""
    name = "recorder"

    def __init__(self, react=None):
        self.emit = None
        self.starts = 0
        self.samples = []
        self.threads = []
        self.react = react

    def start(self, emit):
        self.starts += 1
        self.emit = emit

    def on_sample(self, device_id, boot_id, seq, uptime_ms, values):
        self.samples.append((device_id, boot_id, seq, uptime_ms, values))
        self.threads.append(threading.get_ident())
        if self.react is not None:
            self.react(self, device_id, boot_id, seq, uptime_ms, values)


def board(model=None, **kwargs):
    return CommsServer(ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER), rng=random.Random(5),
                       model=model, **kwargs)


def subscribe(server):
    queue = ResultQueue()
    server._subscriber = (None, queue)
    return queue


async def ingest(server, *messages, writer=None):
    # _ingest is driven directly, so start the model as CommsServer.start() would.
    if server.model_start_seconds is None:
        server.model_start_seconds = server.model.start(asyncio.get_running_loop())
    reader = asyncio.StreamReader()
    for message in messages:
        reader.feed_data(encode_frame(message))
    reader.feed_eof()
    writer = Writer() if writer is None else writer
    try:
        await server._ingest(reader, writer)
    except asyncio.IncompleteReadError:
        pass
    return writer.messages


async def until(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.002)


def drain(queue):
    results = []
    while not queue._queue.empty():
        results.append(queue._queue.get_nowait()[1])
    return results


def in_thread(function, *args):
    box = []
    thread = threading.Thread(target=lambda: box.append(function(*args)))
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    return box[0]


# --- The built-in dummy model and the command echo -------------------------------------------

def test_dummy_model_answers_each_new_stream_sample_exactly_once():
    async def check():
        server = board()
        queue = subscribe(server)
        acks = await ingest(server, *(sensor(seq) for seq in range(5)), sensor(4))
        assert [ack["status"] for ack in acks] == ["accepted"] * 5 + ["duplicate"]
        results = drain(queue)
        assert [result["result_id"] for result in results] == ["1:7:%d" % seq for seq in range(5)]
        for result in results:
            assert validate_message(result, "GESTURE_RESULT", "ltc-comms") == result
            assert result["request_id"] is None and result["v"] == 3
            assert result["gesture"] in DUMMY_GESTURES and result["confidence"] == 1.0
        assert server.model.name == "dummy"
        assert isinstance(server.model.model, DummyModel)
        metrics = server.metrics
        assert (metrics["model_samples"], metrics["model_results"]) == (5, 5)
        assert metrics["model_errors"] == metrics["model_results_rejected"] == 0
        assert metrics["model_results_duplicate"] == metrics["model_results_late"] == 0
    asyncio.run(check())


def test_without_a_subscriber_each_model_result_counts_as_disconnected():
    async def check():
        server = board()
        await ingest(server, sensor(0), sensor(1))
        assert server.metrics["model_results"] == 2
        assert server.metrics["disconnected_results"] == 2
    asyncio.run(check())


def test_commands_bypass_the_model_and_get_one_cmd_echo():
    async def check():
        recorder = Recorder()
        server = board(recorder)
        queue = subscribe(server)
        acks = await ingest(server, sensor(9, request_id=9), sensor(9, request_id=9))
        assert [(ack["status"], ack["request_id"]) for ack in acks] == [("accepted", 9),
                                                                        ("duplicate", 9)]
        assert recorder.samples == []
        results = drain(queue)
        assert len(results) == 1
        echo = results[0]
        assert echo["result_id"] == "cmd:1:7:9" and echo["request_id"] == 9 and echo["seq"] == 9
        assert echo["gesture"] in DUMMY_GESTURES and echo["confidence"] == 1.0
        assert validate_message(echo, "GESTURE_RESULT", "ltc-comms") == echo
        assert server.metrics["model_samples"] == server.metrics["model_results"] == 0
        assert server.metrics["accepted"] == 1
    asyncio.run(check())


# --- on_sample -------------------------------------------------------------------------------

def test_on_sample_gets_eleven_floats_scaled_per_channel_on_the_loop_before_the_ack():
    raw = [12345, -12345, 10000, 4500, -4500, 100, 18000, -9000, 1, 10000, 0]
    expected = (1.2345, -1.2345, 1.0, 45.0, -45.0, 1.0, 180.0, -90.0, 0.01, 1.0, 0.0)
    writer = Writer()
    acks_seen = []

    def react(model, *args):
        acks_seen.append(len(writer.messages))

    async def check():
        recorder = Recorder(react)
        server = board(recorder)
        await ingest(server, sensor(0, values=raw), sensor(0, values=raw),
                     sensor(1, values=[32767, -32768] + [0] * 9), writer=writer)
        assert len(writer.messages) == 3
        # Called once per new sample (never for the duplicate) and before that sample's ACK.
        assert acks_seen == [0, 2]
        assert recorder.starts == 1
        assert set(recorder.threads) == {threading.get_ident()}
        device_id, boot_id, seq, uptime_ms, values = recorder.samples[0]
        assert (device_id, boot_id, seq, uptime_ms) == (1, 7, 0, 1000)
        assert type(values) is tuple and len(values) == 11
        assert all(type(value) is float for value in values)
        assert values == expected
        assert values == tuple(value / scale for value, scale in zip(raw, SCALES))
        assert recorder.samples[1][4][:3] == (3.2767, -3.2768, 0.0)
    asyncio.run(check())


def test_on_sample_exception_is_counted_rate_limited_and_never_stops_ingestion(caplog):
    caplog.set_level(logging.WARNING, logger="ultra96.model")

    def react(model, *args):
        raise ValueError("private model detail")

    async def check():
        server = board(Recorder(react))
        acks = await ingest(server, sensor(0), sensor(1), sensor(2))
        assert [ack["status"] for ack in acks] == ["accepted"] * 3
        assert server.metrics["model_errors"] == 3
        assert server.metrics["model_samples"] == 3
    asyncio.run(check())
    warnings = [record for record in caplog.records if "on_sample raised" in record.getMessage()]
    assert len(warnings) == 1
    assert "ValueError" in warnings[0].getMessage()
    assert "private model detail" not in caplog.text


def test_on_sample_system_exit_is_contained_like_any_model_error():
    # Model scripts in this project report missing files with raise SystemExit(...).
    def react(model, *args):
        raise SystemExit("Missing models/gesture_cnn.pth")

    async def check():
        server = board(Recorder(react))
        acks = await ingest(server, sensor(0), sensor(1))
        assert [ack["status"] for ack in acks] == ["accepted"] * 2
        assert server.metrics["model_errors"] == 2
    asyncio.run(check())


def test_slow_on_sample_is_counted_and_its_duration_tracked(monkeypatch):
    monkeypatch.setattr(model_module, "SLOW_CALL_SECONDS", 0.001)

    def react(model, *args):
        time.sleep(0.004)

    async def check():
        slow = board(Recorder(react))
        await ingest(slow, sensor(0), sensor(1))
        assert slow.metrics["model_slow_calls"] == 2
        assert slow.metrics["model_max_call_ms"] >= 3.0
        monkeypatch.setattr(model_module, "SLOW_CALL_SECONDS", 1.0)
        fast = board(Recorder())
        await ingest(fast, sensor(0))
        assert fast.metrics["model_slow_calls"] == 0
        assert 0.0 <= fast.metrics["model_max_call_ms"] < 1000.0
    asyncio.run(check())


# --- emit ------------------------------------------------------------------------------------

def test_emit_from_a_worker_thread_reaches_the_subscriber():
    outcomes = []
    workers = []

    def react(model, device_id, boot_id, seq, uptime_ms, values):
        def work():
            outcomes.append((threading.get_ident(),
                             model.emit("PICKUPHOLD", 0.75, device_id, boot_id, seq)))
        workers.append(threading.Thread(target=work))
        workers[-1].start()

    async def check():
        server = board(Recorder(react))
        queue = subscribe(server)
        acks = await ingest(server, sensor(3))
        assert acks[0]["status"] == "accepted"
        result = await asyncio.wait_for(queue.get(), 2)
        assert result == dict(v=3, type="GESTURE_RESULT", session_id="ltc-comms", device_id=1,
                              boot_id=7, seq=3, request_id=None, result_id="1:7:3",
                              gesture="PICKUPHOLD", confidence=0.75)
        # The result can arrive before the worker has recorded what emit returned.
        workers[0].join(2)
        assert not workers[0].is_alive()
        assert outcomes[0][0] != threading.get_ident() and outcomes[0][1] is True
        assert server.metrics["model_results"] == 1
    asyncio.run(check())


VALID = ("CHOP", 0.5, 1, 7, 0)
INVALID = [
    ("gesture", ""), ("gesture", "A" * 33), ("gesture", "FIRE EXT"), ("gesture", "É"),
    ("gesture", "１"), ("gesture", None), ("gesture", 5), ("gesture", b"CHOP"),
    ("confidence", 1.0000001), ("confidence", -0.0001), ("confidence", True),
    ("confidence", "0.5"), ("confidence", None), ("confidence", float("nan")),
    ("confidence", float("inf")), ("confidence", 10 ** 400), ("confidence", Fraction(10 ** 400)),
    ("device_id", 0), ("device_id", 3), ("device_id", True), ("device_id", 1.0), ("device_id", "1"),
    ("boot_id", -1), ("boot_id", 2 ** 32), ("boot_id", 7.0), ("boot_id", True),
    ("seq", -1), ("seq", 2 ** 32), ("seq", 0.0), ("seq", None),
]
_POSITION = {"gesture": 0, "confidence": 1, "device_id": 2, "boot_id": 3, "seq": 4}


def _with(field, value):
    arguments = list(VALID)
    arguments[_POSITION[field]] = value
    return arguments


@pytest.mark.parametrize("field,value", INVALID)
def test_invalid_emit_returns_false_and_counts_rejected_on_any_thread(field, value):
    async def check():
        recorder = Recorder()
        server = board(recorder)
        queue = subscribe(server)
        await ingest(server, sensor(0))
        arguments = _with(field, value)
        assert recorder.emit(*arguments) is False
        assert server.metrics["model_results_rejected"] == 1
        assert in_thread(recorder.emit, *arguments) is False
        await until(lambda: server.metrics["model_results_rejected"] == 2)
        assert server.metrics["model_results"] == 0 and drain(queue) == []
        assert server.metrics["model_results_late"] == 0
    asyncio.run(check())


def test_invalid_emits_log_one_warning_per_second(caplog):
    caplog.set_level(logging.WARNING, logger="ultra96.model")

    async def check():
        recorder = Recorder()
        server = board(recorder)
        await ingest(server, sensor(0))
        for _ in range(5):
            assert recorder.emit("", 0.5, 1, 7, 0) is False
        assert server.metrics["model_results_rejected"] == 5
    asyncio.run(check())
    assert sum("invalid result" in record.getMessage() for record in caplog.records) == 1


class Float32:
    """Stands in for numpy.float32: a numbers.Real that is not a float."""
    def __init__(self, value):
        self.value = value

    def __float__(self):
        return float(self.value)


class Int64:
    """Stands in for numpy.int64: a numbers.Integral that is not an int."""
    def __init__(self, value):
        self.value = value

    def __int__(self):
        return int(self.value)

    def __index__(self):
        return int(self.value)


class Label(str):
    """Stands in for numpy.str_, a str subclass."""


numbers.Real.register(Float32)
numbers.Integral.register(Int64)


def test_numpy_like_scalars_and_fractions_are_normalized_to_plain_json_values():
    normalized = normalize_result(Label("WASH"), Float32(0.25), Int64(2), Int64(9), Int64(4))
    assert normalized == ("WASH", 0.25, 2, 9, 4)
    assert [type(item) for item in normalized] == [str, float, int, int, int]
    assert normalize_result("WASH", Fraction(3, 4), 1, 0, 0) == ("WASH", 0.75, 1, 0, 0)
    assert normalize_result("WASH", 1, 1, 0, 0)[1] == 1.0
    assert type(normalize_result("WASH", 0, 1, 0, 0)[1]) is float
    assert normalize_result("WASH", Int64(1), 1, 0, 0) == ("WASH", 1.0, 1, 0, 0)
    assert normalize_result("WASH", 0.5, Int64(3), 0, 0) is None
    assert normalize_result("WASH", Float32(1.5), 1, 0, 0) is None
    assert normalize_result("WASH", Float32(float("nan")), 1, 0, 0) is None

    async def check():
        recorder = Recorder()
        server = board(recorder)
        queue = subscribe(server)
        await ingest(server, sensor(4, device=2, boot=9))
        assert recorder.emit(Label("WASH"), Float32(0.25), Int64(2), Int64(9), Int64(4)) is True
        result = drain(queue)[0]
        assert result["result_id"] == "2:9:4"
        assert type(result["gesture"]) is str and type(result["confidence"]) is float
        assert all(type(result[key]) is int for key in ("device_id", "boot_id", "seq"))
        encode_frame(result)  # Plain values serialize as strict JSON.
    asyncio.run(check())


def test_emit_refuses_unaccepted_identities_and_repeated_results():
    async def check():
        events = []
        recorder = Recorder()
        server = board(recorder, observer=lambda event, **fields: events.append((event, fields)))
        queue = subscribe(server)
        await ingest(server, sensor(5), sensor(0xFFFFFFFF, device=2, boot=0xFFFFFFFF))
        # Valid arguments are accepted for delivery; the identity is checked on the loop.
        assert recorder.emit("CHOP", 0.5, 1, 8, 5) is True     # namespace never seen
        assert recorder.emit("CHOP", 0.5, 1, 7, 6) is True     # above the high-water mark
        assert recorder.emit("CHOP", 0.5, 2, 7, 5) is True     # other device, same boot and seq
        assert server.metrics["model_results_rejected"] == 3
        assert recorder.emit("CHOP", 0.5, 1, 7, 5) is True
        assert recorder.emit("WASH", 0.25, 1, 7, 5) is True    # a second result for one sample
        assert in_thread(recorder.emit, "THROW", 1, 1, 7, 5) is True
        await until(lambda: server.metrics["model_results_duplicate"] == 2)
        assert recorder.emit("FIREEXT", 0, 2, 0xFFFFFFFF, 0xFFFFFFFF) is True
        results = drain(queue)
        assert [(item["result_id"], item["gesture"]) for item in results] == [
            ("1:7:5", "CHOP"), ("2:4294967295:4294967295", "FIREEXT")]
        assert server.metrics["model_results"] == 2
        assert server.metrics["model_results_rejected"] == 3
        refused = [fields["reason"] for event, fields in events if event == "model_result_refused"]
        assert refused == ["unknown_identity"] * 3 + ["duplicate"] * 2
        assert [fields["seq"] for event, fields in events if event == "model_result"] == [
            5, 0xFFFFFFFF]
    asyncio.run(check())


def test_model_events_are_accepted_by_the_bounded_event_log(tmp_path):
    from ultra96.diagnostics import BoundedEventLog

    async def check():
        sink = BoundedEventLog(tmp_path / "events")
        recorder = Recorder()
        server = board(recorder, observer=sink)
        await ingest(server, sensor(1))
        recorder.emit("CHOP", 0.5, 1, 7, 1)
        recorder.emit("CHOP", 0.5, 1, 7, 1)
        recorder.emit("CHOP", 0.5, 1, 7, 2)
        assert sink.close()["complete"]
        assert server.metrics["observer_errors"] == 0
        records = [json.loads(line) for line in sink.events_path.read_text().splitlines()]
        assert [(item["event"], item.get("reason")) for item in records
                if item["event"].startswith("model_")] == [
            ("model_result", None), ("model_result_refused", "duplicate"),
            ("model_result_refused", "unknown_identity")]
        assert all("gesture" not in item and "confidence" not in item for item in records)
    asyncio.run(check())


def test_result_memory_is_bounded_to_the_last_4096_ids():
    async def check():
        server = board()
        await ingest(server, *(sensor(seq) for seq in range(4100)))
        assert server.metrics["model_results"] == 4100
        assert len(server._emitted) == 4096
        assert next(iter(server._emitted)) == "1:7:4"
    asyncio.run(check())


def test_emit_before_start_and_after_close_returns_false_and_counts_late():
    async def check():
        recorder = Recorder()
        server = board(recorder)
        recorder.start(server.model.emit)  # A handle obtained before the service started.
        assert recorder.emit(*VALID) is False
        assert server.metrics["model_results_late"] == 1
        await ingest(server, sensor(0), sensor(1))
        # A result queued from a worker thread is late when the service stops before the
        # event loop delivers it.
        assert in_thread(recorder.emit, "CHOP", 0.5, 1, 7, 0) is True
        server.model.close()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert server.metrics["model_results_late"] == 2
        await server.close()
        assert recorder.emit("CHOP", 0.5, 1, 7, 1) is False
        assert in_thread(recorder.emit, "CHOP", 0.5, 1, 7, 1) is False
        assert server.metrics["model_results_late"] == 4
        assert server.metrics["model_results"] == server.metrics["model_results_rejected"] == 0
    asyncio.run(check())


# --- start and loading -----------------------------------------------------------------------

def test_start_runs_once_before_any_listener_opens():
    async def check():
        listeners = []

        class Model(Recorder):
            def start(self, emit):
                listeners.append(len(server._listeners))
                super().start(emit)

        model = Model()
        server = board(model, ingest_port=0, gateway_port=0)
        await server.start()
        try:
            assert listeners == [0] and model.starts == 1
            assert server.ingest_port != 0 and server.gateway_port != 0
            assert server.model_start_seconds >= 0
            with pytest.raises(RuntimeError):
                await server.start()
            assert model.starts == 1
        finally:
            await server.close()
    asyncio.run(check())


def test_start_that_raises_is_a_model_start_error_and_no_listener_opens():
    async def check():
        class Broken(Recorder):
            def start(self, emit):
                self.emit = emit
                raise OSError("weights missing")

        model = Broken()
        server = board(model, ingest_port=0, gateway_port=0)
        with pytest.raises(ModelStartError) as failure:
            await server.start()
        assert isinstance(failure.value.__cause__, OSError)
        assert "weights missing" not in str(failure.value)
        assert server._listeners == [] and server._accept_tasks == set()
        assert server.ingest_port == 0 and server.gateway_port == 0
        assert model.emit(*VALID) is False
        assert server.metrics["model_results_late"] == 1
        await server.close()
    asyncio.run(check())


def test_start_that_exits_is_a_model_start_error_too():
    async def check():
        class Exits(Recorder):
            def start(self, emit):
                raise SystemExit(0)

        server = board(Exits(), ingest_port=0, gateway_port=0)
        with pytest.raises(ModelStartError) as failure:
            await server.start()
        assert isinstance(failure.value.__cause__, SystemExit)
        assert server._listeners == []
        await server.close()
    asyncio.run(check())


def test_a_module_is_named_by_its_import_name_whatever_its_globals():
    module = types.ModuleType("b07_named_model")
    module.name = object()  # e.g. a logger or `from os import *`
    assert model_module.model_name(module) == "b07_named_model"
    assert model_module.model_name(DummyModel()) == "dummy"

    class Unnamed:
        pass
    assert model_module.model_name(Unnamed()) == "Unnamed"


def test_leftover_model_threads_cannot_keep_a_stopped_service_alive(monkeypatch, capsys):
    from ultra96 import server as server_module
    release = threading.Event()
    worker = threading.Thread(target=release.wait, daemon=False)
    worker.start()
    try:
        assert worker in server_module._model_threads_left()
        exits = []
        monkeypatch.setattr(server_module.os, "_exit", exits.append)
        server_module._exit_now(0)
        assert exits == [0]
        event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert event == {"event": "exit_with_model_threads", "threads": 1}
    finally:
        release.set()
        worker.join(2)
    assert worker not in server_module._model_threads_left()


def write_model(folder, source, package=None):
    """Write a model module (optionally inside a package); return its import name."""
    name = "b07_model_" + uuid.uuid4().hex
    folder = Path(folder)
    if package is not None:
        package = package + "_" + uuid.uuid4().hex
        (folder / package).mkdir()
        (folder / package / "__init__.py").write_text("", encoding="utf-8")
        (folder / package / (name + ".py")).write_text(source, encoding="utf-8")
        return package + "." + name
    (folder / (name + ".py")).write_text(source, encoding="utf-8")
    return name


GOOD_MODEL = '''
_emit = None


def start(emit):
    global _emit
    _emit = emit


def on_sample(device_id, boot_id, seq, uptime_ms, values):
    _emit("CHOP", 0.5, device_id, boot_id, seq)
'''


@pytest.fixture
def import_state(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith("b07_"):
            del sys.modules[name]


def test_load_model_imports_a_module_from_the_given_directory(tmp_path, import_state):
    spec = write_model(tmp_path, GOOD_MODEL, package="b07_pkg")
    module = load_model(spec, path=tmp_path)
    assert module.__name__ == spec and sys.path[0] == str(tmp_path)
    assert model_module.model_name(module) == spec
    assert isinstance(load_model(), DummyModel)
    with pytest.raises(ValueError):
        load_model("")
    with pytest.raises(ImportError):
        load_model("b07_missing_" + uuid.uuid4().hex)
    incomplete = write_model(tmp_path, "def start(emit):\n    pass\n")
    with pytest.raises(TypeError, match="on_sample"):
        load_model(incomplete, path=tmp_path)
    asynchronous = write_model(
        tmp_path, "def start(emit):\n    pass\n\n\nasync def on_sample(*args):\n    pass\n")
    with pytest.raises(TypeError, match="async def"):
        load_model(asynchronous, path=tmp_path)

    async def check():
        server = board(module)
        queue = subscribe(server)
        await ingest(server, sensor(2))
        assert [(item["result_id"], item["gesture"], item["confidence"])
                for item in drain(queue)] == [("1:7:2", "CHOP", 0.5)]
        assert server.model.name == spec
    asyncio.run(check())


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    from tools.generate_comms_pki import generate_pki
    folder = tmp_path_factory.mktemp("model-interface-pki")
    generate_pki(folder)
    return folder


async def tls_connect(pki, port):
    from common.tls import TLS_SERVER_NAME, client_context
    return await asyncio.open_connection("127.0.0.1", port, ssl=client_context(pki / "ca-cert.pem"),
                                         server_hostname=TLS_SERVER_NAME)


async def tls_close(writer):
    writer.close()
    with contextlib.suppress(ConnectionError, ssl.SSLError):
        await writer.wait_closed()


def test_model_errors_over_real_tls_keep_acks_flowing_and_the_connection_open(pki):
    from common.tls import server_context

    def react(model, *args):
        raise RuntimeError("model bug")

    async def check():
        server = CommsServer(server_context(pki / "server-cert.pem", pki / "server-key.pem"),
                             ingest_port=0, gateway_port=0, model=Recorder(react))
        await server.start()
        try:
            reader, writer = await tls_connect(pki, server.ingest_port)
            for seq in range(3):
                await write_frame(writer, sensor(seq))
                ack = await read_frame(reader)
                assert (ack["seq"], ack["status"]) == (seq, "accepted")
            await tls_close(writer)
            assert server.metrics["model_errors"] == 3
            assert server.metrics["rejected"] == 0
            assert server.metrics["model_results"] == 0
        finally:
            await server.close()
    asyncio.run(check())


def test_run_service_with_a_module_model_reports_ready_and_delivers_its_results(
        pki, tmp_path, import_state):
    spec = write_model(tmp_path, GOOD_MODEL)
    args = SimpleNamespace(cert=str(pki / "server-cert.pem"), key=str(pki / "server-key.pem"),
                           session_id="ltc-comms", ingest_port=0, gateway_port=0,
                           model=spec, model_path=str(tmp_path))
    output = io.StringIO()

    async def check():
        with contextlib.redirect_stdout(output):
            service = asyncio.create_task(_run(args))
            await until(lambda: '"listening"' in output.getvalue(), timeout=5)
            lines = [json.loads(line) for line in output.getvalue().splitlines()]
            assert [line["event"] for line in lines] == ["model_ready", "listening"]
            assert lines[0]["model"] == spec and lines[0]["seconds"] >= 0
            phone, phone_writer = await tls_connect(pki, lines[1]["gateway_port"])
            await write_frame(phone_writer, dict(v=1, type="SUBSCRIBE", session_id="ltc-comms"))
            assert (await read_frame(phone))["type"] == "SUBSCRIBED"
            laptop, laptop_writer = await tls_connect(pki, lines[1]["ingest_port"])
            await write_frame(laptop_writer, sensor(0))
            assert (await read_frame(laptop))["status"] == "accepted"
            result = validate_message(await read_frame(phone), "GESTURE_RESULT", "ltc-comms")
            assert (result["result_id"], result["gesture"], result["confidence"]) == (
                "1:7:0", "CHOP", 0.5)
            await tls_close(laptop_writer)
            await tls_close(phone_writer)
            service.cancel()
            with pytest.raises(asyncio.CancelledError):
                await service
        stopped = json.loads(output.getvalue().splitlines()[-1])
        assert stopped["event"] == "stopped"
        assert stopped["metrics"]["model_results"] == 1
        assert stopped["metrics"]["model_samples"] == 1
    asyncio.run(check())


def run_cli(pki, *arguments, timeout=15):
    """Start the real service; stop it once it reports listening or exits by itself."""
    process = subprocess.Popen(
        [sys.executable, "-m", "ultra96.server", "--cert", str(pki / "server-cert.pem"),
         "--key", str(pki / "server-key.pem"), "--ingest-port", "0", "--gateway-port", "0",
         *arguments],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = []

    def read():
        for line in process.stdout:
            lines.append(json.loads(line))
            if lines[-1]["event"] == "listening":
                return

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(timeout)
    if process.poll() is None:
        process.kill()
    reader.join(5)
    process.wait(5)
    errors = process.stderr.read()
    process.stdout.close()
    process.stderr.close()
    return lines, process.returncode, errors


def test_cli_loads_a_model_module_and_prints_model_ready(pki, tmp_path):
    spec = write_model(tmp_path, GOOD_MODEL)
    lines, _, errors = run_cli(pki, "--model", spec, "--model-path", str(tmp_path))
    assert [line["event"] for line in lines] == ["model_ready", "listening"], errors
    assert lines[0]["model"] == spec
    lines, _, errors = run_cli(pki)
    assert [line["event"] for line in lines] == ["model_ready", "listening"], errors
    assert lines[0]["model"] == "dummy"


@pytest.mark.parametrize("case", ["start_raises", "import_raises", "missing", "no_on_sample",
                                  "import_exits", "start_exits"])
def test_cli_model_failure_prints_model_failed_and_exits_nonzero(pki, tmp_path, case):
    sources = {
        "start_raises": "def start(emit):\n    raise RuntimeError('weights missing')\n"
                        "def on_sample(*args):\n    pass\n",
        "import_raises": "raise RuntimeError('cannot import model dependencies')\n",
        "no_on_sample": "def start(emit):\n    pass\n",
        # A bare sys.exit() must still end in model_failed and a nonzero exit.
        "import_exits": "raise SystemExit(0)\n",
        "start_exits": "import sys\ndef start(emit):\n    sys.exit(0)\n"
                       "def on_sample(*args):\n    pass\n",
    }
    spec = ("b07_missing_" + uuid.uuid4().hex if case == "missing"
            else write_model(tmp_path, sources[case]))
    lines, returncode, errors = run_cli(pki, "--model", spec, "--model-path", str(tmp_path))
    assert returncode not in (0, None), errors
    assert [line["event"] for line in lines] == ["model_failed"], errors
    assert lines[0]["model"] == spec
    assert lines[0]["error_type"] == {"start_raises": "RuntimeError", "import_raises": "RuntimeError",
                                      "missing": "ModuleNotFoundError",
                                      "no_on_sample": "TypeError", "import_exits": "SystemExit",
                                      "start_exits": "SystemExit"}[case]


def test_cli_does_not_blame_the_model_for_a_bad_session(pki, tmp_path):
    spec = write_model(tmp_path, GOOD_MODEL)
    lines, returncode, errors = run_cli(pki, "--model", spec, "--model-path", str(tmp_path),
                                        "--session-id", "")
    assert returncode not in (0, None)
    assert lines == [], errors
    assert "ProtocolError" in errors
