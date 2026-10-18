import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from laptop.dual_bridge import _parser
from laptop.reporting import reserve_report


ROOT = Path(__file__).parents[1]


def run_program(program, *, timeout=3):
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(program)], cwd=str(ROOT),
        capture_output=True, text=True, timeout=timeout)


def test_report_reservation_never_overwrites_and_finalizes_exact_json(tmp_path):
    path = tmp_path / "capture.json"
    reservation = reserve_report(path, {"started_at_utc": "2026-09-17T00:00:00Z"})
    placeholder = json.loads(path.read_text(encoding="utf-8"))
    assert placeholder["status"] == "incomplete"
    assert placeholder["started_at_utc"] == "2026-09-17T00:00:00Z"

    final = '{"clean":true,"value":7}\n'
    reservation.finalize(final)
    assert path.read_text(encoding="utf-8") == final
    with pytest.raises(FileExistsError):
        reserve_report(path, {})
    assert path.read_text(encoding="utf-8") == final


def test_interrupted_reservation_remains_explicitly_incomplete(tmp_path):
    path = tmp_path / "interrupted.json"
    reserve_report(path, {"mode": "physical"})

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["status"] == "incomplete"
    assert "clean" not in saved


def test_finalization_refuses_a_replaced_reservation(tmp_path):
    path = tmp_path / "replaced.json"
    reservation = reserve_report(path, {"mode": "physical"})
    path.unlink()
    path.write_text("someone-else", encoding="utf-8")

    with pytest.raises(RuntimeError, match="replaced"):
        reservation.finalize('{"clean":true}\n')
    assert path.read_text(encoding="utf-8") == "someone-else"


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "-inf"])
def test_progress_interval_rejects_negative_or_nonfinite_values(value):
    with pytest.raises(SystemExit):
        _parser().parse_args(["--ca", "unused", "--mock",
                              "--progress-interval", value])


def test_progress_zero_is_allowed():
    args = _parser().parse_args(
        ["--ca", "unused", "--mock", "--progress-interval", "0"])
    assert args.progress_interval == 0


def test_progress_defaults_to_one_second():
    args = _parser().parse_args(["--ca", "unused", "--mock"])
    assert args.progress_interval == 1.0


def test_cli_progress_is_on_stderr_and_saved_report_equals_final_stdout(tmp_path):
    report_path = tmp_path / "capture.json"
    program = rf"""
        import asyncio
        import sys
        from laptop import dual_bridge

        original_run = dual_bridge.DualBridge.run

        class Writer:
            def close(self): pass
            async def wait_closed(self): pass

        async def run_with_local_transport(self, duration=600, **kwargs):
            messages = {{1: [], 2: []}}
            for device, bridge in self.bridges.items():
                async def connect():
                    return object(), Writer()
                async def write(_writer, message, timeout, device=device):
                    messages[device].append(message)
                async def read(_reader, timeout, device=device):
                    message = messages[device][-1]
                    return {{
                        "v": message["v"], "type": "INGEST_ACK",
                        "session_id": message["session_id"],
                        "device_id": message["device_id"],
                        "boot_id": message["boot_id"], "seq": message["seq"],
                        "status": "accepted",
                        "request_id": message["request_id"],
                    }}
                bridge._connector = connect
                bridge._write_frame = write
                bridge._read_frame = read
            kwargs.pop("mock", None)
            kwargs.pop("mock_rate", None)
            return await original_run(self, duration=0.4, mock=True,
                                      mock_rate=100, **kwargs)

        dual_bridge.DualBridge.run = run_with_local_transport
        sys.argv = ["dual_bridge", "--ca", "SECRET-CA", "--mock",
                    "--duration", "0.4", "--expected-rate", "20",
                    "--progress-interval", "0.01", "--report", {str(report_path)!r}]
        raise SystemExit(dual_bridge.main())
    """

    process = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(program)], cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    first = process.stderr.readline()
    second = process.stderr.readline()
    assert "device=1" in first
    assert "device=2" in second
    assert process.poll() is None
    stdout, remaining_stderr = process.communicate(timeout=3)
    completed = subprocess.CompletedProcess(
        process.args, process.returncode, stdout, first + second + remaining_stderr)

    stdout_report = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert report_path.read_text(encoding="utf-8") == completed.stdout
    assert json.loads(report_path.read_text(encoding="utf-8")) == stdout_report
    assert stdout_report["run"]["mode"] == "synthetic"
    assert stdout_report["run"]["requested_duration_seconds"] == 0.4
    assert len(stdout_report["run"]["revision"]) == 40
    assert stdout_report["run"]["finished_at_utc"].endswith("Z")
    assert "progress mode=synthetic" in completed.stderr
    assert "device=1" in completed.stderr
    assert "device=2" in completed.stderr
    assert "SECRET-CA" not in completed.stderr
    assert len(completed.stdout.strip().splitlines()) == 1


def test_existing_report_is_rejected_before_bridge_or_input_initialization(tmp_path):
    report_path = tmp_path / "existing.json"
    report_path.write_text("keep-me", encoding="utf-8")
    program = rf"""
        import sys
        from laptop import dual_bridge

        def forbidden(*args, **kwargs):
            raise AssertionError("Bridge construction proves input initialization began")

        dual_bridge.Bridge = forbidden
        sys.argv = ["dual_bridge", "--ca", "unused", "--mock",
                    "--report", {str(report_path)!r}]
        raise SystemExit(dual_bridge.main())
    """

    completed = run_program(program)

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert report_path.read_text(encoding="utf-8") == "keep-me"
    assert "already exists" in completed.stderr


@pytest.mark.parametrize("target_kind", ["missing-parent", "directory"])
def test_unusable_report_path_is_rejected_before_bridge_initialization(
        tmp_path, target_kind):
    report_path = (tmp_path / "missing" / "capture.json"
                   if target_kind == "missing-parent" else tmp_path)
    program = rf"""
        import sys
        from laptop import dual_bridge

        def forbidden(*args, **kwargs):
            raise AssertionError("Bridge construction proves input initialization began")

        dual_bridge.Bridge = forbidden
        sys.argv = ["dual_bridge", "--ca", "unused", "--mock",
                    "--report", {str(report_path)!r}]
        raise SystemExit(dual_bridge.main())
    """

    completed = run_program(program)

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "report reservation failed" in completed.stderr


def test_report_finalization_failure_forces_nonzero_and_nonclean_stdout(tmp_path):
    report_path = tmp_path / "capture.json"
    program = rf"""
        import sys
        from laptop import dual_bridge
        import laptop.reporting

        async def clean_run(self, duration=600, **kwargs):
            return {{"clean": True, "mock_input": True, "devices": {{}}}}

        def fail(self, payload):
            raise OSError("SECRET write detail")

        dual_bridge.DualBridge.run = clean_run
        laptop.reporting.ReportReservation.finalize = fail
        sys.argv = ["dual_bridge", "--ca", "unused", "--mock",
                    "--progress-interval", "0", "--report", {str(report_path)!r}]
        raise SystemExit(dual_bridge.main())
    """

    completed = run_program(program)

    stdout_report = json.loads(completed.stdout)
    assert completed.returncode != 0
    assert stdout_report["clean"] is False
    assert stdout_report["report_saved"] is False
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "incomplete"
    assert "report write failed" in completed.stderr
    assert "SECRET write detail" not in completed.stderr
