"""The short demo commands preserve capture evidence and fail closed offline."""

import builtins
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys

import pytest

import demo


def physical_report():
    devices = {}
    for device_id, count in ((1, 610), (2, 630)):
        devices[str(device_id)] = {
            "clean": True,
            "protected_ble": True,
            "unfinished": False,
            "input": "physical",
            "ble_connections": 1,
            "transport_connections": 1,
            "received": count,
            "sent": count,
            "acked": count,
            "source": {
                "device_id": device_id,
                "boot_id": 7,
                "complete": True,
                "clean": True,
                "generated": count,
                "source_submitted": count,
                "source_failures": 0,
                "source_consistent": True,
                "received": count,
                "acked": count,
                "missing_received": 0,
                "missing_acked": 0,
            },
        }
    return {
        "clean": True,
        "mock_input": False,
        "report_saved": True,
        "session_id": "ltc-comms",
        "devices": devices,
        "run": {"mode": "physical", "requested_duration_seconds": 60.0},
    }


@pytest.fixture
def dummy_ca(tmp_path):
    path = tmp_path / "dummy-ca.pem"
    path.write_text("TEST FIXTURE: never used for TLS", encoding="utf-8")
    return path


def packet_lines():
    rows = []
    for device in (1, 2):
        identity = dict(device_id=device, boot_id=7, seq=0)
        rows.append(dict(identity, type="sensor", direction="ESP->laptop", version=2,
                         uptime_ms=100, values=list(range(8)), validation="decoded"))
        rows.append(dict(identity, type="sensor_ack", direction="Ultra96->laptop",
                         validation="accepted"))
    return "".join(json.dumps(row) + "\n" for row in rows)


def child_command(report_path, payload, exit_code=0, *, check_live=False):
    """A real local child emits both streams and writes the requested evidence."""
    program = """
import json
from pathlib import Path
import sys
import time

path = Path(sys.argv[1])
print("child stdout: progress 1", flush=True)
print("child stderr: progress 2", file=sys.stderr, flush=True)
if sys.argv[4] == "live":
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        log = path.with_name("live.log")
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        if "child stdout: progress 1" in text and "child stderr: progress 2" in text:
            break
        time.sleep(0.01)
    else:
        print("live.log was not flushed before child completion", flush=True)
        raise SystemExit(19)
if sys.argv[2] != "MISSING":
    path.write_text(sys.argv[2], encoding="utf-8")
path.with_name("packets.jsonl").write_text(sys.argv[5], encoding="utf-8")
raise SystemExit(int(sys.argv[3]))
"""
    encoded = "MISSING" if payload is None else (
        payload if isinstance(payload, str) else json.dumps(payload))
    return [sys.executable, "-u", "-c", program, str(report_path), encoded,
            str(exit_code), "live" if check_live else "normal", packet_lines()]


def install_child(monkeypatch, payload, exit_code=0, *, check_live=False):
    monkeypatch.setattr(
        demo, "capture_command",
        lambda args, report_path: child_command(
            report_path, payload, exit_code, check_live=check_live))


def saved_capture(root, name="B07-saved", payload=None, exit_code="0"):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "packets.jsonl").write_text(packet_lines(), encoding="utf-8")
    report = directory / "report.json"
    report.write_text(json.dumps(physical_report() if payload is None else payload),
                      encoding="utf-8")
    if exit_code is not None:
        (directory / "exit-code.txt").write_text(str(exit_code), encoding="utf-8")
    return directory


def assert_not_passed(output):
    assert "CAPTURE PASSED" not in output
    assert "Phone expected increase:" not in output


def cli_status(argv):
    # argparse may report invalid input via SystemExit; both entry paths must fail.
    try:
        return demo.main(argv)
    except SystemExit as exc:
        return exc.code


def test_run_streams_both_child_streams_and_saves_a_unique_capture(
        tmp_path, dummy_ca, monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", lambda _: pytest.fail("Capture must not ask for phone counters"))
    install_child(monkeypatch, physical_report(), check_live=True)
    root = tmp_path / "captures"
    argv = ["run", "--ca", str(dummy_ca), "--output-root", str(root)]

    assert demo.main(argv) == 0
    first_output = capsys.readouterr().out
    first = next(root.glob("B07-*"))
    original_log = (first / "live.log").read_text(encoding="utf-8")
    assert "child stdout: progress 1" in first_output
    assert "child stderr: progress 2" in first_output
    assert "child stdout: progress 1" in original_log
    assert "child stderr: progress 2" in original_log
    assert json.loads((first / "report.json").read_text(encoding="utf-8")) == physical_report()
    assert (first / "exit-code.txt").read_text(encoding="utf-8").strip() == "0"
    assert "CAPTURE PASSED" in first_output
    assert "Phone expected increase: 1240" in first_output
    assert "Generated" in first_output and "Received" in first_output and "ACKed" in first_output
    assert "Matched identity:" in first_output and "Sensor:" in first_output
    assert not list(first.glob("phone-observation-*.json"))

    assert demo.main(argv) == 0
    assert len(list(root.glob("B07-*"))) == 2
    assert (first / "live.log").read_text(encoding="utf-8") == original_log


def test_run_defaults_build_the_existing_physical_dual_bridge_command(
        tmp_path, dummy_ca, monkeypatch):
    monkeypatch.delenv("LTC_COMMS_SESSION", raising=False)
    emitted = []
    original_command = demo.capture_command

    def safe_command(args, report_path):
        emitted.append(original_command(args, report_path))
        return child_command(report_path, physical_report())

    monkeypatch.setattr(demo, "capture_command", safe_command)
    root = tmp_path / "captures"
    assert demo.main(["run", "--ca", str(dummy_ca), "--output-root", str(root)]) == 0
    command = emitted[0]
    assert command[:4] == [sys.executable, "-u", "-m", "laptop.dual_bridge"]
    options = dict(zip(command[4::2], command[5::2]))
    assert options["--ca"] == str(dummy_ca)
    assert options["--left-address"] == "38:18:2B:19:82:AE"
    assert options["--right-address"] == "38:18:2B:18:9D:6A"
    assert options["--port"] == "18889"
    assert float(options["--duration"]) == 60
    assert options["--session-id"] == "ltc-comms"
    assert Path(options["--report"]).parent.parent == root
    assert Path(options["--report"]).name == "report.json"
    assert "--mock" not in command
    assert "--diagnostic-unprotected" not in command


def test_explicit_capture_options_reach_the_existing_bridge(tmp_path):
    report_path = tmp_path / "report.json"
    args = demo._parser().parse_args([
        "run", "--duration", "2.5", "--ca", str(tmp_path / "custom ca.pem"),
        "--left-address", "left-device", "--right-address", "right-device",
        "--port", "19999", "--output-root", str(tmp_path)])
    command = demo.capture_command(args, report_path)
    options = dict(zip(command[4::2], command[5::2]))
    assert float(options["--duration"]) == 2.5
    assert options["--left-address"] == "left-device"
    assert options["--right-address"] == "right-device"
    assert options["--port"] == "19999"
    assert options["--ca"] == str(tmp_path / "custom ca.pem")
    assert options["--report"] == str(report_path)


@pytest.mark.parametrize("mode", ["run", "live"])
@pytest.mark.parametrize("environment, extra, expected", [
    (None, [], "ltc-comms"),
    ("week7-demo", [], "week7-demo"),
    ("another-session", ["--session-id", "week7-demo"], "week7-demo"),
])
def test_capture_session_selection(tmp_path, monkeypatch, mode, environment, extra, expected):
    if environment is None:
        monkeypatch.delenv("LTC_COMMS_SESSION", raising=False)
    else:
        monkeypatch.setenv("LTC_COMMS_SESSION", environment)
    args = demo._parser().parse_args([mode, *extra])
    command = demo.capture_command(args, tmp_path / "report.json")
    assert command[command.index("--session-id") + 1] == expected


def test_capture_defaults_to_comms_local():
    assert demo._parser().parse_args(["run"]).output_root == demo.ROOT / ".comms-local"


def test_latest_report_finds_existing_legacy_capture(tmp_path, monkeypatch):
    new_root = tmp_path / ".comms-local"
    legacy = saved_capture(tmp_path / ".week7-local")
    monkeypatch.setattr(demo, "OUTPUT_ROOT", new_root)
    assert demo.latest_report(new_root) == legacy / "report.json"


def test_nonzero_child_exit_survives_capture_and_saved_report_review(
        tmp_path, dummy_ca, monkeypatch, capsys):
    install_child(monkeypatch, physical_report(), exit_code=7)
    root = tmp_path / "captures"
    assert demo.main(["run", "--ca", str(dummy_ca), "--output-root", str(root)]) == 7
    capture = next(root.glob("B07-*"))
    assert (capture / "exit-code.txt").read_text(encoding="utf-8").strip() == "7"
    assert_not_passed(capsys.readouterr().out)

    assert demo.main(["report", str(capture)]) != 0
    assert_not_passed(capsys.readouterr().out)


@pytest.mark.parametrize("payload", [None, "{broken", [], {"status": "incomplete"}])
def test_zero_child_exit_with_missing_or_invalid_evidence_still_fails(
        tmp_path, dummy_ca, monkeypatch, capsys, payload):
    install_child(monkeypatch, payload)
    root = tmp_path / "captures"
    assert demo.main(["run", "--ca", str(dummy_ca), "--output-root", str(root)]) != 0
    capture = next(root.glob("B07-*"))
    assert (capture / "exit-code.txt").read_text(encoding="utf-8").strip() == "0"
    assert_not_passed(capsys.readouterr().out)


@pytest.mark.parametrize("path_kind", ["directory", "json"])
def test_report_reads_saved_evidence_without_starting_capture(
        tmp_path, monkeypatch, capsys, path_kind):
    capture = saved_capture(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("The report command tried to start capture hardware")

    monkeypatch.setattr(demo, "capture_command", forbidden)
    path = capture if path_kind == "directory" else capture / "report.json"
    assert demo.main(["report", str(path)]) == 0
    output = capsys.readouterr().out
    assert "CAPTURE PASSED" in output
    assert "Phone expected increase: 1240" in output


@pytest.mark.parametrize("latest_kind", ["missing", "corrupt", "incomplete"])
def test_report_uses_the_latest_directory_even_when_an_older_capture_passed(
        tmp_path, capsys, latest_kind):
    older = saved_capture(tmp_path, "B07-older")
    latest = tmp_path / "B07-latest"
    latest.mkdir()
    if latest_kind != "missing":
        (latest / "report.json").write_text(
            "{broken" if latest_kind == "corrupt" else '{"status":"incomplete"}',
            encoding="utf-8")
    os.utime(older, (100, 100))
    os.utime(latest, (200, 200))

    assert demo.main(["report", "--output-root", str(tmp_path)]) != 0
    assert_not_passed(capsys.readouterr().out)


@pytest.mark.parametrize("exit_code", [None, "not-an-exit-code", "7"])
def test_report_cannot_pass_without_a_saved_successful_child_exit(
        tmp_path, capsys, exit_code):
    capture = saved_capture(tmp_path, exit_code=exit_code)
    assert demo.main(["report", str(capture)]) != 0
    assert_not_passed(capsys.readouterr().out)


@pytest.mark.parametrize("field,value", [
    (("clean",), False),
    (("report_saved",), False),
    (("mock_input",), True),
    (("devices", "2", "clean"), False),
    (("devices", "2", "protected_ble"), False),
    (("devices", "2", "unfinished"), True),
    (("devices", "2", "source"), None),
    (("devices", "2", "source", "complete"), False),
    (("devices", "2", "source", "clean"), False),
    (("devices", "2", "source", "generated"), True),
    (("devices", "2", "source", "generated"), 0),
    (("devices", "2", "source", "generated"), 630.0),
    (("devices", "2", "source", "source_submitted"), 629),
    (("devices", "2", "source", "received"), 629),
    (("devices", "2", "source", "acked"), 629),
    (("devices", "2", "source", "missing_received"), 1),
    (("devices", "2", "source", "missing_acked"), 1),
    (("devices", "2", "received"), 629),
    (("devices", "2", "sent"), 629),
    (("devices", "2", "acked"), 629),
])
def test_unverified_physical_counts_never_produce_a_pass_or_phone_total(
        tmp_path, capsys, field, value):
    report = copy.deepcopy(physical_report())
    target = report
    for key in field[:-1]:
        target = target[key]
    target[field[-1]] = value
    if field[-1] == "generated" and (value is True or value == 0):
        # Equal counts must still fail when they are bools or zero: equality alone
        # is insufficient to establish a positive integer capture count.
        source = report["devices"]["2"]["source"]
        for key in ("source_submitted", "received", "acked"):
            source[key] = value
        for key in ("received", "sent", "acked"):
            report["devices"]["2"][key] = value
    capture = saved_capture(tmp_path, payload=report)
    assert demo.main(["report", str(capture)]) != 0
    assert_not_passed(capsys.readouterr().out)


@pytest.mark.parametrize("duration", ["0", "-1", "nan", "inf", "-inf"])
def test_nonpositive_or_nonfinite_duration_is_rejected_before_capture(
        tmp_path, dummy_ca, monkeypatch, duration):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid duration reached capture setup")

    monkeypatch.setattr(demo, "capture_command", forbidden)
    assert cli_status(["run", "--duration=" + duration, "--ca", str(dummy_ca),
                       "--output-root", str(tmp_path / "captures")]) != 0


def test_missing_ca_is_rejected_before_creating_a_capture(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Missing CA reached capture setup")

    monkeypatch.setattr(demo, "capture_command", forbidden)
    root = tmp_path / "captures"
    assert cli_status(["run", "--ca", str(tmp_path / "missing.pem"),
                       "--output-root", str(root)]) != 0
    assert not root.exists() or not list(root.iterdir())


def test_tunnel_preserves_the_ssh_process_exit_code(monkeypatch):
    monkeypatch.setattr(demo, "tunnel_command", lambda *args, **kwargs: [
        sys.executable, "-c", "raise SystemExit(7)"])
    assert demo.main(["tunnel"]) == 7


def test_interrupt_stops_owned_child_and_cannot_pass_its_saved_report(
        tmp_path, dummy_ca, monkeypatch, capsys):
    children = []
    real_popen = demo.subprocess.Popen
    interrupted = False

    def spawn(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def interrupt_on_ready(*args, **kwargs):
        nonlocal interrupted
        if not interrupted and args and str(args[0]).strip() == "READY":
            interrupted = True
            raise KeyboardInterrupt
        return builtins.print(*args, **kwargs)

    def waiting_child(args, report_path):
        program = (
            "from pathlib import Path; import sys, time; "
            "Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8'); "
            "print('READY', flush=True); time.sleep(30)"
        )
        return [sys.executable, "-u", "-c", program, str(report_path),
                json.dumps(physical_report())]

    monkeypatch.setattr(demo, "capture_command", waiting_child)
    monkeypatch.setattr(demo.subprocess, "Popen", spawn)
    monkeypatch.setattr(demo, "print", interrupt_on_ready, raising=False)
    root = tmp_path / "captures"
    try:
        assert demo.main(["run", "--ca", str(dummy_ca), "--output-root", str(root)]) == 130
        assert interrupted
        assert len(children) == 1
        assert children[0].poll() is not None, "Interrupted capture left its child running"
        capture = next(root.glob("B07-*"))
        assert (capture / "exit-code.txt").read_text(encoding="utf-8").strip() == "130"
        assert "READY" in (capture / "live.log").read_text(encoding="utf-8")
        assert_not_passed(capsys.readouterr().out)
        assert demo.main(["report", str(capture)]) != 0
        assert_not_passed(capsys.readouterr().out)
    finally:
        # A regression must fail the assertion without leaking the test child.
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


def test_log_close_failure_cannot_pass_an_otherwise_clean_child_report(
        tmp_path, dummy_ca, monkeypatch, capsys):
    install_child(monkeypatch, physical_report())
    real_open = Path.open

    @contextmanager
    def failing_close(path, *args, **kwargs):
        with real_open(path, *args, **kwargs) as stream:
            yield stream
        raise OSError("simulated log close failure")

    def open_with_close_failure(path, *args, **kwargs):
        if path.name == "live.log" and args and args[0] == "x":
            return failing_close(path, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_with_close_failure)
    root = tmp_path / "captures"
    assert demo.main(["run", "--ca", str(dummy_ca), "--output-root", str(root)]) != 0
    capture = next(root.glob("B07-*"))
    assert (capture / "exit-code.txt").read_text(encoding="utf-8").strip() != "0"
    assert_not_passed(capsys.readouterr().out)
