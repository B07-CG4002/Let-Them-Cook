import struct

import pytest

from common.sensor import SensorPacket, dummy_values
from laptop.source_audit import SourceAudit, SourceStats, parse_source_stats


def packet(device_id, boot_id, seq):
    return SensorPacket(device_id, boot_id, seq, (100 * seq) & 0xFFFFFFFF,
                        dummy_values(seq))


def test_literal_source_snapshot_decodes_all_wire_fields():
    raw = (b"LCS1" + bytes([2, 0, 0, 0])
           + bytes.fromhex("07000000030000000300000000000000"))

    assert parse_source_stats(raw, expected_device_id=2) == SourceStats(
        device_id=2, boot_id=7, next_seq=3, submitted=3, failures=0)


def test_legacy_source_snapshot_still_decodes():
    raw = (b"W7S1" + bytes([2, 0, 0, 0])
           + bytes.fromhex("07000000030000000300000000000000"))

    assert parse_source_stats(raw, expected_device_id=2) == SourceStats(
        device_id=2, boot_id=7, next_seq=3, submitted=3, failures=0)


@pytest.mark.parametrize("raw", [
    struct.pack("<4sB3xIIII", b"BAD!", 2, 7, 3, 3, 0),
    b"W7S1" + bytes([2, 1, 0, 0]) + bytes(16),
    bytes(23),
])
def test_source_snapshot_rejects_wrong_schema(raw):
    with pytest.raises(ValueError):
        parse_source_stats(raw, expected_device_id=2)


def test_source_snapshot_rejects_wrong_device_identity():
    raw = struct.pack("<4sB3xIIII", b"W7S1", 1, 7, 3, 3, 0)
    with pytest.raises(ValueError, match="device"):
        parse_source_stats(raw, expected_device_id=2)


def test_source_contract_rejects_device_ids_outside_two_gloves():
    raw = struct.pack("<4sB3xIIII", b"W7S1", 3, 7, 0, 0, 0)
    with pytest.raises(ValueError, match="device"):
        parse_source_stats(raw)
    with pytest.raises(ValueError, match="device"):
        SourceAudit(expected_device_id=3)
    valid = struct.pack("<4sB3xIIII", b"W7S1", 1, 7, 0, 0, 0)
    with pytest.raises(ValueError, match="expected device"):
        parse_source_stats(valid, expected_device_id=True)


def test_empty_source_interval_is_not_a_clean_capture():
    audit = SourceAudit(expected_device_id=1)
    audit.start(SourceStats(1, 7, 4, 9, 2))
    audit.finish(SourceStats(1, 7, 4, 9, 2))

    assert audit.report()["generated"] == 0
    assert audit.report()["clean"] is False


def test_reconciliation_detects_a_missing_generated_tail_packet():
    audit = SourceAudit(expected_device_id=2)
    audit.start(SourceStats(2, 7, 0, 0, 0))
    for seq in (0, 1):
        sample = packet(2, 7, seq)
        audit.received(sample)
        audit.acknowledged(sample)
    audit.finish(SourceStats(2, 7, 3, 3, 0))

    report = audit.report()
    assert report["generated"] == 3
    assert report["received"] == 2
    assert report["acked"] == 2
    assert report["missing_received"] == 1
    assert report["missing_acked"] == 1
    assert report["clean"] is False


def test_reconciliation_anchors_first_packet_and_rejects_wrong_packet_identity():
    audit = SourceAudit(expected_device_id=2)
    audit.start(SourceStats(2, 7, 10, 20, 0))
    audit.received(packet(2, 7, 11))
    audit.acknowledged(packet(2, 7, 11))
    audit.received(packet(1, 7, 10))
    audit.received(packet(2, 8, 10))
    audit.finish(SourceStats(2, 7, 12, 22, 0))

    report = audit.report()
    assert report["sequence_anomalies"] == 1
    assert report["identity_mismatches"] == 1
    assert report["boot_mismatches"] == 1
    assert report["clean"] is False


def test_reconciliation_tracks_uint32_wrap_in_constant_state():
    audit = SourceAudit(expected_device_id=2)
    audit.start(SourceStats(2, 9, 0xFFFFFFFE, 10, 4))
    for seq in (0xFFFFFFFE, 0xFFFFFFFF, 0):
        sample = packet(2, 9, seq)
        audit.received(sample)
        audit.acknowledged(sample)
    audit.finish(SourceStats(2, 9, 1, 13, 4))

    report = audit.report()
    assert report["generated"] == 3
    assert report["first_received_seq"] == 0xFFFFFFFE
    assert report["last_received_seq"] == 0
    assert report["clean"] is True
    assert not hasattr(audit, "packets")
