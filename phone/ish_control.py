#!/usr/bin/env python3
"""Start a temporary, key-only iSH management sshd through an existing master.

Run: python3 ish_control.py "$comms_ssh_socket"
The sibling control-public.json contains only the supplied client public key,
board status directory, and run id. PHONE_CONTROL_READY reports local startup
and a reverse-forward request; it does not prove remote root authentication.
"""
import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time


SSHD = "/usr/sbin/sshd"
FORWARD = "127.0.0.1:22222:127.0.0.1:2222"


class ControlError(Exception):
    """A public diagnostic that never includes secret command output."""


def validate_metadata(data):
    if not isinstance(data, dict) or set(data) != {"id", "remote_dir", "client_public_key"}:
        raise ControlError("PHONE_CONTROL_METADATA_INVALID: expected id, remote_dir, client_public_key")
    identifier, remote, key = (data[name] for name in ("id", "remote_dir", "client_public_key"))
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier):
        raise ControlError("PHONE_CONTROL_METADATA_INVALID: unsafe id")
    if (not isinstance(remote, str) or len(remote) > 512 or
            not re.fullmatch(r"/(?:[A-Za-z0-9_-][A-Za-z0-9_.-]*/)*[A-Za-z0-9_-][A-Za-z0-9_.-]*", remote)):
        raise ControlError("PHONE_CONTROL_METADATA_INVALID: unsafe absolute remote_dir")
    if not isinstance(key, str) or not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}(?: [A-Za-z0-9_.@:-]{1,128})?", key):
        raise ControlError("PHONE_CONTROL_METADATA_INVALID: expected one Ed25519 public key")
    try:
        blob = base64.b64decode(key.split()[1], validate=True)
    except (ValueError, binascii.Error):
        blob = b""
    prefix = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)
    if not blob.startswith(prefix) or len(blob) != len(prefix) + 32:
        raise ControlError("PHONE_CONTROL_METADATA_INVALID: malformed Ed25519 public key")
    return data


def require_root():
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise ControlError("PHONE_CONTROL_ROOT_REQUIRED: run in the iSH root shell")


def check_root_account(lookup=None):
    try:
        if lookup is None:
            import spwd
            lookup = spwd.getspnam
        password = lookup("root").sp_pwdp
    except (ImportError, KeyError, OSError):
        raise ControlError("PHONE_CONTROL_ROOT_STATUS_UNKNOWN: could not inspect root account lock status") from None
    # Linux OpenSSH uses '!' for account locking. iSH may use '*' to disable
    # password login while leaving public-key authentication available.
    if not isinstance(password, str) or password.startswith("!"):
        raise ControlError("PHONE_CONTROL_ROOT_LOCKED: root is locked; account was left unchanged")


def check_port_free(port=2222):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("127.0.0.1", port))
    except OSError:
        raise ControlError("PHONE_CONTROL_PORT_BUSY: loopback port {} is unavailable".format(port)) from None


def ensure_runtime_directory(directory=Path("/run/sshd")):
    try:
        info = os.lstat(str(directory))
    except FileNotFoundError:
        require_root()
        directory.mkdir(mode=0o755)
        info = os.lstat(str(directory))
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ControlError("PHONE_CONTROL_RUNTIME_UNSAFE: existing sshd runtime directory is unsafe")


def mux_command(socket_path, operation):
    args = ["ssh", "-F", "/dev/null", "-S", socket_path, "-o", "BatchMode=yes", "-O", operation]
    if operation in ("forward", "cancel"):
        args += ["-R", FORWARD]
    return args + ["127.0.0.1"]


def run_command(args, marker, **kwargs):
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=20, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        raise ControlError(marker) from None
    if result.returncode:
        raise ControlError(marker)
    return result.stdout


def private_file(path, content):
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def config_text(directory):
    def quoted(name):
        return '"' + str(directory / name).replace("\\", "\\\\").replace('"', '\\"') + '"'
    entries = [
        ("AddressFamily", "inet"), ("ListenAddress", "127.0.0.1"), ("Port", "2222"),
        ("HostKey", quoted("host_ed25519")), ("PidFile", quoted("sshd.pid")),
        ("AuthorizedKeysFile", quoted("authorized_keys")), ("StrictModes", "yes"),
        ("AllowUsers", "root"), ("PermitRootLogin", "prohibit-password"),
        ("PubkeyAuthentication", "yes"), ("AuthenticationMethods", "publickey"),
        ("UsePAM", "no"), ("PasswordAuthentication", "no"),
        # OpenSSH <8.7 has a separate challenge-response default that can
        # re-enable keyboard-interactive authentication unless both are off.
        ("ChallengeResponseAuthentication", "no"),
        ("KbdInteractiveAuthentication", "no"), ("PermitEmptyPasswords", "no"),
        ("DisableForwarding", "yes"), ("PermitTTY", "no"), ("X11Forwarding", "no"),
        ("PermitUserEnvironment", "no"), ("PermitUserRC", "no"), ("LogLevel", "VERBOSE"),
    ]
    return "".join("{} {}\n".format(key, value) for key, value in entries)


def verify_scoped_process(pid, directory, proc_root=Path("/proc")):
    """Check a saved PID using iSH-supported executable, fd and session data.

    iSH truncates rewritten process titles and reports zero start ticks, so
    neither cmdline nor start time can identify a later cleanup target.
    The fresh scope's log path is retained and must never be reused.
    """
    try:
        folder = proc_root / str(pid)
        executable = os.path.realpath(os.readlink(str(folder / "exe")))
        log = os.path.realpath(os.readlink(str(folder / "fd" / "2")))
        if executable != os.path.realpath(SSHD) or log != os.path.realpath(str(directory / "sshd.log")):
            raise ValueError("different executable or log")
        fields = (folder / "stat").read_text().rsplit(")", 1)[1].split()
        if int(fields[2]) != pid or int(fields[3]) != pid:
            raise ValueError("not the scoped session leader")
    except (OSError, UnicodeError, ValueError, IndexError):
        raise ControlError("PHONE_CONTROL_PROCESS_UNVERIFIED: scoped sshd identity could not be verified") from None


def wait_for_listener(process):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ControlError("PHONE_CONTROL_SSHD_EXITED: inspect the scoped sshd.log")
        try:
            with socket.create_connection(("127.0.0.1", 2222), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise ControlError("PHONE_CONTROL_SSHD_NOT_READY: inspect the scoped sshd.log")


def check_sshd_config(config):
    runtime = Path("/run/sshd")
    if runtime.exists() or runtime.is_symlink():
        ensure_runtime_directory(runtime)
    args = [SSHD, "-t", "-f", str(config)]
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
        private_file(config.parent / "sshd-check.log", result.stderr)
        if result.returncode and "Missing privilege separation directory: /run/sshd" in result.stderr:
            ensure_runtime_directory(runtime)
            run_command(args, "PHONE_CONTROL_CONFIG_INVALID: inspect the scoped sshd_config")
        elif result.returncode:
            raise ControlError("PHONE_CONTROL_CONFIG_INVALID: sshd rejected the scoped configuration")
    except (OSError, subprocess.TimeoutExpired):
        raise ControlError("PHONE_CONTROL_CONFIG_INVALID: sshd configuration check could not run") from None


def master_exec_args(remote_code, remote_dir, socket_path, home):
    command = " ".join(shlex.quote(arg) for arg in ["python3", "-c", remote_code, remote_dir])
    return ["ssh", "-F", str(home / ".ssh" / "week7-ish-password.conf"), "-S", socket_path,
            "-o", "BatchMode=yes", "-o", "ControlMaster=no", "-o", "ClearAllForwardings=yes",
            "-o", "ProxyCommand=false", "week7-ultra96", command]


def check_remote_available(metadata, socket_path, home):
    remote_code = (
        "import os,socket,stat,sys; "
        "d=sys.argv[1]; s=os.lstat(d); "
        "assert stat.S_ISDIR(s.st_mode) and s.st_uid==os.geteuid() and not s.st_mode&0o077; "
        "assert not os.path.lexists(os.path.join(d,'phone-ready.json')); "
        "p=socket.socket(socket.AF_INET,socket.SOCK_STREAM); p.bind(('127.0.0.1',22222)); p.close()"
    )
    run_command(master_exec_args(remote_code, metadata["remote_dir"], socket_path, home),
                "PHONE_CONTROL_REMOTE_UNAVAILABLE: board scope, unused status path, and free port 22222 are required")


def publish_status(status, metadata, socket_path, home):
    # argv is shell-quoted because ssh passes its remote command through a shell.
    # The already-prepared board directory must exist and the status must be new.
    remote_code = (
        "import json,os,stat,sys; "
        "d=sys.argv[1]; s=os.lstat(d); "
        "assert stat.S_ISDIR(s.st_mode) and s.st_uid==os.geteuid() and not s.st_mode&0o077; "
        "payload=sys.stdin.read(); json.loads(payload); p=os.path.join(d,'phone-ready.json'); "
        "fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); "
        "f=os.fdopen(fd,'w'); f.write(payload); f.close()"
    )
    args = master_exec_args(remote_code, metadata["remote_dir"], socket_path, home)
    run_command(args, "PHONE_CONTROL_PUBLISH_FAILED: status publication failed", input=json.dumps(status) + "\n")


def start_control(metadata, socket_path, home=None):
    metadata = validate_metadata(metadata)
    home = Path.home() if home is None else Path(home)
    if not isinstance(socket_path, str) or not socket_path.startswith("/") or re.search(r"[\x00-\x1f\x7f]", socket_path):
        raise ControlError("PHONE_CONTROL_SOCKET_INVALID: supply the existing absolute master socket path")
    require_root()
    check_root_account()
    run_command(mux_command(socket_path, "check"), "PHONE_CONTROL_MASTER_UNAVAILABLE: existing SSH master is required")
    check_port_free()
    check_remote_available(metadata, socket_path, home)
    root = home / "week7-control"
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022 or (hasattr(os, "geteuid") and info.st_uid != os.geteuid()):
        raise ControlError("PHONE_CONTROL_SCOPE_UNSAFE: week7-control directory is unsafe")
    directory = Path(tempfile.mkdtemp(prefix=metadata["id"] + "-", dir=str(root)))
    process = None
    forwarded = False
    forward_attempted = False
    config = directory / "sshd_config"
    try:
        private_file(directory / "authorized_keys", metadata["client_public_key"] + "\n")
        private_file(config, config_text(directory))
        host_key = directory / "host_ed25519"
        run_command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", metadata["id"], "-f", str(host_key)],
                    "PHONE_CONTROL_HOSTKEY_FAILED: dedicated host key generation failed")
        public_key = Path(str(host_key) + ".pub").read_text().strip()
        validate_metadata(dict(metadata, client_public_key=public_key))
        fingerprint = run_command(["ssh-keygen", "-l", "-E", "sha256", "-f", str(host_key) + ".pub"],
                                  "PHONE_CONTROL_HOSTKEY_FAILED: public fingerprint unavailable").split()[1]
        check_sshd_config(config)
        log_fd = os.open(str(directory / "sshd.log"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(log_fd, "wb") as log:
            process = subprocess.Popen([SSHD, "-D", "-e", "-f", str(config)], stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, start_new_session=True)
        wait_for_listener(process)
        forward_attempted = True
        run_command(mux_command(socket_path, "forward"), "PHONE_CONTROL_FORWARD_FAILED: reverse forward was not confirmed")
        forwarded = True
        status = {"id": metadata["id"], "state": "sshd_started_reverse_requested", "directory": str(directory),
                  "pid": process.pid, "host_public_key": public_key,
                  "host_fingerprint": fingerprint, "root_authentication_verified": False,
                  "local_address": "127.0.0.1:2222", "remote_address": "127.0.0.1:22222"}
        private_file(directory / "state.json", json.dumps(dict(status, socket_path=socket_path), indent=2) + "\n")
        publish_status(status, metadata, socket_path, home)
        return status
    except BaseException as error:
        cleanup = {"reverse_cancel": "outcome_unknown_board_observation_required" if forward_attempted else "not_attempted",
                   "sshd_stop": "not_started", "directory": str(directory)}
        if forwarded:
            try:
                run_command(mux_command(socket_path, "cancel"), "cancel failed")
                cleanup["reverse_cancel"] = "requested_board_observation_pending"
            except ControlError:
                cleanup["reverse_cancel"] = "request_failed_board_observation_required"
        if process is not None and process.poll() is None:
            try:
                # This is our still-unreaped Popen child; its PID cannot be
                # reused until it exits and is reaped. iSH /proc is not needed.
                process.terminate()
                process.wait(timeout=5)
                cleanup["sshd_stop"] = "terminated_owned_child"
            except (ControlError, OSError, subprocess.TimeoutExpired):
                cleanup["sshd_stop"] = "unverified_or_still_running_manual_review_required"
        elif process is not None:
            cleanup["sshd_stop"] = "already_exited"
        private_file(directory / "cleanup.json", json.dumps(cleanup, indent=2) + "\n")
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        message = str(error) if isinstance(error, ControlError) else "PHONE_CONTROL_START_FAILED: inspect the scoped files"
        raise ControlError(message + "; cleanup report: " + str(directory / "cleanup.json")) from None


def stop_control(state_path, home=None):
    """Request cleanup only after verifying the saved scope and live PID."""
    require_root()
    home = Path.home() if home is None else Path(home)
    root = home.absolute() / "week7-control"
    state_path = Path(state_path).absolute()
    directory = state_path.parent
    if state_path.name != "state.json" or directory.parent != root:
        raise ControlError("PHONE_CONTROL_STOP_SCOPE_INVALID: expected a scoped week7-control state.json")
    for path, is_directory in [(root, True), (directory, True), (state_path, False)]:
        info = path.lstat()
        expected_type = stat.S_ISDIR if is_directory else stat.S_ISREG
        if not expected_type(info.st_mode) or info.st_mode & 0o022 or (hasattr(os, "geteuid") and info.st_uid != os.geteuid()):
            raise ControlError("PHONE_CONTROL_STOP_SCOPE_INVALID: unsafe saved state")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ControlError("PHONE_CONTROL_STOP_SCOPE_INVALID: saved state must be an object")
    pid = state.get("pid")
    socket_path = state.get("socket_path")
    if (state.get("directory") != str(directory) or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or
            not isinstance(socket_path, str) or not socket_path.startswith("/") or re.search(r"[\x00-\x1f\x7f]", socket_path)):
        raise ControlError("PHONE_CONTROL_STOP_SCOPE_INVALID: inconsistent saved state")
    verify_scoped_process(pid, directory)
    try:
        run_command(mux_command(socket_path, "cancel"), "cancel failed")
        reverse = "requested_board_observation_pending"
    except ControlError:
        reverse = "request_failed_board_observation_required"
    verify_scoped_process(pid, directory)
    os.kill(pid, signal.SIGTERM)
    return {"directory": str(directory), "sshd_stop": "signal_requested", "reverse_cancel": reverse}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket", nargs="?", help="existing SSH ControlPath")
    parser.add_argument("--stop", metavar="STATE_JSON", help="request cleanup of a verified scoped sshd")
    args = parser.parse_args(argv)
    if bool(args.socket) == bool(args.stop):
        parser.error("supply the master socket or --stop STATE_JSON")
    try:
        if args.stop:
            status = stop_control(Path(args.stop))
        else:
            metadata_path = Path(__file__).resolve().with_name("control-public.json")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            status = start_control(metadata, args.socket)
    except (ControlError, OSError, ValueError) as error:
        print(str(error) if isinstance(error, ControlError) else "PHONE_CONTROL_INPUT_INVALID: cannot read public metadata", file=sys.stderr)
        return 1
    print(("PHONE_CONTROL_STOP_REQUESTED " if args.stop else "PHONE_CONTROL_READY ") + json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
