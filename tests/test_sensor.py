"""Contract tests use literal external vectors, not codec-generated expectations."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]
VECTORS = json.loads((FIXTURES / "comms-golden.json").read_text())["vectors"]


def test_encoder_emits_lc_magic():
    from common.sensor import SensorPacket, encode_packet

    raw = encode_packet(SensorPacket(**VECTORS[0]["packet"]))
    assert len(raw) == 32
    assert raw[:2] == b"LC"


def test_encoder_can_emit_legacy_magic_for_old_firmware_commands():
    from common.sensor import SensorPacket, encode_packet

    packet = SensorPacket(**VECTORS[0]["packet"])
    raw = encode_packet(packet, magic=b"W7")
    assert raw == b"W7" + (FIXTURES / "comms-dummy.bin").read_bytes()[2:]


def test_encoder_rejects_unknown_magic():
    from common.sensor import SensorPacket, encode_packet

    with pytest.raises(ValueError, match="magic"):
        encode_packet(SensorPacket(**VECTORS[0]["packet"]), magic=b"WC")


@pytest.mark.parametrize("version", [1, 2])
def test_decoder_accepts_legacy_w7_magic(version):
    from common.sensor import SensorPacket, decode_packet

    raw = bytearray((FIXTURES / "comms-dummy.bin").read_bytes())
    raw[:2] = b"W7"
    raw[2] = version
    assert decode_packet(raw) == SensorPacket(**VECTORS[0]["packet"], version=version)


@pytest.mark.parametrize("vector", VECTORS)
def test_fixed_vector_decodes_encodes_and_maps_to_wire_message(vector):
    from common.sensor import SensorPacket, decode_packet, encode_packet

    raw = (FIXTURES / vector["binary"]).read_bytes()
    assert len(raw) == 32
    assert raw.hex() == vector["hex"]
    expected = SensorPacket(**vector["packet"])
    assert decode_packet(raw) == expected
    assert encode_packet(expected) == raw
    assert expected.to_message("ltc-comms") == vector["message"]


@pytest.mark.parametrize("length", [0, 1, 16, 20, 31, 33, 64])
def test_decoder_rejects_truncation_and_trailing_bytes(length):
    from common.sensor import decode_packet

    raw = (FIXTURES / "comms-dummy.bin").read_bytes()
    with pytest.raises(ValueError):
        decode_packet((raw + bytes(64))[:length])


@pytest.mark.parametrize("offset,value", [(0, 0), (1, 0), (2, 0), (2, 3), (3, 0), (3, 3), (3, 255)])
def test_decoder_rejects_wrong_magic_version_and_device(offset, value):
    from common.sensor import decode_packet

    raw = bytearray((FIXTURES / "comms-dummy.bin").read_bytes())
    raw[offset] = value
    with pytest.raises(ValueError):
        decode_packet(raw)


@pytest.mark.parametrize("field,value", [
    ("device_id", 0), ("device_id", 3), ("device_id", True),
    ("boot_id", -1), ("boot_id", 2**32), ("boot_id", 1.0),
    ("seq", -1), ("seq", 2**32), ("seq", False),
    ("uptime_ms", -1), ("uptime_ms", 2**32), ("uptime_ms", "1"),
    ("values", [0] * 7), ("values", [0] * 9),
    ("values", [-32769] + [0] * 7), ("values", [32768] + [0] * 7),
    ("values", [False] + [0] * 7), ("values", [0.0] * 8),
    ("values", "12345678"), ("values", None),
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


@pytest.mark.parametrize("seq,expected", [
    (0, (-1000, -990, -980, -970, -960, -950, -940, -930)),
    (42, (-958, -948, -938, -928, -918, -908, -898, -888)),
    (1999, (999, 1009, 1019, 1029, 1039, 1049, 1059, 1069)),
    (2000, (-1000, -990, -980, -970, -960, -950, -940, -930)),
    (4294967295, (295, 305, 315, 325, 335, 345, 355, 365)),
])
def test_dummy_values_match_independent_literals_at_wrap_boundaries(seq, expected):
    from common.sensor import dummy_values

    assert dummy_values(seq) == expected


@pytest.mark.parametrize("seq", [-1, 2**32, True, 1.0, "1"])
def test_dummy_values_reject_invalid_sequence(seq):
    from common.sensor import dummy_values

    with pytest.raises(ValueError):
        dummy_values(seq)


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
