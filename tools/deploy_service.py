"""Deploy the Ultra96 board service from this checkout, with strict SSH trust and rollback.

The owner runs it and types both passwords; nothing is saved. It uploads only
common/ and ultra96/ into a new source folder under BOARD_ROOT, optionally
installs a renewed server certificate, and then, on the board:
  1. compiles the new source and checks that the certificate and key match and
     have not expired;
  2. checks that the running ultra96.server is this user's process under
     BOARD_ROOT and owns 127.0.0.1:8888 and :9999 (or, when none runs, that both
     ports are free) and copies its code aside for rollback; nothing on the board
     has changed up to here;
  3. stops it with SIGTERM and waits until the process has exited; if it has not
     within 10 s, sends SIGKILL after checking it is still the same process;
  4. installs the uploaded certificate (backing up the old pair) and starts the
     new service in its own session (it keeps running after SSH closes), with its
     output in BOARD_ROOT/evidence/server-co-<stamp>.log;
  5. waits until it listens on both ports. Any failure from step 3 on stops the
     new service, restores the previous certificate files and restarts the
     previous code; a final record is printed and saved in every case.
  6. only with --runner or --runner-module (and --model ultra96.model_bridge):
     once the new service listens, starts the model owner's runner program as
     root through sudo (ultra96/model_runner_launch.py), replacing the runner
     this tool started last. The owner types the sudo password too; it reaches
     sudo on stdin. A runner that does not start leaves the new service running
     and the tool exits nonzero.
Apart from step 6 it never uses sudo, and it never touches other users' processes.

    python -m tools.deploy_service --server-cert-dir <folder with server-cert.pem and server-key.pem>
    python -m tools.deploy_service --dry-run
    python -m tools.deploy_service --model ultra96.model_bridge --runner /home/xilinx/<folder>/<runner>.py
    python -m tools.deploy_service --model ultra96.model_bridge --runner-module src.socket_worker \
        --runner-cwd /tmp/cnn_conv1d/ai_integration --runner-arg=--weights --runner-arg <file> ...
"""
import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import json
from pathlib import Path
import re
import shlex
import socket
import sys

from tools.demo_service import BOARD_ROOT
from ultra96.protocol import validate_session

ROOT = Path(__file__).resolve().parents[1]
JUMP_HOST = "stujump.comp.nus.edu.sg"
BOARD_HOST = "makerslab-fpga-35.ddns.comp.nus.edu.sg"
RECORDS = ROOT / ".comms-local" / "deployments"
BRIDGE_MODEL = "ultra96.model_bridge"
PYNQ_PYTHON = "/usr/local/share/pynq-venv/bin/python"
MODULE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")

REMOTE_PROGRAM = r'''
import compileall, json, os, shutil, signal, ssl, subprocess, sys, time
from pathlib import Path

base = Path(sys.argv[1]); source = Path(sys.argv[2]); stamp = sys.argv[3]
options = json.loads(sys.argv[4])
PORTS = {8888, 9999}
# The sudo password for the runner arrives on stdin, never in argv.
sudo_password = sys.stdin.readline() if options.get("runner") else ""


def info(pid):
    path = Path("/proc") / str(pid)
    if path.stat().st_uid != os.getuid():
        raise PermissionError("wrong process owner")
    argv = [x.decode() for x in (path / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")]
    return dict(pid=int(pid), argv=argv, cwd=os.readlink(str(path / "cwd")),
                start_ticks=(path / "stat").read_text().split()[21])


def owned():
    matches = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            i = info(p.name)
            if "-m" in i["argv"] and i["argv"][i["argv"].index("-m") + 1] == "ultra96.server":
                if not Path(i["cwd"]).is_relative_to(base):
                    raise RuntimeError("an ultra96.server of this user runs outside " + str(base))
                matches.append(i)
        except (FileNotFoundError, PermissionError, ProcessLookupError, IndexError):
            pass
    return matches


def listening(inodes=None):
    """Listening TCP ports (IPv4 and IPv6); with inodes, only those sockets, loopback IPv4 only."""
    found = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        if not Path(table).exists():
            continue
        for line in Path(table).read_text().splitlines()[1:]:
            cols = line.split()
            if cols[3] != "0A" or (inodes is not None and cols[9] not in inodes):
                continue
            ip, port = cols[1].rsplit(":", 1)
            if inodes is not None and ip != "0100007F":
                raise RuntimeError("non-loopback application listener")
            found.add(int(port, 16))
    return found


def listeners(pid):
    inodes = set()
    for fd in (Path("/proc") / str(pid) / "fd").iterdir():
        try:
            target = os.readlink(str(fd))
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
        except FileNotFoundError:
            pass
    return listening(inodes)


def launch(argv, cwd, log):
    with open(log, "ab", buffering=0) as out:
        return subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out,
                                stderr=out, start_new_session=True)


def healthy(proc, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("server exited " + str(proc.returncode))
        if listeners(proc.pid) == PORTS:
            return
        time.sleep(0.1)
    raise RuntimeError("server listeners not ready")


def check_certificate(cert, key):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))  # fails when the key does not match
    decoded = ssl._ssl._test_decode_cert(str(cert))
    expires = ssl.cert_time_to_seconds(decoded["notAfter"])
    if expires <= time.time() + 3600:
        raise RuntimeError("server certificate expires within an hour: " + decoded["notAfter"])
    return decoded["notAfter"]


tls = base / "tls"; evidence = base / "evidence"
evidence.mkdir(mode=0o700, exist_ok=True)
services = owned()
if len(services) > 1:
    raise RuntimeError("more than one owned ultra96.server runs: " + json.dumps(services))
old = services[0] if services else None
if old is not None and listeners(old["pid"]) != PORTS:
    raise RuntimeError("the running service does not own exactly 127.0.0.1:8888 and :9999")
if old is None and listening() & PORTS:
    raise RuntimeError("port 8888 or 9999 is in use by another process; nothing was changed")
if not compileall.compile_dir(str(source), quiet=1):
    raise RuntimeError("new source compile failed")
record = dict(timestamp=stamp, previous=old, new_source=str(source), options=options)

# Every check that needs no change on the board comes first.
incoming = source / "tls-incoming"
if incoming.is_dir():
    record["certificate_not_after"] = check_certificate(incoming / "server-cert.pem",
                                                        incoming / "server-key.pem")
else:
    record["certificate_not_after"] = check_certificate(tls / "server-cert.pem",
                                                        tls / "server-key.pem")
rollback = None
if old is not None:
    # An independent minimal copy of the running code, for rollback.
    rollback = base / ("rollback-co-" + stamp)
    rollback.mkdir(mode=0o700)
    for name in ("common", "ultra96"):
        shutil.copytree(str(Path(old["cwd"]) / name), str(rollback / name),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (rollback / "previous-process.json").write_text(json.dumps(old, indent=2))
    record["rollback_source"] = str(rollback)

new_args = [sys.executable, "-u", "-m", "ultra96.server", "--cert", str(tls / "server-cert.pem"),
            "--key", str(tls / "server-key.pem"), "--session-id", options["session_id"]]
if options.get("event_log"):
    new_args += ["--event-log-dir", str(evidence / ("observer-co-live-" + stamp))]
if options.get("model"):
    new_args += ["--model", options["model"]]
if options.get("model_path"):
    new_args += ["--model-path", options["model_path"]]


def alive(process):
    """True while the recorded process (same PID and start time) exists and is not a zombie."""
    try:
        fields = (Path("/proc") / str(process["pid"]) / "stat").read_text().split()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return fields[21] == process["start_ticks"] and fields[2] != "Z"


def wait_gone(process, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(process):
            return True
        time.sleep(0.1)
    return not alive(process)


def stop_old():
    # Closing the ports is not enough: a model thread can keep the process alive.
    os.kill(old["pid"], signal.SIGTERM)
    if wait_gone(old, 10):
        return "sigterm"
    if info(old["pid"]) != old:  # never signal a reused PID
        raise RuntimeError("the old service changed while stopping")
    os.kill(old["pid"], signal.SIGKILL)
    if wait_gone(old, 5):
        return "sigkill"
    raise RuntimeError("the old service did not exit after SIGKILL")


backup = None


def install_certificate():
    global backup
    backup = tls / ("backup-" + stamp)
    backup.mkdir(mode=0o700, parents=True)
    for name in ("server-cert.pem", "server-key.pem"):
        if (tls / name).exists():
            shutil.copy2(str(tls / name), str(backup / name))
    for name in ("server-cert.pem", "server-key.pem"):
        shutil.copyfile(str(incoming / name), str(tls / (name + ".new")))
        os.chmod(str(tls / (name + ".new")), 0o600 if name.endswith("key.pem") else 0o644)
        os.replace(str(tls / (name + ".new")), str(tls / name))
    record["certificate_backup"] = str(backup)


def restore_certificate():
    if backup is not None:
        for name in ("server-cert.pem", "server-key.pem"):
            if (backup / name).exists():
                shutil.copy2(str(backup / name), str(tls / name))
        record["certificate_restored"] = True


def finish(status, current=None):
    record["status"] = status
    if current is not None:
        record["current"] = current
        (evidence / "server.pid").write_text(str(current["pid"]) + "\n")
    (evidence / "active-deployment.json").write_text(json.dumps(record, indent=2))
    print(json.dumps(record), flush=True)


# From the first change on the board, every failure ends in a restore and a final record.
stopped = False
new = None
try:
    if old is not None:
        if info(old["pid"]) != old:
            raise RuntimeError("the running service changed since it was checked")
        stopped = True
        record["old_stop"] = stop_old()
    if incoming.is_dir():
        install_certificate()
        shutil.rmtree(str(incoming))
    check_certificate(tls / "server-cert.pem", tls / "server-key.pem")
    new = launch(new_args, source, evidence / ("server-co-" + stamp + ".log"))
    healthy(new, options["start_timeout"])
    record.update(ports=sorted(listeners(new.pid)), log=str(evidence / ("server-co-" + stamp + ".log")))
    (source / "deployment.json").write_text(json.dumps(dict(record, status="deployed"), indent=2))
    finish("deployed", info(new.pid))
except BaseException as failure:
    record["error"] = type(failure).__name__ + ": " + str(failure)
    if new is not None and new.poll() is None:
        new.terminate()
        try:
            new.wait(timeout=5)
        except subprocess.TimeoutExpired:
            new.kill()
            new.wait(timeout=5)
    restore_certificate()
    if old is None:
        finish("failed_nothing_running")
    elif not stopped:
        finish("failed_old_still_running", old)
    else:
        try:
            args = list(old["argv"])
            if "--event-log-dir" in args:
                args[args.index("--event-log-dir") + 1] = str(evidence / ("rollback-observer-" + stamp))
            previous = launch(args, rollback, evidence / ("rollback-server-" + stamp + ".log"))
            healthy(previous, options["start_timeout"])
            finish("rolled_back", info(previous.pid))
        except BaseException as second:
            record["rollback_error"] = type(second).__name__ + ": " + str(second)
            finish("rollback_failed_nothing_running")
    raise


def start_runner(runner):
    """As root (sudo): replace the runner started last with this one; the service keeps running."""
    launch_options = {key: value for key, value in runner.items() if key != "sudo"}
    launch_options.update(log=str(evidence / ("model-runner-" + stamp + ".log")),
                          pidfile=str(evidence / "model-runner.json"), service_log=record["log"])
    argv = ["/usr/bin/python3", "-I", str(source / "ultra96" / "model_runner_launch.py"),
            json.dumps(launch_options)]
    if runner.get("sudo", True):
        argv = ["sudo", "-S", "-p", "", "--"] + argv
    try:
        done = subprocess.run(argv, input=sudo_password, capture_output=True, text=True, timeout=60,
                              cwd="/")
    except subprocess.TimeoutExpired:
        return dict(status="failed", error="the runner launcher did not answer within 60 s",
                    previous_running=recorded_runner())
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    if lines:
        return json.loads(lines[-1])
    # Without a JSON line, sudo itself refused (wrong password, no sudo rights).
    return dict(status="failed", error=(done.stderr.strip() or "sudo exit " + str(done.returncode))[-300:],
                previous="untouched", previous_running=recorded_runner())


def recorded_runner():
    """The PID of the runner the launcher started last, while it still runs (read only; it is root's)."""
    try:
        record = json.loads((evidence / "model-runner.json").read_text())
        fields = (Path("/proc") / str(record["pid"]) / "stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None
    return record["pid"] if fields[19] == str(record["start_ticks"]) and fields[0] != "Z" else None


if options.get("runner"):
    record["runner"] = start_runner(options["runner"])
    finish("deployed", record["current"])
    if record["runner"].get("status") != "running":
        sys.exit(3)
'''


def source_files(root=ROOT):
    """The files the board needs: common/*.py, common/*.json and ultra96/*.py."""
    files = []
    for folder, suffixes in (("common", (".py", ".json")), ("ultra96", (".py",))):
        for path in sorted((Path(root) / folder).iterdir()):
            if path.is_file() and path.suffix in suffixes:
                files.append((folder + "/" + path.name, path))
    if not any(name == "ultra96/server.py" for name, _ in files):
        raise FileNotFoundError("ultra96/server.py not found under " + str(root))
    return files


def manifest(files):
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files}


def remote_command(base, source, stamp, options):
    return " ".join(shlex.quote(part) for part in (
        "/usr/bin/python3", "-c", REMOTE_PROGRAM, base, source, stamp,
        json.dumps(options, sort_keys=True)))


def options_from(args):
    validate_session(args.session_id)
    if args.model_path and not args.model:
        raise ValueError("--model-path needs --model")
    if not 5 <= args.start_timeout <= 300:
        raise ValueError("--start-timeout must be 5..300 seconds")
    options = dict(session_id=args.session_id, event_log=not args.no_event_log, model=args.model,
                   model_path=args.model_path, start_timeout=args.start_timeout)
    runner = getattr(args, "runner", None)
    module = getattr(args, "runner_module", None)
    cwd = getattr(args, "runner_cwd", None)
    if runner is None and module is None:
        if cwd is not None or getattr(args, "runner_arg", []):
            raise ValueError("--runner-cwd and --runner-arg need --runner or --runner-module")
        return options
    if runner is not None and module is not None:
        raise ValueError("give --runner (a script) or --runner-module (python -m), not both")
    if args.model != BRIDGE_MODEL:
        raise ValueError(("--runner" if module is None else "--runner-module") + " needs --model " + BRIDGE_MODEL)
    if module is not None:
        if not MODULE_NAME.fullmatch(module):
            raise ValueError("--runner-module must be a dotted Python module name, such as src.socket_worker")
        if cwd is None:
            raise ValueError("--runner-module needs --runner-cwd, the board folder it runs from")
    paths = [("--runner", runner), ("--runner-cwd", cwd), ("--runner-python", args.runner_python)]
    for name, value in paths:
        if value is not None and not (value.startswith("/") and value.isprintable()):
            raise ValueError(name + " must be an absolute path on the board" + _git_bash_hint([value]))
    if not all(isinstance(a, str) and a.isprintable() for a in args.runner_arg) or _git_bash_hint(args.runner_arg):
        raise ValueError("--runner-arg must be printable board values" + _git_bash_hint(args.runner_arg))
    options["runner"] = (dict(script=runner) if module is None else dict(module=module))
    if cwd is not None:
        options["runner"]["cwd"] = cwd
    options["runner"].update(python=args.runner_python, args=list(args.runner_arg))
    return options


def runner_line(runner):
    """The runner's command as a shell line, for the plan the tool prints."""
    target = ["-m", runner["module"]] if runner.get("module") else [runner["script"]]
    line = " ".join(shlex.quote(part) for part in [runner["python"]] + target + runner["args"])
    cwd = runner.get("cwd")
    return ("cd " + shlex.quote(cwd) + " && " + line) if cwd else line


def certificate_files(folder):
    if folder is None:
        return None
    folder = Path(folder)
    pair = (folder / "server-cert.pem", folder / "server-key.pem")
    for path in pair:
        if not path.is_file():
            raise FileNotFoundError("missing " + str(path))
    return pair


def _connect(jump_user, board_user, want_sudo=False):
    """(jump, board, sudo password or None); every password is typed, none is saved."""
    import paramiko
    jump, board = paramiko.SSHClient(), paramiko.SSHClient()
    for client in (jump, board):
        client.load_host_keys(str(Path.home() / ".ssh" / "known_hosts"))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        jump.connect(JUMP_HOST, username=jump_user,
                     password=getpass.getpass(f"Password for {jump_user}@{JUMP_HOST}: "),
                     look_for_keys=False, allow_agent=False, timeout=10, auth_timeout=15)
        channel = jump.get_transport().open_channel(
            "direct-tcpip", (BOARD_HOST, 22), ("127.0.0.1", 0), timeout=10)
        board_password = getpass.getpass(f"Password for {board_user}@{BOARD_HOST}: ")
        board.connect(BOARD_HOST, username=board_user, password=board_password,
                      sock=channel, look_for_keys=False, allow_agent=False, timeout=10,
                      auth_timeout=15)
        sudo_password = None
        if want_sudo:
            sudo_password = getpass.getpass(f"sudo password for {board_user} on the board, for the "
                                            "model runner (Enter: the same as above): ") or board_password
        return jump, board, sudo_password
    except BaseException:
        board.close()
        jump.close()
        raise


def deploy(args):
    options = options_from(args)
    files = source_files()
    certificate = certificate_files(args.server_cert_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    source = BOARD_ROOT + "/source-co-v3-" + stamp
    print(f"Board folder: {source}")
    print(f"Files: {len(files)} from common/ and ultra96/; certificate: "
          f"{'install ' + str(certificate[0].parent) if certificate else 'keep the one on the board'}")
    print("Options: " + json.dumps(options, sort_keys=True))
    if options.get("runner"):
        print("Runner: after the service listens, as root (sudo, a login shell): "
              + runner_line(options["runner"]) + "; it replaces the runner this tool started last")
    if args.dry_run:
        print("Dry run: nothing was connected or changed.")
        return 0
    jump, board, sudo_password = _connect(args.jump_user, args.board_user, bool(options.get("runner")))
    sftp = None
    try:
        sftp = board.open_sftp()
        sftp.mkdir(source, mode=0o700)
        for folder in ("common", "ultra96"):
            sftp.mkdir(source + "/" + folder, mode=0o700)
        for name, path in files:
            sftp.put(str(path), source + "/" + name)
        with sftp.open(source + "/source-manifest.json", "w") as stream:
            stream.write(json.dumps(manifest(files), indent=2, sort_keys=True))
        if certificate:
            sftp.mkdir(source + "/tls-incoming", mode=0o700)
            for path in certificate:
                sftp.put(str(path), source + "/tls-incoming/" + path.name)
            sftp.chmod(source + "/tls-incoming/server-key.pem", 0o600)
        # The longest silent stretch on the board: stop (10 s, then up to 5 s after SIGKILL),
        # the new service's start, its stop (up to 10 s) and the rollback's start; with a
        # runner, up to 60 s more for sudo and the launcher.
        wait = 2 * options["start_timeout"] + 120 + (60 if options.get("runner") else 0)
        stdin, out, err = board.exec_command(remote_command(BOARD_ROOT, source, stamp, options),
                                             timeout=wait)
        if sudo_password is not None:
            stdin.write(sudo_password + "\n")
            stdin.flush()
        stdin.channel.shutdown_write()
        try:
            result = out.read().decode()
            errors = err.read().decode()
            code = out.channel.recv_exit_status()
        except socket.timeout:
            print(f"FAILED: no answer from the board within {wait:.0f} s. The board may still be "
                  "finishing or rolling back: run 'python demo.py service' and read "
                  "BOARD_ROOT/evidence/active-deployment.json before trying again.", file=sys.stderr)
            return 1
        print(result.strip())
        if errors:
            print(errors.strip(), file=sys.stderr)
        lines = [line for line in result.splitlines() if line.startswith("{")]
        if lines:
            RECORDS.mkdir(parents=True, exist_ok=True)
            path = RECORDS / f"deployment-{stamp}.json"
            path.write_text(json.dumps(json.loads(lines[-1]), indent=2), encoding="utf-8")
            print("Local record: " + str(path))
        runner = json.loads(lines[-1]).get("runner") if lines else None
        if code == 3 and runner is not None:
            print("FAILED: the service is deployed and running, but the new model runner is not: "
                  + json.dumps(runner), file=sys.stderr)
            if runner.get("previous_running"):
                print("The runner of the previous --runner deploy (PID {}) is still running and serves "
                      "the new service with its own code. Fix the cause and deploy again with --runner "
                      "or --runner-module, which replaces it; do not start a second runner by hand.".format(
                          runner["previous_running"]), file=sys.stderr)
            return 1
        if code:
            print(f"FAILED: the board reported exit {code}; see the record above.", file=sys.stderr)
            return 1
        if runner is not None:
            print("Model runner running (PID {}){}; log: {}".format(
                runner.get("pid"), ", connected to the service" if runner.get("connected")
                else ", not yet connected to the service", runner.get("log")))
        print("Deployed. Next: python demo.py service (inspect), then the acceptance run.")
        return 0
    finally:
        if sftp is not None:
            sftp.close()
        board.close()
        jump.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server-cert-dir", help="folder with a renewed server-cert.pem and server-key.pem")
    parser.add_argument("--session-id", default="ltc-comms")
    parser.add_argument("--model", help="module passed to ultra96.server --model (default: dummy model)")
    parser.add_argument("--model-path", help="board folder passed to ultra96.server --model-path")
    parser.add_argument("--runner", metavar="BOARD_SCRIPT",
                        help="with --model " + BRIDGE_MODEL + ": the model owner's runner script on the "
                             "board, started as root with sudo once the service listens")
    parser.add_argument("--runner-module", metavar="MODULE",
                        help="instead of --runner: run the runner as python -m MODULE from --runner-cwd "
                             "(for example src.socket_worker)")
    parser.add_argument("--runner-cwd", metavar="BOARD_FOLDER",
                        help="the board folder the runner runs in (needed with --runner-module; "
                             "relative --runner-arg paths are read from it)")
    parser.add_argument("--runner-python", default=PYNQ_PYTHON,
                        help="the Python that runs the runner (default: PYNQ's, " + PYNQ_PYTHON + ")")
    parser.add_argument("--runner-arg", action="append", default=[], metavar="ARG",
                        help="an argument for the runner (repeat; --runner-arg=--flag for a dash)")
    parser.add_argument("--no-event-log", action="store_true", help="start without --event-log-dir")
    parser.add_argument("--start-timeout", type=float, default=30.0,
                        help="seconds to wait for both listeners (model loading counts)")
    parser.add_argument("--jump-user", default="yanjie")
    parser.add_argument("--board-user", default="xilinx")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; connect nothing")
    args = parser.parse_args(argv)
    try:
        return deploy(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # paramiko's own errors: unknown host key, wrong password, ...
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        allowed = getattr(exc, "allowed_types", None)
        if isinstance(allowed, list) and "password" not in allowed:
            # Seen 2026-10-16: off the NUS VPN, stujump offers only public-key login.
            print("The server does not offer password login. Off the NUS VPN, stujump accepts "
                  "only public keys: connect the NUS VPN and run this again.", file=sys.stderr)
        return 2


def _git_bash_hint(values):
    """A hint when a board value is a Windows path: Git Bash turns /home/... into D:/Git/home/..."""
    for value in values:
        for part in [value] + value.split("=")[1:]:
            if len(part) > 2 and part[0].isascii() and part[0].isalpha() and part[1:3] in (":/", ":\\"):
                return (f"; {value!r} is a Windows path. Git Bash rewrites arguments that start "
                        "with /: run this from PowerShell, or set MSYS_NO_PATHCONV=1")
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
