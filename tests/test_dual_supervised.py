"""Supervised capture child (dual_bridge --supervised): stream, control and stop."""
import asyncio
import io
import json
import logging
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest

from common.tls import server_context
from laptop.bridge import Bridge, BridgeConfig
from laptop.dual_bridge import DualBridge, _SupervisedControl, _parser
from laptop.evidence import PacketEvidence
from tools.generate_comms_pki import generate_pki
from tools.video_evidence import _json as strict_json
from tools.video_evidence import show_packet_examples
from ultra96.server import CommsServer


ROOT = Path(__file__).parents[1]
NOTICES = {"status", "log", "finished"}
PHASES = ["startup", "observation", "shutdown", "drain", "complete"]
STATUS_KEYS = {"type", "timestamp_utc", "monotonic_seconds", "phase",
               "elapsed_seconds", "devices"}
DSTAT_KEYS = {"connected", "transport", "mtu", "rate_hz", "gated", "boot_id",
              "first_seq", "last_seq", "received", "processed", "sent", "acked",
              "duplicate_acks", "queue", "dropped", "errors", "ble_connections",
              "transport_connections", "commands"}
DROPPED_KEYS = {"queue", "stale", "generation", "ambiguous"}
ERROR_KEYS = {"malformed", "ack_errors", "transport_errors", "ble_errors",
              "cleanup_errors", "identity_mismatches", "source_stats_errors",
              "disconnects", "gaps", "duplicates", "out_of_order", "new_boots"}

# Windows timers stretch the CLI mock's 50 ms period (20 Hz) to about 62 ms,
# under the 90% coverage bound. These runs feed the mock at 50 Hz instead so
# their verdict can be clean; everything else is the real CLI.
FAST_MOCK = textwrap.dedent("""
    import sys
    from laptop import dual_bridge

    original_run = dual_bridge.DualBridge.run

    async def faster_mock(self, *args, **kwargs):
        kwargs["mock_rate"] = 50
        return await original_run(self, *args, **kwargs)

    dual_bridge.DualBridge.run = faster_mock
    sys.argv = ["dual_bridge"] + sys.argv[1:]
    raise SystemExit(dual_bridge.main())
""")

# FAST_MOCK, plus a report finalize that announces itself in a marker file and
# then waits: the window after DualBridge.run has returned, made wide.
SLOW_FINALIZE = textwrap.dedent("""
    import os
    import sys
    import time
    from laptop import dual_bridge, reporting

    original_run = dual_bridge.DualBridge.run
    original_finalize = reporting.ReportReservation.finalize

    async def faster_mock(self, *args, **kwargs):
        kwargs["mock_rate"] = 50
        return await original_run(self, *args, **kwargs)

    def slow_finalize(self, payload):
        open(os.environ["TEST_FINALIZE_MARKER"], "w").close()
        time.sleep(1.5)
        return original_finalize(self, payload)

    dual_bridge.DualBridge.run = faster_mock
    reporting.ReportReservation.finalize = slow_finalize
    sys.argv = ["dual_bridge"] + sys.argv[1:]
    raise SystemExit(dual_bridge.main())
""")

# A run that logs and then fails: the exception path out of main().
FAILING_RUN = textwrap.dedent("""
    import logging
    import sys
    from laptop import dual_bridge

    async def failing(self, *args, **kwargs):
        logging.getLogger("laptop.dual_bridge").warning("about to fail")
        raise RuntimeError("injected failure")

    dual_bridge.DualBridge.run = failing
    sys.argv = ["dual_bridge"] + sys.argv[1:]
    raise SystemExit(dual_bridge.main())
""")


def is_int(value):
    return type(value) is int


def check_status(item):
    assert set(item) == STATUS_KEYS
    assert item["phase"] in PHASES
    assert item["elapsed_seconds"] is None or item["elapsed_seconds"] >= 0
    assert set(item["devices"]) == {"1", "2"}
    for device in item["devices"].values():
        assert set(device) == DSTAT_KEYS
        assert type(device["connected"]) is bool and type(device["transport"]) is bool
        for key in ("mtu", "rate_hz", "boot_id", "first_seq", "last_seq"):
            assert device[key] is None or is_int(device[key])
        assert device["gated"] in (None, True, False)
        for key in ("received", "processed", "sent", "acked", "duplicate_acks", "queue",
                    "ble_connections", "transport_connections"):
            assert is_int(device[key]) and device[key] >= 0
        assert set(device["dropped"]) == DROPPED_KEYS
        assert set(device["errors"]) == ERROR_KEYS
        assert all(is_int(value) for value in device["dropped"].values())
        assert all(is_int(value) for value in device["errors"].values())


def check_phase_order(statuses):
    order = [PHASES.index(item["phase"]) for item in statuses]
    assert order == sorted(order)
    assert statuses[0]["phase"] == "startup" and statuses[0]["elapsed_seconds"] is None
    assert statuses[-1]["phase"] == "complete"
    assert "observation" in {item["phase"] for item in statuses}


# --- PacketEvidence stream_all, text_log and notify -------------------------

def test_stream_all_streams_every_record_and_notices_but_files_only_records(tmp_path):
    stream = io.StringIO()
    evidence = PacketEvidence(tmp_path / "packets.jsonl", capacity=64, sample_every=3,
                              stream=stream, color=True, stream_all=True, text_log=False)
    for seq in range(5):
        assert evidence.record("sensor", device_id=1, seq=seq, values=[seq] * 11)
    assert evidence.notify("status", phase="observation", devices={})
    assert evidence.record("sensor_ack", device_id=2, seq=0)
    evidence.close()
    assert evidence.summary()["unfinished"] == 0
    evidence.finish(clean=True, report_saved=True, exit_code=0)

    lines = stream.getvalue().splitlines()
    rows = [strict_json(line) for line in lines]
    files = (tmp_path / "packets.jsonl").read_text(encoding="utf-8").splitlines()
    assert [line for line, row in zip(lines, rows) if row["type"] not in NOTICES] == files
    assert [row["type"] for row in rows].count("sensor") == 5  # no 1-in-3 sampling
    assert "\x1b" not in stream.getvalue()
    assert {strict_json(line)["type"] for line in files} == {"sensor", "sensor_ack"}
    assert rows[-1]["type"] == "finished" and rows[-1]["exit_code"] == 0
    assert any(row["type"] == "status" for row in rows)
    assert not (tmp_path / "packets.log").exists()
    summary = evidence.summary()
    assert summary["written"] == 6
    assert (summary["dropped"], summary["write_errors"], summary["unfinished"]) == (0, 0, 0)
    assert (summary["stream_errors"], summary["notify_dropped"]) == (0, 0)


def test_text_log_off_keeps_default_console_sampling_and_notify_is_a_no_op(tmp_path):
    stream = io.StringIO()
    evidence = PacketEvidence(tmp_path / "packets.jsonl", capacity=16, sample_every=3,
                              stream=stream, color=False, text_log=False)
    for seq in range(6):
        assert evidence.record("sensor", device_id=1, seq=seq)
    assert evidence.notify("status", phase="startup") is False
    evidence.close()
    assert len(stream.getvalue().splitlines()) == 2
    assert "status" not in stream.getvalue()
    assert not (tmp_path / "packets.log").exists()
    assert len((tmp_path / "packets.jsonl").read_text().splitlines()) == 6
    assert set(evidence.summary()) == {"written", "dropped", "write_errors", "unfinished",
                                       "console_sample_every"}


def test_broken_stream_stops_streaming_but_never_fails_the_evidence(tmp_path):
    class BrokenPipe(io.StringIO):
        writes = 0

        def write(self, text):
            BrokenPipe.writes += 1
            raise BrokenPipeError("reader went away")

    evidence = PacketEvidence(tmp_path / "packets.jsonl", capacity=64,
                              stream=BrokenPipe(), stream_all=True, text_log=False)
    for seq in range(20):
        assert evidence.record("sensor", device_id=2, seq=seq)
        evidence.notify("status", phase="observation", seq=seq)
    evidence.close()
    summary = evidence.summary()
    evidence.finish(clean=True, report_saved=True, exit_code=0)

    assert summary["write_errors"] == 0
    assert summary["stream_errors"] == 1
    assert (summary["dropped"], summary["unfinished"], summary["written"]) == (0, 0, 20)
    assert BrokenPipe.writes == 1
    records = [strict_json(line) for line in
               (tmp_path / "packets.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [record["seq"] for record in records] == list(range(20))


def test_notices_drop_oldest_while_the_stream_is_stalled(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Stalled(io.StringIO):
        def write(self, text):
            entered.set()
            assert release.wait(5)
            return super().write(text)

    stream = Stalled()
    evidence = PacketEvidence(None, stream=stream, stream_all=True)
    try:
        evidence.notify("status", n=0)
        assert entered.wait(2)
        extra = PacketEvidence.NOTICE_CAPACITY + 7
        for n in range(1, extra + 1):
            assert evidence.notify("status", n=n)
        assert evidence.summary()["notify_dropped"] == 7
    finally:
        release.set()
        evidence.finish()
    numbers = [json.loads(line)["n"] for line in stream.getvalue().splitlines()
               if json.loads(line)["type"] == "status"]
    assert numbers == [0] + list(range(8, extra + 1))
    assert evidence.summary()["written"] == 0  # notices are never evidence


def test_finished_is_last_and_non_finite_notices_are_never_written(tmp_path):
    stream = io.StringIO()
    evidence = PacketEvidence(None, stream=stream, stream_all=True)
    assert evidence.notify("status", elapsed_seconds=float("nan"))
    assert evidence.notify("log", level="WARNING", logger="x", message="kept")
    evidence.finish(clean=False, report_saved=False, exit_code=1)
    assert evidence.notify("log", level="WARNING", logger="x", message="late") is False
    rows = [strict_json(line) for line in stream.getvalue().splitlines()]
    assert [row["type"] for row in rows] == ["log", "finished"]
    assert rows[-1]["clean"] is False and rows[-1]["exit_code"] == 1
    assert evidence.summary()["notify_dropped"] == 1


def test_finished_follows_every_record_even_when_close_timed_out(tmp_path):
    release = threading.Event()

    class StalledOnce(io.StringIO):
        stalled = False

        def write(self, text):
            if not self.stalled:
                self.stalled = True
                assert release.wait(5)
            return super().write(text)

    stream = StalledOnce()
    evidence = PacketEvidence(tmp_path / "packets.jsonl", stream=stream,
                              stream_all=True, text_log=False)
    try:
        for seq in range(5):
            assert evidence.record("sensor", device_id=1, seq=seq)
        evidence.close(timeout=0.05)  # the reader is stalled: records remain
        assert evidence.summary()["unfinished"] > 0
        assert evidence.notify("log", level="WARNING", logger="x", message="before")
        evidence.finish(timeout=0.05, clean=False, report_saved=True, exit_code=1)
    finally:
        release.set()
    evidence._thread.join(5)
    assert not evidence._thread.is_alive()
    rows = [strict_json(line) for line in stream.getvalue().splitlines()]
    # Ordinary notices interleave with the records; finished comes after all.
    assert sorted(row["type"] for row in rows[:-1]) == ["log"] + ["sensor"] * 5
    assert [row["seq"] for row in rows if row["type"] == "sensor"] == list(range(5))
    assert rows[-1]["type"] == "finished" and rows[-1]["exit_code"] == 1


def test_a_notice_racing_finish_is_refused_never_written_after_finished():
    attempts = []

    class Racing(PacketEvidence):
        def _notice(self, kind, fields):
            if kind == "finished":
                # Another thread logs while finish() builds the last notice.
                other = threading.Thread(target=lambda: attempts.append(
                    self.notify("log", level="WARNING", logger="x", message="late")))
                other.start()
                other.join(0.1)
                attempts.append(other)
            return PacketEvidence._notice(kind, fields)

    stream = io.StringIO()
    evidence = Racing(None, stream=stream, stream_all=True)
    evidence.finish(clean=True, report_saved=True, exit_code=0)
    other = attempts.pop(0)
    other.join(5)
    assert attempts == [False]
    rows = [strict_json(line) for line in stream.getvalue().splitlines()]
    assert [row["type"] for row in rows] == ["finished"]
    evidence.finish(clean=False, report_saved=False, exit_code=1)  # once only
    assert stream.getvalue().count('"finished"') == 1


# --- DualBridge.run: stop_event and status_callback --------------------------

class Writer:
    def close(self):
        pass

    async def wait_closed(self):
        pass


def make_dual(*, expected_rate=20):
    def bridge(device):
        item = Bridge(BridgeConfig(
            ca_file="unused", diagnostic_unprotected=True,
            expected_device_id=device, source_audit=True))
        messages = []

        async def connect():
            return object(), Writer()

        async def write(_writer, message, timeout):
            messages.append(message)

        async def read(_reader, timeout):
            message = messages[-1]
            return {"v": message["v"], "type": "INGEST_ACK",
                    "session_id": message["session_id"], "device_id": message["device_id"],
                    "boot_id": message["boot_id"], "seq": message["seq"],
                    "status": "accepted", "request_id": message["request_id"]}

        item._connector, item._write_frame, item._read_frame = connect, write, read
        return item

    left, right = bridge(1), bridge(2)
    return left, right, DualBridge(left, right, expected_rate=expected_rate,
                                   startup_timeout=2, drain_timeout=2, shutdown_timeout=2)


def test_operator_stop_ends_observation_like_the_deadline_with_status_snapshots():
    async def check():
        left, right, dual = make_dual()
        stop = asyncio.Event()
        statuses = []
        running = asyncio.create_task(dual.run(
            duration=30, mock=True, mock_rate=50, stop_event=stop,
            status_callback=statuses.append))

        async def observing():
            while not (left.metrics.acked >= 10 and right.metrics.acked >= 10
                       and any(item["phase"] == "observation" for item in statuses)):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(observing(), 5)
        stop.set()
        return dual, statuses, await asyncio.wait_for(running, 10)

    started = time.monotonic()
    dual, statuses, report = asyncio.run(check())
    assert time.monotonic() - started < 10
    assert dual.stop_reason == "operator" and dual.phase == "complete"
    assert report["clean"] is True
    assert 0 < report["common_observation_seconds"] < 5
    for device in ("1", "2"):
        item = report["devices"][device]
        assert item["runtime_failures"] == []
        assert item["source"]["complete"] is True and item["source"]["clean"] is True
        assert item["source"]["generated"] == item["acked"]
    wrapped = [dict(status, type="status", timestamp_utc="t", monotonic_seconds=0.0)
               for status in statuses]
    for item in wrapped:
        check_status(item)
        json.dumps(item, allow_nan=False)
    check_phase_order(wrapped)
    observing = [item for item in statuses if item["phase"] == "observation"]
    assert any(item["devices"]["1"]["connected"] and item["devices"]["1"]["transport"]
               for item in observing)
    final = statuses[-1]["devices"]
    for device in ("1", "2"):
        assert final[device]["acked"] == report["devices"][device]["acked"]
        assert final[device]["connected"] is False and final[device]["transport"] is False
        assert final[device]["first_seq"] == 0
        assert final[device]["last_seq"] == report["devices"][device]["acked"] - 1


def test_stop_before_both_streams_start_fails_startup_with_the_marker():
    async def check():
        _, _, dual = make_dual()
        stop = asyncio.Event()
        stop.set()
        return dual, await dual.run(duration=30, mock=True, mock_rate=50, stop_event=stop)

    dual, report = asyncio.run(check())
    assert dual.stop_reason == "operator"
    assert report["clean"] is False
    assert report["common_observation_seconds"] == 0.0
    for device in ("1", "2"):
        assert "startup:StopRequested" in report["devices"][device]["runtime_failures"]


def test_without_stop_event_or_status_the_run_is_unchanged():
    async def check():
        _, _, dual = make_dual()
        created = []
        original = asyncio.create_task

        def record_task(coroutine, **kwargs):
            created.append(kwargs.get("name"))
            return original(coroutine, **kwargs)

        asyncio.create_task = record_task
        try:
            report = await dual.run(duration=0.3, mock=True, mock_rate=100)
        finally:
            asyncio.create_task = original
        return dual, report, created

    dual, report, created = asyncio.run(check())
    assert report["clean"] is True
    assert dual.stop_reason == "duration" and dual.phase == "complete"
    assert "status" not in created and "writer-1" in created


def test_a_failing_status_callback_is_logged_and_never_changes_the_verdict(caplog):
    def broken(status):
        raise OSError("stalled")

    async def check():
        _, _, dual = make_dual()
        return await dual.run(duration=0.3, mock=True, mock_rate=100,
                              status_callback=broken)

    with caplog.at_level(logging.WARNING, logger="laptop.dual_bridge"):
        report = asyncio.run(check())
    assert report["clean"] is True and report["progress_error"] is False
    assert [record.getMessage() for record in caplog.records].count(
        "status output failed error_type=callback") == 1


# --- _SupervisedControl: stdin lines and console signals ----------------------

def test_control_lines_stop_and_command_only_where_controls_exist(caplog):
    loop = asyncio.new_event_loop()
    submitted = []
    try:
        bridges = {1: SimpleNamespace(commands=None),
                   2: SimpleNamespace(commands=object(),
                                      submit_command=lambda: submitted.append(2))}
        dual = SimpleNamespace(bridges=bridges, phase="observation")
        control = _SupervisedControl(loop, dual, io.StringIO(
            "command 2\ncommand 1\nbogus\nmore bogus\n\n"))
        with caplog.at_level(logging.WARNING, logger="laptop.dual_bridge"):
            control._read()  # end of input: a graceful stop request
            loop.run_until_complete(asyncio.sleep(0))
            assert control.stop_event.is_set()
            dual.phase = "startup"
            control._line("command 2")
            loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()
    messages = [record.getMessage() for record in caplog.records]
    assert submitted == [2]
    assert "command 1 ignored: no control connection" in messages
    assert "command 2 ignored: the observation is not running" in messages
    assert sum("control input ignored" in message for message in messages) == 1

    loop = asyncio.new_event_loop()
    try:
        control = _SupervisedControl(loop, dual, io.StringIO("stop\n"))
        control._line("stop")
        loop.run_until_complete(asyncio.sleep(0))
        assert control.stop_event.is_set()
    finally:
        loop.close()


@pytest.mark.parametrize("name", ["SIGINT", "SIGBREAK"])
def test_console_signals_request_a_graceful_stop(name):
    number = getattr(signal, name, None)
    if number is None:
        pytest.skip(f"{name} is not available on this platform")
    previous = signal.getsignal(number)
    loop = asyncio.new_event_loop()
    control = _SupervisedControl(loop, SimpleNamespace(bridges={}, phase=None))
    try:
        control.start()
        assert signal.getsignal(number) == control._signal
        signal.raise_signal(number)
        loop.run_until_complete(asyncio.sleep(0.01))
        assert control.stop_event.is_set()
        # The CLI keeps the handlers until exit: once the loop has closed, a
        # late Ctrl+Break must do nothing rather than end the process.
        loop.close()
        signal.raise_signal(number)
        time.sleep(0.01)
    finally:
        control.restore_signals()
        loop.close()
    assert signal.getsignal(number) == previous


def test_supervised_flag_is_off_by_default_and_excludes_the_keyboard():
    assert _parser().parse_args(["--ca", "unused", "--mock"]).supervised is False
    assert _parser().parse_args(["--ca", "unused", "--mock", "--supervised"]).supervised
    completed = subprocess.run(
        [sys.executable, "-m", "laptop.dual_bridge", "--ca", "unused", "--left-address", "a",
         "--right-address", "b", "--keyboard", "--supervised"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=10)
    assert completed.returncode == 2 and completed.stdout == ""
    assert "--keyboard" in completed.stderr


# --- The CLI child against a real local Ultra96 service -----------------------

@pytest.fixture(scope="module")
def ingest(tmp_path_factory):
    pki = tmp_path_factory.mktemp("pki")
    generate_pki(pki)
    started = threading.Event()
    state = {}

    def serve():
        async def main():
            server = CommsServer(server_context(
                pki / "server-cert.pem", pki / "server-key.pem"),
                ingest_port=0, gateway_port=0)
            await server.start()
            state.update(loop=asyncio.get_running_loop(), stop=asyncio.Event(),
                         port=server.ingest_port)
            started.set()
            try:
                await state["stop"].wait()
            finally:
                await server.close()

        try:
            asyncio.run(main())
        finally:
            started.set()

    thread = threading.Thread(target=serve, name="test-ultra96", daemon=True)
    thread.start()
    assert started.wait(10) and "port" in state
    yield pki / "ca-cert.pem", state["port"]
    state["loop"].call_soon_threadsafe(state["stop"].set)
    thread.join(10)


class Child:
    """One supervised capture child; stdout lines are pumped into a queue."""

    def __init__(self, tmp_path, ingest, *, duration, fast=True, pump=True, creationflags=0,
                 script=None, env=None):
        ca, port = ingest
        self.folder = tmp_path / "capture"
        self.folder.mkdir()
        self.stderr_path = tmp_path / "stderr.txt"
        if script is not None:
            program = ["-c", script]
        else:
            program = ["-c", FAST_MOCK] if fast else ["-m", "laptop.dual_bridge"]
        command = [sys.executable, "-u", *program, "--ca", str(ca), "--port", str(port),
                   "--mock", "--duration", str(duration), "--expected-rate", "20",
                   "--report", str(self.folder / "report.json"),
                   "--evidence", str(self.folder / "packets.jsonl"),
                   "--progress-interval", "0", "--supervised"]
        with self.stderr_path.open("w", encoding="utf-8") as stderr:
            self.process = subprocess.Popen(
                command, cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=stderr, text=True, encoding="utf-8", creationflags=creationflags,
                env=dict(os.environ, PYTHONIOENCODING="utf-8", **(env or {})))
        self.lines = []
        self._queue = queue.Queue()
        if pump:
            threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.process.stdout:
            self._queue.put(line)
        self._queue.put(None)

    def wait_for(self, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while True:
            line = self._queue.get(timeout=max(0.01, deadline - time.monotonic()))
            assert line is not None, "the child's stream ended early"
            self.lines.append(line)
            item = strict_json(line)
            if predicate(item):
                return item

    def write(self, text):
        self.process.stdin.write(text)
        self.process.stdin.flush()

    def finish(self, timeout=30):
        code = self.process.wait(timeout)
        while True:
            line = self._queue.get(timeout=5)
            if line is None:
                break
            self.lines.append(line)
        return code

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(5)
        for handle in (self.process.stdin, self.process.stdout):
            try:
                handle.close()
            except OSError:
                pass

    def rows(self):
        return [strict_json(line) for line in self.lines]

    def report(self):
        return json.loads((self.folder / "report.json").read_text(encoding="utf-8"))


def observing(after):
    return lambda item: (item["type"] == "status" and item["phase"] == "observation"
                         and item["elapsed_seconds"] >= after)


def check_capture_folder(child, rows, report):
    assert sorted(path.name for path in child.folder.iterdir()) == [
        "packets.jsonl", "report.json"]  # no packets.log in supervised mode
    files = (child.folder / "packets.jsonl").read_text(encoding="utf-8").splitlines()
    records = [strict_json(line) for line in files]
    assert all(isinstance(row["type"], str) and row["type"] for row in records)
    assert not NOTICES & {row["type"] for row in records}
    assert {"sensor", "sensor_ack"} <= {row["type"] for row in records}
    assert report["evidence"]["write_errors"] == 0 and report["evidence"]["dropped"] == 0
    assert report["evidence"]["written"] == len(files)
    return files


def test_supervised_stdout_is_json_lines_and_packets_jsonl_stays_evidence_only(
        tmp_path, ingest, capsys):
    child = Child(tmp_path, ingest, duration=1.0, fast=False)
    try:
        code = child.finish()
    finally:
        child.close()
    rows = child.rows()
    report = child.report()

    assert all(isinstance(row["type"], str) and row["type"] for row in rows)
    assert [row["type"] for row in rows].count("finished") == 1
    finished = rows[-1]
    assert finished["type"] == "finished"
    assert finished["report_saved"] is True
    assert finished["clean"] is report["clean"]
    assert finished["exit_code"] == code == (0 if report["clean"] else 1)
    assert not any("devices" in row and row["type"] != "status" for row in rows)
    statuses = [row for row in rows if row["type"] == "status"]
    for item in statuses:
        check_status(item)
    check_phase_order(statuses)
    assert len(statuses) >= 4  # 0.25 s apart over a 1 s observation

    files = check_capture_folder(child, rows, report)
    assert [line.rstrip("\n") for line, row in zip(child.lines, rows)
            if row["type"] not in NOTICES] == files
    assert show_packet_examples(child.folder) is True
    capsys.readouterr()
    assert report["run"]["stop"] == "duration"
    assert report["evidence"]["stream_errors"] == 0
    assert report["evidence"]["notify_dropped"] == 0
    for device in ("1", "2"):
        source = report["devices"][device]["source"]
        assert source["complete"] is True and source["clean"] is True
        assert statuses[-1]["devices"][device]["acked"] == report["devices"][device]["acked"]
    assert child.stderr_path.read_text(encoding="utf-8") == ""


def test_stop_on_stdin_ends_a_long_run_early_and_cleanly(tmp_path, ingest):
    child = Child(tmp_path, ingest, duration=60)
    try:
        status = child.wait_for(observing(1.0))
        child.write("hello\nstill not a command\ncommand 1\n")
        child.write("stop\n")
        stopped_at = time.monotonic()
        code = child.finish()
        ended_after = time.monotonic() - stopped_at
    finally:
        child.close()
    rows = child.rows()
    report = child.report()

    assert ended_after < 15
    assert code == 0 and rows[-1]["type"] == "finished"
    assert rows[-1]["clean"] is True and rows[-1]["exit_code"] == 0
    assert report["clean"] is True
    assert report["run"]["stop"] == "operator"
    assert report["run"]["requested_duration_seconds"] == 60
    assert status["elapsed_seconds"] <= report["common_observation_seconds"] \
        <= status["elapsed_seconds"] + 3
    for device in ("1", "2"):
        item = report["devices"][device]
        assert item["runtime_failures"] == []
        assert item["source"]["complete"] is True and item["source"]["clean"] is True
        assert item["source"]["generated"] == item["received"] == item["acked"]
    logs = [row for row in rows if row["type"] == "log"]
    assert all(row["level"] == "WARNING" and row["logger"] == "laptop.dual_bridge"
               for row in logs)
    messages = [row["message"] for row in logs]
    assert sum("control input ignored" in message for message in messages) == 1
    assert "command 1 ignored: no control connection" in messages
    statuses = [row for row in rows if row["type"] == "status"]
    check_phase_order(statuses)
    check_capture_folder(child, rows, report)
    assert child.stderr_path.read_text(encoding="utf-8") == ""


def test_end_of_stdin_also_stops_gracefully(tmp_path, ingest):
    child = Child(tmp_path, ingest, duration=60)
    try:
        child.wait_for(observing(0.5))
        child.process.stdin.close()
        code = child.finish()
    finally:
        child.close()
    report = child.report()
    assert code == 0 and child.rows()[-1]["type"] == "finished"
    assert report["clean"] is True
    assert report["run"]["stop"] == "operator"
    assert report["common_observation_seconds"] < 10
    for device in ("1", "2"):
        source = report["devices"][device]["source"]
        assert source["complete"] is True and source["clean"] is True


@pytest.mark.skipif(os.name != "nt", reason="Ctrl+Break is a Windows console event")
def test_ctrl_break_to_the_process_group_stops_gracefully(tmp_path, ingest):
    # The supervisor starts the child in its own process group, as here.
    child = Child(tmp_path, ingest, duration=60,
                  creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        child.wait_for(observing(0.5))
        try:
            os.kill(child.process.pid, signal.CTRL_BREAK_EVENT)
        except OSError as exc:
            child.write("stop\n")
            child.finish()
            pytest.skip(f"console control events unavailable: {exc}")
        try:
            code = child.finish(timeout=15)
        except subprocess.TimeoutExpired:
            child.write("stop\n")
            child.finish()
            pytest.skip("console control events are not delivered without a console")
    finally:
        child.close()
    report = child.report()
    assert code == 0 and child.rows()[-1]["type"] == "finished"
    assert report["clean"] is True
    assert report["run"]["stop"] == "operator"


@pytest.mark.skipif(os.name != "nt", reason="Ctrl+Break is a Windows console event")
def test_ctrl_break_while_the_report_is_finalized_changes_nothing(tmp_path, ingest):
    marker = tmp_path / "finalize-started"
    child = Child(tmp_path, ingest, duration=60, script=SLOW_FINALIZE,
                  env={"TEST_FINALIZE_MARKER": str(marker)},
                  creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        child.wait_for(observing(0.5))
        child.write("stop\n")
        child.wait_for(lambda item: item["type"] == "status" and item["phase"] == "complete")
        deadline = time.monotonic() + 10
        while not marker.exists():
            assert time.monotonic() < deadline, "the report was never finalized"
            time.sleep(0.01)
        try:
            os.kill(child.process.pid, signal.CTRL_BREAK_EVENT)
        except OSError as exc:
            child.finish()
            pytest.skip(f"console control events unavailable: {exc}")
        code = child.finish()
    finally:
        child.close()
    rows = child.rows()
    report = child.report()
    assert code == 0  # not 0xC000013A: the default Ctrl+Break action never returned
    assert rows[-1]["type"] == "finished" and rows[-1]["clean"] is True
    assert [row["type"] for row in rows].count("finished") == 1
    assert report["clean"] is True and report["report_saved"] is True
    assert report["run"]["stop"] == "operator"


def test_a_failing_run_still_ends_the_stream_with_finished(tmp_path, ingest):
    child = Child(tmp_path, ingest, duration=5, script=FAILING_RUN)
    try:
        code = child.finish()
    finally:
        child.close()
    rows = child.rows()
    assert code == 1
    assert [row["type"] for row in rows] == ["log", "log", "finished"]
    assert [row["message"] for row in rows[:2]] == [
        "about to fail", "capture failed: RuntimeError"]
    assert rows[1]["level"] == "ERROR"
    assert (rows[-1]["clean"], rows[-1]["report_saved"], rows[-1]["exit_code"]) == (
        False, False, 1)
    assert "RuntimeError: injected failure" in child.stderr_path.read_text(encoding="utf-8")
    assert child.report()["status"] == "incomplete"  # the reservation's placeholder


def test_closed_stdout_pipe_never_makes_the_capture_unclean(tmp_path, ingest, capsys):
    child = Child(tmp_path, ingest, duration=60, pump=False)
    try:
        while True:
            item = strict_json(child.process.stdout.readline())
            if observing(0.5)(item):
                break
        child.process.stdout.close()  # the supervisor's end of the pipe goes away
        time.sleep(0.5)  # at least one status write meets the broken pipe
        child.write("stop\n")
        code = child.process.wait(30)
    finally:
        child.close()
    report = child.report()
    assert code == 0
    assert report["clean"] is True
    assert report["run"]["stop"] == "operator"
    assert report["evidence"]["stream_errors"] == 1
    assert report["evidence"]["write_errors"] == 0
    assert report["evidence"]["unfinished"] == 0
    check_capture_folder(child, [], report)
    assert show_packet_examples(child.folder) is True
    capsys.readouterr()
