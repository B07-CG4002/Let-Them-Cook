"""Rehearse BLE or synthetic input through local TLS and an independent subscriber.

This intentionally does not establish SSH or claim real Ultra96/Phone evidence.
"""
import argparse
import asyncio
from collections import OrderedDict
import json
from pathlib import Path
from common.tls import client_context, server_context, TLS_SERVER_NAME
from common.wire import read_frame, write_frame
from laptop.bridge import Bridge, BridgeConfig
from ultra96.protocol import validate_message
from ultra96.server import CommsServer


async def rehearse(pki_dir, duration=60, target=100, ble=False, diagnostic_unprotected=False):
    folder = Path(pki_dir)
    server = CommsServer(server_context(folder / "server-cert.pem", folder / "server-key.pem"),
                         ingest_port=0, gateway_port=0)
    await server.start()
    phone_writer = None
    consumer = None
    results = 0
    duplicate_results = 0
    first_result = last_result = None
    recent = OrderedDict()
    receive_errors = []
    bridge = Bridge(BridgeConfig(ca_file=str(folder / "ca-cert.pem"), port=server.ingest_port,
                                 diagnostic_unprotected=diagnostic_unprotected))
    try:
        phone, phone_writer = await asyncio.open_connection("127.0.0.1", server.gateway_port,
            ssl=client_context(folder / "ca-cert.pem"), server_hostname=TLS_SERVER_NAME)
        await write_frame(phone_writer, dict(v=1, type="SUBSCRIBE", session_id="ltc-comms"))
        validate_message(await read_frame(phone), "SUBSCRIBED", "ltc-comms")
        async def receive():
            nonlocal results, duplicate_results, first_result, last_result
            while True:
                try:
                    result = validate_message(await read_frame(phone, timeout=max(5.0, duration + 5)),
                                              "GESTURE_RESULT", "ltc-comms")
                    trace = result["result_id"]
                    if trace in recent:
                        duplicate_results += 1
                    recent[trace] = None
                    if len(recent) > 4096:
                        recent.popitem(last=False)
                    results += 1
                    first_result = first_result or trace
                    last_result = trace
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    receive_errors.append(type(exc).__name__)
                    return
        consumer = asyncio.create_task(receive())
        summary = await bridge.run(duration=duration, target=target, mock=not ble)
        for _ in range(20):
            if results >= summary["acked"] - summary["duplicate_acks"] or consumer.done():
                break
            await asyncio.sleep(0.025)
    finally:
        if consumer:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        if phone_writer:
            phone_writer.close()
            try:
                await asyncio.wait_for(phone_writer.wait_closed(), 1)
            except (ConnectionError, asyncio.TimeoutError):
                phone_writer.transport.abort()
        await server.close()
    failures = ["malformed", "ack_errors", "transport_errors", "ble_errors", "cleanup_errors",
                "queue_dropped", "stale_dropped", "gaps", "duplicates", "out_of_order"]
    passed = (summary["acked"] > 0 and (not target or summary["acked"] >= target)
              and results == summary["acked"] - summary["duplicate_acks"]
              and not duplicate_results and not receive_errors
              and not any(summary[field] for field in failures))
    return dict(passed=passed, source="real BLE" if ble else "synthetic",
                topology="local TLS only; SSH, remote Ultra96 and real Phone not exercised",
                protected_policy=ble and not diagnostic_unprotected,
                bridge=summary, results=results, duplicate_results=duplicate_results,
                receive_errors=receive_errors, first_result=first_result, last_result=last_result,
                server=server.metrics)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pki-dir", required=True)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--target", type=int, default=100)
    parser.add_argument("--ble", action="store_true")
    parser.add_argument("--diagnostic-unprotected", action="store_true")
    args = parser.parse_args()
    summary = asyncio.run(rehearse(args.pki_dir, args.duration, args.target,
                                   args.ble, args.diagnostic_unprotected))
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
