"""Sensor version 5 (the three-flex gloves with the wide gyro range) through Communications.

Version 5 is version 4's packet (40 bytes, twelve int16 values) with the gyro carried at
10 counts per deg/s instead of 100, and LCS5 source statistics (Wen Ree's
common/sensor_v5.py; the firmware's COMMS_WIDE_GYRO=1). Every check here is local: no
BLE, SSH, Ultra96 or phone. They pin that version 5 is chosen explicitly at every end and
carried with its own codec (common.sensor_versions); that a version 4 packet or an LCS4
board is never read as version 5, nor the other way; that the Ultra96 service restores
physical units once, with the version 5 scales, so the model gets deg/s; that the
twelve-channel paths (hand events, the hand12 runner) treat 5 as they treat 4; and that
results stay at v=3. The hand events of a version-5 service, end to end through the gate
(whose admission reads only version 4), are tested in test_comms_hand_service.py, whose
hand-event scenarios run in versions 4 and 5.
"""
import asyncio
import csv
import functools
import json
from pathlib import Path
import socket
import struct
import sys

import pytest

import demo
import flash
from common import hand_contract, sensor, sensor_v4, sensor_v5, sensor_versions
from common.control import COMMAND, ControlFrame, decode_control, encode_control, transformed_values
from common.wire import ProtocolError, read_frame, write_frame
from laptop import dual_bridge
from laptop.bridge import Bridge, BridgeConfig
from laptop.controls import ControlChannel
from laptop.dual_bridge import DualBridge, _parser
from laptop.goodput import GoodputMeter
from laptop.source_audit import parse_source_stats
from tools import check_board, export_capture, live_session
from tools.live_view import header_lines, packets_text, values_text
from tools.video_evidence import show_packet_examples
from ultra96.model_bridge import Bridge as ModelBridge
from ultra96.protocol import validate_message

from test_dual_bridge import async_test, install_ack_transport
from test_live_session import FakeConsole, LocalBoard, run_folder, start_args
from test_live_session import pki  # noqa: F401  (the module-scoped PKI fixture: (folder, other))
from test_v4_service import connect, disconnect, start_server, subscribe
from test_video_evidence import ack, capture, packets

ROOT = Path(__file__).parents[1]
SESSION = "ltc-comms"
LEFT, RIGHT = "38:18:2B:19:82:AE", "38:18:2B:18:9D:6A"
# gx 450.1, gy -800.2, gz 12.0 deg/s in version 5; the same int16 values are a tenth of
# that in version 4.
ROW12 = (1200, -300, -8500, 4501, -8002, 120, 17900, -3000, 600, 6000, 5500, 5000)


def v5_config(device, **extra):
    return BridgeConfig(ca_file="unused", diagnostic_unprotected=True, expected_device_id=device,
                        source_audit=True, sensor_version=5, **extra)


def stats(magic, device=1, flags=2):
    """A 36-byte source-statistics value: boot 42, next seq 7, 7 submitted."""
    return struct.pack("<4sBB2xIIIIIII", magic, device, flags, 42, 7, 7, 0, 0, 0, 0)


# --- common.sensor_versions -------------------------------------------------------------------

def test_the_version_table_names_each_codec_and_refuses_any_other_version():
    assert sensor_versions.VERSIONS == (3, 4, 5) and sensor_versions.DEFAULT == 3
    assert sensor_versions.CODECS == {3: sensor, 4: sensor_v4, 5: sensor_v5}
    assert sensor_versions.TWELVE_CHANNEL == (4, 5)
    assert [sensor_versions.source_stats_magic(v) for v in (3, 4, 5)] == [b"LCS3", b"LCS4", b"LCS5"]
    assert sensor_versions.describe(5) == "v5 (40 bytes, 12 values, gyro x10, LCS5)"
    assert sensor_versions.describe(4) == "v4 (40 bytes, 12 values, gyro x100, LCS4)"
    assert sensor_versions.describe(3) == "v3 (38 bytes, 11 values, gyro x100, LCS3)"
    for bad in (2, 6, True, 5.0, "5", None):
        with pytest.raises(ValueError, match="sensor_version must be 3, 4 or 5"):
            sensor_versions.codec(bad)


def test_version_5_differs_from_4_only_in_the_gyro_scale():
    assert sensor_v5.CHANNELS == sensor_v4.CHANNELS and sensor_v5.PACKET_SIZE == sensor_v4.PACKET_SIZE
    assert [i for i, (a, b) in enumerate(zip(sensor_v4.SCALES, sensor_v5.SCALES)) if a != b] == [3, 4, 5]
    assert sensor_v5.physical_values(ROW12)[3:6] == (450.1, -800.2, 12.0)
    assert sensor_v4.physical_values(ROW12)[3:6] == (45.01, -80.02, 1.2)


def test_wen_rees_bridge_sample_converts_each_version_with_its_own_scales():
    # common.hand_contract keeps its own scale tables (the staged hand12 worker has no
    # common/sensor_v5.py), so they must stay equal to the codecs'.
    assert hand_contract.SCALES == sensor_v4.SCALES and hand_contract.SCALES_V5 == sensor_v5.SCALES
    for codec in (sensor_v4, sensor_v5):
        sample = hand_contract.to_bridge_sample(codec.SensorPacket(1, 9, 1, 20, ROW12).to_message(SESSION))
        assert sample["values"] == list(codec.physical_values(ROW12)), codec.VERSION
    v5 = hand_contract.to_bridge_sample(sensor_v5.SensorPacket(1, 9, 1, 20, ROW12).to_message(SESSION))
    assert v5["values"][3:6] == [450.1, -800.2, 12.0]  # not the version-4 reading 45.01, -80.02, 1.2
    with pytest.raises(ProtocolError):
        hand_contract.to_bridge_sample(sensor.SensorPacket(1, 9, 1, 20, ROW12[:11]).to_message(SESSION))


# --- common.control and laptop.controls ---------------------------------------------------------

def command_frame(codec, device=1, request=9):
    packet = codec.SensorPacket(device, 42, request, 0, ROW12)
    return ControlFrame(COMMAND, device, request, payload=codec.encode_packet(packet))


def test_a_v5_command_round_trips_and_a_v4_packet_is_never_a_v5_command():
    frame = command_frame(sensor_v5)
    assert decode_control(encode_control(frame, sensor_version=5), sensor_version=5) == frame
    with pytest.raises(ValueError):
        encode_control(command_frame(sensor_v4), sensor_version=5)
    with pytest.raises(ValueError):
        encode_control(frame, sensor_version=4)
    assert transformed_values(ROW12, sensor_version=5) == tuple(value + 1 for value in ROW12)
    with pytest.raises(ValueError):
        transformed_values(ROW12[:11], sensor_version=5)
    with pytest.raises(ValueError, match="3, 4 or 5"):
        encode_control(frame, sensor_version=6)


@async_test
async def test_the_control_channel_of_a_v5_bridge_speaks_v5():
    channel = ControlChannel(1, sensor_version=5)
    assert channel.sensor_version == 5 and channel._sensor_codec is sensor_v5
    frame = command_frame(sensor_v5)
    assert channel._decode_control(channel._encode_control(frame, mtu=247), mtu=247) == frame
    assert channel._transformed_values(ROW12) == tuple(value + 1 for value in ROW12)
    with pytest.raises(ValueError, match="3, 4 or 5"):
        ControlChannel(1, sensor_version=6)


# --- laptop.source_audit and laptop.goodput -----------------------------------------------------

def test_lcs5_source_statistics_are_read_only_by_a_version_5_bridge():
    parsed = parse_source_stats(stats(b"LCS5"), expected_device_id=1, sensor_version=5)
    assert (parsed.device_id, parsed.boot_id, parsed.next_seq, parsed.submitted) == (1, 42, 7, 7)
    for magic, version in ((b"LCS4", 5), (b"LCS3", 5), (b"LCS5", 4), (b"LCS5", 3)):
        with pytest.raises(ValueError, match="magic"):
            parse_source_stats(stats(magic), expected_device_id=1, sensor_version=version)
    with pytest.raises(ValueError, match="3, 4 or 5"):
        parse_source_stats(stats(b"LCS5"), sensor_version=6)


def test_goodput_of_a_v5_bridge_counts_v5_packets_at_40_bytes_only():
    meter = GoodputMeter(sensor_version=5)
    meter.start(0.0)
    assert meter.record(sensor_v5.SensorPacket(1, 42, 0, 0, ROW12), 0.1) is not False
    assert meter.record(sensor_v4.SensorPacket(1, 42, 1, 0, ROW12), 0.2) is False
    meter.stop(1.0)
    report = meter.report(1.0)
    assert (report["packets"], report["packet_bytes"]) == (1, 40)


# --- laptop.bridge and laptop.dual_bridge -------------------------------------------------------

@async_test
async def test_a_v5_bridge_decodes_v5_and_refuses_v4_packets_and_lcs4_boards():
    bridge = Bridge(v5_config(1))
    assert (bridge.packet_size, bridge.sensor_mtu_floor, bridge.max_packets_per_notification) == (40, 43, 6)
    raw = sensor_v5.encode_packet(sensor_v5.SensorPacket(1, 42, 3, 60, ROW12))
    assert bridge._codec("decode_packet", raw).values == ROW12
    assert len(bridge._codec("split_notification", raw * 6)) == 6
    old = sensor_v4.encode_packet(sensor_v4.SensorPacket(1, 42, 3, 60, ROW12))
    for data in (old, raw + old):
        with pytest.raises(ValueError):
            bridge._codec("split_notification", data)
    assert bridge._parse_stats(stats(b"LCS5"), expected_device_id=1).boot_id == 42
    with pytest.raises(ValueError, match="magic"):
        bridge._parse_stats(stats(b"LCS4"), expected_device_id=1)
    with pytest.raises(ValueError, match="3, 4 or 5"):
        BridgeConfig(ca_file="unused", sensor_version=6)


def test_the_dual_bridge_option_accepts_5():
    assert _parser().parse_args(["--ca", "unused", "--mock", "--sensor-version", "5"]).sensor_version == 5


@async_test
async def test_the_dual_bridge_refuses_a_v4_and_a_v5_board_together():
    with pytest.raises(ValueError, match="same sensor version"):
        DualBridge(Bridge(BridgeConfig(ca_file="unused", diagnostic_unprotected=True,
                                       expected_device_id=1, source_audit=True, sensor_version=4)),
                   Bridge(v5_config(2)))


@async_test
async def test_a_v5_mock_dual_run_sends_40_byte_12_value_v5_packets_and_reconciles():
    left, right = Bridge(v5_config(1)), Bridge(v5_config(2))
    sent = {1: install_ack_transport(left), 2: install_ack_transport(right)}
    report = await DualBridge(left, right, expected_rate=20, startup_timeout=1,
                              drain_timeout=1).run(duration=0.15, mock=True, mock_rate=100)
    assert report["clean"] is True
    for device, messages in sent.items():
        assert messages and {message["v"] for message in messages} == {5}
        assert {len(message["values"]) for message in messages} == {12}
        assert {tuple(message["values"]) for message in messages} <= set(sensor_v5.DUMMY_FIXTURES)
        item = report["devices"][str(device)]
        assert item["configuration"]["sensor_version"] == 5
        assert item["source"]["generated"] == item["received"] == item["acked"] > 0
        assert item["sensor_goodput"]["packet_bytes"] == 40 * item["sensor_goodput"]["packets"]


def test_the_cli_gives_both_bridges_version_5_including_the_command_path(monkeypatch, capsys):
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
        "--rate", "50", "--duration", "1", "--sensor-version", "5"])
    assert dual_bridge.main() == 1
    assert json.loads(capsys.readouterr().out)["run"]["sensor_version"] == 5
    for device in (1, 2):
        config, control_version, packet = seen[device]
        assert (config.sensor_version, config.controls_enabled, control_version) == (5, True, 5)
        assert isinstance(packet, sensor_v5.SensorPacket) and packet.device_id == device
        assert len(sensor_v5.encode_packet(packet)) == 40


# --- ultra96.server, ultra96.protocol and ultra96.model_bridge ------------------------------------

class RecordingModel:
    name = "recording-v5"

    def __init__(self):
        self.values = []

    def start(self, emit):
        self.emit = emit

    def on_sample(self, device_id, boot_id, seq, uptime_ms, values):
        self.values.append(tuple(values))


@pytest.mark.parametrize("version", [4, 5])
def test_the_model_gets_physical_units_restored_once_with_the_services_scales(pki, version):  # noqa: F811
    async def check():
        model = RecordingModel()
        server = await start_server(pki[0], sensor_version=version, model=model)
        try:
            # Both twelve-channel versions have the hand-event gate (its delivery in version 5:
            # test_comms_hand_service.py).
            assert server._hand_gate is not None
            laptop, laptop_writer = await connect(pki[0], server.ingest_port)
            codec = sensor_versions.codec(version)
            await write_frame(laptop_writer, codec.SensorPacket(1, 9, 1, 20, ROW12).to_message(SESSION))
            assert (await read_frame(laptop))["v"] == version
            assert model.values == [codec.physical_values(ROW12)]
            assert model.values[0][3:6] == ((450.1, -800.2, 12.0) if version == 5 else (45.01, -80.02, 1.2))
            await disconnect(laptop_writer)
            # A packet of the other twelve-channel version is refused and never reaches the model.
            other = sensor_versions.codec(9 - version)
            laptop, laptop_writer = await connect(pki[0], server.ingest_port)
            await write_frame(laptop_writer, other.SensorPacket(1, 9, 2, 40, ROW12).to_message(SESSION))
            with pytest.raises(Exception):
                await read_frame(laptop, timeout=2)
            assert len(model.values) == 1 and server.metrics["accepted"] == 1
            await disconnect(laptop_writer)
        finally:
            await server.close()
    asyncio.run(check())


def test_a_v5_service_acks_at_v5_and_publishes_v3_results(pki):  # noqa: F811
    async def check():
        server = await start_server(pki[0], sensor_version=5)
        try:
            phone, phone_writer = await subscribe(pki[0], server)
            laptop, laptop_writer = await connect(pki[0], server.ingest_port)
            await write_frame(laptop_writer, sensor_v5.SensorPacket(2, 42, 0, 0, ROW12).to_message(SESSION))
            assert await read_frame(laptop) == dict(v=5, type="INGEST_ACK", session_id=SESSION, device_id=2,
                                                    boot_id=42, seq=0, request_id=None, status="accepted")
            published = await read_frame(phone)
            assert published["v"] == 3 and published["result_id"] == "2:42:0"
            assert validate_message(published, "GESTURE_RESULT", SESSION, sensor_version=5) is published
            await disconnect(laptop_writer)
            await disconnect(phone_writer)
        finally:
            await server.close()
    asyncio.run(check())


def test_the_model_bridge_offers_the_twelve_channels_and_hand_events_in_version_5():
    bridge = ModelBridge()
    bridge.configure_sensor_version(5)
    assert bridge.channels == sensor_v5.CHANNELS == sensor_v4.CHANNELS and bridge.hand_events is True
    v3 = ModelBridge()
    v3.configure_sensor_version(3)
    assert len(v3.channels) == 11 and v3.hand_events is False
    with pytest.raises(ValueError, match="3, 4 or 5"):
        ModelBridge().configure_sensor_version(6)
    # The runner's hello is the version 4 one: the worker sees physical units either way.
    ours, runner = socket.socketpair()
    runner.settimeout(5)
    try:
        bridge.attach(ours)
        hello = json.loads(runner.makefile("rb").readline())
        assert hello == dict(type="hello", format=1, channels=list(sensor_v5.CHANNELS), hand_events=1)
    finally:
        runner.close()
        ours.close()


# --- demo.py, tools.live_session and tools.live_view ---------------------------------------------

def test_start_run_and_live_pass_version_5_to_the_capture(tmp_path):
    base = ["start", "--left-address", LEFT, "--right-address", RIGHT]
    v3 = live_session.capture_command(demo._parser().parse_args(base), tmp_path / "report.json")
    v5 = live_session.capture_command(demo._parser().parse_args(base + ["--sensor-version", "5"]),
                                      tmp_path / "report.json")
    assert v5[-5:] == ["--sensor-version", "5", "--progress-interval", "0", "--supervised"]
    assert [part for part in v5 if part not in ("--sensor-version", "5")] == v3
    for mode in ("run", "live"):
        command = demo.capture_command(demo._parser().parse_args([mode]), tmp_path / "report.json")
        command5 = demo.capture_command(demo._parser().parse_args([mode, "--sensor-version", "5"]),
                                        tmp_path / "report.json")
        assert command5 == command + ["--sensor-version", "5"]


def test_the_live_view_shows_v5_values_in_deg_per_second_and_names_the_format():
    assert "gx +450.10 gy -800.20 gz +12.00 deg/s" in values_text(ROW12, 5)
    assert "gx +45.01 gy -80.02 gz +1.20 deg/s" in values_text(ROW12, 4)
    assert packets_text(5) == "v5 (40 bytes, 12 values, gyro x10)"
    assert packets_text(4) == "v4 (40 bytes, 12 values)"
    options = dict(folder="F", started=1_800_000_000.0, command="python demo.py start",
                   revision="abc", session_id="ltc-comms", rate_hz=50, duration=None,
                   keyboard=False, addresses={1: "L", 2: "R"}, tunnel="T")
    assert any("sensor packets v5 (40 bytes, 12 values, gyro x10)" in line
               for line in header_lines(**options, sensor_version=5))


class V5Board(LocalBoard):
    """test_live_session's local service and phone, with a version 5 service."""

    async def _start(self):
        import ultra96.server as server
        original = server.CommsServer
        server.CommsServer = functools.partial(original, sensor_version=5)
        try:
            await super()._start()
        finally:
            server.CommsServer = original


def test_a_v5_start_run_passes_its_packet_check(pki, tmp_path, capsys):  # noqa: F811
    with V5Board(pki[0]) as board:
        console = FakeConsole()
        args = start_args(pki, board, tmp_path, "--duration", "3", "--sensor-version", "5")
        live_session.main(args, console=console)
        phone_results = board.results
    folder = run_folder(tmp_path)
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    assert report["mock_input"] is True and report["run"]["sensor_version"] == 5
    for device in ("1", "2"):
        assert report["devices"][device]["source"]["complete"] is True
        assert report["devices"][device]["runtime_failures"] == []
    log = (folder / "run.log").read_text(encoding="utf-8")
    assert "sensor packets v5 (40 bytes, 12 values, gyro x10)" in log
    assert "LEFT   result #" in log and phone_results > 0
    assert any("v5 packets" in frame[0] for frame in console.frames)
    capsys.readouterr()
    assert show_packet_examples(folder) is True
    assert "protocol version 5 (40 bytes, 12 values" in capsys.readouterr().out


# --- tools.video_evidence and tools.export_capture -------------------------------------------------

def v5_sensor(device, *, boot=None, seq=7, codec=sensor_v5):
    boot = 100 + device if boot is None else boot
    raw = codec.encode_packet(codec.SensorPacket(device, boot, seq, 100, ROW12))
    return {"timestamp_utc": "2026-10-29T00:00:00+00:00", "monotonic_seconds": 1.0,
            "type": "sensor", "device_id": device, "direction": "ESP->laptop",
            "version": codec.VERSION, "boot_id": boot, "seq": seq, "uptime_ms": 100,
            "values": list(ROW12), "raw_hex": raw.hex(), "validation": "decoded",
            "received_monotonic": 1.0}


def test_a_v5_capture_is_shown_and_exported_in_deg_per_second(tmp_path, capsys):
    folder = capture(tmp_path)
    packets(folder, [v5_sensor(1, seq=0), ack(1, seq=0), v5_sensor(2, seq=0), ack(2, seq=0)])
    assert show_packet_examples(folder) is True
    assert "protocol version 5 (40 bytes, 12 values" in capsys.readouterr().out
    out = tmp_path / "samples.csv"
    assert export_capture.export(folder / "packets.jsonl", out) == 2
    rows = list(csv.reader(out.open(encoding="utf-8")))
    assert rows[0] == ["device_id", "glove", "boot_id", "seq", "uptime_ms", *sensor_v5.CHANNELS]
    assert rows[1][8:11] == ["450.1000", "-800.2000", "12.0000"]
    assert rows[1][5:] == ["{:.4f}".format(value / scale) for value, scale in zip(ROW12, sensor_v5.SCALES)]


def test_a_v4_packet_in_a_v5_capture_is_refused(tmp_path, capsys):
    folder = capture(tmp_path)
    # Recorded as version 5, but the raw bytes are a version 4 packet.
    wrong = dict(v5_sensor(2), raw_hex=v5_sensor(2, codec=sensor_v4)["raw_hex"])
    packets(folder, [v5_sensor(1), ack(1), wrong, ack(2)])
    assert show_packet_examples(folder) is False
    assert "NOT PASSED" in capsys.readouterr().out


# --- tools.check_board ----------------------------------------------------------------------------

def test_the_board_check_uses_the_v5_mtu_floor_and_configuration():
    base = {"received": 10, "acked": 10, "stream_results": 10, "command_results": 0,
            "commands": {"completed": 0}, "receive_errors": [],
            "source": {"clean": True, "complete": True, "generated": 10},
            "configuration": {"sensor_version": 5, "source_rate_confirmed_hz": 50,
                              "negotiated_att_mtu": 43}}
    assert check_board.verdict(base, rate=50, commands=0, sensor_version=5) == []
    assert check_board.verdict(base, rate=50, commands=0, sensor_version=4) == [
        "the bridge ran sensor version 5, not 4"]
    small = dict(base, configuration=dict(base["configuration"], negotiated_att_mtu=42))
    assert check_board.verdict(small, rate=50, commands=0, sensor_version=5) == ["ATT MTU below 43"]
    assert check_board.gaps_line({"received": 0}, 5).startswith("SEQUENCE v5 packets: 0 received")


# --- flash.py and the firmware environments --------------------------------------------------------

def test_wide_gyro_flashes_the_v5_environments_which_exist():
    assert flash.environments(wide_gyro=True) == ("firebeetle32-left-flex3-wide",
                                                  "firebeetle32-right-flex3-wide")
    assert flash.environments(glove=True, wide_gyro=True) == (
        "firebeetle32-left-glove-flex3-wide", "firebeetle32-right-glove-flex3-wide")
    assert flash.environments(glove=True, flex3=True, wide_gyro=True) == flash.environments(
        glove=True, wide_gyro=True)
    assert flash.environments(flex3=True) == ("firebeetle32-left-flex3", "firebeetle32-right-flex3")
    assert [flash.sensor_version(*options) for options in ((), (True,), (False, True), (True, True))] == [
        3, 4, 5, 5]
    ini = (ROOT / "firmware" / "esp32" / "platformio.ini").read_text(encoding="utf-8")
    for glove in (False, True):
        for name in flash.environments(glove=glove, wide_gyro=True):
            section = ini.split("[env:" + name + "]", 1)[1].split("\n[", 1)[0]
            assert "-DCOMMS_WIDE_GYRO=1" in section, name
            if glove:
                assert "-DGLOVE_GYRO_RANGE_DPS=1000" in section, name


def test_the_wide_gyro_option_reaches_the_flash_step(monkeypatch):
    calls = []
    monkeypatch.setattr(flash, "_flash", lambda **options: calls.append(options))
    assert flash.main(["--glove", "--wide-gyro"]) == 0
    assert calls == [dict(glove=True, flex3=False, wide_gyro=True)]
