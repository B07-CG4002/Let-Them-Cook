"""Decode ESP source counters and reconcile a finite capture in constant space."""
from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Optional



SOURCE_STATS_UUID = "6e1c0006-7a45-4dc4-b678-3f2d5a9c1001"
# 'LCS4', device ID, flags, two reserved zero bytes, then seven uint32 totals
# since boot: boot ID, next sequence, submitted, failures, idle, unread, refused.
_SOURCE_STATS = struct.Struct("<4sBB2xIIIIIII")
# LCS4 follows sensor packet version 4. A board still running version 2
# firmware reports LCS1, so an audited bridge refuses it before subscribing.
SOURCE_STATS_MAGIC = b"LCS4"
SOURCE_STATS_SIZE = _SOURCE_STATS.size
# Flag bit 0: the board sends only while its glove module reports movement.
SOURCE_FLAG_GATED = 0x01
# Flag bit 1: a sensor notification may carry several packets
# (common.sensor.split_notification). Set by every board since 2026-10-19; a
# laptop older than that refuses it here, before subscribing.
SOURCE_FLAG_BATCHED = 0x02
_KNOWN_FLAGS = SOURCE_FLAG_GATED | SOURCE_FLAG_BATCHED
_UINT32_MASK = 0xFFFFFFFF
_COUNTERS = ("boot_id", "next_seq", "submitted", "failures", "idle", "unread", "refused")


def _delta32(end: int, start: int) -> int:
    return (end - start) & _UINT32_MASK


@dataclass(frozen=True)
class SourceStats:
    device_id: int
    boot_id: int
    next_seq: int
    submitted: int
    failures: int
    # A gated board skips the send ticks its glove module calls idle. idle,
    # unread and refused count send ticks that produced no packet and took
    # no sequence number.
    gated: bool = False
    idle: int = 0
    unread: int = 0
    refused: int = 0

    def __post_init__(self):
        if type(self.device_id) is not int or self.device_id not in (1, 2):
            raise ValueError("source statistics device ID must be 1 or 2")
        if type(self.gated) is not bool:
            raise ValueError("source statistics gated must be a bool")
        for name in _COUNTERS:
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= _UINT32_MASK:
                raise ValueError(f"source statistics {name} must be uint32")


def parse_source_stats(data: bytes, *, expected_device_id: Optional[int] = None) -> SourceStats:
    """Parse the exact protected 36-byte source-statistics value."""
    if (expected_device_id is not None
            and (type(expected_device_id) is not int
                 or expected_device_id not in (1, 2))):
        raise ValueError("expected device ID must be 1 or 2")
    if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) != _SOURCE_STATS.size:
        # The earlier 24-byte LCS4 and the version 2 LCS1 values both end here.
        raise ValueError("source statistics must contain exactly 36 bytes; "
                         "flash this board with the current firmware")
    raw = bytes(data)
    if raw[6:8] != bytes(2):
        raise ValueError("source statistics reserved bytes must be zero")
    (magic, device_id, flags, boot_id, next_seq, submitted, failures,
     idle, unread, refused) = _SOURCE_STATS.unpack(raw)
    if magic != SOURCE_STATS_MAGIC:
        raise ValueError("Expected LCS4 three-flex firmware; flash the *-glove-flex3 environment. Old LCS3 gloves use src.hand_ai.record_gui")
    if flags & ~_KNOWN_FLAGS:
        raise ValueError("source statistics flags contain unknown bits")
    if expected_device_id is not None and device_id != expected_device_id:
        raise ValueError("source statistics device identity mismatch")
    return SourceStats(device_id, boot_id, next_seq, submitted, failures,
                       gated=bool(flags & SOURCE_FLAG_GATED), idle=idle, unread=unread,
                       refused=refused)

