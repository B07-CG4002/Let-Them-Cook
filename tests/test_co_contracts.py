import random
import struct
from pathlib import Path

import pytest

from common import sensor


def test_v3_fixed_layout_and_explicit_command_correlation():
    packet = sensor.SensorPacket(2, 0x01020304, 9, 10, (-32768, 32767, 1, 2, 3, 4, 5, 6, 7, 8, 9))
    raw = sensor.encode_packet(packet)
    assert raw[:8] == bytes.fromhex('4c43030204030201')
    assert len(raw) == 38
    assert raw[16:].hex() == '0080ff7f010002000300040005000600070008000900'
    assert sensor.decode_packet(raw) == packet
    assert packet.to_message('test')['v'] == 3
    assert packet.to_message('test')['request_id'] is None
    assert packet.to_message('test', request_id=9)['request_id'] == 9
    with pytest.raises(ValueError):
        packet.to_message('test', request_id=8)
    for old in (1, 2):
        with pytest.raises(ValueError):
            sensor.SensorPacket(2, 0x01020304, 9, 10, packet.values, version=old)


def test_fixture_selection_seeded_and_schema_valid():
    assert hasattr(sensor, 'choose_fixture'), 'editable random fixture selection is missing'
    left, right = random.Random(7), random.Random(7)
    selected = [sensor.choose_fixture(left) for _ in range(30)]
    assert selected == [sensor.choose_fixture(right) for _ in range(30)]
    assert len(set(selected)) > 1
    for values in selected:
        sensor.SensorPacket(1, 1, 1, 1, values)


def test_fixture_header_matches_editable_json():
    from tools.generate_dummy_fixtures import render_header
    header = render_header()
    assert (Path(__file__).resolve().parents[1] / 'firmware/esp32/include/comms_fixtures.h').read_text() == header
    assert 'kDummyFixtures[][11]' in header


def test_control_roundtrip_literal_header_and_wrapping():
    from common.control import ControlFrame, encode_control, decode_control, SET_RATE, transformed_values
    frame = ControlFrame(SET_RATE, 2, 0x01020304, payload=struct.pack('<H', 50))
    raw = encode_control(frame, mtu=64)
    assert raw.hex() == '42370102020004030201000000003200'
    assert decode_control(raw, mtu=64) == frame
    assert transformed_values((32767, -32768, -1, 0, 10, 20, 30, 40, 50, 60, 70)) == (
        -32768, -32767, 0, 1, 11, 21, 31, 41, 51, 61, 71)
    with pytest.raises(ValueError):
        transformed_values((0,) * 8)


def test_command_frame_carries_one_version_3_packet():
    from common.control import COMMAND, RESPONSE_FLAG, ControlFrame, encode_control, decode_control
    values = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
    payload = sensor.encode_packet(sensor.SensorPacket(2, 7, 9, 0, values))
    raw = encode_control(ControlFrame(COMMAND, 2, 9, payload=payload), mtu=64)
    assert len(raw) == 52
    assert decode_control(raw, mtu=64).payload == payload
    response = sensor.encode_packet(sensor.SensorPacket(2, 7, 9, 500, tuple(v + 1 for v in values)))
    assert len(encode_control(ControlFrame(COMMAND | RESPONSE_FLAG, 2, 9, payload=response), mtu=64)) == 52
    # A version 2 board's 32-byte packet, or a v3 packet with a trailing byte, is refused.
    legacy = bytes.fromhex('4c430202070000000900000000000000') + bytes(16)
    for wrong in (legacy, payload[:32], payload + bytes(1)):
        with pytest.raises(ValueError):
            encode_control(ControlFrame(COMMAND, 2, 9, payload=wrong), mtu=64)


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
