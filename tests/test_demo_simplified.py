"""The short demo commands review evidence without asking for phone counters."""
import builtins
from pathlib import Path
import subprocess
import sys

import pytest

import demo
from test_video_evidence import capture, packets, sensor, ack


@pytest.mark.parametrize("mode,seconds,keyboard", [("run", 60, False), ("live", 120, True)])
def test_short_modes_have_explicit_source_rate_and_preserve_bridge_options(mode, seconds, keyboard):
    args = demo._parser().parse_args([mode])
    assert (args.duration, args.rate, args.keyboard) == (seconds, 10, keyboard)
    command = demo.capture_command(args, Path("capture/report.json"))
    assert command[command.index("--rate") + 1] == "10"
    assert ("--keyboard" in command) is keyboard
    assert "--mock" not in command
    changed = demo._parser().parse_args([mode, "--duration", "90", "--rate", "50"])
    assert (changed.duration, changed.rate) == (90, 50)


def test_report_prints_matching_packets_without_input_or_phone_observation_files(tmp_path, monkeypatch, capsys):
    folder = capture(tmp_path)
    packets(folder, [sensor(1), ack(1), sensor(2), ack(2)])
    monkeypatch.setattr(builtins, "input", lambda _: pytest.fail("Phone count prompt is obsolete"))
    before = {path.name for path in folder.iterdir()}
    assert demo.main(["report", str(folder)]) == 0
    output = capsys.readouterr().out
    assert "Matched identity:" in output and "Sensor:" in output and "ACK:" in output
    assert "Phone expected increase: 7" in output
    assert "camera" in output.lower() and "not checked" in output
    assert "MATCH:" not in output
    assert {path.name for path in folder.iterdir()} == before


@pytest.mark.parametrize("bad_packets", [None, "{broken\n"])
def test_packet_evidence_failure_cannot_pass_review(tmp_path, capsys, bad_packets):
    folder = capture(tmp_path)
    if bad_packets is not None:
        (folder / "packets.jsonl").write_text(bad_packets, encoding="utf-8")
    assert demo.main(["report", str(folder)]) != 0
    output = capsys.readouterr().out
    assert "CAPTURE PASSED" not in output and "Phone expected increase:" not in output


def test_live_dispatches_capture_and_service_dispatches_maintenance(monkeypatch):
    calls = []
    monkeypatch.setattr(demo, "run_capture", lambda args: calls.append(args) or 7)
    assert demo.main(["live"]) == 7
    assert calls[0].keyboard is True
    from tools import demo_service
    monkeypatch.setattr(demo_service, "main", lambda start=False: calls.append(start) or 8)
    assert demo.main(["service"]) == 8
    assert calls[-1] is False
    assert demo.main(["service", "--start"]) == 8
    assert calls[-1] is True


@pytest.mark.parametrize("mode", ["run", "live", "report", "service"])
def test_help_does_not_access_devices_or_services_outside_repo(tmp_path, mode):
    result = subprocess.run([sys.executable, str(Path(demo.__file__)), mode, "--help"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
