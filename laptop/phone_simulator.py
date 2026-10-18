"""Independent Phone gateway receiver; connect through the Phone's own SSH forward."""
import argparse
import asyncio
import json
import ssl
import sys

from common.tls import TLS_SERVER_NAME, client_context
from common.wire import STREAM_LIMIT, read_frame, write_frame
from ultra96.protocol import validate_message, validate_session


async def receive_results(host, port, ca_file, session_id="ltc-comms", count=0,
                          on_result=None, timeout=5.0):
    """Receive one TLS subscription; return count, or raise for invalid peers/frames."""
    validate_session(session_id)
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port,
        ssl=client_context(ca_file), server_hostname=TLS_SERVER_NAME,
        limit=STREAM_LIMIT, ssl_handshake_timeout=timeout), timeout=timeout)
    writer.transport.set_write_buffer_limits(high=32768, low=16384)
    received = 0
    try:
        await write_frame(writer, {"v": 1, "type": "SUBSCRIBE", "session_id": session_id}, timeout)
        validate_message(await read_frame(reader, timeout), "SUBSCRIBED", session_id)
        while count <= 0 or received < count:
            result = validate_message(await read_frame(reader, timeout), "GESTURE_RESULT", session_id)
            received += 1
            if on_result is not None:
                on_result(result)
        return received
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except (asyncio.TimeoutError, ConnectionError, OSError, ssl.SSLError):
            writer.transport.abort()


async def _run(args):
    total = 0
    reconnects = 0
    delay = 0.5

    def display(result):
        nonlocal total, delay
        total += 1
        delay = 0.5
        print(json.dumps(result, separators=(",", ":")), flush=True)

    while args.count <= 0 or total < args.count:
        try:
            await receive_results(args.host, args.port, args.ca, args.session_id,
                                  count=max(0, args.count - total), on_result=display)
        except ssl.SSLCertVerificationError:
            # A trust failure is configuration/security failure; never weaken verification.
            raise
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            if not args.reconnect:
                raise
            reconnects += 1
            print(json.dumps({"event": "reconnect", "attempt": reconnects,
                              "reason": type(exc).__name__, "delay_s": delay}), file=sys.stderr, flush=True)
            await asyncio.sleep(delay)
            delay = min(5.0, delay * 2)
    print(json.dumps({"event": "complete", "results": total, "reconnects": reconnects}),
          file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19999)
    parser.add_argument("--ca", required=True)
    parser.add_argument("--session-id", default="ltc-comms")
    parser.add_argument("--count", type=int, default=0, help="0 receives until interrupted")
    parser.add_argument("--reconnect", action="store_true", help="resubscribe after transport failures")
    args = parser.parse_args()
    if args.count < 0:
        parser.error("--count must be nonnegative")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
