"""Fixed 40-byte sensor codec, protocol version 4: 16-byte header + 12 x int16.

Only version 4 exists. Values travel as int16; physical = int16 / SCALES[i].
A BLE sensor notification carries 1 to 6 whole packets back to back, oldest
first (split_notification); commands carry exactly one (decode_packet).
"""

from dataclasses import dataclass
import struct
import json
from pathlib import Path
import random
from typing import Tuple


VERSION = 4
CHANNELS = ("ax", "ay", "az", "gx", "gy", "gz", "yaw", "pitch", "roll", "flex1", "flex2", "flex3")
# g x10000, deg/s x100, degrees x100, flex fraction x10000 (provisional until checked
# against real glove samples).
SCALES = (10000, 10000, 10000, 100, 100, 100, 100, 100, 100, 10000, 10000, 10000)
CHANNEL_COUNT = len(CHANNELS)
_PACKET = struct.Struct("<2sBBIII{}h".format(CHANNEL_COUNT))
PACKET_SIZE = _PACKET.size
SENSOR_MTU_FLOOR = PACKET_SIZE + 3  # ATT notification header
# The glove sends the samples waiting since its last notification together
# (firmware comms_send_queue.h); six fit one 251-byte link-layer packet.
MAX_PACKETS_PER_NOTIFICATION = 6
PACKET_MAGIC = b"LC"
SENSOR_CHARACTERISTIC_UUID = "6e1c0005-7a45-4dc4-b678-3f2d5a9c1001"


def _integer(value, minimum: int, maximum: int, name: str) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("{} must be an integer in [{}, {}]".format(name, minimum, maximum))


@dataclass(frozen=True)
class SensorPacket:
    device_id: int
    boot_id: int
    seq: int
    uptime_ms: int
    values: Tuple[int, ...]
    version: int = VERSION

    def __post_init__(self) -> None:
        _integer(self.device_id, 1, 2, "device_id")
        _integer(self.version, VERSION, VERSION, "version")
        for name in ("boot_id", "seq", "uptime_ms"):
            _integer(getattr(self, name), 0, 0xFFFFFFFF, name)
        if not isinstance(self.values, (list, tuple)) or len(self.values) != CHANNEL_COUNT:
            raise ValueError("values must contain exactly twelve int16 integers")
        for value in self.values:
            _integer(value, -32768, 32767, "channel")
        object.__setattr__(self, "values", tuple(self.values))

    def to_message(self, session_id: str, request_id=None) -> dict:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a nonempty string")
        if request_id is not None:
            _integer(request_id, 1, 0xFFFFFFFF, "request_id")
            if request_id != self.seq:
                raise ValueError("commands require seq equal to request_id")
        return {
            "v": self.version,
            "type": "SENSOR_BATCH",
            "session_id": session_id,
            "device_id": self.device_id,
            "boot_id": self.boot_id,
            "seq": self.seq,
            "uptime_ms": self.uptime_ms,
            "values": list(self.values),
            "request_id": request_id,
        }


def encode_packet(packet: SensorPacket) -> bytes:
    if not isinstance(packet, SensorPacket):
        raise ValueError("packet must be a SensorPacket")
    return _PACKET.pack(
        PACKET_MAGIC, packet.version, packet.device_id, packet.boot_id, packet.seq, packet.uptime_ms,
        *packet.values
    )


def decode_packet(data: bytes) -> SensorPacket:
    if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) != PACKET_SIZE:
        raise ValueError("sensor packet must contain exactly {} bytes".format(PACKET_SIZE))
    magic, version, device_id, boot_id, seq, uptime_ms, *values = _PACKET.unpack(data)
    if magic != PACKET_MAGIC:
        raise ValueError("invalid sensor packet magic; expected LC")
    if version != VERSION:
        raise ValueError("unsupported sensor packet version")
    return SensorPacket(device_id, boot_id, seq, uptime_ms, tuple(values), version)


def split_notification(data) -> Tuple[bytes, ...]:
    """Cut one BLE sensor notification into its 40-byte packets, oldest first.

    Every packet must decode; otherwise the whole notification is refused, so a
    bad notification stays one malformed item, as a bad single packet was.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ValueError("sensor notification must be bytes")
    data = bytes(data)
    count, rest = divmod(len(data), PACKET_SIZE)
    if rest or not 1 <= count <= MAX_PACKETS_PER_NOTIFICATION:
        raise ValueError("sensor notification must contain 1 to {} whole {}-byte packets".format(
            MAX_PACKETS_PER_NOTIFICATION, PACKET_SIZE))
    pieces = tuple(data[index * PACKET_SIZE:(index + 1) * PACKET_SIZE] for index in range(count))
    for piece in pieces:
        decode_packet(piece)
    return pieces


def physical_values(values) -> Tuple[float, ...]:
    """Restore the agreed physical units from the transported int16 values."""
    if not isinstance(values, (list, tuple)) or len(values) != CHANNEL_COUNT:
        raise ValueError("values must contain exactly twelve int16 integers")
    for value in values:
        _integer(value, -32768, 32767, "channel")
    return tuple(value / scale for value, scale in zip(values, SCALES))

