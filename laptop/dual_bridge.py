"""Run two independently owned B07 Communications ESP32 reception paths."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import sys
from typing import Optional

from laptop.bridge import Bridge, BridgeConfig
from laptop import reporting
from laptop.controls import keyboard_loop
from laptop.evidence import PacketEvidence


LOG = logging.getLogger(__name__)


class DualBridge:
    """Coordinate a common observation without coupling either device path."""

    def __init__(self, left: Bridge, right: Bridge, *, expected_rate=10.0,
                 startup_timeout=30.0, drain_timeout=10.0,
                 shutdown_timeout=25.0):
        self.bridges = {1: left, 2: right}
        for value, name in ((expected_rate, "expected rate"),
                            (startup_timeout, "startup timeout"),
                            (drain_timeout, "drain timeout"),
                            (shutdown_timeout, "shutdown timeout")):
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive finite seconds")
        self.expected_rate = float(expected_rate)
        self.startup_timeout = float(startup_timeout)
        self.drain_timeout = float(drain_timeout)
        self.shutdown_timeout = float(shutdown_timeout)
        for device_id, bridge in self.bridges.items():
            if bridge.config.expected_device_id != device_id:
                raise ValueError(f"device {device_id} bridge has the wrong expected identity")
            if bridge.source_audit is None:
                raise ValueError("dual reception requires source auditing")
        if left.config.session_id != right.config.session_id:
            raise ValueError("both bridges must use the same session ID")
        if (left.config.address and right.config.address
                and left.config.address.lower() == right.config.address.lower()):
            raise ValueError("physical devices require distinct BLE addresses")

    async def _wait_for_start(self, active, input_tasks, on_tick=None):
        async def both_active():
            while not all(event.is_set() for event in active.values()):
                if on_tick is not None:
                    on_tick("startup")
                for task in input_tasks.values():
                    if task.done():
                        task.result()
                        raise RuntimeError("input worker exited before both streams became active")
                await asyncio.sleep(0.01)

        await asyncio.wait_for(both_active(), self.startup_timeout)

    async def run(self, duration=600.0, *, mock=False, mock_rate: Optional[float] = None,
                  ble_options=None, progress_interval=0.0, progress_callback=None,
                  keyboard=False, file_data=None, file_device=1):
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) \
                or not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration must be positive finite seconds")
        if not mock:
            addresses = [bridge.config.address for bridge in self.bridges.values()]
            if not all(addresses):
                raise ValueError("physical dual reception requires both BLE addresses")
            if addresses[0].lower() == addresses[1].lower():
                raise ValueError("physical devices require distinct BLE addresses")
        rate = self.expected_rate if mock_rate is None else mock_rate
        if not isinstance(rate, (int, float)) or isinstance(rate, bool) \
                or not math.isfinite(rate) or rate <= 0:
            raise ValueError("mock rate must be positive finite")
        if not isinstance(progress_interval, (int, float)) \
                or isinstance(progress_interval, bool) \
                or not math.isfinite(progress_interval) or progress_interval < 0:
            raise ValueError("progress interval must be zero or positive finite seconds")
        ble_options = ble_options or {}
        if (keyboard or file_data is not None) and (mock or
                any(bridge.control is None for bridge in self.bridges.values())):
            raise ValueError("keyboard/file requires protected physical control connections")
        if file_device not in (1, 2):
            raise ValueError("file device must be 1 or 2")
        stop = {device: asyncio.Event() for device in self.bridges}
        active = {device: asyncio.Event() for device in self.bridges}
        writers = {
            device: asyncio.create_task(bridge.writer_loop(), name=f"writer-{device}")
            for device, bridge in self.bridges.items()
        }
        inputs = {}
        for device, bridge in self.bridges.items():
            if mock:
                coroutine = bridge.dummy_loop(
                    rate, stop_event=stop[device], active_event=active[device])
            else:
                options = dict(ble_options.get(device, {}))
                coroutine = bridge.ble_loop(
                    stop_event=stop[device], active_event=active[device], **options)
            inputs[device] = asyncio.create_task(coroutine, name=f"input-{device}")

        failures = {1: [], 2: []}
        command_workers = {device: asyncio.create_task(bridge.commands.run(),
                           name=f"commands-{device}")
                           for device, bridge in self.bridges.items() if bridge.commands is not None}
        control_stop = asyncio.Event()
        keyboard_task = file_task = None
        file_result = None
        progress_failed = False
        next_progress = asyncio.get_running_loop().time()

        def emit_progress(phase, *, force=False):
            nonlocal next_progress, progress_failed
            if progress_callback is None or progress_interval == 0 or progress_failed:
                return
            now = asyncio.get_running_loop().time()
            if not force and now < next_progress:
                return
            snapshots = []
            for device, bridge in self.bridges.items():
                metrics = bridge.metrics
                drops = (bridge.inbox.dropped + metrics.stale_dropped
                         + bridge.inbox.generation_dropped
                         + metrics.generation_dropped + metrics.ambiguous_dropped)
                errors = (metrics.malformed + metrics.ack_errors
                          + metrics.transport_errors + metrics.ble_errors
                          + metrics.cleanup_errors + metrics.identity_mismatches
                          + metrics.source_stats_errors + metrics.disconnects
                          + bridge.tracker.gaps + bridge.tracker.duplicates
                          + bridge.tracker.out_of_order + bridge.tracker.new_boots)
                if bridge.source_audit is not None:
                    audit = bridge.source_audit
                    errors += (audit.sequence_anomalies
                               + audit.ack_sequence_anomalies
                               + audit.identity_mismatches + audit.boot_mismatches
                               + audit.interruptions + audit.snapshot_errors)
                snapshots.append({
                    "device_id": device,
                    "received": bridge.callback_received,
                    "processed": metrics.received,
                    "acked": metrics.acked,
                    "queue": bridge.inbox.size,
                    "drops": drops,
                    "errors": errors,
                    "sensor_goodput": bridge.goodput.report(bridge.clock()),
                })
            try:
                progress_callback(
                    phase, "synthetic" if mock else "physical", snapshots)
            except Exception:
                progress_failed = True
                LOG.warning("progress output failed error_type=callback")
            next_progress = now + float(progress_interval)

        startup_ok = False
        observation_start = observation_end = None
        try:
            emit_progress("startup", force=True)
            try:
                await self._wait_for_start(active, inputs, emit_progress)
                startup_ok = True
            except (Exception, asyncio.TimeoutError) as exc:
                for device in self.bridges:
                    if not active[device].is_set():
                        failures[device].append(f"startup:{type(exc).__name__}")

            if startup_ok:
                emit_progress("observation", force=True)
                observation_start = asyncio.get_running_loop().time()
                for bridge in self.bridges.values():
                    bridge.begin_observation(observation_start)
                if keyboard:
                    keyboard_task = asyncio.create_task(keyboard_loop(
                        lambda device: self.bridges[device].submit_command(), control_stop))
                if file_data is not None:
                    file_task = asyncio.create_task(
                        self.bridges[file_device].control.transfer_file(file_data))
                deadline = observation_start + duration
                while True:
                    now = asyncio.get_running_loop().time()
                    if now >= deadline:
                        break
                    emit_progress("observation")
                    if keyboard_task is not None and keyboard_task.done():
                        error = keyboard_task.exception()
                        if error is not None and "keyboard:error" not in failures[1]:
                            failures[1].append("keyboard:error")
                            LOG.error("keyboard failed: %s", error)
                    for device, task in inputs.items():
                        if task.done() and not stop[device].is_set():
                            try:
                                task.result()
                                marker = "input:stopped"
                            except BaseException as exc:
                                marker = f"input:{type(exc).__name__}"
                            if marker not in failures[device]:
                                failures[device].append(marker)
                    for device, task in writers.items():
                        if task.done():
                            try:
                                task.result()
                                marker = "writer:stopped"
                            except BaseException as exc:
                                marker = f"writer:{type(exc).__name__}"
                            if marker not in failures[device]:
                                failures[device].append(marker)
                    await asyncio.sleep(min(0.02, max(0.0, deadline - now)))
                observation_end = asyncio.get_running_loop().time()
                for bridge in self.bridges.values():
                    bridge.finish_observation(observation_end)
        finally:
            control_stop.set()
            if keyboard_task is not None:
                keyboard_task.cancel()
                await asyncio.gather(keyboard_task, return_exceptions=True)
            if file_task is not None:
                if not file_task.done():
                    file_task.cancel()
                done, pending = await asyncio.wait({file_task}, timeout=self.drain_timeout)
                if pending:
                    failures[file_device].append("file:shutdown_timeout")
                    file_task.cancel()
                elif file_task.cancelled():
                    failures[file_device].append("file:interrupted")
                elif file_task.exception() is not None:
                    failures[file_device].append(f"file:{type(file_task.exception()).__name__}")
                else:
                    file_result = file_task.result()
            for device in command_workers:
                self.bridges[device].commands._accepting = False
            if command_workers:
                try:
                    await asyncio.wait_for(asyncio.gather(*(
                        self.bridges[device].commands.join() for device in command_workers)),
                        self.drain_timeout)
                except asyncio.TimeoutError:
                    for device in command_workers:
                        if self.bridges[device].commands.summary()["pending"]:
                            failures[device].append("commands:drain_timeout")
                for task in command_workers.values():
                    task.cancel()
                _, pending = await asyncio.wait(command_workers.values(), timeout=self.shutdown_timeout)
                for device, task in command_workers.items():
                    await self.bridges[device].commands.stop()
                    if task in pending:
                        failures[device].append("commands:shutdown_timeout")
            # Both producers are asked to quiesce together. Each path performs
            # stop-notify and its final source read independently.
            for event in stop.values():
                event.set()
            emit_progress("shutdown", force=True)
            done, pending = await asyncio.wait(inputs.values(), timeout=self.shutdown_timeout)
            for task in done:
                device = int(task.get_name().rsplit("-", 1)[1])
                if not task.cancelled():
                    try:
                        task.result()
                    except BaseException as exc:
                        failures[device].append(f"shutdown:{type(exc).__name__}")
            for task in pending:
                device = int(task.get_name().rsplit("-", 1)[1])
                failures[device].append("shutdown:timeout")
                task.cancel()
            if pending:
                _, still_pending = await asyncio.wait(pending, timeout=0.5)
                for task in still_pending:
                    device = int(task.get_name().rsplit("-", 1)[1])
                    bridge = self.bridges[device]
                    bridge._retained.add(task)

                    def consume(child, *, bridge=bridge):
                        bridge._retained.discard(child)
                        if not child.cancelled():
                            child.exception()

                    task.add_done_callback(consume)

            drain_deadline = asyncio.get_running_loop().time() + self.drain_timeout
            while (any(bridge.inbox.size or bridge._inflight
                       for bridge in self.bridges.values())
                   and asyncio.get_running_loop().time() < drain_deadline):
                emit_progress("drain")
                await asyncio.sleep(0.01)
            for device, bridge in self.bridges.items():
                if bridge.inbox.size or bridge._inflight:
                    failures[device].append("drain:timeout")

            for task in writers.values():
                task.cancel()
            _, writer_pending = await asyncio.wait(
                writers.values(), timeout=self.shutdown_timeout)
            for device, task in writers.items():
                if task in writer_pending:
                    failures[device].append("writer:timeout")
                    bridge = self.bridges[device]
                    bridge._retained.add(task)

                    def consume_writer(child, *, bridge=bridge):
                        bridge._retained.discard(child)
                        if not child.cancelled():
                            child.exception()

                    task.add_done_callback(consume_writer)
                elif not task.cancelled():
                    result = task.exception()
                    if result is not None:
                        failures[device].append(f"writer:{type(result).__name__}")
            for bridge in self.bridges.values():
                bridge.inbox.activate(0)
            close_results = await asyncio.gather(
                *(bridge.close_transport() for bridge in self.bridges.values()),
                return_exceptions=True)
            for device, result in zip(self.bridges, close_results):
                if isinstance(result, BaseException):
                    failures[device].append(f"transport_close:{type(result).__name__}")
            emit_progress("complete", force=True)

        observed_duration = (observation_end - observation_start
                             if observation_start is not None and observation_end is not None
                             else 0.0)
        expected_samples = observed_duration * self.expected_rate
        devices = {}
        clean = startup_ok and not progress_failed
        anomaly_fields = (
            "malformed", "stale_dropped", "generation_dropped", "duplicate_acks",
            "ack_errors", "ambiguous_dropped", "transport_errors", "ble_errors",
            "cleanup_errors", "identity_mismatches", "source_stats_errors",
            "disconnects", "queue_dropped", "callback_generation_dropped",
            "gaps", "duplicates", "out_of_order", "new_boots",
        )
        for device, bridge in self.bridges.items():
            item = bridge.summary()
            coverage_ok = (item["observation_received"] > 0
                           and item["observation_received"] >= 0.9 * expected_samples)
            silence_ok = item["observation_max_silence"] <= bridge.config.freshness
            protected_ble = mock or not bridge.config.diagnostic_unprotected
            item.update({
                "runtime_failures": failures[device],
                "expected_samples": expected_samples,
                "coverage_ratio": (item["observation_received"] / expected_samples
                                   if expected_samples else 0.0),
                "sustained_coverage": coverage_ok,
                "silence_ok": silence_ok,
                "protected_ble": protected_ble,
                "input": "synthetic" if mock else "physical",
            })
            item_clean = bool(
                not failures[device] and item["source"] and item["source"]["clean"]
                and coverage_ok and silence_ok and protected_ble and not item["unfinished"]
                and not any(item[field] for field in anomaly_fields)
                and not (item["commands"] and (item["commands"]["failed"] or item["commands"]["pending"]))
                and not (item["controls"] and any(item["controls"].values()))
                and not item["sensor_goodput"]["tracking_overflow"]
            )
            item["clean"] = item_clean
            clean = clean and item_clean
            devices[str(device)] = item
        return {
            "clean": bool(clean),
            "mock_input": mock,
            "session_id": self.bridges[1].config.session_id,
            "common_observation_seconds": observed_duration,
            "expected_rate_hz": self.expected_rate,
            "progress_error": progress_failed,
            "file_transfer": file_result,
            "sensor_goodput": {
                "boundary": "unique valid sensor packet bytes at laptop reception; excludes controls/files/overhead",
                "elapsed_seconds": observed_duration,
                "packet_bytes": sum(item["sensor_goodput"]["packet_bytes"] for item in devices.values()),
                "average_kbps": sum(item["sensor_goodput"]["average_kbps"] for item in devices.values()),
                "rolling_kbps": sum(item["sensor_goodput"]["rolling_kbps"] for item in devices.values()),
            },
            "devices": devices,
        }


def _progress_interval(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "progress interval must be zero or positive finite seconds") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError(
            "progress interval must be zero or positive finite seconds")
    return parsed


def _ack_window(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("ACK window must be 1..64") from exc
    if str(parsed) != value or not 1 <= parsed <= 64:
        raise argparse.ArgumentTypeError("ACK window must be 1..64")
    return parsed


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left-address")
    parser.add_argument("--right-address")
    parser.add_argument("--ca", required=True)
    parser.add_argument("--port", type=int, default=18888)
    parser.add_argument("--session-id", default="ltc-comms")
    parser.add_argument("--duration", type=float, default=600.0)
    parser.add_argument("--queue-capacity", type=int, default=64)
    parser.add_argument("--ack-window", type=_ack_window, default=32,
                        help="maximum sent-but-unacknowledged frames per device")
    parser.add_argument("--freshness", type=float, default=2.0)
    parser.add_argument("--expected-rate", type=float, default=10.0)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    parser.add_argument("--drain-timeout", type=float, default=10.0)
    parser.add_argument("--progress-interval", type=_progress_interval, default=1.0,
                        help="seconds between per-device stderr updates; 0 disables")
    parser.add_argument("--report", help="new path for the final JSON report")
    parser.add_argument("--keyboard", action="store_true", help="press 1/2 to command either ESP")
    parser.add_argument("--file", type=Path, help="transfer a 1..65536 byte file over BLE")
    parser.add_argument("--file-device", type=int, choices=(1, 2), default=1)
    parser.add_argument("--rate", type=_source_rate, help="set actual source rate on BOTH ESPs, 1..200 Hz")
    parser.add_argument("--seed", type=int, help="reproducible laptop fixture choice for tests")
    parser.add_argument("--evidence", type=Path, help="new JSONL path; matching .log is also written")
    parser.add_argument("--console-sample", type=int, default=10)
    parser.add_argument("--mock", action="store_true",
                        help="explicit two-device synthetic input, not BLE evidence")
    parser.add_argument("--diagnostic-unprotected", action="store_true")
    return parser


def _source_rate(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("source rate must be integer 1..200 Hz") from exc
    if not 1 <= parsed <= 200:
        raise argparse.ArgumentTypeError("source rate must be integer 1..200 Hz")
    return parsed


def main():
    parser = _parser()
    args = parser.parse_args()
    controls_enabled = args.keyboard or args.file is not None or args.rate is not None
    if controls_enabled and (args.mock or args.diagnostic_unprotected):
        parser.error("controls require authenticated physical BLE")
    if args.console_sample < 1:
        parser.error("--console-sample must be positive")
    if args.keyboard and not sys.stdin.isatty():
        parser.error("--keyboard requires an interactive terminal")
    file_data = None
    if args.file is not None:
        try:
            with args.file.open("rb") as source:
                file_data = source.read(65537)
            if not 1 <= len(file_data) <= 65536:
                raise ValueError("file must contain 1..65536 bytes")
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    if not args.mock and (not args.left_address or not args.right_address):
        parser.error("physical mode requires --left-address and --right-address")
    if (args.left_address and args.right_address
            and args.left_address.lower() == args.right_address.lower()):
        parser.error("left and right BLE addresses must be distinct")
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    mode = "synthetic" if args.mock else "physical"
    run_metadata = {
        "started_at_utc": started_at,
        "mode": mode,
        "requested_duration_seconds": args.duration,
        "expected_rate_hz": args.rate if args.rate is not None else args.expected_rate,
        "source_rate_requested_hz": args.rate,
        "keyboard": args.keyboard, "file_device": args.file_device if args.file else None,
        "seed": args.seed,
        "queue_capacity": args.queue_capacity, "freshness_seconds": args.freshness,
        "ack_window": args.ack_window,
        "session_id": args.session_id,
        "revision": reporting.local_revision(Path(__file__).parents[1]),
        **reporting.source_provenance(Path(__file__).parents[1]),
    }
    reservation = None
    if args.report:
        try:
            reservation = reporting.reserve_report(args.report, run_metadata)
        except (OSError, ValueError, RuntimeError) as exc:
            detail = str(exc) if isinstance(
                exc, (FileExistsError, FileNotFoundError, NotADirectoryError)) \
                else type(exc).__name__
            print(f"report reservation failed: {detail}", file=sys.stderr)
            return 2
    logging.basicConfig(level=logging.INFO)
    evidence = PacketEvidence(args.evidence, sample_every=args.console_sample)

    def bridge(device_id, address):
        return Bridge(BridgeConfig(
            ca_file=args.ca, port=args.port, session_id=args.session_id,
            queue_capacity=args.queue_capacity, freshness=args.freshness,
            ack_window=args.ack_window,
            address=address, diagnostic_unprotected=args.diagnostic_unprotected,
            expected_device_id=device_id, source_audit=True, controls_enabled=controls_enabled,
            source_rate=args.rate, seed=None if args.seed is None else args.seed + device_id),
            evidence=evidence)

    async def run():
        dual = DualBridge(
            bridge(1, args.left_address), bridge(2, args.right_address),
            expected_rate=args.rate if args.rate is not None else args.expected_rate,
            startup_timeout=args.startup_timeout,
            drain_timeout=args.drain_timeout)
        return await dual.run(
            duration=args.duration, mock=args.mock, mock_rate=args.expected_rate,
            progress_interval=args.progress_interval,
            progress_callback=_write_progress, keyboard=args.keyboard,
            file_data=file_data, file_device=args.file_device)

    try:
        report = _run_with_bounded_loop(run())
    finally:
        evidence.close()
    report["evidence"] = evidence.summary()
    if any(report["evidence"][key] for key in ("dropped", "write_errors", "unfinished")):
        report["clean"] = False
    report["run"] = dict(
        run_metadata,
        finished_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    report_error = False
    if reservation is not None:
        report["report_saved"] = True
        serialized = json.dumps(report, sort_keys=True) + "\n"
        try:
            reservation.finalize(serialized)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            report_error = True
            report["clean"] = False
            report["report_saved"] = False
            report["report_error"] = type(exc).__name__
            print(f"report write failed: {type(exc).__name__}", file=sys.stderr)
    serialized = json.dumps(report, sort_keys=True) + "\n"
    print(serialized, end="")
    return 0 if report["clean"] and not report_error else 1


def _write_progress(phase, mode, snapshots):
    for item in snapshots:
        print(
            "progress mode={mode} phase={phase} device={device_id} "
            "received={received} processed={processed} acked={acked} "
            "queue={queue} drops={drops} errors={errors} "
            "BLE_sensor_kbps_rolling={sensor_goodput[rolling_kbps]:.3f} "
            "BLE_sensor_kbps_average={sensor_goodput[average_kbps]:.3f}".format(
                mode=mode, phase=phase, **item),
            file=sys.stderr, flush=True)
    print("progress combined_BLE_sensor_kbps_rolling={:.3f} combined_average_kbps={:.3f} "
          "boundary=laptop_reception bytes=sensor_packet_including_header excludes=BLE_TLS_SSH_controls_files".format(
              sum(item["sensor_goodput"]["rolling_kbps"] for item in snapshots),
              sum(item["sensor_goodput"]["average_kbps"] for item in snapshots)),
          file=sys.stderr, flush=True)


def _run_with_bounded_loop(coroutine, *, retirement_timeout=0.1):
    """Run the CLI owner without asyncio.run's unbounded final task gather."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coroutine)
    finally:
        pending = {task for task in asyncio.all_tasks(loop) if not task.done()}
        for task in pending:
            task.cancel()
        if pending:
            _, pending = loop.run_until_complete(
                asyncio.wait(pending, timeout=retirement_timeout))
        # These owned tasks have exceeded both coordinator and loop retirement
        # bounds. Closing the private CLI loop prevents further callbacks; the
        # failure is already latched in the returned report.
        for task in pending:
            task._log_destroy_pending = False
        asyncio.set_event_loop(None)
        loop.close()


if __name__ == "__main__":
    raise SystemExit(main())
