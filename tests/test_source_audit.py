import struct

import pytest

from common.sensor import DUMMY_FIXTURES, SensorPacket
from laptop.source_audit import SOURCE_STATS_SIZE, SourceAudit, SourceStats, parse_source_stats

LAYOUT = "<4sBB2xIIIIIII"


def stats_bytes(magic=b"LCS3", device=2, flags=0, boot=7, next_seq=3, submitted=3,
                failures=0, idle=0, unread=0, refused=0):
    return struct.pack(LAYOUT, magic, device, flags, boot, next_seq, submitted, failures,
                       idle, unread, refused)


def packet(device_id, boot_id, seq):
    return SensorPacket(device_id, boot_id, seq, (100 * seq) & 0xFFFFFFFF,
                        DUMMY_FIXTURES[seq % len(DUMMY_FIXTURES)])


def test_literal_source_snapshot_decodes_all_wire_fields():
    raw = (b"LCS3" + bytes([2, 1, 0, 0])
           + bytes.fromhex("07000000" "03000000" "03000000" "00000000"
                           "0a000000" "02000000" "01000000"))

    assert len(raw) == SOURCE_STATS_SIZE == 36
    assert parse_source_stats(raw, expected_device_id=2) == SourceStats(
        device_id=2, boot_id=7, next_seq=3, submitted=3, failures=0,
        gated=True, idle=10, unread=2, refused=1)


def test_ungated_snapshot_has_a_clear_flag_and_zero_skip_counters():
    stats = parse_source_stats(stats_bytes(), expected_device_id=2)
    assert stats.gated is False
    assert (stats.idle, stats.unread, stats.refused) == (0, 0, 0)


@pytest.mark.parametrize("flags, gated", [(0x02, False), (0x03, True)])
def test_snapshot_of_a_batching_board_is_accepted(flags, gated):
    # Every board since 2026-10-19 sets bit 1: its sensor notifications may
    # carry several packets. It does not change any counter.
    stats = parse_source_stats(stats_bytes(flags=flags), expected_device_id=2)
    assert stats.gated is gated
    assert (stats.next_seq, stats.submitted, stats.failures) == (3, 3, 0)


@pytest.mark.parametrize("raw", [
    stats_bytes(magic=b"BAD!"),
    stats_bytes(magic=b"W7S1"),
    # Reserved bytes 6 and 7 must stay zero.
    b"LCS3" + bytes([2, 0, 1, 0]) + bytes(28),
    b"LCS3" + bytes([2, 0, 0, 1]) + bytes(28),
    # Only flag bits 0 (gated) and 1 (several packets per notification) are defined.
    stats_bytes(flags=0x04),
    stats_bytes(flags=0x83),
    stats_bytes(flags=0xFF),
    bytes(35),
    stats_bytes() + bytes(1),
])
def test_source_snapshot_rejects_wrong_schema(raw):
    with pytest.raises(ValueError):
        parse_source_stats(raw, expected_device_id=2)


@pytest.mark.parametrize("raw", [
    # The earlier 24-byte LCS3 value and the version 2 LCS1 value.
    struct.pack("<4sB3xIIII", b"LCS3", 2, 7, 3, 3, 0),
    struct.pack("<4sB3xIIII", b"LCS1", 2, 7, 3, 3, 0),
])
def test_24_byte_source_snapshot_asks_for_a_reflash(raw):
    with pytest.raises(ValueError, match="exactly 36 bytes; flash this board"):
        parse_source_stats(raw, expected_device_id=2)


@pytest.mark.parametrize("magic", [b"LCS1", b"LCS2", b"LCS4", b"lcs3"])
def test_source_snapshot_from_other_firmware_asks_for_a_reflash(magic):
    # Version 2 firmware reported LCS1; only the current LCS3 marker is accepted.
    raw = stats_bytes(magic=magic)
    with pytest.raises(ValueError, match="flash this board with the current firmware"):
        parse_source_stats(raw, expected_device_id=2)


def test_source_snapshot_rejects_wrong_device_identity():
    raw = stats_bytes(device=1)
    with pytest.raises(ValueError, match="device"):
        parse_source_stats(raw, expected_device_id=2)


def test_source_contract_rejects_device_ids_outside_two_gloves():
    raw = stats_bytes(device=3, next_seq=0, submitted=0)
    with pytest.raises(ValueError, match="device"):
        parse_source_stats(raw)
    with pytest.raises(ValueError, match="device"):
        SourceAudit(expected_device_id=3)
    valid = stats_bytes(device=1, next_seq=0, submitted=0)
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


def test_source_stats_fields_are_typed():
    with pytest.raises(ValueError, match="gated"):
        SourceStats(1, 7, 0, 0, 0, gated=1)
    for name in ("idle", "unread", "refused"):
        with pytest.raises(ValueError, match=name):
            SourceStats(1, 7, 0, 0, 0, **{name: -1})
        with pytest.raises(ValueError, match=name):
            SourceStats(1, 7, 0, 0, 0, **{name: 1 << 32})


def test_gated_report_counts_skipped_ticks_without_affecting_clean():
    audit = SourceAudit(expected_device_id=1)
    audit.start(SourceStats(1, 7, 10, 10, 0, gated=True, idle=100, unread=4, refused=1))
    for seq in (10, 11, 12):
        sample = packet(1, 7, seq)
        audit.received(sample)
        audit.acknowledged(sample)
    audit.finish(SourceStats(1, 7, 13, 13, 0, gated=True, idle=150, unread=6, refused=1))

    report = audit.report()
    assert report["gated"] is True
    assert report["generated"] == 3
    assert (report["source_idle"], report["source_unread"], report["source_refused"]) == (50, 2, 0)
    assert report["source_ticks"] == 3 + 50 + 2 + 0
    assert report["clean"] is True


def test_skip_counters_wrap_like_the_sequence_counters():
    audit = SourceAudit(expected_device_id=2)
    audit.start(SourceStats(2, 9, 0, 0, 0, gated=True, idle=0xFFFFFFFF, unread=0xFFFFFFFE))
    audit.finish(SourceStats(2, 9, 0, 0, 0, gated=True, idle=4, unread=1, refused=2))

    report = audit.report()
    assert (report["source_idle"], report["source_unread"], report["source_refused"]) == (5, 3, 2)
    # Idle the whole time: nothing was generated, so the capture is not clean.
    assert report["generated"] == 0
    assert report["clean"] is False


def test_skip_counts_are_unknown_across_a_reboot_and_gated_follows_the_end_snapshot():
    audit = SourceAudit(expected_device_id=1)
    audit.start(SourceStats(1, 7, 0, 0, 0, gated=False))
    audit.finish(SourceStats(1, 8, 5, 5, 0, gated=True, idle=3))

    report = audit.report()
    assert report["gated"] is True
    for name in ("generated", "source_idle", "source_unread", "source_refused", "source_ticks"):
        assert report[name] is None
    assert report["clean"] is False

    started_only = SourceAudit(expected_device_id=1)
    started_only.start(SourceStats(1, 7, 0, 0, 0, gated=True))
    assert started_only.report()["gated"] is True
    assert SourceAudit(expected_device_id=1).report()["gated"] is None


def test_snapshots_differing_only_in_skip_counters_are_different_snapshots():
    # The bridge compares the quiesced and final reads whole: once notifications
    # are off, the firmware commits no tick at all, idle ones included.
    base = SourceStats(1, 7, 3, 3, 0, gated=True, idle=5)
    for name in ("idle", "unread", "refused"):
        assert SourceStats(1, 7, 3, 3, 0, gated=True,
                           **dict({"idle": 5}, **{name: 6})) != base
