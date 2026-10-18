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


async def read_frame(reader, timeout=5.0, idle_timeout=None):
    """Read exactly one frame.

    Without idle_timeout one deadline covers both header and body. With
    idle_timeout the first header byte may take up to idle_timeout seconds (a
    quiet peer between frames); once it arrives, the rest of the header and the
    body must arrive within timeout.
    """
    async def receive(first=b""):
        try:
            header = first + await reader.readexactly(4 - len(first))
        except asyncio.IncompleteReadError as exc:
            if exc.partial or first:
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
    if idle_timeout is None:
        return await asyncio.wait_for(receive(), timeout=timeout)
    # A clean EOF here is still IncompleteReadError with no partial bytes.
    first = await asyncio.wait_for(reader.readexactly(1), timeout=idle_timeout)
    return await asyncio.wait_for(receive(first), timeout=timeout)


async def write_frame(writer, message, timeout=5.0):
    """One owning task must serialize calls to this function per connection."""
    writer.write(encode_frame(message))
    await asyncio.wait_for(writer.drain(), timeout=timeout)
