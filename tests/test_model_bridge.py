"""The separate-runner bridge: samples out and results in as JSON lines (no hardware)."""
import asyncio
import io
import json
import os
from pathlib import PurePosixPath
import queue
import socket
import stat
import threading
import time

import pytest

from common.sensor import CHANNELS
from ultra96 import model_bridge
from ultra96.model import load_model
from ultra96.model_bridge import Bridge
from ultra96.model_runner_example import DEFAULT_SOCKET, run
from tools.demo_service import BOARD_ROOT

from tests.test_model_interface import board, ingest, sensor, subscribe

unix_only = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs Unix sockets")


class Emits:
    def __init__(self, answer=True):
        self.calls = queue.Queue()
        self.answer = answer

    def __call__(self, *fields):
        self.calls.put(fields)
        return self.answer


def connected(bridge, emit=None):
    """A bridge serving one end of a socket pair; returns (bridge, emit, runner socket, runner lines)."""
    emit = Emits() if emit is None else emit
    bridge.start(emit)
    service_end, runner = socket.socketpair()
    runner.settimeout(5)
    bridge.attach(service_end)
    return emit, runner, runner.makefile("r", encoding="utf-8")


def wait_for(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_runner_gets_hello_then_each_sample_with_the_on_sample_arguments():
    bridge = Bridge()
    _, runner, lines = connected(bridge)
    assert json.loads(lines.readline()) == dict(type="hello", format=1, channels=list(CHANNELS))
    values = tuple(0.5 + i for i in range(11))
    bridge.on_sample(2, 7, 42, 12345, values)
    assert json.loads(lines.readline()) == dict(type="sample", device_id=2, boot_id=7, seq=42,
                                                uptime_ms=12345, values=list(values))
    wait_for(lambda: bridge.counts["samples_sent"] == 1)
    bridge.close()


def test_result_lines_call_emit_and_bad_lines_are_skipped_without_closing():
    bridge = Bridge()
    emit, runner, lines = connected(bridge)
    lines.readline()
    runner.sendall(b'not json\n{"type":"other"}\n{"type":"result","gesture":"CHOP"}\n\n'
                   b'{"type":"result","gesture":"CHOP","confidence":0.9,"device_id":1,'
                   b'"boot_id":7,"seq":42}\n')
    assert emit.calls.get(timeout=5) == ("CHOP", 0.9, 1, 7, 42)
    assert bridge.counts["bad_lines"] == 3 and bridge.counts["results"] == 1
    bridge.on_sample(1, 7, 43, 1, (0.0,) * 11)
    assert json.loads(lines.readline())["seq"] == 43  # still connected
    bridge.close()


def test_a_line_too_deeply_nested_to_parse_is_skipped_without_closing(monkeypatch):
    # Python 3.10 (the board's) raises RecursionError for deep nesting; 3.12 may not.
    def loads(text):
        if text.startswith("[[["):
            raise RecursionError("maximum recursion depth exceeded")
        return json.loads(text)
    monkeypatch.setattr(model_bridge, "json", type("Json", (), dict(loads=staticmethod(loads),
                                                                     dumps=staticmethod(json.dumps))))
    bridge = Bridge()
    emit, runner, lines = connected(bridge)
    lines.readline()
    runner.sendall(b"[" * 2000 + b"\n" + b'{"type":"result","gesture":"CHOP","confidence":1,'
                   b'"device_id":1,"boot_id":7,"seq":3}\n')
    assert emit.calls.get(timeout=5) == ("CHOP", 1, 1, 7, 3)
    assert bridge.counts["bad_lines"] == 1
    bridge.close()


def test_the_bridge_names_the_connecting_process_where_the_system_tells_it():
    left, right = socket.socketpair()
    with left, right:
        pid = model_bridge._peer_pid(left)
    assert pid == (os.getpid() if hasattr(socket, "SO_PEERCRED") else None)


def test_results_emit_refuses_are_counted():
    bridge = Bridge()
    emit, runner, lines = connected(bridge, Emits(answer=False))
    runner.sendall(b'{"type":"result","gesture":"bad label!","confidence":2,"device_id":1,'
                   b'"boot_id":7,"seq":1}\n')
    emit.calls.get(timeout=5)
    wait_for(lambda: bridge.counts["results_refused"] == 1)
    bridge.close()


def test_an_overlong_line_is_skipped_and_the_next_line_still_works():
    bridge = Bridge()
    emit, runner, lines = connected(bridge)
    runner.sendall(b"x" * (model_bridge.MAX_LINE_BYTES * 3) + b"\n" +
                   b'{"type":"result","gesture":"WASH","confidence":1,"device_id":2,"boot_id":1,"seq":2}\n')
    assert emit.calls.get(timeout=5) == ("WASH", 1, 2, 1, 2)
    assert bridge.counts["bad_lines"] == 1
    bridge.close()


def test_samples_without_a_runner_or_with_a_full_queue_are_dropped_and_counted():
    bridge = Bridge()
    bridge.start(Emits())
    bridge.on_sample(1, 7, 1, 1, (0.0,) * 11)
    assert bridge.counts["samples_dropped_no_runner"] == 1
    full = queue.Queue(1)
    full.put("waiting")
    bridge._current = (None, full)  # a runner that stopped reading
    bridge.on_sample(1, 7, 2, 1, (0.0,) * 11)
    assert bridge.counts["samples_dropped_full"] == 1


def test_a_new_runner_replaces_the_old_one_and_a_gone_runner_stops_the_samples():
    bridge = Bridge()
    _, first, first_lines = connected(bridge)
    first_lines.readline()
    service_end, second = socket.socketpair()
    second.settimeout(5)
    bridge.attach(service_end)
    assert first_lines.readline() == ""  # the old runner sees the end of the stream
    second_lines = second.makefile("r", encoding="utf-8")
    second_lines.readline()
    bridge.on_sample(1, 7, 5, 1, (0.0,) * 11)
    assert json.loads(second_lines.readline())["seq"] == 5
    second_lines.close()
    second.close()
    wait_for(lambda: bridge._current is None)
    bridge.on_sample(1, 7, 6, 1, (0.0,) * 11)
    assert bridge.counts["samples_dropped_no_runner"] == 1


@unix_only
def test_listener_is_private_and_replaces_a_stale_socket_but_not_a_file(tmp_path):
    path = tmp_path / "model-runner.sock"
    first = model_bridge.listen(path)
    with pytest.raises(FileExistsError):
        model_bridge.listen(path)  # a second service must not take a running one's socket
    assert stat.S_ISSOCK(path.lstat().st_mode)
    first.close()  # the old service died; its socket file stays
    second = model_bridge.listen(path)
    assert stat.S_ISSOCK(path.lstat().st_mode) and stat.S_IMODE(path.stat().st_mode) == 0o600
    second.close()
    path.unlink()
    path.write_text("not a socket")
    with pytest.raises(FileExistsError):
        model_bridge.listen(path)


@unix_only
def test_service_with_the_bridge_sends_a_glove_sample_to_the_runner_and_its_result_to_the_phone(
        tmp_path, monkeypatch):
    path = tmp_path / "model-runner.sock"
    monkeypatch.setenv("COMMS_MODEL_SOCKET", str(path))
    monkeypatch.setattr(model_bridge, "_BRIDGE", Bridge())
    module = load_model("ultra96.model_bridge")
    output = io.StringIO()

    def runner():
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(path))
        with sock:
            run(sock, every=1, label="CHOP", out=output)

    async def check():
        server = board(module)
        results = subscribe(server)
        server.model_start_seconds = server.model.start(asyncio.get_running_loop())
        threading.Thread(target=runner, daemon=True).start()
        await asyncio.get_running_loop().run_in_executor(
            None, wait_for, lambda: model_bridge._BRIDGE._current is not None)
        raw = [i * 100 for i in range(11)]
        acks = await ingest(server, sensor(3, values=raw))
        assert acks[0]["status"] == "accepted"
        result = await asyncio.wait_for(results.get(), 5)
        assert (result["gesture"], result["confidence"], result["seq"]) == ("CHOP", 1.0, 3)
        assert result["result_id"] == "1:7:3"
        model_bridge._BRIDGE.close()

    asyncio.run(check())
    assert output.getvalue().startswith("connected, channels: ax,")


def test_the_example_runner_and_the_deployed_bridge_agree_on_the_socket():
    # A deployed bridge lives in BOARD_ROOT/<source folder>/ultra96/; its socket is in BOARD_ROOT.
    assert DEFAULT_SOCKET == BOARD_ROOT + "/model-runner.sock"
    deployed = PurePosixPath(BOARD_ROOT) / "source-co-v3-stamp" / "ultra96" / "model_bridge.py"
    assert str(deployed.parents[2] / "model-runner.sock") == DEFAULT_SOCKET
