"""Check one flashed glove board over real BLE against a local service; no SSH, Ultra96 or phone.

The board -> laptop relay -> a CommsServer on this laptop (throwaway certificates,
built-in dummy model) -> a local subscriber. It sets the rate, sends commands over
the control channel and stops the BLE stream normally, so the board's own source
statistics reconcile: generated on the board = received = acknowledged.

    python -m tools.check_board right
    python -m tools.check_board left --rate 50 --duration 65 --commands 3 --report out.json
    python -m tools.check_board right --sensor-version 4 --address ADDR --duration 300

Use it after flashing. Only the named board is contacted (by its address in
.comms-local/boards.json, or by --address); the other may stay powered. With
--address the saved glove mapping is neither read nor changed, so a bare test
board can be checked; left|right then only selects device ID 1|2, which must be
the ID the board was flashed with. Pair a board first (python -m
laptop.windows_pairing --address ADDR). --sensor-version 4 checks a three-flex
board (flash.py --flex3: 40-byte packets, 12 values, LCS4) against a version 4
local service. The last lines report sequence gaps: missing sequence numbers,
the gaps they form and the longest one.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

from common import sensor, sensor_v4
from common.tls import client_context, server_context, TLS_SERVER_NAME
from common.wire import read_frame, write_frame
from laptop.bridge import Bridge, BridgeConfig, StreamTracker
from tools.demo_boards import address as board_address, load_boards
from ultra96.protocol import validate_message, validate_subscribed
from ultra96.server import CommsServer

_FAILURE_FIELDS = ("malformed", "ack_errors", "transport_errors", "ble_errors", "cleanup_errors",
                   "queue_dropped", "stale_dropped", "gaps", "duplicates", "out_of_order",
                   "identity_mismatches")
CODECS = {sensor.VERSION: sensor, sensor_v4.VERSION: sensor_v4}


class GapTracker(StreamTracker):
    """The bridge's sequence tracker, also keeping how the missing sequence numbers fall.

    gaps (inherited) counts the missing numbers; gap_events counts the jumps they make,
    longest_gap the most numbers one jump skipped and longest_gap_after_seq where it began.
    """

    def __init__(self):
        super().__init__()
        self.gap_events = self.longest_gap = 0
        self.longest_gap_after_seq = None

    def observe(self, packet):
        previous = self._last.get(packet.device_id)
        before = self.gaps
        accepted = super().observe(packet)
        missing = self.gaps - before
        if missing:
            self.gap_events += 1
            if missing > self.longest_gap:
                self.longest_gap, self.longest_gap_after_seq = missing, previous[1]
        return accepted

    def report(self):
        return {"missing_seqs": self.gaps, "gap_events": self.gap_events,
                "longest_gap": self.longest_gap,
                "longest_gap_after_seq": self.longest_gap_after_seq}


def reception_rate(summary):
    """(streaming seconds, packets per second) from the first to the last packet received.

    The run's elapsed_seconds also covers the BLE connect, the rate confirmation and the
    stop, so received / elapsed_seconds understates a board that loses nothing; this
    rate counts only the streaming interval. (None, None) before two packets arrived.
    """
    first, last = summary.get("first_received_at"), summary.get("last_received_at")
    received = summary.get("received")
    if (type(received) is not int or received < 2 or not isinstance(first, (int, float))
            or not isinstance(last, (int, float)) or last <= first):
        return None, None
    return last - first, (received - 1) / (last - first)


def _gap_detail(gaps):
    """How the missing seqs fall, for the gaps criterion; empty without GapTracker counts."""
    if not isinstance(gaps, dict) or not gaps.get("gap_events"):
        return ""
    return " (missing seqs in {} gaps, longest {} after seq {})".format(
        gaps["gap_events"], gaps.get("longest_gap"), gaps.get("longest_gap_after_seq"))


def verdict(summary, *, rate, commands, sensor_version=3):
    """Return the list of failed criteria (empty means passed)."""
    failed = []
    mtu_floor = CODECS[sensor_version].SENSOR_MTU_FLOOR
    source = summary.get("source") or {}
    if not (source.get("clean") and source.get("complete")):
        failed.append("source statistics not clean and complete: " + str(summary.get("source_issue")))
    if not summary.get("acked"):
        failed.append("nothing acknowledged")
    if not (source.get("generated") == summary.get("received") == summary.get("acked")):
        failed.append("generated, received and acknowledged differ")
    failed += ["{}={}".format(field, summary.get(field)) + (
                   _gap_detail(summary.get("sequence_gaps")) if field == "gaps" else "")
               for field in _FAILURE_FIELDS if summary.get(field)]
    configuration = summary.get("configuration") or {}
    if configuration.get("sensor_version", sensor_version) != sensor_version:
        failed.append("the bridge ran sensor version {}, not {}".format(
            configuration.get("sensor_version"), sensor_version))
    if configuration.get("source_rate_confirmed_hz") != rate:
        failed.append("rate {} Hz not confirmed".format(rate))
    if (configuration.get("negotiated_att_mtu") or 0) < mtu_floor:
        failed.append("ATT MTU below {}".format(mtu_floor))
    if summary.get("stream_results") != summary.get("acked"):
        failed.append("dummy-model results differ from acknowledged samples")
    completed = (summary.get("commands") or {}).get("completed")
    if completed != commands or summary.get("command_results") != commands:
        failed.append("{} commands requested, {} completed, {} results".format(
            commands, completed, summary.get("command_results")))
    if summary.get("receive_errors"):
        failed.append("subscriber errors: " + ",".join(summary["receive_errors"]))
    return failed


async def check(address, device_id, pki, *, rate, duration, commands, sensor_version=3):
    folder = Path(pki)
    server = CommsServer(server_context(folder / "server-cert.pem", folder / "server-key.pem"),
                         ingest_port=0, gateway_port=0, sensor_version=sensor_version)
    await server.start()
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", server.gateway_port, ssl=client_context(folder / "ca-cert.pem"),
        server_hostname=TLS_SERVER_NAME)
    subscribe = dict(v=1, type="SUBSCRIBE", session_id="ltc-comms")
    await write_frame(writer, subscribe)
    validate_subscribed(await read_frame(reader), subscribe)
    counts = {"stream": 0, "command": 0}
    seen, errors = set(), []

    async def receive():
        while True:
            try:
                # validate_message knows which result version the selected sensor version uses.
                result = validate_message(await read_frame(reader, timeout=duration + 60),
                                          "GESTURE_RESULT", "ltc-comms",
                                          sensor_version=sensor_version)
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
                                 ack_window=32, controls_enabled=True, source_rate=rate,
                                 sensor_version=sensor_version))
    bridge.tracker = GapTracker()
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
                   receive_errors=errors, server_metrics=server.metrics,
                   sequence_gaps=bridge.tracker.report())
    streaming, rate = reception_rate(summary)
    summary.update(streaming_seconds=None if streaming is None else round(streaming, 3),
                   reception_rate_hz=None if rate is None else round(rate, 3))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("board", choices=("left", "right"),
                        help="device ID 1 (left) or 2 (right); also the saved address unless --address")
    parser.add_argument("--address", metavar="ADDR",
                        help="check the board at this Bluetooth address instead of the saved "
                             "left/right one; .comms-local/boards.json is not read or changed")
    parser.add_argument("--sensor-version", type=int, choices=(3, 4), default=3,
                        help="3 (38-byte packets, 11 values) or 4 (the three-flex boards: 40-byte "
                             "packets, 12 values, LCS4); default: 3")
    parser.add_argument("--rate", type=int, default=50)
    parser.add_argument("--duration", type=float, default=65.0)
    parser.add_argument("--commands", type=int, default=3)
    parser.add_argument("--pki-dir", help="rehearsal certificates; default: a new throwaway set")
    parser.add_argument("--report", help="also write the summary JSON here")
    args = parser.parse_args(argv)
    if not 1 <= args.rate <= 200 or not 5 <= args.duration <= 3600 or not 0 <= args.commands <= 8:
        parser.error("rate 1..200, duration 5..3600 s, commands 0..8")
    device_id = 1 if args.board == "left" else 2
    if args.address is not None:
        try:
            address = board_address(args.address)
        except ValueError as exc:
            parser.error("--address: {}".format(exc))
    else:
        address = dict(load_boards())[args.board]
    with tempfile.TemporaryDirectory(prefix="b07-check-") as temporary:
        pki = args.pki_dir
        if pki is None:
            from tools.generate_comms_pki import generate_pki
            pki = Path(temporary) / "pki"
            generate_pki(pki)
        summary = asyncio.run(check(address, device_id, pki, rate=args.rate,
                                    duration=args.duration, commands=args.commands,
                                    sensor_version=args.sensor_version))
    failed = verdict(summary, rate=args.rate, commands=args.commands,
                     sensor_version=args.sensor_version)
    summary.update(board=args.board, device_id=device_id, address=address, passed=not failed,
                   failed_criteria=failed, sensor_version=args.sensor_version,
                   address_source="--address" if args.address is not None else "boards.json")
    text = json.dumps(summary, sort_keys=True, default=str)
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    print(text)
    print(summary_line(summary, args.board, device_id, args.commands, failed))
    print(gaps_line(summary, args.sensor_version))
    for reason in failed:
        print("  - " + reason)
    return 0 if not failed else 1


def summary_line(summary, board, device_id, commands, failed):
    """One line for the terminal. Idle, unread and refused ticks sent nothing;
    a gated board's idle ticks are expected and do not fail the check."""
    source = summary.get("source") or {}
    gated = {True: "yes", False: "no"}.get(source.get("gated"), "unknown")
    return ("{} {} (device {}): generated={} received={} acked={} missing_ble={} "
            "missing_ack={} gaps={} commands={}/{} gated={} idle={} unread={} refused={}").format(
        "CHECK PASSED" if not failed else "CHECK FAILED", board.upper(), device_id,
        source.get("generated"), summary.get("received"), summary.get("acked"),
        source.get("missing_received"), source.get("missing_acked"), summary.get("gaps"),
        summary.get("command_results"), commands, gated, source.get("source_idle"),
        source.get("source_unread"), source.get("source_refused"))


def gaps_line(summary, sensor_version=3):
    """The sequence gaps on one line: what a long run at a fixed rate is checked for.

    A gap is a jump in the sequence numbers the laptop processed. The board numbers
    every packet it sends, so the missing numbers are packets lost over BLE, or
    dropped from a full laptop queue (also counted as queue_dropped). The source
    audit's sequence_anomalies counts the same jumps against the board's counters.
    """
    gaps = summary.get("sequence_gaps") or {}
    source = summary.get("source") or {}
    streaming, rate = reception_rate(summary)
    longest = gaps.get("longest_gap")
    where = gaps.get("longest_gap_after_seq")
    return ("SEQUENCE v{} packets: {} received, reception rate {} over {} s from first to "
            "last packet (run {} s with connect and stop); {} seqs missing in {} gaps, "
            "longest {}{}; queue_dropped={} duplicates={} out_of_order={} "
            "source sequence_anomalies={}").format(
        sensor_version, summary.get("received"),
        "-" if rate is None else "{:.1f}/s".format(rate),
        "-" if streaming is None else "{:.3f}".format(streaming),
        summary.get("elapsed_seconds"), gaps.get("missing_seqs", summary.get("gaps")),
        gaps.get("gap_events"), longest,
        " (after seq {})".format(where) if longest and where is not None else "",
        summary.get("queue_dropped"), summary.get("duplicates"), summary.get("out_of_order"),
        source.get("sequence_anomalies"))


if __name__ == "__main__":
    sys.exit(main())
