import pytest

from common.sensor import DUMMY_FIXTURES, PACKET_SIZE, SensorPacket, encode_packet


def packet(seq, *, device=1, boot=4):
    return SensorPacket(device, boot, seq, 0, DUMMY_FIXTURES[seq % len(DUMMY_FIXTURES)])


def test_goodput_counts_unique_valid_packet_bytes_and_includes_silence():
    from laptop.goodput import GoodputMeter
    meter = GoodputMeter(window_seconds=5)
    meter.start(10)
    for seq in range(100):
        assert meter.record(packet(seq), 10 + seq / 10)
    assert not meter.record(packet(99), 20)
    report = meter.report(20)
    # Version 3: 38 bytes per packet, so 10 packets/s is 3.04 kbps.
    assert PACKET_SIZE == 38
    assert report['packet_bytes'] == 3800
    assert report['packets'] == 100
    assert report['elapsed_seconds'] == 10
    assert report['average_kbps'] == pytest.approx(3.04)
    assert report['rolling_kbps'] == pytest.approx(3.04)
    assert meter.report(25)['rolling_kbps'] == 0
    assert meter.report(25)['average_kbps'] == pytest.approx(3800*8/15/1000)


def test_observation_excludes_startup_and_drain_and_counts_zero_traffic():
    from laptop.goodput import GoodputMeter
    meter = GoodputMeter()
    meter.record(packet(0), 1)
    meter.start(3)
    assert not meter.record(packet(0), 3)
    meter.record(packet(1), 4)
    meter.stop(8)
    meter.record(packet(2), 9)
    report = meter.report(20)
    assert report['packet_bytes'] == 38
    assert report['elapsed_seconds'] == 5
    assert report['average_kbps'] == pytest.approx(.0608)
    empty = GoodputMeter()
    empty.start(0)
    assert empty.report(10)['average_kbps'] == 0


def test_goodput_accepts_any_v3_values_and_rejects_undecoded_bytes_and_reordering():
    from laptop.goodput import GoodputMeter
    meter = GoodputMeter()
    meter.start(0)
    assert not meter.record(encode_packet(SensorPacket(1, 1, 0, 0, (0,)*11)), 1)
    assert meter.record(SensorPacket(1, 1, 0, 0, (0,)*11), 2)
    assert meter.record(packet(5), 3)
    assert not meter.record(packet(4), 4)
    assert meter.record(packet(6), 5)
    assert meter.report(6)['packets'] == 3


def test_goodput_window_and_timestamps_are_validated():
    from laptop.goodput import GoodputMeter
    for seconds in (0, -1, float('nan'), True):
        with pytest.raises(ValueError):
            GoodputMeter(window_seconds=seconds)
    meter = GoodputMeter()
    with pytest.raises(ValueError):
        meter.start(float('nan'))
    meter.start(5)
    with pytest.raises(ValueError):
        meter.stop(4)
