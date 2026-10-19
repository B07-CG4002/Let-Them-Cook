"""Contract tests use literal external vectors, not codec-generated expectations."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]
VECTORS = json.loads((FIXTURES / "comms-golden.json").read_text())["vectors"]


def test_version_3_layout_constants_match_the_specification():
    from common import sensor

    assert sensor.VERSION == 3
    assert sensor.PACKET_SIZE == 38
    assert sensor.SENSOR_MTU_FLOOR == 41
    assert sensor.CHANNELS == ("ax", "ay", "az", "gx", "gy", "gz",
                               "yaw", "pitch", "roll", "flex1", "flex2")
    assert sensor.SCALES == (10000, 10000, 10000, 100, 100, 100, 100, 100, 100, 10000, 10000)
    assert sensor.CHANNEL_COUNT == 11


def test_encoder_emits_lc_magic():
    from common.sensor import SensorPacket, encode_packet

    raw = encode_packet(SensorPacket(**VECTORS[0]["packet"]))
    assert len(raw) == 38
    assert raw[:3] == b"LC\x03"


@pytest.mark.parametrize("magic", [b"W7", b"WC", b"lc"])
def test_decoder_rejects_any_other_packet_magic(magic):
    from common.sensor import decode_packet

    raw = bytearray((FIXTURES / "comms-dummy.bin").read_bytes())
    raw[:2] = magic
    with pytest.raises(ValueError, match="magic"):
        decode_packet(raw)


@pytest.mark.parametrize("vector", VECTORS)
def test_fixed_vector_decodes_encodes_and_maps_to_wire_message(vector):
    from common.sensor import SensorPacket, decode_packet, encode_packet

    raw = (FIXTURES / vector["binary"]).read_bytes()
    assert len(raw) == 38
    assert raw.hex() == vector["hex"]
    expected = SensorPacket(**vector["packet"])
    assert decode_packet(raw) == expected
    assert encode_packet(expected) == raw
    assert expected.to_message("ltc-comms") == vector["message"]


@pytest.mark.parametrize("length", [0, 1, 16, 20, 31, 32, 33, 37, 39, 64])
def test_decoder_rejects_truncation_and_trailing_bytes(length):
    from common.sensor import decode_packet

    raw = (FIXTURES / "comms-dummy.bin").read_bytes()
    with pytest.raises(ValueError):
        decode_packet((raw + bytes(64))[:length])


@pytest.mark.parametrize("count", [1, 2, 3, 6])
def test_notification_splits_into_whole_packets_oldest_first(count):
    from common.sensor import (MAX_PACKETS_PER_NOTIFICATION, SensorPacket, decode_packet,
                               encode_packet, split_notification)

    assert MAX_PACKETS_PER_NOTIFICATION == 6
    packets = [SensorPacket(2, 9, 40 + index, 1000 + 20 * index, tuple(range(index, index + 11)))
               for index in range(count)]
    pieces = split_notification(bytearray(b"".join(encode_packet(p) for p in packets)))
    assert all(type(piece) is bytes and len(piece) == 38 for piece in pieces)
    assert [decode_packet(piece) for piece in pieces] == packets


@pytest.mark.parametrize("length", [0, 1, 37, 39, 75, 77, 7 * 38, 13 * 38])
def test_notification_of_partial_or_too_many_packets_is_refused(length):
    from common.sensor import split_notification

    raw = (FIXTURES / "comms-dummy.bin").read_bytes()
    with pytest.raises(ValueError):
        split_notification((raw * 13)[:length])


def test_one_bad_packet_refuses_the_whole_notification():
    from common.sensor import split_notification

    raw = (FIXTURES / "comms-dummy.bin").read_bytes()
    damaged = bytearray(raw)
    damaged[2] = 2  # version 2 inside an otherwise valid pair
    with pytest.raises(ValueError):
        split_notification(raw + bytes(damaged))
    with pytest.raises(ValueError):
        split_notification("not bytes")


@pytest.mark.parametrize("offset,value", [
    (0, 0), (1, 0), (2, 0), (2, 1), (2, 2), (2, 4), (3, 0), (3, 3), (3, 255),
])
def test_decoder_rejects_wrong_magic_version_and_device(offset, value):
    from common.sensor import decode_packet

    raw = bytearray((FIXTURES / "comms-dummy.bin").read_bytes())
    raw[offset] = value
    with pytest.raises(ValueError):
        decode_packet(raw)


def test_decoder_rejects_a_version_2_board_packet():
    from common.sensor import decode_packet

    # The former 32-byte version 2 layout: header and eight int16 channels.
    raw = bytes.fromhex("4c430202123456782a00000004030201") + bytes(16)
    assert len(raw) == 32
    with pytest.raises(ValueError):
        decode_packet(raw)


@pytest.mark.parametrize("field,value", [
    ("device_id", 0), ("device_id", 3), ("device_id", True),
    ("boot_id", -1), ("boot_id", 2**32), ("boot_id", 1.0),
    ("seq", -1), ("seq", 2**32), ("seq", False),
    ("uptime_ms", -1), ("uptime_ms", 2**32), ("uptime_ms", "1"),
    ("values", [0] * 8), ("values", [0] * 10), ("values", [0] * 12),
    ("values", [-32769] + [0] * 10), ("values", [32768] + [0] * 10),
    ("values", [False] + [0] * 10), ("values", [0.0] * 11),
    ("values", "12345678901"), ("values", None),
    ("version", 1), ("version", 2), ("version", 4),
])
def test_packet_rejects_invalid_ranges_and_non_integer_inputs(field, value):
    from common.sensor import SensorPacket, encode_packet

    args = dict(VECTORS[0]["packet"])
    args[field] = value
    with pytest.raises(ValueError):
        encode_packet(SensorPacket(**args))


def test_packet_values_do_not_change_when_input_list_is_mutated():
    from common.sensor import SensorPacket, encode_packet

    args = dict(VECTORS[0]["packet"])
    args["values"] = list(args["values"])
    packet = SensorPacket(**args)
    args["values"][0] = 999
    assert encode_packet(packet) == (FIXTURES / "comms-dummy.bin").read_bytes()
    assert isinstance(packet.values, tuple)


def test_physical_values_divide_each_channel_by_its_scale():
    from common.sensor import physical_values

    assert physical_values(VECTORS[0]["packet"]["values"]) == (
        0.12, -0.03, -0.85, 40.0, -20.0, 15.0, 12.5, -30.0, 6.0, 0.6, 0.55)
    assert physical_values(VECTORS[1]["packet"]["values"])[:4] == (-3.2768, 3.2767, -0.0001, 0.0)


@pytest.mark.parametrize("values", [[0] * 10, [0] * 12, [32768] + [0] * 10, [0.5] + [0] * 10, None])
def test_physical_values_reject_anything_but_eleven_int16(values):
    from common.sensor import physical_values

    with pytest.raises(ValueError):
        physical_values(values)


@pytest.mark.parametrize("session", ["", None, 1, True])
def test_wire_message_rejects_invalid_session_identifier(session):
    from common.sensor import SensorPacket

    with pytest.raises(ValueError):
        SensorPacket(**VECTORS[0]["packet"]).to_message(session)


def test_firmware_serializer_and_security_policy_on_host(tmp_path):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("A native C++ compiler is required for the firmware cross-check")
    executable = tmp_path / "comms-packet-test.exe"
    subprocess.run([
        compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror",
        "-I", str(ROOT / "firmware/esp32/include"),
        str(ROOT / "firmware/esp32/test/host_packet.cpp"),
        "-o", str(executable),
    ], check=True, capture_output=True, text=True)
    result = subprocess.run([str(executable)], check=True, capture_output=True, text=True)
    assert result.stdout.splitlines() == [vector["hex"] for vector in VECTORS]
