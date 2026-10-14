"""Check one flashed glove board over real BLE against a local service; no SSH, Ultra96 or phone.

The board -> laptop relay -> a CommsServer on this laptop (throwaway certificates,
built-in dummy model) -> a local subscriber. It sets the rate, sends commands over
the control channel and stops the BLE stream normally, so the board's own source
statistics reconcile: generated on the board = received = acknowledged.

    python -m tools.check_board right
    python -m tools.check_board left --rate 50 --duration 65 --commands 3 --report out.json

Use it after flashing. Only the named board is contacted (by its address in
.comms-local/boards.json); the other may stay powered.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

from common.sensor import SENSOR_MTU_FLOOR
from common.tls import client_context, server_context, TLS_SERVER_NAME
from common.wire import read_frame, write_frame
from laptop.bridge import Bridge, BridgeConfig
from tools.demo_boards import load_boards
from ultra96.protocol import validate_message
from ultra96.server import CommsServer

_FAILURE_FIELDS = ("malformed", "ack_errors", "transport_errors", "ble_errors", "cleanup_errors",
                   "queue_dropped", "stale_dropped", "gaps", "duplicates", "out_of_order",
                   "identity_mismatches")


def verdict(summary, *, rate, commands):
    """Return the list of failed criteria (empty means passed)."""
    failed = []
    source = summary.get("source") or {}
    if not (source.get("clean") and source.get("complete")):
        failed.append("source statistics not clean and complete: " + str(summary.get("source_issue")))
    if not summary.get("acked"):
        failed.append("nothing acknowledged")
    if not (source.get("generated") == summary.get("received") == summary.get("acked")):
        failed.append("generated, received and acknowledged differ")
    failed += ["{}={}".format(field, summary.get(field)) for field in _FAILURE_FIELDS
               if summary.get(field)]
    configuration = summary.get("configuration") or {}
    if configuration.get("source_rate_confirmed_hz") != rate:
        failed.append("rate {} Hz not confirmed".format(rate))
    if (configuration.get("negotiated_att_mtu") or 0) < SENSOR_MTU_FLOOR:
        failed.append("ATT MTU below {}".format(SENSOR_MTU_FLOOR))
    if summary.get("stream_results") != summary.get("acked"):
        failed.append("dummy-model results differ from acknowledged samples")
    completed = (summary.get("commands") or {}).get("completed")
    if completed != commands or summary.get("command_results") != commands:
        failed.append("{} commands requested, {} completed, {} results".format(
            commands, completed, summary.get("command_results")))
    if summary.get("receive_errors"):
        failed.append("subscriber errors: " + ",".join(summary["receive_errors"]))
    return failed


async def check(address, device_id, pki, *, rate, duration, commands):
    folder = Path(pki)
    server = CommsServer(server_context(folder / "server-cert.pem", folder / "server-key.pem"),
                         ingest_port=0, gateway_port=0)
    await server.start()
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", server.gateway_port, ssl=client_context(folder / "ca-cert.pem"),
        server_hostname=TLS_SERVER_NAME)
    await write_frame(writer, dict(v=1, type="SUBSCRIBE", session_id="ltc-comms"))
    validate_message(await read_frame(reader), "SUBSCRIBED", "ltc-comms")
    counts = {"stream": 0, "command": 0}
    seen, errors = set(), []

    async def receive():
        while True:
            try:
                result = validate_message(await read_frame(reader, timeout=duration + 60),
                                          "GESTURE_RESULT", "ltc-comms")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.append(type(exc).__name__)
                return
            if result["result_id"] in seen:
                errors.append("duplicate_result")
            seen.add(result["result_id"])
            counts["command" if result["request_id"] is not None else "stream"] += 1

    consumer = asyncio.create_task(receive())
    bridge = Bridge(BridgeConfig(ca_file=str(folder / "ca-cert.pem"), port=server.ingest_port,
                                 address=address, expected_device_id=device_id, source_audit=True,
                                 ack_window=32, controls_enabled=True, source_rate=rate))
    worker = asyncio.create_task(bridge.commands.run())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def send_commands():
        while bridge.source_rate_confirmed is None:
            await asyncio.sleep(0.1)
        await asyncio.sleep(2)
        for _ in range(commands):
            bridge.submit_command()
            await bridge.commands.join()
            await asyncio.sleep(0.5)

    sender = asyncio.create_task(send_commands())
    writer_task = asyncio.create_task(bridge.writer_loop())
    ble_task = asyncio.create_task(bridge.ble_loop(stop_event=stop))
    started = loop.time()
    try:
        while loop.time() - started < duration and not ble_task.done():
            await asyncio.sleep(0.05)
        if ble_task.done():
            ble_task.result()
            raise RuntimeError("BLE loop stopped early")
        # Every command must have been submitted, not just the queue be empty for a moment.
        await asyncio.wait({sender}, timeout=10 + commands * 15)
        await asyncio.wait_for(bridge.commands.join(), 10)
        stop.set()  # normal stop: remote notify off, final source statistics read
        await asyncio.wait_for(ble_task, 30)
        deadline = loop.time() + 10
        while (bridge.inbox.size or bridge._inflight) and loop.time() < deadline:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.5)  # let the last results arrive
    finally:
        for task in (sender, writer_task, ble_task, worker):
            task.cancel()
        await asyncio.gather(sender, writer_task, ble_task, worker, return_exceptions=True)
        bridge.inbox.activate(0)
        await bridge.close_transport()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        writer.close()
        await server.close()
    summary = dict(bridge.summary(), elapsed_seconds=round(loop.time() - started, 3),
                   stream_results=counts["stream"], command_results=counts["command"],
                   receive_errors=errors, server_metrics=server.metrics)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("board", choices=("left", "right"))
    parser.add_argument("--rate", type=int, default=50)
    parser.add_argument("--duration", type=float, default=65.0)
    parser.add_argument("--commands", type=int, default=3)
    parser.add_argument("--pki-dir", help="rehearsal certificates; default: a new throwaway set")
    parser.add_argument("--report", help="also write the summary JSON here")
    args = parser.parse_args(argv)
    if not 1 <= args.rate <= 200 or not 5 <= args.duration <= 3600 or not 0 <= args.commands <= 8:
        parser.error("rate 1..200, duration 5..3600 s, commands 0..8")
    device_id = 1 if args.board == "left" else 2
    address = dict(load_boards())[args.board]
    with tempfile.TemporaryDirectory(prefix="b07-check-") as temporary:
        pki = args.pki_dir
        if pki is None:
            from tools.generate_comms_pki import generate_pki
            pki = Path(temporary) / "pki"
            generate_pki(pki)
        summary = asyncio.run(check(address, device_id, pki, rate=args.rate,
                                    duration=args.duration, commands=args.commands))
    failed = verdict(summary, rate=args.rate, commands=args.commands)
    summary.update(board=args.board, device_id=device_id, address=address, passed=not failed,
                   failed_criteria=failed)
    text = json.dumps(summary, sort_keys=True, default=str)
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    print(text)
    source = summary.get("source") or {}
    print("{} {} (device {}): generated={} received={} acked={} commands={}/{}".format(
        "CHECK PASSED" if not failed else "CHECK FAILED", args.board.upper(), device_id,
        source.get("generated"), summary.get("received"), summary.get("acked"),
        summary.get("command_results"), args.commands))
    for reason in failed:
        print("  - " + reason)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
