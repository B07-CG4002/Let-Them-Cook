"""Standalone B07 Communications Android/Termux receiver (Python 3.8+, standard library).

Connects only to this Phone's loopback SSH forward. TLS identity remains the
Ultra96 service identity. No Laptop connection, replay request or plaintext mode.
"""
import argparse
import asyncio
from collections import OrderedDict
import json
import math
from pathlib import Path
import ssl
import sys

MAX_FRAME_BYTES = 16384
TLS_IDENTITY = "ultra96.week7.internal"
GESTURES = ("REST", "FIST", "OPEN", "POINT")


class ProtocolError(ValueError):
    pass


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value):
    raise ProtocolError("non-finite JSON number")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ProtocolError("non-finite JSON number")
    return number


def _bounded_integer(value):
    # All integer fields in the Phone contract fit uint32. Bound conversion
    # explicitly so Python 3.8 and versions with an integer-digit cap agree.
    if len(value.lstrip("-")) > 10:
        raise ProtocolError("integer outside B07 Communications field bounds")
    return int(value)


def encode_frame(message):
    if not isinstance(message, dict):
        raise ProtocolError("JSON root must be an object")
    try:
        data = json.dumps(message, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as error:
        raise ProtocolError("invalid JSON") from error
    if not 1 <= len(data) <= MAX_FRAME_BYTES:
        raise ProtocolError("frame outside 1..16384 bytes")
    return len(data).to_bytes(4, "big") + data


async def read_frame(reader, timeout=5.0, first_byte_timeout=None, allow_idle=False):
    # A subscribed connection may be quiet indefinitely. Once a byte arrives,
    # the remaining prefix and body must complete within one shared frame budget.
    first = b""
    if allow_idle or first_byte_timeout is not None:
        try:
            first = await asyncio.wait_for(reader.readexactly(1),
                                           None if allow_idle else first_byte_timeout)
        except asyncio.IncompleteReadError as error:
            raise ProtocolError("malformed or incomplete frame") from error

    async def read():
        try:
            size = int.from_bytes(first + await reader.readexactly(4 - len(first)), "big")
            if not 1 <= size <= MAX_FRAME_BYTES:
                raise ProtocolError("frame outside 1..16384 bytes")
            message = json.loads((await reader.readexactly(size)).decode("utf-8"),
                                 object_pairs_hook=_unique_object, parse_constant=_reject_constant,
                                 parse_float=_finite_float, parse_int=_bounded_integer)
            if not isinstance(message, dict):
                raise ProtocolError("JSON root must be an object")
            return message
        except (asyncio.IncompleteReadError, UnicodeError, ValueError, RecursionError) as error:
            raise ProtocolError("malformed or incomplete frame") from error
    return await asyncio.wait_for(read(), timeout)


def _header(message, fields, kind, session, versions=(1,)):
    if (not isinstance(message, dict) or set(message) != fields or
            type(message.get("v")) is not int or message["v"] not in versions or
            message.get("type") != kind or message.get("session_id") != session):
        raise ProtocolError("unexpected schema or session")
    validate_session(message["session_id"])


def validate_session(session):
    if (not isinstance(session, str) or not 1 <= len(session) <= 128 or
            any(ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF for character in session)):
        raise ProtocolError("session must contain 1..128 Unicode characters without controls")
    return session


def validate_subscribed(message, session):
    _header(message, {"v", "type", "session_id"}, "SUBSCRIBED", session)


def validate_result(message, session):
    fields = {"v", "type", "session_id", "device_id", "boot_id", "seq", "result_id", "gesture", "confidence"}
    version = message.get("v") if isinstance(message, dict) else None
    if version == 2:
        fields.add("request_id")
    _header(message, fields, "GESTURE_RESULT", session, versions=(1, 2))
    for field in ("device_id", "boot_id", "seq"):
        if type(message[field]) is not int or not 0 <= message[field] < 2**32:
            raise ProtocolError("invalid uint32 " + field)
    expected_id = "{}:{}:{}".format(message["device_id"], message["boot_id"], message["seq"])
    if version == 2 and message["request_id"] is not None:
        request = message["request_id"]
        if type(request) is not int or not 1 <= request < 2**32 or request != message["seq"]:
            raise ProtocolError("invalid request correlation")
        expected_id = "cmd:" + expected_id
    valid_gesture = (message["gesture"] == GESTURES[message["seq"] % 4]
                     if version == 1 else message["gesture"] in GESTURES)
    if (message["device_id"] not in (1, 2) or message["result_id"] != expected_id or
            not valid_gesture or
            type(message["confidence"]) not in (int, float) or message["confidence"] != 1.0):
        raise ProtocolError("invalid dummy result")
    return message


def tls_context(ca_file):
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


async def receive(args, statistics=None):
    context = tls_context(args.ca)
    if statistics is None:
        statistics = dict(received=0, reconnects=0)
    seen = OrderedDict()
    count = 0
    reconnects = 0
    backoff = 0.5
    started = asyncio.get_running_loop().time()
    while args.duration is None or asyncio.get_running_loop().time() - started < args.duration:
        writer = None
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(
                "127.0.0.1", args.port, ssl=context, server_hostname=TLS_IDENTITY,
                ssl_handshake_timeout=5.0, limit=MAX_FRAME_BYTES + 4), 5.0)
            writer.write(encode_frame(dict(v=1, type="SUBSCRIBE", session_id=args.session)))
            await asyncio.wait_for(writer.drain(), 5.0)
            validate_subscribed(await read_frame(reader), args.session)
            print("subscribed session=" + args.session, file=sys.stderr, flush=True)
            while args.duration is None or asyncio.get_running_loop().time() - started < args.duration:
                message = validate_result(await read_frame(reader, allow_idle=True), args.session)
                if message["result_id"] in seen:
                    continue
                seen[message["result_id"]] = None
                if len(seen) > 4096:
                    seen.popitem(last=False)
                # Print each live message immediately; there is no disconnected backlog.
                print(json.dumps(message, allow_nan=False, separators=(",", ":")), flush=True)
                count += 1
                statistics["received"] = count
                backoff = 0.5
                if args.count and count >= args.count:
                    return statistics
        except (OSError, ProtocolError, asyncio.TimeoutError) as error:
            reconnects += 1
            statistics["reconnects"] = reconnects
            # Avoid logging credential-bearing command lines or certificate contents.
            print("reconnect reason=" + type(error).__name__, file=sys.stderr, flush=True)
        finally:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), 1.0)
                except asyncio.CancelledError:
                    writer.transport.abort()
                    raise
                except (OSError, asyncio.TimeoutError):
                    writer.transport.abort()
        await asyncio.sleep(backoff)
        backoff = min(5.0, backoff * 2)
    return statistics


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ca", required=True, type=Path, help="Verified B07 Communications public CA PEM")
    parser.add_argument("--port", type=int, default=19999, help="This Phone's localhost forward port")
    parser.add_argument("--session", default="ltc-comms")
    parser.add_argument("--count", type=int, default=0, help="Stop after N unique results; 0 continues")
    parser.add_argument("--duration", type=float, help="Overall test duration in seconds")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or args.count < 0:
        parser.error("invalid port, count or session")
    try:
        validate_session(args.session)
    except ProtocolError as error:
        parser.error(str(error))
    if args.duration is not None and not 0 < args.duration < float("inf"):
        parser.error("duration must be finite and positive")
    try:
        async def run():
            statistics = dict(received=0, reconnects=0)
            if args.duration is None:
                return await receive(args, statistics)
            # Bound the whole run, including reconnect sleep and TLS startup.
            try:
                return await asyncio.wait_for(receive(args, statistics), args.duration)
            except asyncio.TimeoutError:
                return statistics
        summary = asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    except (OSError, ssl.SSLError, ValueError) as error:
        print("configuration_error=" + type(error).__name__, file=sys.stderr)
        return 1
    print(json.dumps(summary, separators=(",", ":")), file=sys.stderr)
    return 0 if not args.count or summary["received"] >= args.count else 1


if __name__ == "__main__":
    raise SystemExit(main())
