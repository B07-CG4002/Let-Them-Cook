"""Start the model owner's runner program for ultra96.model_bridge, replacing the one it last started.

tools/deploy_service runs this as root with sudo, once the new service listens:

    sudo -S /usr/bin/python3 -I model_runner_launch.py '<json options>'

It stops the runner recorded in the pid file (only when that process is still the
same one), starts the new runner in its own session through a login shell (so the
board's /etc/profile.d settings, such as PYNQ's environment, apply as in an
interactive `sudo -E` shell), with stdin from /dev/null and its output in a log
file, and checks that it is still running a moment later. It prints one JSON
line. Standard library only, and it imports nothing from this repository.

A runner it did not start (one started by hand) it stops only when the options
name it in "replace" by PID and start ticks (/proc/PID/stat field 22). Before
anything is stopped, each such entry must be gone (or its PID reused: other
start ticks) or look like a Python model runner: argv[0] names python, an
argument names socket_worker (Wen Ree's workers, socket_worker_hand among them)
or model_runner, and it is not PID 1, this launcher or an ultra96.server.
Otherwise the status is "failed" and nothing is signalled or started. After the
recorded runner has stopped and before the new one starts, each named runner
gets SIGTERM (that PID only, not its group) and, only while it is still the same
PID and start ticks after the wait, SIGKILL; the JSON line reports each in
"replaced" (stop: sigterm, sigkill or already_gone).
If the bridge reports that a second runner took over the connection, the new
runner is stopped again, so the two do not keep replacing each other, and the
status is "conflict". When it fails before it reaches the previous runner,
"previous" is "untouched" and "previous_running" names that runner's PID while
it still runs.

Options: python, script or module, cwd, args (list), log, pidfile, service_log, settle
(seconds the runner must survive), connect_wait (seconds to look for the bridge's "model
runner connected" line with the runner's PID in service_log), conflict_wait (seconds to
keep watching for a second runner after that), replace (list of {"pid", "start_ticks"}).
With script it runs `python script args` in cwd (default: the script's folder); with
module, `python -m module args` in cwd (required), the way a runner whose code imports
its own package is started by hand (for example `cd <checkout> && python -m src.socket_worker ...`).
"""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

CONNECTED = "model runner connected"
REPLACED = "a new model runner connected; closing the previous one"
CONFLICT = ("another model runner is connected to the bridge (one started by hand?), and the two "
            "kept replacing each other; the new runner was stopped. Stop the other runner, or find "
            "its PID and start ticks with 'python demo.py service' and deploy again with "
            "--replace-runner PID:TICKS")
# An argument of a process that may be replaced as a model runner names one of these.
RUNNER_MARKERS = ("socket_worker", "model_runner")
_TICKS = re.compile(r"[0-9]+")


def _stat(pid):
    try:
        return (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return None


def start_ticks(pid):
    fields = _stat(pid)
    return None if fields is None else fields[19]


def alive(pid, ticks):
    fields = _stat(pid)
    return fields is not None and fields[19] == ticks and fields[0] != "Z"


def _recorded(pidfile):
    try:
        previous = json.loads(Path(pidfile).read_text())
        return int(previous["pid"]), str(previous["start_ticks"])
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None


def recorded_runner(pidfile):
    """The PID of the runner this launcher started last, while it still runs; else None."""
    recorded = _recorded(pidfile)
    return recorded[0] if recorded is not None and alive(*recorded) else None


def _stop_group(pid, ticks, wait):
    # The runner runs in its own session; signal the whole group so its children stop too.
    for sig, seconds in ((signal.SIGTERM, wait), (signal.SIGKILL, wait)):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return "sigterm" if sig == signal.SIGTERM else "sigkill"
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not alive(pid, ticks):
                return "sigterm" if sig == signal.SIGTERM else "sigkill"
            time.sleep(0.1)
    raise RuntimeError("the runner did not exit after SIGKILL")


def stop_previous(pidfile, wait=5.0):
    """Stop the runner this launcher started last; never signal a reused PID."""
    recorded = _recorded(pidfile)
    if recorded is None:
        return "none"
    if not alive(*recorded):
        return "not_running"
    return _stop_group(*recorded, wait)


def cmdline(pid):
    """The process's argv, or None once it has gone."""
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return [part.decode("utf-8", "replace") for part in raw.rstrip(b"\0").split(b"\0")] if raw else []


def _signal(pid, sig):
    os.kill(pid, sig)


def replacement(entry):
    """(pid, start ticks) of one "replace" entry."""
    if not isinstance(entry, dict) or set(entry) != {"pid", "start_ticks"}:
        raise ValueError("a replace entry is {\"pid\": ..., \"start_ticks\": ...}: " + json.dumps(entry))
    pid, ticks = entry["pid"], entry["start_ticks"]
    if type(pid) is not int or pid < 1 or not (isinstance(ticks, str) and _TICKS.fullmatch(ticks)):
        raise ValueError("a replace entry needs a positive integer pid and decimal start_ticks: "
                         + json.dumps(entry))
    return pid, str(int(ticks))


def not_a_runner(pid, argv):
    """Why the process may not be replaced as a model runner; None when it may."""
    if pid == 1:
        return "PID 1 is init"
    if pid == os.getpid():
        return "it is this launcher"
    if not argv or "python" not in os.path.basename(argv[0]):
        return "it is not a Python program"
    if any("ultra96.server" in part or part.endswith("ultra96/server.py") for part in argv):
        return "it is an ultra96.server"
    if not any(marker in part for part in argv[1:] for marker in RUNNER_MARKERS):
        return "no argument names " + ", ".join(RUNNER_MARKERS)
    return None


def _refuse(pid, ticks, reason, argv):
    return RuntimeError("--replace-runner {}:{} refused, {}: {}".format(
        pid, ticks, reason, " ".join(argv)[:300]))


def check_replacements(entries):
    """Before anything is signalled: each named runner is gone, or a Python model runner."""
    for pid, ticks in entries:
        argv = cmdline(pid) if alive(pid, ticks) else None
        reason = None if argv is None else not_a_runner(pid, argv)
        if reason is not None:
            raise _refuse(pid, ticks, reason, argv)


def _wait_gone(pid, ticks, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid, ticks):
            return True
        time.sleep(0.1)
    return not alive(pid, ticks)


def replace_runner(pid, ticks, wait=5.0):
    """Stop one runner started by hand: SIGTERM to that PID only, then SIGKILL only while it
    is still the same PID and start ticks. Returns its report for the JSON line."""
    argv = cmdline(pid) if alive(pid, ticks) else None
    report = dict(pid=pid, start_ticks=ticks, argv=argv)
    if argv is None:
        return dict(report, stop="already_gone")
    reason = not_a_runner(pid, argv)  # checked again: the PID may have run exec since
    if reason is not None:
        raise _refuse(pid, ticks, reason, argv)
    try:
        _signal(pid, signal.SIGTERM)
    except ProcessLookupError:
        return dict(report, stop="already_gone")
    if _wait_gone(pid, ticks, wait):
        return dict(report, stop="sigterm")
    if not alive(pid, ticks):  # never SIGKILL a reused PID
        return dict(report, stop="sigterm")
    try:
        _signal(pid, signal.SIGKILL)
    except ProcessLookupError:  # it exited between the check and the signal
        return dict(report, stop="sigterm")
    if _wait_gone(pid, ticks, wait):
        return dict(report, stop="sigkill")
    raise RuntimeError("the runner PID {} did not exit after SIGKILL".format(pid))


def _tail(path, lines=10):
    try:
        return Path(path).read_text(errors="replace").splitlines()[-lines:]
    except FileNotFoundError:
        return []


def _log_since(service_log, offset):
    try:
        with open(service_log, "rb") as stream:
            stream.seek(offset)
            return stream.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return ""


def module_file(cwd, module):
    """The file `python -m module` runs from cwd: <module path>.py, or its package's __main__.py."""
    path = Path(cwd).joinpath(*module.split("."))
    for candidate in (path.with_name(path.name + ".py"), path / "__main__.py"):
        if candidate.is_file():
            return candidate
    return None


def runner_command(options, result):
    """(argv after the Python, working folder) of the runner the options describe."""
    if options.get("module"):
        module, cwd = options["module"], Path(options["cwd"])
        result.update(module=module, cwd=str(cwd))
        if not cwd.is_dir():
            raise FileNotFoundError("runner folder not found: " + str(cwd))
        if module_file(cwd, module) is None:
            raise FileNotFoundError("runner module not found: {} in {}".format(module, cwd))
        return ["-m", module], cwd
    script = Path(options["script"])
    cwd = Path(options.get("cwd") or script.parent)
    result.update(script=str(script), cwd=str(cwd))
    if not script.is_file():
        raise FileNotFoundError("runner script not found: " + str(script))
    if not cwd.is_dir():
        raise FileNotFoundError("runner folder not found: " + str(cwd))
    return [str(script)], cwd


def launch(options, result):
    result.update(log=options["log"])
    replace = [replacement(entry) for entry in options.get("replace", [])]
    command, cwd = runner_command(options, result)
    if not Path(options["python"]).is_file():
        raise FileNotFoundError("runner python not found: " + options["python"])
    # A refused entry fails here, while every runner is still as it was.
    check_replacements(replace)
    result["previous"] = stop_previous(options["pidfile"])
    if replace:
        result["replaced"] = []
    for pid, ticks in replace:
        try:
            result["replaced"].append(replace_runner(pid, ticks))
        except Exception:
            result["replaced"].append(dict(pid=pid, start_ticks=ticks, stop="failed"))
            raise
    service_log = options.get("service_log")
    offset = os.path.getsize(service_log) if service_log and os.path.exists(service_log) else 0
    argv = ["/bin/bash", "-l", "-c", 'exec "$@"', "model-runner", options["python"]] + command
    argv += list(options.get("args", []))
    with open(options["log"], "ab", buffering=0) as out:
        os.chmod(options["log"], 0o644)
        runner = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                  stdout=out, stderr=out, start_new_session=True)
    ticks = start_ticks(runner.pid)
    record = dict(pid=runner.pid, start_ticks=ticks, argv=argv, started=time.time())
    Path(options["pidfile"]).write_text(json.dumps(record) + "\n")
    os.chmod(options["pidfile"], 0o644)
    result.update(pid=runner.pid)
    deadline = time.monotonic() + options.get("settle", 3.0)
    while time.monotonic() < deadline:
        if runner.poll() is not None:
            result.update(status="exited", returncode=runner.returncode, log_tail=_tail(options["log"]))
            return result
        time.sleep(0.1)
    result["status"] = "running"
    result["connected"] = False
    if service_log:
        # The bridge logs the connecting process's PID; bash exec keeps the runner's.
        mine = "{} (pid {})".format(CONNECTED, runner.pid)
        deadline = time.monotonic() + options.get("connect_wait", 10.0)
        while time.monotonic() < deadline and runner.poll() is None:
            text = _log_since(service_log, offset)
            if REPLACED in text:
                result.update(status="conflict", connected=False, error=CONFLICT,
                              stopped=_stop_group(runner.pid, ticks, 5.0))
                return result
            if mine in text and not result["connected"]:
                result["connected"] = True
                # Keep watching a little: a second runner shows up as a replaced connection.
                deadline = min(deadline, time.monotonic() + options.get("conflict_wait", 3.0))
            time.sleep(0.2)
        if runner.poll() is not None:
            result.update(status="exited", returncode=runner.returncode, log_tail=_tail(options["log"]))
    return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    result = {}
    try:
        options = json.loads(argv[0])
        launch(options, result)
    except Exception as exc:
        result.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
        if "previous" not in result:  # failed before it reached the previous runner
            result["previous"] = "untouched"
            try:
                result["previous_running"] = recorded_runner(options["pidfile"])
            except Exception:
                result["previous_running"] = None
    print(json.dumps(result), flush=True)
    return 0 if result.get("status") == "running" else 1


if __name__ == "__main__":
    sys.exit(main())
