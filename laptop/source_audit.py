"""Decode ESP source counters and reconcile a finite capture in constant space."""
from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Optional

from common.sensor import SensorPacket


SOURCE_STATS_UUID = "6e1c0006-7a45-4dc4-b678-3f2d5a9c1001"
_SOURCE_STATS = struct.Struct("<4sB3xIIII")
_UINT32_MASK = 0xFFFFFFFF


def _delta32(end: int, start: int) -> int:
    return (end - start) & _UINT32_MASK


@dataclass(frozen=True)
class SourceStats:
    device_id: int
    boot_id: int
    next_seq: int
    submitted: int
    failures: int

    def __post_init__(self):
        if type(self.device_id) is not int or self.device_id not in (1, 2):
            raise ValueError("source statistics device ID must be 1 or 2")
        for name in ("boot_id", "next_seq", "submitted", "failures"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= _UINT32_MASK:
                raise ValueError(f"source statistics {name} must be uint32")


def parse_source_stats(data: bytes, *, expected_device_id: Optional[int] = None) -> SourceStats:
    """Parse the exact protected 24-byte source-statistics value."""
    if (expected_device_id is not None
            and (type(expected_device_id) is not int
                 or expected_device_id not in (1, 2))):
        raise ValueError("expected device ID must be 1 or 2")
    if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) != _SOURCE_STATS.size:
        raise ValueError("source statistics must contain exactly 24 bytes")
    raw = bytes(data)
    if raw[5:8] != b"\0\0\0":
        raise ValueError("source statistics reserved bytes must be zero")
    magic, device_id, boot_id, next_seq, submitted, failures = _SOURCE_STATS.unpack(raw)
    if magic not in (b"LCS1", b"W7S1"):
        raise ValueError("invalid source statistics magic")
    if expected_device_id is not None and device_id != expected_device_id:
        raise ValueError("source statistics device identity mismatch")
    return SourceStats(device_id, boot_id, next_seq, submitted, failures)


# A descriptive alias for callers that use codec-style naming.
decode_source_stats = parse_source_stats


class SourceAudit:
    """Reconcile source generation, BLE reception and acknowledged delivery.

    Only counters and sequence endpoints are retained, so memory use is
    independent of capture duration.
    """

    def __init__(self, expected_device_id: int):
        if type(expected_device_id) is not int or expected_device_id not in (1, 2):
            raise ValueError("expected device ID must be 1 or 2")
        self.expected_device_id = expected_device_id
        self.start_stats: Optional[SourceStats] = None
        self.end_stats: Optional[SourceStats] = None
        self.received_count = 0
        self.acked_count = 0
        self.first_received_seq: Optional[int] = None
        self.last_received_seq: Optional[int] = None
        self.first_acked_seq: Optional[int] = None
        self.last_acked_seq: Optional[int] = None
        self._next_received: Optional[int] = None
        self._next_acked: Optional[int] = None
        self.sequence_anomalies = 0
        self.ack_sequence_anomalies = 0
        self.identity_mismatches = 0
        self.boot_mismatches = 0
        self.interruptions = 0
        self.snapshot_errors = 0

    def _check_stats(self, stats: SourceStats) -> None:
        if not isinstance(stats, SourceStats):
            raise TypeError("snapshot must be SourceStats")
        if stats.device_id != self.expected_device_id:
            raise ValueError("source statistics device identity mismatch")

    def start(self, stats: SourceStats) -> None:
        self._check_stats(stats)
        if self.start_stats is not None:
            raise RuntimeError("source audit already started")
        self.start_stats = stats
        self._next_received = stats.next_seq
        self._next_acked = stats.next_seq

    def finish(self, stats: SourceStats) -> None:
        self._check_stats(stats)
        if self.start_stats is None:
            raise RuntimeError("source audit has no start snapshot")
        if self.end_stats is not None:
            raise RuntimeError("source audit already finished")
        self.end_stats = stats

    def mark_interruption(self) -> None:
        self.interruptions += 1

    def mark_snapshot_error(self) -> None:
        self.snapshot_errors += 1

    def _packet_matches(self, packet: SensorPacket) -> bool:
        if packet.device_id != self.expected_device_id:
            self.identity_mismatches += 1
            return False
        if self.start_stats is None or packet.boot_id != self.start_stats.boot_id:
            self.boot_mismatches += 1
            return False
        return True

    def received(self, packet: SensorPacket) -> bool:
        if not self._packet_matches(packet):
            return False
        if self.first_received_seq is None:
            self.first_received_seq = packet.seq
        if packet.seq != self._next_received:
            self.sequence_anomalies += 1
        self._next_received = (packet.seq + 1) & _UINT32_MASK
        self.last_received_seq = packet.seq
        self.received_count += 1
        return True

    def acknowledged(self, packet: SensorPacket) -> bool:
        if not self._packet_matches(packet):
            return False
        if self.first_acked_seq is None:
            self.first_acked_seq = packet.seq
        if packet.seq != self._next_acked:
            self.ack_sequence_anomalies += 1
        self._next_acked = (packet.seq + 1) & _UINT32_MASK
        self.last_acked_seq = packet.seq
        self.acked_count += 1
        return True

    def report(self) -> dict:
        start, end = self.start_stats, self.end_stats
        same_boot = bool(start and end and start.boot_id == end.boot_id)
        generated = _delta32(end.next_seq, start.next_seq) if same_boot else None
        submitted = _delta32(end.submitted, start.submitted) if same_boot else None
        failures = _delta32(end.failures, start.failures) if same_boot else None
        source_consistent = bool(same_boot and generated == submitted + failures)
        missing_received = (max(0, generated - self.received_count)
                            if generated is not None else None)
        missing_acked = (max(0, generated - self.acked_count)
                         if generated is not None else None)
        complete = start is not None and end is not None
        clean = bool(
            complete and same_boot and source_consistent and generated > 0 and failures == 0
            and self.received_count == generated and self.acked_count == generated
            and self.sequence_anomalies == 0 and self.ack_sequence_anomalies == 0
            and self.identity_mismatches == 0 and self.boot_mismatches == 0
            and self.interruptions == 0 and self.snapshot_errors == 0
        )
        return {
            "complete": complete,
            "clean": clean,
            "device_id": self.expected_device_id,
            "boot_id": start.boot_id if start is not None else None,
            "start_next_seq": start.next_seq if start is not None else None,
            "end_next_seq": end.next_seq if end is not None else None,
            "generated": generated,
            "source_submitted": submitted,
            "source_failures": failures,
            "source_consistent": source_consistent,
            "received": self.received_count,
            "acked": self.acked_count,
            "missing_received": missing_received,
            "missing_acked": missing_acked,
            "first_received_seq": self.first_received_seq,
            "last_received_seq": self.last_received_seq,
            "first_acked_seq": self.first_acked_seq,
            "last_acked_seq": self.last_acked_seq,
            "sequence_anomalies": self.sequence_anomalies,
            "ack_sequence_anomalies": self.ack_sequence_anomalies,
            "identity_mismatches": self.identity_mismatches,
            "boot_mismatches": self.boot_mismatches,
            "interruptions": self.interruptions,
            "snapshot_errors": self.snapshot_errors,
        }
