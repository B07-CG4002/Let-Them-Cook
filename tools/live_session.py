"""python demo.py start: the SSH tunnel, both gloves, a live view and one run log, in one command.

1. Checks the CA and that the two local ports are free, then makes the run folder and its
   run.log (this process is the only writer of that file).
2. Starts one OpenSSH process with two forwards: 127.0.0.1:18889 -> Ultra96 8888 (samples
   and ACKs) and 127.0.0.1:19999 -> Ultra96 9999 (the read-only board view). The owner types
   both passwords at OpenSSH's own prompts; nothing is stored, and nothing is printed or read
   from the keyboard here while OpenSSH is asking.
3. Waits until the board view's forward answers, which proves the login is done, and reads
   the service's state (model, phone subscriber).
4. Starts the capture (laptop.dual_bridge --supervised) in its own process group and draws
   the live view from its JSON stream and the board view, until q, Ctrl+C or --duration.
5. Stops the capture gracefully (it reads the gloves' final counters), then the board view,
   then the tunnel, and writes the summary and the CAPTURE PASSED check at the end of run.log.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid

from common.tls import client_context
from laptop import board_monitor
from laptop.board_monitor import BoardMonitor
from tools.live_view import (GLOVES, RunState, counts_text, header_lines, log_line,
                             summary_lines)
from tools.ssh_tunnel import tunnel_command, windows_ssh

ROOT = Path(__file__).resolve().parents[1]
# The capture's own shutdown budgets (commands, BLE tail, drain, writers) stay well below this.
CAPTURE_STOP_SECONDS = 120.0
# Without --duration the capture runs until it is told to stop.
UNLIMITED_SECONDS = 86400.0
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def child_options():
    """Children get their own process group: a console Ctrl+C reaches only this process,
    which then stops them in order (the capture first, the tunnel last)."""
    return {"creationflags": _NEW_GROUP} if os.name == "nt" else {"start_new_session": True}


def capture_command(args, report_path):
    """The capture child: the options of demo.py run, plus --supervised."""
    command = [sys.executable, "-u", "-m", "laptop.dual_bridge",
               "--ca", str(Path(args.ca).expanduser().resolve()), "--port", str(args.port),
               "--left-address", args.left_address, "--right-address", args.right_address,
               "--duration", str(args.duration or UNLIMITED_SECONDS),
               "--session-id", args.session_id, "--ack-window", "32",
               "--report", str(report_path),
               "--evidence", str(report_path.with_name("packets.jsonl"))]
    if args.mock:
        # Synthetic input for tests only; it never counts as a physical capture.
        command += ["--mock", "--expected-rate", str(args.rate)]
    else:
        command += ["--rate", str(args.rate)]
    if args.seed is not None:
        command += ["--seed", str(args.seed)]
    return command + ["--progress-interval", "0", "--supervised"]


def port_in_use(port):
    """True when something already holds 127.0.0.1:port (checked by binding, never by connecting)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def port_owner(port):
    """The PID listening on 127.0.0.1:port according to netstat (Windows), or None."""
    if os.name != "nt":
        return None
    try:
        output = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                                text=True, timeout=10, errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.splitlines():
        parts = line.split()
        if (len(parts) >= 5 and parts[0] == "TCP" and parts[1] == "127.0.0.1:{}".format(port)
                and parts[3] == "LISTENING"):
            return parts[4]
    return None


class Console:
    """The live view's screen and the keyboard. Only the view thread draws."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.live = False
        self._output_mode = None

    @staticmethod
    def enable_ctrl_c():
        # A host may start us with Ctrl+C ignored, an attribute children inherit.
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
            except (OSError, AttributeError):
                pass

    def enter(self):
        """Switch to an in-place view (VT sequences, alternate screen) when on a console."""
        try:
            if not self.stream.isatty():
                return False
        except (AttributeError, ValueError):
            return False
        if os.name == "nt":
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.GetStdHandle(-11)
                mode = ctypes.c_uint32()
                if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                    return False
                # ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING; restored on leave.
                if not kernel32.SetConsoleMode(handle, mode.value | 0x1 | 0x4):
                    return False
                self._output_mode = mode.value
            except (OSError, AttributeError):
                return False
        self.live = True
        self._write("\x1b[?1049h\x1b[?25l\x1b[H\x1b[J")
        return True

    def draw(self, lines):
        # Home, each line cleared to its end, then the rest of the screen: nothing scrolls.
        self._write("\x1b[H" + "\x1b[K\n".join(lines) + "\x1b[K\x1b[J")

    def leave(self):
        if self.live:
            self.live = False
            self._write("\x1b[?25h\x1b[?1049l")
        if self._output_mode is not None:
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), self._output_mode)
            except (OSError, AttributeError):
                pass
            self._output_mode = None

    @staticmethod
    def size():
        size = shutil.get_terminal_size((100, 40))
        return size.columns, size.lines

    def _write(self, text):
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def keys(self):
        """Keys typed since the last call (Windows console only; never blocks)."""
        if not self.live or os.name != "nt":
            return []
        try:
            import msvcrt
        except ImportError:
            return []
        keys = []
        for _ in range(16):
            if not msvcrt.kbhit():
                break
            key = msvcrt.getwch()
            if key in ("\x00", "\xe0"):
                if msvcrt.kbhit():
                    msvcrt.getwch()  # the second half of a function or arrow key
                continue
            keys.append(key)
        return keys


class Session:
    """One run: the children, the threads and the run log."""

    def __init__(self, args, folder, console):
        self.args = args
        self.folder = folder
        self.console = console
        self.events = queue.Queue()
        self.report_path = folder / "report.json"
        self.log_path = folder / "run.log"
        forwards = "127.0.0.1:{} -> Ultra96 8888, 127.0.0.1:{} -> Ultra96 9999".format(
            args.port, args.monitor_port)
        self.tunnel_text = ("already open (--no-tunnel), " if args.no_tunnel else
                            "{} via {}, ".format(args.target, args.jump)) + forwards
        self.state = RunState(folder=folder, addresses={1: args.left_address, 2: args.right_address},
                              rate_hz=args.rate, session_id=args.session_id,
                              duration=args.duration, keyboard=args.keyboard,
                              tunnel=self.tunnel_text)
        self.ssh = self.capture = self.monitor = None
        self.ssh_lines = []
        self.threads = {}
        self.log_done = threading.Event()
        self.view_done = threading.Event()
        self.stop_requested = threading.Event()
        self.break_pressed = threading.Event()
        self.ssh_exit_noted = False
        self.capture_unkillable = False
        self.log_failed = None
        self._log = None

    # ----- events and the run log --------------------------------------------------------

    def post(self, source, kind, **payload):
        self.events.put((time.time(), source, kind, payload))

    def note(self, source, text, message=True):
        self.post(source, "note", text=text, message=message)

    def open_log(self, command, revision):
        self._log = self.log_path.open("x", encoding="utf-8", newline="\n")
        args = self.args
        for line in header_lines(folder=self.folder, started=time.time(), command=command,
                                 revision=revision, session_id=args.session_id, rate_hz=args.rate,
                                 duration=args.duration, keyboard=args.keyboard,
                                 addresses={1: args.left_address, 2: args.right_address},
                                 tunnel=self.tunnel_text):
            self._log.write(line + "\n")
        self._log.flush()
        self._start(self._log_writer, "run-log")

    def _log_writer(self):
        while True:
            try:
                batch = [self.events.get(timeout=0.1)]
            except queue.Empty:
                if self.log_done.is_set():
                    return
                continue
            while len(batch) < 2000:
                try:
                    batch.append(self.events.get_nowait())
                except queue.Empty:
                    break
            lines = []
            with self.state.lock:
                for when, source, kind, payload in batch:
                    try:
                        lines += self.state.apply(when, source, kind, payload)
                    except Exception as exc:  # one odd event must not stop the log
                        lines.append((when, "run", "could not read a {} {} event ({}: {})".format(
                            source, kind, type(exc).__name__, exc)))
            if self.log_failed is None and lines:
                try:
                    self._log.write("".join(log_line(*line) + "\n" for line in lines))
                    self._log.flush()  # closing the window ends the process within 5 s
                except (OSError, ValueError) as exc:
                    self.log_failed = exc

    def write_tail(self, lines):
        """Stop the timeline writer (once) and append plain lines: report, summary, verdict."""
        if not self.log_done.is_set():
            self.log_done.set()
            thread = self.threads.get("run-log")
            if thread is not None:
                thread.join(30)
        if self.log_failed is None:
            try:
                self._log.write("".join(line + "\n" for line in lines))
                self._log.flush()
            except (OSError, ValueError) as exc:
                self.log_failed = exc

    def close_log(self):
        try:
            self._log.close()
        except OSError as exc:
            self.log_failed = self.log_failed or exc

    # ----- threads -----------------------------------------------------------------------

    def _start(self, target, name, *args):
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        self.threads[name] = thread
        thread.start()
        return thread

    def _pump_capture(self, stream):
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("not an object")
            except ValueError:
                self.post("capture", "stderr", line="capture output: " + line[:300])
                continue
            self.post("capture", "record", record=record)

    def _pump_text(self, stream, source):
        for line in stream:
            line = line.rstrip()
            if line:
                if source == "tunnel":
                    self.ssh_lines.append(line)
                self.post(source, "stderr", line=line[:500])

    def _draw(self):
        while not self.view_done.wait(0.25):
            columns, rows = self.console.size()
            with self.state.lock:
                lines = self.state.render(time.time(), columns, rows - 1)
            self.console.draw(lines)

    def _print_plain(self):
        """Without a console: messages as they come, and the counts every five seconds."""
        seen = 0
        next_counts = time.time() + 5.0
        while not self.view_done.wait(0.25):
            with self.state.lock:
                lines = self.state.new_messages(seen)
                seen = self.state.message_count
                now = time.time()
                if now >= next_counts and self.state.observation_started is not None:
                    next_counts = now + 5.0
                    lines += [(now, glove.name, "counts " + counts_text(glove))
                              for glove in self.state.gloves.values()]
            for line in lines:
                print(log_line(*line), flush=True)

    def start_view(self):
        if self.console.enter():
            self._start(self._draw, "live-view")
        else:
            self._start(self._print_plain, "plain-view")

    def stop_view(self):
        self.view_done.set()
        for name in ("live-view", "plain-view"):
            thread = self.threads.get(name)
            if thread is not None:
                thread.join(5)
        self.console.leave()

    # ----- the board view ----------------------------------------------------------------

    def start_monitor(self):
        def on_event(kind, payload):
            self.post("board", kind, **payload)
        self.monitor = BoardMonitor(self.args.monitor_port, self.args.ca, self.args.session_id,
                                    on_event).start()

    def board_state(self):
        return self.monitor.state if self.monitor is not None else None

    def wait_for_forward(self, deadline=None):
        """The board view's first answer once the forward is up; None if ssh ended first."""
        while True:
            state = self.board_state()
            if state in board_monitor.FORWARD_UP:
                return state
            if self.ssh is not None and self.ssh.poll() is not None:
                return None
            if deadline is not None and time.monotonic() > deadline:
                return board_monitor.WAITING
            time.sleep(0.2)

    def wait_for_board_settle(self, limit=4.0):
        """After the capture, give the board view time to report the last results.

        True once every published result is counted as sent or not sent and the counts
        stayed still for 0.75 s; False when that did not happen in time or the view is down.
        """
        if self.board_state() != board_monitor.CONNECTED:
            return False
        end = time.monotonic() + limit
        last, stable_since = None, time.monotonic()
        while time.monotonic() < end:
            with self.state.lock:
                boards = [glove.board for glove in self.state.gloves.values()]
            snapshot = json.dumps(boards, sort_keys=True)
            settled = all(board is not None and board["results"] + board["commands"]
                          <= board["sent"] + board["unsent"] for board in boards)
            if snapshot != last:
                last, stable_since = snapshot, time.monotonic()
            elif settled and time.monotonic() - stable_since >= 0.75:
                return self.board_state() == board_monitor.CONNECTED
            time.sleep(0.25)
        return False

    # ----- the tunnel --------------------------------------------------------------------

    def start_tunnel(self):
        command = tunnel_command(self.args.jump, self.args.target, self.args.port, 8888,
                                 batch=False, forwards=((self.args.monitor_port, 9999),),
                                 ssh=windows_ssh())
        # Win32-OpenSSH prompts on and reads from the console itself (CONIN$), so ssh can keep
        # its stdin closed and its messages piped into the run log.
        self.ssh = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace",
                                    **child_options())
        self._start(self._pump_text, "ssh-stderr", self.ssh.stderr, "tunnel")
        self.note("tunnel", "ssh started (pid {}); waiting for the two logins".format(self.ssh.pid))

    def note_ssh_exit(self, context=""):
        """Log the tunnel's end once, with ssh's exit code."""
        if self.ssh is not None and self.ssh.poll() is not None and not self.ssh_exit_noted:
            self.ssh_exit_noted = True
            self.note("tunnel", "ssh exited (code {}){}".format(self.ssh.returncode, context))

    def stop_tunnel(self):
        if self.ssh is None:
            return
        if self.ssh.poll() is not None:
            self.note_ssh_exit()
            return
        if os.name == "nt":
            # The whole tree: before the stujump login ends, the ProxyCommand ssh does not
            # notice that the outer one has gone and would keep prompting on this console.
            try:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.ssh.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
        if self.ssh.poll() is None:
            self.ssh.terminate()
        try:
            self.ssh.wait(5)
        except subprocess.TimeoutExpired:
            self.ssh.kill()
            try:
                self.ssh.wait(5)
            except subprocess.TimeoutExpired:
                self.note("tunnel", "ssh (pid {}) did not exit: close it before the next "
                          "start".format(self.ssh.pid))
                return
        self.ssh_exit_noted = True
        self.note("tunnel", "tunnel closed")

    # ----- the capture -------------------------------------------------------------------

    def start_capture(self):
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.capture = subprocess.Popen(
            capture_command(self.args, self.report_path), cwd=ROOT, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", bufsize=1, **child_options())
        self._start(self._pump_capture, "capture-stdout", self.capture.stdout)
        self._start(self._pump_text, "capture-stderr", self.capture.stderr, "capture")
        self.note("run", "capture started (pid {})".format(self.capture.pid))

    def tell_capture(self, line):
        try:
            self.capture.stdin.write(line + "\n")
            self.capture.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def request_stop(self, reason):
        if self.stop_requested.is_set():
            return
        self.stop_requested.set()
        with self.state.lock:
            self.state.stop_reason = reason
            self.state.phase = "stopping"
        self.note("run", "stopping ({}); the capture reads the gloves' final counters".format(reason))
        if self.capture is not None and self.capture.poll() is None and not self.tell_capture("stop"):
            self.note("run", "could not tell the capture to stop; it stops at its duration")

    def kill_capture(self):
        if self.capture is None or self.capture.poll() is not None or self.capture_unkillable:
            return
        self.capture.kill()
        try:
            self.capture.wait(10)
        except subprocess.TimeoutExpired:
            self.capture_unkillable = True
            self.note("run", "the capture (pid {}) did not exit within 10 s of being killed"
                      .format(self.capture.pid))
            return
        self.note("run", "capture killed: this run is not a completed test")

    def watch(self):
        """The main loop while the capture runs: keys, Ctrl+C, the tunnel, the stop deadline."""
        args = self.args
        deadline = None
        while self.capture.poll() is None:
            try:
                for key in self.console.keys():
                    if key in ("q", "Q"):
                        self.request_stop("stopped by the operator (q)")
                    elif key == "\x03":
                        self.request_stop("stopped by the operator (Ctrl+C)")
                    elif key in ("1", "2") and args.keyboard and not self.stop_requested.is_set():
                        if self.tell_capture("command " + key):
                            self.note(GLOVES[int(key)], "command requested (key {})".format(key))
                if self.break_pressed.is_set():
                    self.request_stop("stopped by Ctrl+Break")
                if self.ssh is not None and self.ssh.poll() is not None and not self.ssh_exit_noted:
                    if self.stop_requested.is_set():
                        self.note_ssh_exit(" while the run was stopping: the last ACKs and "
                                           "board counts may be cut off")
                    else:
                        self.note_ssh_exit(": the tunnel is gone")
                        self.request_stop("the SSH tunnel closed")
                if self.stop_requested.is_set():
                    deadline = deadline or time.monotonic() + CAPTURE_STOP_SECONDS
                    if time.monotonic() > deadline:
                        self.note("run", "the capture did not stop within {:.0f} s".format(
                            CAPTURE_STOP_SECONDS))
                        self.kill_capture()
                        return False
                time.sleep(0.05)
            except KeyboardInterrupt:
                if not self.stop_requested.is_set():
                    self.request_stop("stopped by the operator (Ctrl+C)")
                else:
                    self.note("run", "second Ctrl+C: stopping at once")
                    self.kill_capture()
                    return False
        return True


def _revision():
    try:
        from laptop import reporting
        revision = reporting.local_revision(ROOT)
        dirty = reporting.source_provenance(ROOT).get("worktree_dirty")
    except Exception:
        return None
    return "{} (with local changes)".format(revision) if revision and dirty else revision


def _verdict(report_path):
    """demo.py's CAPTURE PASSED check, captured as lines."""
    import demo
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = demo.show_report(report_path)
    return code, buffer.getvalue().splitlines()


def _precheck(args):
    if not Path(args.ca).expanduser().is_file():
        return "CA file not found: {}. Use start --ca with your existing trusted CA.".format(args.ca)
    try:
        client_context(Path(args.ca).expanduser())
    except (ssl.SSLError, OSError, ValueError) as exc:
        return ("CA file {} could not be loaded as a CA certificate ({}). Use start --ca with "
                "your ca-cert.pem.".format(args.ca, exc))
    if args.left_address.casefold() == args.right_address.casefold():
        return "The two glove addresses must be distinct."
    if args.port == args.monitor_port:
        return "--port and --monitor-port must differ."
    if args.no_tunnel:
        if not port_in_use(args.port):
            return ("Nothing listens on 127.0.0.1:{}: open the tunnel with python demo.py tunnel "
                    "in another terminal, or run start without --no-tunnel.".format(args.port))
        return None
    for port in (args.port, args.monitor_port):
        if port_in_use(port):
            owner = port_owner(port)
            return ("Port 127.0.0.1:{} is already in use{}. If it is an open 'python demo.py "
                    "tunnel', close it, or keep it and run start --no-tunnel.".format(
                        port, " by PID " + owner if owner else ""))
    return None


def main(args, console=None):
    """Run python demo.py start with demo's parsed arguments; return the exit code."""
    Console.enable_ctrl_c()
    problem = _precheck(args)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    root = Path(args.output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = root / "B07-{}-{}".format(stamp, uuid.uuid4().hex[:8])
    folder.mkdir()
    command = "python demo.py " + " ".join(shlex.quote(part) for part in getattr(args, "argv", ["start"]))
    session = Session(args, folder, console or Console())
    session.open_log(command, _revision())
    print("Run folder: {}".format(folder), flush=True)

    exit_code, completed = 2, False
    previous_break = previous_interrupt = None
    if hasattr(signal, "SIGBREAK"):
        try:
            previous_break = signal.signal(signal.SIGBREAK, lambda *_: session.break_pressed.set())
        except ValueError:
            previous_break = None
    try:
        failure = None
        try:
            exit_code, completed = _run(session)
        except KeyboardInterrupt:
            session.note("run", "interrupted: this run is not a completed test")
            exit_code = 130
        except Exception as exc:
            failure = "{}: {}".format(type(exc).__name__, exc)
            session.note("run", "failed: " + failure)
            exit_code = 2
        finally:
            # Cleanup must finish even if Ctrl+C is pressed again: an ssh left running would
            # keep holding the ports. Each step runs even when an earlier one fails.
            try:
                previous_interrupt = signal.signal(signal.SIGINT, signal.SIG_IGN)
            except ValueError:
                previous_interrupt = None
            for step in (session.kill_capture, session.stop_view,
                         session.monitor.stop if session.monitor is not None else None,
                         session.stop_tunnel):
                if step is None:
                    continue
                try:
                    step()
                except Exception as exc:
                    session.note("run", "cleanup step {} failed: {}: {}".format(
                        getattr(step, "__name__", step), type(exc).__name__, exc))
        if failure is not None:
            print("The run failed: " + failure, file=sys.stderr)
        return _finish(session, exit_code, completed)
    finally:
        # Ctrl+Break and Ctrl+C stay handled until the summary and the verdict are written.
        if previous_break is not None:
            signal.signal(signal.SIGBREAK, previous_break)
        if previous_interrupt is not None:
            signal.signal(signal.SIGINT, previous_interrupt)


def _abort(session, reason):
    """End a run before the capture started; return (exit code, completed)."""
    with session.state.lock:
        session.state.stop_reason = reason
    session.note("run", "not started: " + reason)
    return 2, False


def _run(session):
    """Tunnel, board view and capture; return (exit code, whether the capture ended normally)."""
    args = session.args
    session.start_monitor()
    if args.no_tunnel:
        state = session.wait_for_forward(deadline=time.monotonic() + 8.0)
    else:
        print("Opening the SSH tunnel. When OpenSSH asks, type the password for {}, then the "
              "password for {}.".format(args.jump, args.target), flush=True)
        with session.state.lock:
            session.state.phase = "tunnel"
        session.start_tunnel()
        state = session.wait_for_forward()
        if state is None:
            code = session.ssh.returncode
            session.note("tunnel", "ssh ended before the tunnel was up (exit {})".format(code))
            time.sleep(0.2)  # let the last stderr lines arrive
            print("The SSH tunnel did not come up (ssh exit {}).".format(code), file=sys.stderr)
            for line in session.ssh_lines[-8:]:
                print("  " + line, file=sys.stderr)
            if any("publickey" in line and "password" not in line for line in session.ssh_lines):
                print("stujump offers only public-key login off the NUS VPN: connect the VPN "
                      "and run this again.", file=sys.stderr)
            return _abort(session, "the SSH tunnel did not come up (ssh exit {})".format(code))
        session.note("tunnel", "tunnel up")
    if state == board_monitor.NO_SERVICE:
        print("The tunnel is up, but no Communications service answered on the Ultra96. "
              "Check it with python demo.py service.", file=sys.stderr)
        return _abort(session, "no Communications service answered on the Ultra96")
    if state == board_monitor.TLS_FAILED:
        print("The Ultra96 service's certificate was refused ({}). It may have expired: see "
              "section 9.2 of the hand-off document.".format(session.monitor.detail),
              file=sys.stderr)
        return _abort(session, "the Ultra96 certificate was refused")
    if not port_in_use(args.port):
        print("The sample forward 127.0.0.1:{} is not listening.".format(args.port), file=sys.stderr)
        return _abort(session, "the sample forward was not listening")
    if state == board_monitor.CONNECTED:
        end = time.monotonic() + 3.0
        while time.monotonic() < end and session.state.board is None:
            time.sleep(0.05)
    elif state == board_monitor.WAITING:
        session.note("board", "no board view on 127.0.0.1:{} (the open tunnel does not forward "
                     "it): model results and phone delivery are not shown".format(args.monitor_port))
    with session.state.lock:
        session.state.phase = "connecting"
        board = session.state.board
    if board is not None and board["subscriber"] is None:
        session.note("board", "the phone is NOT subscribed: its results are dropped until it "
                     "connects (tap Communications Connect on the phone)")
    session.start_capture()
    session.start_view()
    completed = session.watch()
    with session.state.lock:
        session.state.phase = "stopping"
    settled = False
    try:
        for name in ("capture-stdout", "capture-stderr"):
            session.threads[name].join(10)
        settled = session.wait_for_board_settle()
    except KeyboardInterrupt:
        pass  # the capture has already finished; only the last board counters are skipped
    with session.state.lock:
        session.state.board_settled = settled
    if not completed:
        return 130, False  # killed: not a completed test
    code = session.capture.returncode
    return (code if code is not None else 1), True


def _end_reason(args, report, completed):
    """Why a run that nobody stopped ended, from what the capture reported."""
    if not completed:
        return "interrupted"
    observed = report.get("common_observation_seconds")
    stop = (report.get("run") or {}).get("stop") if isinstance(report.get("run"), dict) else None
    if (stop == "duration" and args.duration and isinstance(observed, (int, float))
            and observed >= 0.99 * args.duration):
        return "the duration ended"
    return "the capture ended by itself (see the messages in the log)"


def _finish(session, exit_code, completed):
    """exit-code.txt, then the report, the summary and the CAPTURE PASSED check in run.log.

    The summary is written before the packet check, which reads all of packets.jsonl
    (about 10 s per hour of run), so a window closed during that check keeps it.
    """
    import demo
    ended = time.time()
    (session.folder / "exit-code.txt").write_text("{}\n".format(exit_code), encoding="utf-8")
    try:
        report = json.loads(session.report_path.read_text(encoding="utf-8-sig"))
        report = report if isinstance(report, dict) else {}
    except (OSError, ValueError):
        report = {}
    # The counts part of demo.py's check (no packet scan); the full check follows.
    counted = bool(report) and demo._clean_capture(report, exit_code)
    with session.state.lock:
        session.state.phase = "finished"
        if session.state.stop_reason is None:
            session.state.stop_reason = _end_reason(session.args, report, completed)
        summary = summary_lines(session.state, report, ended, exit_code=exit_code,
                                passed=counted if session.capture is not None else None)
    session.write_tail(["", "=" * 79, "Report (report.json)", "=" * 79,
                        json.dumps(report, indent=2, sort_keys=True) if report else "(no report.json)"]
                       + summary + [""])
    for line in summary + [""]:
        print(line)
    if session.capture is not None and report:
        print("Checking packets.jsonl for the CAPTURE PASSED check (about 10 s per hour of run; "
              "if this window closes, run: python demo.py report {})".format(session.folder),
              flush=True)
        code, verdict = _verdict(session.report_path)
        if exit_code == 0:
            exit_code = code
    else:
        verdict = ["CAPTURE NOT PASSED: the capture did not start."] if session.capture is None             else ["CAPTURE NOT PASSED: report.json is missing or unreadable."]
        exit_code = exit_code or 1
    session.write_tail(verdict)
    session.close_log()
    for line in verdict:
        print(line)
    print("Run log: {}".format(session.log_path))
    if session.log_failed is not None:
        print("The run log could not be written completely: {}".format(session.log_failed),
              file=sys.stderr)
        exit_code = exit_code or 1
    return exit_code
