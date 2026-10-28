"""Short local commands for the configured B07 physical communication demo."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import uuid

from tools.ssh_tunnel import JUMP_HOST, default_jump, tunnel_command, windows_ssh
from laptop.dual_bridge import _source_rate
from tools.demo_boards import load_boards

ROOT = Path(__file__).resolve().parent
OUTPUT_ROOT = ROOT / ".comms-local"


def _duration(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("duration must be positive finite seconds")
    return seconds


def _port(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be 1..65535")
    return port


def _sensor_version_option(parser):
    parser.add_argument("--sensor-version", type=int, choices=(3, 4), default=3,
                        help="sensor packets of BOTH gloves: 3 = the current boards (38-byte "
                             "packets, 11 values); 4 = the three-flex boards (v4, 40-byte packets, "
                             "12 values, flashed with flash.py --flex3). With 4 the Ultra96 service "
                             "must be deployed with python -m tools.deploy_service "
                             "--sensor-version 4; default: 3")


def _jump_option(parser):
    parser.add_argument("--jump", default=default_jump(),
                        help="your NUS login for the jump host, user@" + JUMP_HOST +
                             "; default: LTC_JUMP_USER@" + JUMP_HOST + " when set, else the owner's")


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    tunnel = commands.add_parser("tunnel", help="open the interactive SSH tunnel")
    _jump_option(tunnel)
    tunnel.add_argument("--target", default="xilinx@makerslab-fpga-35.ddns.comp.nus.edu.sg")
    tunnel.add_argument("--port", type=_port, default=18889)
    tunnel.add_argument("--monitor-port", type=_port, default=19999,
                        help="local port forwarded to the Ultra96 gateway 9999 for the board view "
                             "of 'start --no-tunnel'; default: 19999")

    # One command for a whole run: tunnel, gloves, live view, run log and the final check.
    start = commands.add_parser(
        "start", help="tunnel, both gloves, live view and run.log in one command; q or Ctrl+C stops")
    start.add_argument("--duration", type=_duration,
                       help="seconds of capture; default: until you press q or Ctrl+C")
    start.add_argument("--rate", type=_source_rate, default=50,
                       help="set BOTH gloves to 1..200 Hz; default: 50, the gloves' rate")
    start.add_argument("--keyboard", action="store_true",
                       help="keys 1 and 2 send a command to the LEFT or RIGHT glove")
    start.add_argument("--no-tunnel", action="store_true",
                       help="use a tunnel that is already open (python demo.py tunnel)")
    _jump_option(start)
    start.add_argument("--target", default="xilinx@makerslab-fpga-35.ddns.comp.nus.edu.sg")
    start.add_argument("--port", type=_port, default=18889,
                       help="local port forwarded to the Ultra96 ingest 8888; default: 18889")
    start.add_argument("--monitor-port", type=_port, default=19999,
                       help="local port forwarded to the Ultra96 gateway 9999; default: 19999")
    start.add_argument("--ca", type=Path, default=Path.home() / ".codex" / "private" /
                       "cg4002-week7-20260906" / "ca-cert.pem")
    start.add_argument("--left-address", help="override saved LEFT glove address")
    start.add_argument("--right-address", help="override saved RIGHT glove address")
    start.add_argument("--session-id", default=os.environ.get("LTC_COMMS_SESSION", "ltc-comms"),
                       help="session expected by the Ultra96 server; default: LTC_COMMS_SESSION or ltc-comms")
    start.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    start.add_argument("--seed", type=int, help="repeat laptop random fixture selection")
    _sensor_version_option(start)
    start.add_argument("--mock", action="store_true", help=argparse.SUPPRESS)  # tests only

    # The same capture implementation serves recording and live keyboard demos.
    for mode, seconds, keyboard in (("run", 60.0, False), ("live", 120.0, True)):
        run = commands.add_parser(mode, help=f"{seconds:g}s physical capture; keyboard={keyboard}")
        run.add_argument("--duration", type=_duration, default=seconds,
                         help=f"seconds; default: {seconds:g}")
        run.add_argument("--ca", type=Path, default=Path.home() / ".codex" / "private" /
                         "cg4002-week7-20260906" / "ca-cert.pem")
        run.add_argument("--left-address", help="override saved LEFT board address")
        run.add_argument("--right-address", help="override saved RIGHT board address")
        run.add_argument("--port", type=_port, default=18889)
        run.add_argument("--session-id", default=os.environ.get("LTC_COMMS_SESSION", "ltc-comms"),
                         help="session expected by the Ultra96 server; default: LTC_COMMS_SESSION or ltc-comms")
        run.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
        run.add_argument("--keyboard", action="store_true", default=keyboard,
                         help="individual keys 1/2 command the selected ESP")
        run.add_argument("--rate", type=_source_rate, default=50,
                         help="set BOTH physical ESP sources to 1..200 Hz; default: 50, the gloves' rate")
        run.add_argument("--file", type=Path, help="send a 1..65536 byte file through BLE")
        run.add_argument("--file-device", type=int, choices=(1, 2), default=1)
        run.add_argument("--seed", type=int, help="repeat laptop random fixture selection")
        _sensor_version_option(run)

    report = commands.add_parser("report", help="view the latest capture, or a given report")
    report.add_argument("path", nargs="?", type=Path, help="capture folder or report.json")
    report.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    service = commands.add_parser("service", help="inspect the Ultra96 service before the demo")
    service.add_argument("--start", action="store_true", help="start only after confirming both ports are free")
    service.add_argument("--session-id", default=os.environ.get("LTC_COMMS_SESSION", "ltc-comms"),
                         help="session the started server accepts; default: LTC_COMMS_SESSION or ltc-comms")
    _jump_option(service)
    return parser


def capture_command(args, report_path):
    """Keep physical input and current protocol settings explicit in the child."""
    command = [sys.executable, "-u", "-m", "laptop.dual_bridge",
            "--ca", str(args.ca.expanduser().resolve()), "--port", str(args.port),
            "--left-address", args.left_address, "--right-address", args.right_address,
            "--duration", str(args.duration), "--session-id", args.session_id,
            "--ack-window", "32", "--progress-interval", "1",
            "--report", str(report_path), "--evidence", str(report_path.with_name("packets.jsonl"))]
    if args.keyboard:
        command.append("--keyboard")
    for flag, value in (("--rate", args.rate), ("--seed", args.seed),
                        ("--file", args.file.expanduser().resolve() if args.file else None),
                        ("--file-device", args.file_device)):
        if value is not None:
            command.extend((flag, str(value)))
    if getattr(args, "sensor_version", 3) == 4:
        # Only the three-flex boards name it, so a version 3 command line stays as it was.
        command.extend(("--sensor-version", "4"))
    return command


def _stop_child(child):
    """Retire only the process started by this invocation."""
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def _console_line(line):
    """Color the operator's terminal while the saved live log remains plain text."""
    if not sys.stdout.isatty():
        return line
    if "disconnected" in line or "failed" in line or "rejected" in line:
        color = "\x1b[33m"
    elif "device_id=1" in line or "device=1" in line:
        color = "\x1b[36m"
    elif "device_id=2" in line or "device=2" in line:
        color = "\x1b[35m"
    else:
        return line
    return color + line.rstrip("\n") + "\x1b[0m\n"


def run_tunnel(args):
    command = tunnel_command(args.jump, args.target, args.port, 8888, batch=False,
                             forwards=((args.monitor_port, 9999),), ssh=windows_ssh())
    print(f"Opening SSH tunnel on 127.0.0.1:{args.port} (samples) and 127.0.0.1:{args.monitor_port}"
          " (board view); enter passwords at SSH prompts.", flush=True)
    print("Keep this terminal open. Ctrl+C stops this tunnel.", flush=True)
    child = subprocess.Popen(command, cwd=ROOT)
    try:
        return child.wait()
    except KeyboardInterrupt:
        return 130
    finally:
        _stop_child(child)


def _device_rows(report):
    devices = report.get("devices")
    devices = devices if isinstance(devices, dict) else {}
    for identity in ("1", "2"):
        device = devices.get(identity)
        device = device if isinstance(device, dict) else {}
        source = device.get("source")
        yield identity, device, source if isinstance(source, dict) else {}


def _clean_capture(report, exit_code):
    if (exit_code != 0 or report.get("clean") is not True or
            report.get("mock_input") is not False or report.get("report_saved") is not True):
        return False
    for _, device, source in _device_rows(report):
        generated = source.get("generated")
        if (device.get("clean") is not True or device.get("protected_ble") is not True or
                device.get("unfinished") is not False or source.get("complete") is not True or
                source.get("clean") is not True or type(generated) is not int or generated <= 0):
            return False
        counts = [source.get(name) for name in ("source_submitted", "received", "acked")]
        counts += [device.get(name) for name in ("received", "sent", "acked")]
        if any(type(value) is not int or value != generated for value in counts):
            return False
        if any(source.get(name) != 0 for name in ("missing_received", "missing_acked")):
            return False
    return True


def show_report(path, *, save_readable=False):
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        path = path / "report.json"
    print(f"Report: {path}")
    try:
        report = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(report, dict):
            raise ValueError("report must contain a JSON object")
        exit_code = int(path.with_name("exit-code.txt").read_text(encoding="utf-8-sig").strip())
    except (OSError, ValueError) as error:
        print(f"CAPTURE NOT PASSED: missing, incomplete or unreadable evidence ({error}).")
        return 1

    if save_readable:
        path.with_name("report-readable.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Exit={exit_code}  clean={report.get('clean')}  mock_input={report.get('mock_input')}"
          f"  report_saved={report.get('report_saved')}")
    # A gated glove sends only while moving; Idle counts the send ticks it skipped.
    print("Device  Generated  Received  ACKed  MissingBLE  MissingACK  Gated  Idle")
    for identity, _, source in _device_rows(report):
        values = [source.get(name) for name in
                  ("generated", "received", "acked", "missing_received", "missing_acked",
                   "source_idle")]
        text = [str(value) if type(value) is int else "N/A" for value in values]
        gated = {True: "yes", False: "no"}.get(source.get("gated"), "N/A")
        print(f"{identity:>6}  {text[0]:>9}  {text[1]:>8}  {text[2]:>5}  {text[3]:>10}  {text[4]:>10}"
              f"  {gated:>5}  {text[5]:>4}")
    goodput = report.get("sensor_goodput")
    if isinstance(goodput, dict):
        print("Measured combined BLE sensor goodput at laptop reception: "
              f"{goodput.get('average_kbps', 0):.3f} kbps over "
              f"{goodput.get('elapsed_seconds', 0):.3f} observation seconds.")
        print("Sensor-packet bytes include application header; exclude BLE/TLS/SSH and command/file traffic.")
    command_total = 0
    for identity, device, _ in _device_rows(report):
        commands = device.get("commands")
        if isinstance(commands, dict):
            command_total += commands.get("completed", 0)
            print(f"Device {identity} commands: {commands}")
    if report.get("file_transfer") is not None:
        print(f"BLE file result: {report['file_transfer']}")
    if not _clean_capture(report, exit_code):
        print("CAPTURE NOT PASSED: inspect report.json and the run's log (run.log, or live.log "
              "for demo.py run); do not infer a phone total.")
        return 1
    # Review this exact run automatically; no separate report script or phone input.
    from tools.video_evidence import show_packet_examples
    if not show_packet_examples(path.parent):
        print("CAPTURE NOT PASSED: packet evidence could not be verified.")
        return 1
    total = sum(source["generated"] for _, _, source in _device_rows(report)) + command_total
    print("CAPTURE PASSED: physical ESP input through Ultra96 ingestion ACKs.")
    print(f"Phone expected increase: {total}")
    print("This count holds while the Ultra96 runs the built-in dummy model "
          "(one result per accepted sample or command).")
    print("Film the iPhone Received count before and after with your camera; phone receipt is not checked here.")
    return 0


def run_capture(args):
    if not args.ca.expanduser().is_file():
        print(f"CA file not found: {args.ca}. Use run --ca with your existing trusted CA.",
              file=sys.stderr)
        return 2
    if args.left_address.casefold() == args.right_address.casefold():
        print("The two ESP addresses must be distinct.", file=sys.stderr)
        return 2
    if args.keyboard and not sys.stdin.isatty():
        print("--keyboard requires an interactive terminal (press 1 or 2, no Enter).", file=sys.stderr)
        return 2
    if args.file is not None and (not args.file.expanduser().is_file()
                                 or not 1 <= args.file.expanduser().stat().st_size <= 65536):
        print("--file must refer to a file containing 1..65536 bytes.", file=sys.stderr)
        return 2
    root = args.output_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = root / f"B07-{stamp}-{uuid.uuid4().hex[:8]}"
    folder.mkdir()
    report_path = folder / "report.json"
    print(f"Physical two-ESP capture: {args.duration:g} seconds. Evidence: {folder}", flush=True)
    if getattr(args, "sensor_version", 3) == 4:
        print("Sensor packets: version 4 (the three-flex boards); the Ultra96 service must run "
              "with --sensor-version 4.", flush=True)
    child = None
    exit_code = 2
    try:
        # Python's UTF-8 stream keeps paths and messages stable across Windows consoles.
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        with (folder / "live.log").open("x", encoding="utf-8") as log:
            child = subprocess.Popen(
                capture_command(args, report_path), cwd=ROOT, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
            for line in child.stdout:
                log.write(line)
                log.flush()
                print(_console_line(line), end="", flush=True)
            exit_code = child.wait()
    except KeyboardInterrupt:
        exit_code = 130
        print("\nCapture interrupted; this is not a completed test.")
    except OSError as error:
        exit_code = 2
        print(f"Capture failed: {error}", file=sys.stderr)
    finally:
        _stop_child(child)
        if child is not None and child.stdout is not None:
            child.stdout.close()
        (folder / "exit-code.txt").write_text(f"{exit_code}\n", encoding="utf-8")
    result = show_report(report_path, save_readable=True)
    print(f"Saved in: {folder}")
    return exit_code if exit_code != 0 else result


def latest_report(output_root):
    root = Path(output_root).expanduser()
    folders = [path for path in root.glob("B07-*") if path.is_dir()]
    if not folders:
        raise FileNotFoundError(f"No B07 captures in {root}; run a capture first.")
    # Select the newest attempt, even when interrupted and missing its report.
    return max(folders, key=lambda path: (path.stat().st_mtime_ns, path.name)) / "report.json"


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.action == "tunnel":
            return run_tunnel(args)
        if args.action == "start":
            if args.left_address is None or args.right_address is None:
                boards = dict(load_boards())
                args.left_address = args.left_address or boards['left']
                args.right_address = args.right_address or boards['right']
            args.argv = list(argv) if argv is not None else sys.argv[1:]
            from tools.live_session import main as start_main
            return start_main(args)
        if args.action in ("run", "live"):
            if args.left_address is None or args.right_address is None:
                boards = dict(load_boards())
                args.left_address = args.left_address or boards['left']
                args.right_address = args.right_address or boards['right']
            print(f'Board mapping: LEFT / ID 1 = {args.left_address}; RIGHT / ID 2 = {args.right_address}')
            return run_capture(args)
        if args.action == "service":
            from tools.demo_service import main as service_main
            return service_main(start=args.start, session_id=args.session_id, jump=args.jump)
        return show_report(args.path if args.path is not None else latest_report(args.output_root))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Demo could not complete: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
