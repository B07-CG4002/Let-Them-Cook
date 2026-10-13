"""Bounded, strict UTF-8 JSON object framing shared by all B07 Communications peers."""
import asyncio
import json
import math
import struct

MAX_FRAME_SIZE = 16384
STREAM_LIMIT = MAX_FRAME_SIZE + 4


class ProtocolError(ValueError):
    """A peer supplied an invalid frame or message."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ProtocolError("non-finite JSON number")


def _float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ProtocolError("non-finite JSON number")
    return parsed


def encode_frame(message):
    """Encode an object, rejecting values JSON cannot safely represent."""
    if not isinstance(message, dict):
        raise ProtocolError("frame must contain a JSON object")
    try:
        body = json.dumps(message, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ProtocolError("invalid JSON object") from exc
    if not 0 < len(body) <= MAX_FRAME_SIZE:
        raise ProtocolError("frame length outside 1..16384")
    return struct.pack("!I", len(body)) + body


async def read_frame(reader, timeout=5.0):
    """Read exactly one frame with one deadline for both header and body."""
    async def receive():
        try:
            header = await reader.readexactly(4)
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                raise ProtocolError("incomplete frame header") from exc
            raise  # Clean EOF between frames, distinguishable by consumers.
        length = struct.unpack("!I", header)[0]
        if not 0 < length <= MAX_FRAME_SIZE:
            raise ProtocolError("frame length outside 1..16384")
        try:
            body = await reader.readexactly(length)
        except asyncio.IncompleteReadError as exc:
            raise ProtocolError("incomplete frame body") from exc
        try:
            message = json.loads(body.decode("utf-8"), object_pairs_hook=_object,
                                 parse_constant=_constant, parse_float=_float)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise ProtocolError("invalid UTF-8 JSON frame") from exc
        if not isinstance(message, dict):
            raise ProtocolError("frame must contain a JSON object")
        return message
    return await asyncio.wait_for(receive(), timeout=timeout)


async def write_frame(writer, message, timeout=5.0):
    """One owning task must serialize calls to this function per connection."""
    writer.write(encode_frame(message))
    await asyncio.wait_for(writer.drain(), timeout=timeout)
