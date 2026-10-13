"""Fixed 32-byte sensor codec: deterministic v1 and editable-fixture v2."""

from dataclasses import dataclass
import struct
import json
from pathlib import Path
import random
from typing import Tuple


_PACKET = struct.Struct("<2sBBIII8h")
PACKET_SIZE = _PACKET.size
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
    version: int = 1

    def __post_init__(self) -> None:
        _integer(self.device_id, 1, 2, "device_id")
        _integer(self.version, 1, 2, "version")
        for name in ("boot_id", "seq", "uptime_ms"):
            _integer(getattr(self, name), 0, 0xFFFFFFFF, name)
        if not isinstance(self.values, (list, tuple)) or len(self.values) != 8:
            raise ValueError("values must contain exactly eight int16 integers")
        for value in self.values:
            _integer(value, -32768, 32767, "channel")
        object.__setattr__(self, "values", tuple(self.values))

    def to_message(self, session_id: str, request_id=None) -> dict:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a nonempty string")
        if request_id is not None:
            _integer(request_id, 1, 0xFFFFFFFF, "request_id")
            if self.version != 2 or request_id != self.seq:
                raise ValueError("commands require v2 and seq equal to request_id")
        result = {
            "v": self.version,
            "type": "SENSOR_BATCH",
            "session_id": session_id,
            "device_id": self.device_id,
            "boot_id": self.boot_id,
            "seq": self.seq,
            "uptime_ms": self.uptime_ms,
            "values": list(self.values),
        }
        if self.version == 2:
            result["request_id"] = request_id
        return result


def encode_packet(packet: SensorPacket, *, magic: bytes = b"LC") -> bytes:
    if not isinstance(packet, SensorPacket):
        raise ValueError("packet must be a SensorPacket")
    if type(magic) is not bytes or magic not in (b"LC", b"W7"):
        raise ValueError("packet magic must be LC or W7")
    return _PACKET.pack(
        magic, packet.version, packet.device_id, packet.boot_id, packet.seq, packet.uptime_ms,
        *packet.values
    )


def decode_packet(data: bytes) -> SensorPacket:
    if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) != PACKET_SIZE:
        raise ValueError("sensor packet must contain exactly 32 bytes")
    magic, version, device_id, boot_id, seq, uptime_ms, *values = _PACKET.unpack(data)
    if magic not in (b"LC", b"W7"):
        raise ValueError("invalid sensor packet magic")
    if version not in (1, 2):
        raise ValueError("unsupported sensor packet version")
    return SensorPacket(device_id, boot_id, seq, uptime_ms, tuple(values), version)


def dummy_values(seq: int) -> Tuple[int, ...]:
    _integer(seq, 0, 0xFFFFFFFF, "seq")
    base = (seq % 2000) - 1000
    return tuple(base + 10 * index for index in range(8))


def load_fixtures(path=None):
    """Read editable source data; validate before generation or transmission."""
    source = Path(path) if path is not None else Path(__file__).with_name("dummy_fixtures.json")
    rows = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not 2 <= len(rows) <= 64:
        raise ValueError("provide between two and 64 dummy fixtures")
    return tuple(SensorPacket(1, 0, 0, 0, row, version=2).values for row in rows)


DUMMY_FIXTURES = load_fixtures()
_RANDOM = random.SystemRandom()


def choose_fixture(rng=None):
    """Use system randomness normally; inject random.Random(seed) in tests."""
    return (rng if rng is not None else _RANDOM).choice(DUMMY_FIXTURES)
