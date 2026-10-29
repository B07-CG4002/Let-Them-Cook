"""Sensor version 4 (the three-flex boards) through the laptop launcher and its tools.

Version 5 (the same boards with the wide gyro range) has its own checks in
test_v5_transport.py; the shared paths here also name it where version 4 is no longer
the only twelve-channel version.

Every check here is local: synthetic (--mock) input or recorded rows, a CommsServer on
this machine and no BLE, SSH, Ultra96 or phone. Version 3 stays the default everywhere,
with unchanged command lines; these tests pin that too.
"""
import asyncio
import csv
import functools
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

import demo
from common import sensor, sensor_v4
from common.tls import server_context
from laptop import dual_bridge
from laptop.bridge import Bridge, BridgeConfig
from laptop.dual_bridge import DualBridge, _parser
from tools import check_board, export_capture, live_session
from tools.live_view import RunState, header_lines, values_lines, values_text
from tools.video_evidence import _json as strict_json
from tools.video_evidence import show_packet_examples
from ultra96.server import CommsServer

from test_dual_bridge import async_test, install_ack_transport
from test_dual_supervised import FAST_MOCK, Child
from test_live_session import FakeConsole, LocalBoard, run_folder, start_args
from test_live_session import pki  # noqa: F401  (the module-scoped PKI fixture)
from test_video_evidence import ack, capture, packets
from test_video_evidence import sensor as v3_sensor

ROOT = Path(__file__).parents[1]
LEFT, RIGHT = "38:18:2B:19:82:AE", "38:18:2B:18:9D:6A"
ROW12 = [1200, -300, -8500, 4000, -2000, 1500, 1250, -3000, 600, 6000, 5500, 5000]


def v4_config(device, **extra):
    return BridgeConfig(ca_file="unused", diagnostic_unprotected=True, expected_device_id=device,
                        source_audit=True, sensor_version=4, **extra)


# --- laptop.dual_bridge ---------------------------------------------------------------------

def test_dual_bridge_option_defaults_to_3_and_accepts_only_3_4_or_5():
    assert _parser().parse_args(["--ca", "unused", "--mock"]).sensor_version == 3
    for version in (4, 5):
        assert _parser().parse_args(["--ca", "unused", "--mock", "--sensor-version", str(version)]
                                    ).sensor_version == version
    for bad in ("2", "6", "four"):
        with pytest.raises(SystemExit):
            _parser().parse_args(["--ca", "unused", "--mock", "--sensor-version", bad])


@async_test
async def test_dual_bridge_refuses_two_boards_of_different_sensor_versions():
    left = Bridge(BridgeConfig(ca_file="unused", diagnostic_unprotected=True,
                               expected_device_id=1, source_audit=True))
    with pytest.raises(ValueError, match="same sensor version"):
        DualBridge(left, Bridge(v4_config(2)))


@async_test
async def test_a_v4_mock_dual_run_sends_40_byte_12_value_packets_and_reconciles():
    left, right = Bridge(v4_config(1)), Bridge(v4_config(2))
    sent = {1: install_ack_transport(left), 2: install_ack_transport(right)}
    report = await DualBridge(left, right, expected_rate=20, startup_timeout=1,
                              drain_timeout=1).run(duration=0.15, mock=True, mock_rate=100)

    assert report["clean"] is True
    for device, messages in sent.items():
        assert messages and {message["v"] for message in messages} == {4}
        assert {len(message["values"]) for message in messages} == {12}
        assert {tuple(message["values"]) for message in messages} <= set(sensor_v4.DUMMY_FIXTURES)
        item = report["devices"][str(device)]
        assert item["configuration"]["sensor_version"] == 4
        assert item["source"]["generated"] == item["received"] == item["acked"] > 0
        goodput = item["sensor_goodput"]
        assert goodput["packet_bytes"] == 40 * goodput["packets"]


def test_the_cli_gives_both_bridges_version_4_including_the_command_path(monkeypatch, capsys):
    """--rate enables the protected control path; a submitted command is a v4 packet."""
    seen = {}

    async def observe(self, duration=600.0, **kwargs):
        for device, bridge in self.bridges.items():
            bridge.control.client = object()  # as if BLE were connected
            assert bridge.submit_command() is True
            packet, _ = bridge.commands.queue.get_nowait()
            seen[device] = (bridge.config, bridge.control.sensor_version, packet)
        return {"clean": False, "devices": {}}

    monkeypatch.setattr(dual_bridge.DualBridge, "run", observe)
    monkeypatch.setattr(sys, "argv", [
        "dual_bridge", "--ca", "unused", "--left-address", LEFT, "--right-address", RIGHT,
        "--rate", "50", "--duration", "1", "--sensor-version", "4"])
    assert dual_bridge.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["run"]["sensor_version"] == 4
    for device in (1, 2):
        config, control_version, packet = seen[device]
        assert (config.sensor_version, config.controls_enabled, config.source_rate) == (4, True, 50)
        assert control_version == 4
        assert isinstance(packet, sensor_v4.SensorPacket) and packet.device_id == device
        assert len(sensor_v4.encode_packet(packet)) == 40


@pytest.fixture(scope="module")
def v4_ingest(tmp_path_factory):
    """A version 4 CommsServer on its own thread: (CA path, ingest port)."""
    from tools.generate_comms_pki import generate_pki
    pki_folder = tmp_path_factory.mktemp("v4-pki")
    generate_pki(pki_folder)
    started = threading.Event()
    state = {}

    def serve():
        async def main():
            server = CommsServer(server_context(
                pki_folder / "server-cert.pem", pki_folder / "server-key.pem"),
                ingest_port=0, gateway_port=0, sensor_version=4)
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

    thread = threading.Thread(target=serve, name="test-ultra96-v4", daemon=True)
    thread.start()
    assert started.wait(10) and "port" in state
    yield pki_folder / "ca-cert.pem", state["port"]
    state["loop"].call_soon_threadsafe(state["stop"].set)
    thread.join(10)


def test_a_supervised_v4_mock_capture_end_to_end_passes_the_packet_check_and_exports_flex3(
        tmp_path, v4_ingest, capsys):
    # test_dual_supervised's child (stdin stays open: its end would stop the capture early),
    # with --sensor-version 4 added to its command line.
    old = 'sys.argv = ["dual_bridge"] + sys.argv[1:]'
    assert old in FAST_MOCK
    script = FAST_MOCK.replace(old, old + ' + ["--sensor-version", "4"]')
    child = Child(tmp_path, v4_ingest, duration=1.5, script=script)
    try:
        code = child.finish()
    finally:
        child.close()
    rows = child.rows()
    report = child.report()
    folder = child.folder
    assert child.stderr_path.read_text(encoding="utf-8") == ""
    assert rows[-1]["type"] == "finished" and rows[-1]["exit_code"] == code == 0
    assert report["clean"] is True and report["run"]["sensor_version"] == 4
    for device in ("1", "2"):
        item = report["devices"][device]
        assert item["configuration"]["sensor_version"] == 4
        assert item["source"]["clean"] is True
        assert item["source"]["generated"] == item["received"] == item["acked"]
        assert item["sensor_goodput"]["packet_bytes"] == 40 * item["sensor_goodput"]["packets"]

    records = [strict_json(line) for line in
               (folder / "packets.jsonl").read_text(encoding="utf-8").splitlines()]
    sensors = [row for row in records if row["type"] == "sensor"]
    assert sensors and {row["version"] for row in sensors} == {4}
    assert {len(row["values"]) for row in sensors} == {12}
    assert {len(bytes.fromhex(row["raw_hex"])) for row in sensors} == {40}
    assert {bytes.fromhex(row["raw_hex"])[2] for row in sensors} == {4}

    capsys.readouterr()
    assert show_packet_examples(folder) is True
    output = capsys.readouterr().out
    assert "protocol version 4 (40 bytes, 12 values" in output and "flex3" in output

    assert export_capture.main([str(folder), "--out", str(tmp_path / "samples.csv")]) == 0
    table = list(csv.reader((tmp_path / "samples.csv").open(encoding="utf-8")))
    assert table[0][-3:] == ["flex1", "flex2", "flex3"] and len(table[0]) == 5 + 12
    assert len(table) - 1 == sum(report["devices"][d]["source"]["generated"] for d in ("1", "2"))
    assert all(len(row) == 17 for row in table[1:])


# --- python demo.py start (tools.live_session and tools.live_view) ---------------------------

def test_start_names_the_sensor_version_only_for_version_4(tmp_path):
    base = ["start", "--left-address", LEFT, "--right-address", RIGHT]
    v3 = demo._parser().parse_args(base)
    assert v3.sensor_version == 3
    command = live_session.capture_command(v3, tmp_path / "report.json")
    assert "--sensor-version" not in command
    v4 = demo._parser().parse_args(base + ["--sensor-version", "4"])
    command4 = live_session.capture_command(v4, tmp_path / "report.json")
    assert command4[-5:] == ["--sensor-version", "4", "--progress-interval", "0", "--supervised"]
    assert [part for part in command4 if part not in ("--sensor-version", "4")] == command
    with pytest.raises(SystemExit):
        demo._parser().parse_args(base + ["--sensor-version", "6"])


@pytest.mark.parametrize("mode", ["run", "live"])
def test_run_and_live_pass_version_4_to_the_capture_and_keep_version_3_lines(mode, tmp_path):
    v3 = demo._parser().parse_args([mode])
    assert v3.sensor_version == 3
    command = demo.capture_command(v3, tmp_path / "report.json")
    assert "--sensor-version" not in command
    v4 = demo._parser().parse_args([mode, "--sensor-version", "4"])
    command4 = demo.capture_command(v4, tmp_path / "report.json")
    assert command4 == command + ["--sensor-version", "4"]


def test_help_names_the_three_flex_boards_and_the_v4_service():
    for mode in ("start", "run", "live"):
        # Wide help lines: argparse would otherwise wrap at the hyphen of --sensor-version.
        completed = subprocess.run([sys.executable, "demo.py", mode, "--help"], cwd=str(ROOT),
                                   capture_output=True, text=True, timeout=30,
                                   env=dict(os.environ, COLUMNS="400"))
        assert completed.returncode == 0
        text = " ".join(completed.stdout.split())
        assert "--sensor-version {3,4,5}" in text and "flash.py --flex3" in text
        assert "flash.py --wide-gyro" in text
        assert "tools.deploy_service with the same --sensor-version" in text


def test_values_are_labelled_by_their_own_version():
    text = values_text(ROW12, 4)
    assert "flex1 0.6000 flex2 0.5500 flex3 0.5000 0..1" in text
    assert values_text(ROW12[:11]) == values_text(ROW12[:11], 3)
    assert "flex3" not in values_text(ROW12[:11])
    assert values_text(ROW12).startswith("values ")  # 12 values are not a version 3 packet
    assert values_text(ROW12, 6).startswith("values ")  # no such version
    assert values_lines(ROW12, 4)[1].endswith("flex3 0.5000 0..1")


def test_header_and_title_name_the_sensor_version():
    options = dict(folder="F", started=1_800_000_000.0, command="python demo.py start",
                   revision="abc", session_id="ltc-comms", rate_hz=50, duration=None,
                   keyboard=False, addresses={1: "L", 2: "R"}, tunnel="T")
    assert any("sensor packets v3 (38 bytes, 11 values)" in line
               for line in header_lines(**options))
    assert any("sensor packets v4 (40 bytes, 12 values)" in line
               for line in header_lines(**options, sensor_version=4))
    run = dict(folder="F", addresses={1: "L", 2: "R"}, rate_hz=50, session_id="ltc-comms")
    assert "v4 packets" not in RunState(**run).render(0.0)[0]
    assert "50 Hz  v4 packets  session ltc-comms" in RunState(**run, sensor_version=4).render(0.0)[0]


class V4Board(LocalBoard):
    """test_live_session's local service and phone, with a version 4 service."""

    async def _start(self):
        import ultra96.server as server
        original = server.CommsServer
        server.CommsServer = functools.partial(original, sensor_version=4)
        try:
            await super()._start()
        finally:
            server.CommsServer = original


def test_a_v4_start_run_logs_flex3_values_and_its_capture_passes_the_packet_check(
        pki, tmp_path, capsys):  # noqa: F811
    with V4Board(pki[0]) as board:
        console = FakeConsole()
        args = start_args(pki, board, tmp_path, "--duration", "3", "--sensor-version", "4")
        live_session.main(args, console=console)
        phone_results = board.results
    folder = run_folder(tmp_path)
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    assert report["mock_input"] is True and report["run"]["sensor_version"] == 4
    for device in ("1", "2"):
        assert report["devices"][device]["source"]["complete"] is True
        assert report["devices"][device]["runtime_failures"] == []
    log = (folder / "run.log").read_text(encoding="utf-8")
    assert "sensor packets v4 (40 bytes, 12 values)" in log
    packet_lines = [line for line in log.splitlines() if "LEFT   packet #" in line]
    assert packet_lines and all("flex3" in line for line in packet_lines)
    assert "LEFT   result #" in log and phone_results > 0
    assert any("v4 packets" in frame[0] for frame in console.frames)
    capsys.readouterr()
    assert show_packet_examples(folder) is True


# --- tools.video_evidence and tools.export_capture --------------------------------------------

def v4_sensor(device, *, boot=None, seq=7, values=None):
    boot = 100 + device if boot is None else boot
    values = list(ROW12 if values is None else values)
    raw = sensor_v4.encode_packet(sensor_v4.SensorPacket(device, boot, seq, 100, tuple(values)))
    return dict(v3_sensor(device, boot=boot, seq=seq), version=4, values=values, raw_hex=raw.hex())


def named_version(folder, version):
    path = folder / "report.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["run"]["sensor_version"] = version
    for device in report["devices"].values():
        device["configuration"] = {"sensor_version": version}
    path.write_text(json.dumps(report), encoding="utf-8")


@pytest.mark.parametrize("named", [None, 4])
def test_a_v4_capture_is_read_with_the_v4_codec(tmp_path, capsys, named):
    folder = capture(tmp_path)
    if named:
        named_version(folder, named)
    packets(folder, [v4_sensor(1), ack(1), v4_sensor(2), dict(ack(2), version=4)])
    assert show_packet_examples(folder) is True
    output = capsys.readouterr().out
    assert "protocol version 4 (40 bytes, 12 values" in output
    assert ", ".join(str(value) for value in ROW12) in output


def test_demo_report_passes_a_clean_physical_v4_capture(tmp_path, capsys):
    folder = capture(tmp_path)
    named_version(folder, 4)
    packets(folder, [v4_sensor(1), ack(1), v4_sensor(2), ack(2)])
    assert demo.main(["report", str(folder)]) == 0
    output = capsys.readouterr().out
    assert "protocol version 4 (40 bytes, 12 values" in output
    assert "CAPTURE PASSED" in output and "Phone expected increase: 7" in output


@pytest.mark.parametrize("case", ["v3_rows_in_v4_report", "v4_rows_in_v3_report", "mixed_rows",
                                  "v3_ack_for_v4", "raw_version_byte", "report_disagrees"])
def test_a_capture_never_mixes_versions_and_raw_bytes_must_agree(tmp_path, capsys, case):
    folder = capture(tmp_path)
    rows = [v4_sensor(1), ack(1), v4_sensor(2), ack(2)]
    if case == "v3_rows_in_v4_report":
        named_version(folder, 4)
        rows = [v3_sensor(1), ack(1), v3_sensor(2), ack(2)]
    elif case == "v4_rows_in_v3_report":
        named_version(folder, 3)
    elif case == "mixed_rows":
        rows = [v3_sensor(1), ack(1), v4_sensor(2), ack(2)]
    elif case == "v3_ack_for_v4":
        rows[1] = dict(ack(1), version=3)
    elif case == "raw_version_byte":
        raw = bytearray(bytes.fromhex(rows[0]["raw_hex"]))
        raw[2] = 3
        rows[0] = dict(rows[0], raw_hex=raw.hex())
    else:
        named_version(folder, 4)
        report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
        report["devices"]["2"]["configuration"]["sensor_version"] = 3
        (folder / "report.json").write_text(json.dumps(report), encoding="utf-8")
    packets(folder, rows)
    assert show_packet_examples(folder) is False
    assert "NOT PASSED" in capsys.readouterr().out


def test_export_of_a_v4_capture_adds_the_flex3_column(tmp_path):
    folder = capture(tmp_path)
    packets(folder, [v4_sensor(1, seq=0), v4_sensor(2, seq=0), v4_sensor(1, seq=0),
                     v4_sensor(1, seq=1)])
    out = tmp_path / "samples.csv"
    assert export_capture.export(folder / "packets.jsonl", out) == 3
    rows = list(csv.reader(out.open(encoding="utf-8")))
    assert rows[0] == ["device_id", "glove", "boot_id", "seq", "uptime_ms", *sensor_v4.CHANNELS]
    assert rows[1][5:] == ["{:.4f}".format(value / scale)
                           for value, scale in zip(ROW12, sensor_v4.SCALES)]
    assert rows[1][-1] == "0.5000"


@pytest.mark.parametrize("case", ["mixed", "report_says_4", "report_disagrees"])
def test_export_refuses_a_capture_whose_samples_disagree_on_the_version(tmp_path, capsys, case):
    folder = capture(tmp_path)
    if case == "mixed":
        packets(folder, [v4_sensor(1, seq=0), v3_sensor(2)])
    elif case == "report_says_4":
        named_version(folder, 4)
        packets(folder, [v3_sensor(1), v3_sensor(2)])
    else:  # demo.py report refuses this capture; the export must too
        named_version(folder, 4)
        report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
        report["devices"]["2"]["configuration"]["sensor_version"] = 3
        (folder / "report.json").write_text(json.dumps(report), encoding="utf-8")
        packets(folder, [v4_sensor(1), v4_sensor(2)])
    out = tmp_path / "out"
    out.mkdir()
    assert export_capture.main([str(folder), "--out", str(out / "x.csv")]) == 2
    assert "FAILED" in capsys.readouterr().err
    # Nothing partial is left behind, so the same --out works on the next run.
    assert list(out.iterdir()) == []


def test_an_unreadable_or_pre_option_report_falls_back_to_the_first_sample(tmp_path):
    folder = capture(tmp_path)
    packets(folder, [v4_sensor(1), v4_sensor(2)])
    assert export_capture.capture_version(folder / "packets.jsonl") == 4  # report predates it
    (folder / "report.json").write_text("{torn", encoding="utf-8")
    assert export_capture.capture_version(folder / "packets.jsonl") == 4
    (folder / "report.json").unlink()
    assert export_capture.capture_version(folder / "packets.jsonl") == 4


def test_a_v3_capture_still_exports_eleven_values(tmp_path):
    folder = capture(tmp_path)
    packets(folder, [v3_sensor(1), v3_sensor(2)])
    assert export_capture.export(folder / "packets.jsonl", tmp_path / "v3.csv") == 2
    header = next(csv.reader((tmp_path / "v3.csv").open(encoding="utf-8")))
    assert header[5:] == list(sensor.CHANNELS)


# --- tools.check_board ------------------------------------------------------------------------

def test_gap_tracker_keeps_the_longest_gap_and_the_bridge_counters():
    tracker = check_board.GapTracker()
    for seq in (0, 1, 4, 5, 5, 9, 10, 2):
        tracker.observe(sensor_v4.SensorPacket(2, 7, seq, 0, sensor_v4.DUMMY_FIXTURES[0]))
    assert (tracker.gaps, tracker.duplicates, tracker.out_of_order) == (5, 1, 1)
    assert tracker.report() == {"missing_seqs": 5, "gap_events": 2, "longest_gap": 3,
                                "longest_gap_after_seq": 5}


def test_verdict_uses_the_selected_versions_mtu_floor_and_configuration():
    base = {"received": 10, "acked": 10, "stream_results": 10, "command_results": 0,
            "commands": {"completed": 0}, "receive_errors": [],
            "source": {"clean": True, "complete": True, "generated": 10},
            "configuration": {"sensor_version": 4, "source_rate_confirmed_hz": 50,
                              "negotiated_att_mtu": 43}}
    assert check_board.verdict(base, rate=50, commands=0, sensor_version=4) == []
    small = dict(base, configuration=dict(base["configuration"], negotiated_att_mtu=42))
    assert check_board.verdict(small, rate=50, commands=0, sensor_version=4) == [
        "ATT MTU below 43"]
    v3 = dict(small, configuration=dict(small["configuration"], sensor_version=3))
    assert check_board.verdict(v3, rate=50, commands=0) == []
    assert check_board.verdict(v3, rate=50, commands=0, sensor_version=4) == [
        "the bridge ran sensor version 3, not 4", "ATT MTU below 43"]


def test_the_gaps_criterion_says_how_the_missing_seqs_fall():
    summary = {"received": 10, "acked": 10, "stream_results": 10, "command_results": 0,
               "commands": {"completed": 0}, "receive_errors": [], "gaps": 7,
               "source": {"clean": True, "complete": True, "generated": 10},
               "configuration": {"source_rate_confirmed_hz": 50, "negotiated_att_mtu": 247},
               "sequence_gaps": {"missing_seqs": 7, "gap_events": 2, "longest_gap": 5,
                                 "longest_gap_after_seq": 812}}
    assert check_board.verdict(summary, rate=50, commands=0) == [
        "gaps=7 (missing seqs in 2 gaps, longest 5 after seq 812)"]
    del summary["sequence_gaps"]
    assert check_board.verdict(summary, rate=50, commands=0) == ["gaps=7"]


def test_summary_and_gap_lines_report_sequence_gaps():
    # 306 s of run, of which 299.78 s from the first to the last packet: connect and stop
    # take the rest, so the rate counts only the streaming interval.
    summary = {"received": 14990, "acked": 14990, "gaps": 10, "queue_dropped": 0,
               "duplicates": 0, "out_of_order": 0, "elapsed_seconds": 306.0,
               "first_received_at": 1000.0, "last_received_at": 1299.78,
               "command_results": 0,
               "sequence_gaps": {"missing_seqs": 10, "gap_events": 3, "longest_gap": 6,
                                 "longest_gap_after_seq": 4711},
               "source": {"generated": 15000, "missing_received": 10, "missing_acked": 10,
                          "sequence_anomalies": 3, "gated": False, "source_idle": 0,
                          "source_unread": 0, "source_refused": 0}}
    line = check_board.summary_line(summary, "right", 2, 0, ["gaps=10"])
    assert "acked=14990 missing_ble=10 missing_ack=10 gaps=10 commands=0/0" in line
    gaps = check_board.gaps_line(summary, 4)
    assert gaps.startswith("SEQUENCE v4 packets: 14990 received, reception rate 50.0/s over "
                           "299.780 s from first to last packet (run 306.0 s with connect and "
                           "stop);")
    assert "10 seqs missing in 3 gaps, longest 6 (after seq 4711)" in gaps
    assert gaps.endswith("source sequence_anomalies=3")
    nothing = dict(summary, received=0, first_received_at=None, last_received_at=None)
    assert "reception rate - over - s" in check_board.gaps_line(nothing, 4)


def fake_check(calls, **summary):
    async def check(address, device_id, pki, **options):
        calls.append(dict(options, address=address, device_id=device_id))
        return dict({"received": 0, "acked": 0, "configuration": {}, "source": {}}, **summary)
    return check


def test_address_checks_a_bare_board_without_reading_the_saved_mapping(monkeypatch, tmp_path,
                                                                       capsys):
    calls = []
    monkeypatch.setattr(check_board, "check", fake_check(calls))
    monkeypatch.setattr(check_board, "load_boards",
                        lambda: pytest.fail("boards.json must not be read with --address"))
    code = check_board.main(["right", "--address", "24:6f:28:00:00:42", "--sensor-version", "4",
                             "--pki-dir", str(tmp_path), "--commands", "0"])
    assert code == 1  # the fake run acknowledged nothing
    assert calls == [{"address": "24:6F:28:00:00:42", "device_id": 2, "rate": 50,
                      "duration": 65.0, "commands": 0, "sensor_version": 4}]
    lines = capsys.readouterr().out.splitlines()
    summary = json.loads(lines[0])
    assert (summary["sensor_version"], summary["address_source"]) == (4, "--address")
    assert lines[1].startswith("CHECK FAILED RIGHT (device 2)")
    assert lines[2].startswith("SEQUENCE v4 packets")


def test_without_address_the_saved_board_and_version_3_are_used(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(check_board, "check", fake_check(calls))
    monkeypatch.setattr(check_board, "load_boards",
                        lambda: (("left", "24:6F:28:00:00:22"), ("right", "24:6F:28:00:00:42")))
    check_board.main(["left", "--pki-dir", str(tmp_path)])
    assert calls[0]["address"] == "24:6F:28:00:00:22" and calls[0]["sensor_version"] == 3
    assert json.loads(capsys.readouterr().out.splitlines()[0])["address_source"] == "boards.json"


@pytest.mark.parametrize("bad", ["not-an-address", "01:00:00:00:00:00"])
def test_an_invalid_address_is_refused_before_any_connection(monkeypatch, tmp_path, bad):
    monkeypatch.setattr(check_board, "check", lambda *args, **kwargs: pytest.fail("no check"))
    with pytest.raises(SystemExit) as stopped:
        check_board.main(["left", "--address", bad, "--pki-dir", str(tmp_path)])
    assert stopped.value.code == 2


@pytest.mark.parametrize("version", [3, 4, 5])
def test_check_runs_the_selected_version_through_a_local_service_and_subscriber(
        monkeypatch, tmp_path, version):
    """The real check() with the BLE loop replaced by the bridge's own synthetic source."""
    from tools.generate_comms_pki import generate_pki
    generate_pki(tmp_path / "pki")

    async def synthetic_ble(self, *, stop_event=None, **_):
        self.negotiated_mtu = 247
        self.source_rate_confirmed = self.config.source_rate
        await self.dummy_loop(50, stop_event=stop_event)

    monkeypatch.setattr(Bridge, "ble_loop", synthetic_ble)
    started = time.monotonic()
    summary = asyncio.run(check_board.check("24:6F:28:00:00:42", 2, tmp_path / "pki", rate=50,
                                            duration=1.0, commands=0, sensor_version=version))
    assert time.monotonic() - started < 20
    assert summary["configuration"]["sensor_version"] == version
    assert summary["receive_errors"] == []
    assert summary["stream_results"] == summary["acked"] > 0
    assert summary["sequence_gaps"] == {"missing_seqs": 0, "gap_events": 0, "longest_gap": 0,
                                        "longest_gap_after_seq": None}
    assert summary["server_metrics"]["accepted"] == summary["acked"]
    assert 0 < summary["streaming_seconds"] < summary["elapsed_seconds"]
    assert summary["reception_rate_hz"] == round(
        (summary["received"] - 1) / (summary["last_received_at"] - summary["first_received_at"]), 3)
    assert check_board.verdict(summary, rate=50, commands=0, sensor_version=version) == []
    assert summary["sensor_goodput"]["packet_bytes"] == (
        (38 if version == 3 else 40) * summary["sensor_goodput"]["packets"])
