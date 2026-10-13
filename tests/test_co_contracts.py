import random
import struct
from pathlib import Path

import pytest

from common import sensor


def test_v2_retains_fixed_layout_and_explicit_command_correlation():
    packet = sensor.SensorPacket(2, 0x01020304, 9, 10, (-32768, 32767, 1, 2, 3, 4, 5, 6), version=2)
    raw = sensor.encode_packet(packet)
    assert raw[:8] == bytes.fromhex('4c43020204030201')
    assert len(raw) == 32
    assert sensor.decode_packet(raw) == packet
    assert packet.to_message('test')['request_id'] is None
    assert packet.to_message('test', request_id=9)['request_id'] == 9
    with pytest.raises(ValueError):
        packet.to_message('test', request_id=8)


def test_fixture_selection_seeded_and_schema_valid():
    assert hasattr(sensor, 'choose_fixture'), 'editable random fixture selection is missing'
    left, right = random.Random(7), random.Random(7)
    selected = [sensor.choose_fixture(left) for _ in range(30)]
    assert selected == [sensor.choose_fixture(right) for _ in range(30)]
    assert len(set(selected)) > 1
    for values in selected:
        sensor.SensorPacket(1, 1, 1, 1, values, version=2)


def test_fixture_header_matches_editable_json():
    from tools.generate_dummy_fixtures import render_header
    assert (Path(__file__).resolve().parents[1] / 'firmware/esp32/include/comms_fixtures.h').read_text() == render_header()


def test_control_roundtrip_literal_header_and_wrapping():
    from common.control import ControlFrame, encode_control, decode_control, SET_RATE, transformed_values
    frame = ControlFrame(SET_RATE, 2, 0x01020304, payload=struct.pack('<H', 50))
    raw = encode_control(frame, mtu=64)
    assert raw.hex() == '42370102020004030201000000003200'
    assert decode_control(raw, mtu=64) == frame
    assert transformed_values((32767, -32768, -1, 0, 10, 20, 30, 40)) == (-32768, -32767, 0, 1, 11, 21, 31, 41)


def test_control_limits_and_metadata():
    from common.control import ControlFrame, encode_control, decode_control, FILE_CHUNK, FILE_BEGIN, file_metadata, parse_file_metadata, chunk_size
    assert chunk_size(64) == 47
    assert chunk_size(517) == 180
    for mtu in (23, 63, True, 518):
        with pytest.raises(ValueError):
            chunk_size(mtu)
    frame = ControlFrame(FILE_CHUNK, 1, 5, offset=4, payload=b'abc')
    raw = encode_control(frame, mtu=64)
    for malformed in (raw[:13], b'XX'+raw[2:], raw[:2]+b'\x02'+raw[3:], raw[:5]+b'\x01'+raw[6:]):
        with pytest.raises(ValueError):
            decode_control(malformed, mtu=64)
    with pytest.raises(ValueError):
        encode_control(ControlFrame(FILE_CHUNK, 1, 5, payload=b'x'*48), mtu=64)
    metadata = file_metadata(b'abc')
    length, digest = parse_file_metadata(metadata)
    assert length == 3
    assert digest.hex() == 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
    assert decode_control(encode_control(ControlFrame(FILE_BEGIN, 1, 8, payload=metadata)))
    with pytest.raises(ValueError):
        file_metadata(b'x'*65537)
