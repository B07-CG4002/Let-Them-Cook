"""Copy the model owner's runner folder on the Ultra96 out of /tmp, into a folder that survives a restart.

Wen Ree's FPGA runner lives in /tmp/cnn_conv1d on the board (ai_integration/src/socket_worker.py,
its weights, the bitstream and its .hwh), and the board empties /tmp when it restarts. This tool
copies that folder unchanged (without __pycache__) to BOARD_ROOT/model-runner/<name>, checks
the SHA-256 of every file against the original, and prints the deploy command that starts the
runner from the copy. It changes nothing else: the originals, the running service and the
running runner are untouched, and it needs no sudo. The owner types the jump-host and board
passwords; nothing is saved. Run it again and it reports that the copy is already there; a copy
whose files differ from the source is never overwritten (give another --name).

    python -m tools.stage_model_runner --dry-run
    python -m tools.stage_model_runner
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import sys

from tools.demo_service import BOARD_ROOT
from tools.deploy_service import RECORDS, _connect, _git_bash_hint

SOURCE = "/tmp/cnn_conv1d"
NAME = "cnn_conv1d"
RUNNER_FOLDER = "ai_integration"
RUNNER_MODULE = "src.socket_worker"
WEIGHTS = "fpga/hls/eightclass_weights_20261018_v2.npz"
BITSTREAM = "cnn_conv1d_fixed28_v2.bit"
# Relative to the copied folder: what the deploy command below reads.
EXPECTED = (RUNNER_FOLDER + "/src/socket_worker.py", RUNNER_FOLDER + "/" + WEIGHTS,
            BITSTREAM, BITSTREAM[:-len(".bit")] + ".hwh")

REMOTE_PROGRAM = r'''
import hashlib, json, os, shutil, stat, sys
from pathlib import Path

source = Path(sys.argv[1]); dest = Path(sys.argv[2]); stamp = sys.argv[3]
expected = json.loads(sys.argv[4]); evidence = Path(sys.argv[5])
SKIP = "__pycache__"


def listing(root):
    """{relative path: (size, sha256)} of every regular file, without __pycache__."""
    files = {}
    for folder, dirs, names in os.walk(str(root)):
        dirs[:] = sorted(d for d in dirs if d != SKIP)
        for d in dirs:
            if os.path.islink(os.path.join(folder, d)):
                raise ValueError("symbolic link to a folder: " + os.path.join(folder, d))
        for name in sorted(names):
            path = os.path.join(folder, name)
            mode = os.lstat(path).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError("not a regular file: " + path)
            digest = hashlib.sha256()
            with open(path, "rb") as stream:
                for block in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(block)
            name = os.path.relpath(path, str(root)).replace(os.sep, "/")
            files[name] = [os.path.getsize(path), digest.hexdigest()]
    return files


def finish(status, code, **extra):
    record = dict(status=status, stamp=stamp, source=str(source), dest=str(dest), **extra)
    try:
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / ("model-runner-stage-" + stamp + ".json")).write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        record["record_error"] = str(exc)
    print(json.dumps(record, sort_keys=True), flush=True)
    sys.exit(code)


try:
    if source.is_symlink() or not source.is_dir():
        finish("failed", 1, error="source folder not found (was the board restarted?): " + str(source))
    files = listing(source)
    missing = [name for name in expected if name not in files]
    if missing:
        finish("failed", 1, error="missing in the source: " + ", ".join(missing), files=files)
    if dest.exists() or dest.is_symlink():
        if dest.is_dir() and not dest.is_symlink() and listing(dest) == files:
            finish("already_staged", 0, files=files)
        finish("failed", 1, error="the destination exists with other files; it was not changed. "
                                  "Use another --name", files=files)
    dest.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    partial = dest.parent / (dest.name + ".partial-" + stamp)
    try:
        shutil.copytree(str(source), str(partial), symlinks=True,
                        ignore=shutil.ignore_patterns(SKIP))
        copied = listing(partial)
        if copied != files:
            raise ValueError("the copy does not match the source")
        os.rename(str(partial), str(dest))
    except BaseException:
        shutil.rmtree(str(partial), ignore_errors=True)
        raise
    finish("staged", 0, files=files)
except SystemExit:
    raise
except Exception as exc:
    finish("failed", 1, error=type(exc).__name__ + ": " + str(exc))
'''


def destination(name):
    if not name or name in (".", "..") or not all(c.isalnum() or c in "._-" for c in name):
        raise ValueError("--name must be a plain folder name (letters, digits, '.', '_', '-')")
    return BOARD_ROOT + "/model-runner/" + name


def deploy_command(dest):
    """The PowerShell line that starts the runner from the copy (as Wen Ree starts it)."""
    cwd = dest + "/" + RUNNER_FOLDER
    parts = ["python", "-m", "tools.deploy_service", "--model", "ultra96.model_bridge",
             "--runner-module", RUNNER_MODULE, "--runner-cwd", cwd,
             "--runner-arg=--weights", "--runner-arg", WEIGHTS,
             "--runner-arg=--bit", "--runner-arg", dest + "/" + BITSTREAM]
    return " ".join(parts)


def remote_command(source, dest, stamp):
    return " ".join(shlex.quote(part) for part in (
        "/usr/bin/python3", "-I", "-c", REMOTE_PROGRAM, source, dest, stamp,
        json.dumps(list(EXPECTED)), BOARD_ROOT + "/evidence"))


def stage(args):
    if not (args.source.startswith("/") and args.source.isprintable()):
        raise ValueError("--source must be an absolute path on the board" + _git_bash_hint([args.source]))
    dest = destination(args.name)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print("Copy on the board: {} -> {} (without __pycache__; the original stays)".format(args.source, dest))
    print("Required files: " + ", ".join(EXPECTED))
    print("Then deploy from the copy:\n  " + deploy_command(dest))
    if args.dry_run:
        print("Dry run: nothing was connected or changed.")
        return 0
    jump, board, _ = _connect(args.jump_user, args.board_user)
    try:
        _, out, err = board.exec_command(remote_command(args.source, dest, stamp), timeout=300)
        result = out.read().decode()
        errors = err.read().decode()
        code = out.channel.recv_exit_status()
    finally:
        board.close()
        jump.close()
    lines = [line for line in result.splitlines() if line.startswith("{")]
    if errors:
        print(errors.strip(), file=sys.stderr)
    if not lines:
        print("FAILED: no record from the board (exit {}): {}".format(code, result.strip()),
              file=sys.stderr)
        return 1
    record = json.loads(lines[-1])
    RECORDS.mkdir(parents=True, exist_ok=True)
    path = RECORDS / "model-runner-stage-{}.json".format(stamp)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    files = record.get("files") or {}
    for name in sorted(files):
        print("  {:>10}  {}  {}".format(files[name][0], files[name][1][:16], name))
    print("Local record: " + str(path))
    if code or record.get("status") not in ("staged", "already_staged"):
        print("FAILED: " + str(record.get("error", record.get("status"))), file=sys.stderr)
        return 1
    print(("Copied" if record["status"] == "staged" else "Already copied") +
          ": {} files in {}, each SHA-256 equal to the original.".format(len(files), dest))
    print("Deploy from the copy (PowerShell, NUS VPN on):\n  " + deploy_command(dest))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default=SOURCE, help="the runner folder on the board (default %(default)s)")
    parser.add_argument("--name", default=NAME,
                        help="the copy's folder name under BOARD_ROOT/model-runner (default %(default)s)")
    parser.add_argument("--jump-user", default="yanjie")
    parser.add_argument("--board-user", default="xilinx")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; connect nothing")
    args = parser.parse_args(argv)
    try:
        return stage(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print("FAILED: {}".format(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # paramiko's own errors: unknown host key, wrong password, ...
        print("FAILED: {}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        allowed = getattr(exc, "allowed_types", None)
        if isinstance(allowed, list) and "password" not in allowed:
            print("The server does not offer password login. Off the NUS VPN, stujump accepts "
                  "only public keys: connect the NUS VPN and run this again.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
