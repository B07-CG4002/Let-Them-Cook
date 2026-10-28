"""Put the model owner's runner files in a folder on the Ultra96 that survives a restart.

Three runners are known (--runner):
  conv1d (default)  Wen Ree's Conv1-only runner, src.socket_worker. Its folder /tmp/cnn_conv1d
                    on the board (ai_integration/src/socket_worker.py, its weights, the bitstream
                    and its .hwh) is copied unchanged (without __pycache__), and the board empties
                    /tmp when it restarts.
  full              Wen Ree's full-CNN runner, src.socket_worker_full. The files it needs (four
                    src/ files, the weights, the overlay .bit and its .hwh) are taken from a commit
                    of this checkout (--from-git, default upstream/main: run `git fetch upstream`
                    first), uploaded and moved into place, in the repository's own layout. With
                    --source it copies a board folder in that layout instead.
  hand12            Wen Ree's independent-hand worker src.socket_worker_hand (sensor protocol v4,
                    one model per hand) on the cnn_hand12_v1 overlay, with the compat delivery
                    Communications added to it (edited with his agreement): it sends results back
                    to the model bridge. Its files (the worker, his src/hand_*.py runtime, the
                    common/ modules they import, the overlay .bit, .hwh and its manifest) are
                    uploaded from a commit like the full runner, default HEAD: the team repo still
                    has his observe-only worker, which is refused. The overlay must be Wen Ree's
                    board-verified pair (its SHA-256 is checked before anything is uploaded). The
                    copy gets an empty logs/ folder, where the worker writes a new JSONL record
                    file at each start (--out).
The copy goes to BOARD_ROOT/model-runner/<name>; the SHA-256 of every file is checked against the
original (and, for an upload, against the commit), and the tool prints the deploy command that
starts the runner from the copy. It changes nothing else: the originals, the running service and
the running runner are untouched, and it needs no sudo. The user types the jump-host and board
passwords; nothing is saved. Run it again and it reports that the copy is already there; a copy
whose files differ is never overwritten (give another --name).

--weights picks the weights the printed command uses: a path relative to the runner folder is
copied with the runner; an absolute board path (weights staged on their own) is only printed.

--bundle (hand12 only, in place of --weights) picks the model bundle the hand worker reads. A local
folder is first checked as the runner checks it (src.hand_runtime.ModelBundle) and uploaded into
<copy>/bundle, every file's SHA-256 checked on the board; an absolute board path is only printed.
Trained LEFT/RIGHT bundles come from Wen Ree. A synthetic test bundle (python -m
tools.hand_hls_prepare --synthetic --out <new folder>) is not a trained model and needs
--allow-synthetic. --replace-runner PID:START_TICKS is passed on to the deploy command, which then
replaces that runner started by hand (`python demo.py service` lists them with their start ticks).

    python -m tools.stage_model_runner --dry-run
    python -m tools.stage_model_runner
    python -m tools.stage_model_runner --runner full --dry-run
    python -m tools.stage_model_runner --runner full --weights fpga/hls/eightclass_weights_standard_20261021.npz --name cnn_full_std
    python -m tools.stage_model_runner --runner hand12 --bundle <bundle folder> --dry-run
    python -m tools.stage_model_runner --runner hand12 --bundle <bundle folder> --replace-runner 177087:611890431
"""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import posixpath
import shlex
import subprocess
import sys

from tools.demo_service import BOARD_ROOT
from tools.deploy_service import RECORDS, _connect, _git_bash_hint, replace_entries
from tools.ssh_tunnel import default_jump_user

ROOT = Path(__file__).resolve().parents[1]
# The hand worker's --out in its copy: a folder, so each start writes a new record file there.
LOGS = "logs"


class Runner:
    """Where a runner's files sit in its copy and how the deploy tool starts it.

    A runner without weights (hand12) reads a model bundle instead (--bundle). overlay_sha256
    pins the overlay files a runner may be uploaded with; extra files are copied beside them.
    markers names text a code file must contain (the version the runner needs); folders are
    empty folders made in the copy for the runner to write into."""

    def __init__(self, source, name, folder, module, weights, bitstream, code, git_ref=None,
                 extra=(), overlay_sha256=None, markers=None, folders=()):
        self.source, self.name, self.folder, self.module = source, name, folder, module
        self.weights, self.bitstream, self.code, self.git_ref = weights, bitstream, code, git_ref
        self.extra, self.overlay_sha256 = tuple(extra), dict(overlay_sha256 or {})
        self.markers, self.folders = dict(markers or {}), tuple(folders)

    def path(self, relative):
        """A path inside the runner folder, relative to the copy."""
        return posixpath.join(self.folder, relative) if self.folder else relative

    def expected(self, weights=None):
        """Relative to the copied folder: what the deploy command reads."""
        weights = self.weights if weights is None else weights
        files = [self.path(name) for name in self.code]
        if weights is not None and not weights.startswith("/"):
            files.append(self.path(weights))
        return tuple(files + [self.bitstream, self.bitstream[:-len(".bit")] + ".hwh"] +
                     list(self.extra))


RUNNERS = {
    "conv1d": Runner("/tmp/cnn_conv1d", "cnn_conv1d", "ai_integration", "src.socket_worker",
                     "fpga/hls/eightclass_weights_20261018_v2.npz", "cnn_conv1d_fixed28_v2.bit",
                     ("src/socket_worker.py",)),
    # Wen Ree's README_FULL_CNN.md lists these; it runs from a folder laid out as the repository.
    "full": Runner(None, "cnn_full_20261020", "", "src.socket_worker_full",
                   "fpga/hls/eightclass_weights_20261018_v2.npz",
                   "fpga/overlays/full_cnn/cnn_full_20261020.bit",
                   ("src/ai_common.py", "src/export_full_cnn.py", "src/full_cnn_backend.py",
                    "src/socket_worker_full.py"), git_ref="upstream/main"),
    # Wen Ree's worker with the compat delivery (only in this fork so far, so HEAD) and everything
    # it imports: his independent-hand runtime and the common/ modules (common/__init__.py imports
    # common.sensor, which reads dummy_fixtures.json). The model bundle is not here: see --bundle.
    "hand12": Runner(None, "hand12_v1", "", "src.socket_worker_hand", None,
                     "fpga/overlays/independent_hand_v1/cnn_hand12_v1.bit",
                     ("src/socket_worker_hand.py", "src/hand_runtime.py", "src/hand_backend.py",
                      "src/hand_events.py", "src/hand_ai_flex3/__init__.py", "src/hand_ai_flex3/core.py",
                      "common/__init__.py", "common/sensor.py", "common/dummy_fixtures.json",
                      "common/hand_contract.py", "common/wire.py"),
                     git_ref="HEAD",
                     extra=("fpga/overlays/independent_hand_v1/artifact_manifest.json",),
                     markers={"src/socket_worker_hand.py": "compat_v3_gesture_result"},
                     folders=(LOGS,),
                     # Wen Ree's board-verified pair.
                     overlay_sha256={
                         "fpga/overlays/independent_hand_v1/cnn_hand12_v1.bit":
                             "b8d0407300c913fbde24143b6a632f62c0bf4021290d80fad575d32f59d6523d",
                         "fpga/overlays/independent_hand_v1/cnn_hand12_v1.hwh":
                             "935975fe769d0fda1a802c43c9a65430406a61dc02802f637df40f935660a790"}),
}

# Where an uploaded bundle sits in the hand12 copy, relative to the runner's folder.
BUNDLE = "bundle"
NO_BUNDLE = ("the hand12 runner needs --bundle: a folder with the LEFT and RIGHT models (a local "
             "folder, or an absolute board path). Trained bundles come from Wen Ree; a synthetic "
             "test bundle, which is not a trained model, can be made with `python -m "
             "tools.hand_hls_prepare --synthetic --out <new folder>` and needs --allow-synthetic")
HAND_STARTED = ("Note: the paired runner Wen Ree restored by hand (PID 177087, start_ticks "
                "611890431 on 2026-10-27) was not started by the deploy tool and must be replaced "
                "this way (--replace-runner 177087:611890431 while it still runs); "
                "`python demo.py service` lists hand-started runners with their start ticks.")

# The Conv1-only runner's values, kept under their earlier names.
_CONV1D = RUNNERS["conv1d"]
SOURCE = _CONV1D.source
NAME = _CONV1D.name
RUNNER_FOLDER = _CONV1D.folder
RUNNER_MODULE = _CONV1D.module
WEIGHTS = _CONV1D.weights
BITSTREAM = _CONV1D.bitstream
EXPECTED = _CONV1D.expected()

REMOTE_PROGRAM = r'''
import hashlib, json, os, shutil, stat, sys
from pathlib import Path

source = Path(sys.argv[1]); dest = Path(sys.argv[2]); stamp = sys.argv[3]
expected = json.loads(sys.argv[4]); evidence = Path(sys.argv[5])
# "copy" leaves the source as it is; "move" renames an uploaded folder into place and
# removes it in every other case. A dict of expected files must match exactly (size, SHA-256).
move = len(sys.argv) > 6 and sys.argv[6] == "move"
# Empty folders made in the copy for the runner to write into, once its files are in place.
folders = json.loads(sys.argv[7]) if len(sys.argv) > 7 else []
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


def make_folders():
    for name in folders:
        (dest / name).mkdir(mode=0o755, exist_ok=True)


def finish(status, code, **extra):
    if move and source.is_dir() and not source.is_symlink():
        shutil.rmtree(str(source), ignore_errors=True)
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
    if isinstance(expected, dict) and files != expected:
        wrong = sorted(n for n in set(files) | set(expected) if files.get(n) != expected.get(n))
        finish("failed", 1, error="the uploaded files differ from the originals: " + ", ".join(wrong),
               files=files)
    if dest.exists() or dest.is_symlink():
        # Already there: every file present with the same content (other files may sit beside them).
        if dest.is_dir() and not dest.is_symlink():
            present = listing(dest)
            if all(present.get(name) == entry for name, entry in files.items()):
                make_folders()
                finish("already_staged", 0, files=files)
        finish("failed", 1, error="the destination exists with other files; it was not changed. "
                                  "Use another --name", files=files)
    dest.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if move:
        os.rename(str(source), str(dest))
        if listing(dest) != files:
            finish("failed", 1, error="the moved folder does not match the upload: " + str(dest))
        make_folders()
        finish("staged", 0, files=files)
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
    make_folders()
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


def deploy_command(dest, runner="conv1d", weights=None, bundle=None, allow_synthetic=False,
                   replace=()):
    """The PowerShell line that starts the runner from the copy (as Wen Ree starts it)."""
    runner = RUNNERS[runner]
    cwd = dest + ("/" + runner.folder if runner.folder else "")
    if runner.weights is None:
        # The hand worker reads sensor protocol v4 and a model bundle, on the FPGA, from the
        # service's model socket (ultra96.model_bridge: model-runner.sock in BOARD_ROOT), and
        # writes a new record file in the copy's logs folder at each start.
        parts = ["python", "-m", "tools.deploy_service", "--sensor-version", "4",
                 "--model", "ultra96.model_bridge", "--runner-module", runner.module,
                 "--runner-cwd", cwd,
                 "--runner-arg=--bundle", "--runner-arg", BUNDLE if bundle is None else bundle,
                 "--runner-arg=--backend", "--runner-arg", "fpga",
                 "--runner-arg=--bit", "--runner-arg", dest + "/" + runner.bitstream,
                 "--runner-arg=--socket", "--runner-arg", BOARD_ROOT + "/model-runner.sock",
                 "--runner-arg=--out", "--runner-arg", dest + "/" + LOGS]
        if allow_synthetic:
            parts.append("--runner-arg=--allow-synthetic")
        for process in replace:
            parts += ["--replace-runner", process]
        return " ".join(parts)
    parts = ["python", "-m", "tools.deploy_service", "--model", "ultra96.model_bridge",
             "--runner-module", runner.module, "--runner-cwd", cwd,
             "--runner-arg=--weights", "--runner-arg", runner.weights if weights is None else weights,
             "--runner-arg=--bit", "--runner-arg", dest + "/" + runner.bitstream]
    return " ".join(parts)


def remote_command(source, dest, stamp, expected=EXPECTED, move=False, folders=()):
    parts = ["/usr/bin/python3", "-I", "-c", REMOTE_PROGRAM, source, dest, stamp,
             json.dumps(expected if isinstance(expected, dict) else list(expected)),
             BOARD_ROOT + "/evidence"]
    if move or folders:
        parts.append("move" if move else "copy")
    if folders:
        parts.append(json.dumps(list(folders)))
    return " ".join(shlex.quote(part) for part in parts)


def git_files(ref, names, root=ROOT):
    """{relative path: bytes} of each file at a commit of this checkout."""
    files = {}
    for name in names:
        done = subprocess.run(["git", "-C", str(root), "cat-file", "blob", ref + ":" + name],
                              capture_output=True)
        if done.returncode:
            if ref == "HEAD":
                hint = "commit it first, or give another --from-git"
            else:
                hint = "run `git fetch upstream` first, or give another --from-git"
            raise ValueError("{} is not in {} of this checkout ({})".format(name, ref, hint))
        files[name] = done.stdout
    return files


def listing(files):
    """{relative path: [size, sha256]}, as the board program lists a folder."""
    return {name: [len(data), hashlib.sha256(data).hexdigest()] for name, data in files.items()}


def check_overlay(runner, files):
    """Refuse an overlay other than the one the runner was verified with on the board."""
    for name, pinned in sorted(runner.overlay_sha256.items()):
        actual = hashlib.sha256(files[name]).hexdigest()
        if actual != pinned:
            raise ValueError("{} has SHA-256 {}, not {} (Wen Ree's board-verified overlay); "
                             "nothing was uploaded".format(name, actual, pinned))


def check_code(runner, files, ref):
    """Refuse a code file of another version than the runner needs (its markers)."""
    for name, marker in sorted(runner.markers.items()):
        if marker.encode() not in files[name]:
            raise ValueError("{} at {} has no {!r}: it is not the worker with the compat delivery "
                             "(the team repo still has Wen Ree's observe-only worker, which sends no "
                             "results). {}; nothing was uploaded".format(
                                 name, ref, marker, "Commit it first" if ref == "HEAD" else
                                 "Stage from a commit that has it, such as HEAD"))


def bundle_files(folder, allow_synthetic=False):
    """({BUNDLE/name: bytes}, synthetic) of a local hand model bundle, checked as the runner checks it.

    Every file the manifest's generated_sha256 lists is uploaded (the runner checks them all
    again on the board), plus manifest.json."""
    from src.hand_runtime import ModelBundle  # Wen Ree's check; it needs NumPy, so only here
    try:
        checked = ModelBundle(folder, allow_synthetic=allow_synthetic)
    except Exception as exc:  # its own refusals are ValueError; a broken file raises others
        raise ValueError("--bundle {} is not a usable hand model bundle: {}".format(folder, exc))
    hashes = checked.manifest["generated_sha256"]
    files = {}
    for name in sorted(set(hashes) | {"manifest.json"}):
        if (not all(c.isalnum() or c in "._-/" for c in name) or name.startswith("/") or
                posixpath.normpath(name) != name or ".." in name.split("/")):
            raise ValueError("the bundle's manifest names an unsafe file: {!r}".format(name))
        data = (checked.folder / name).read_bytes()
        if name in hashes and hashlib.sha256(data).hexdigest() != hashes[name]:
            raise ValueError("the bundle file {} changed after it was checked".format(name))
        files[BUNDLE + "/" + name] = data
    return files, checked.synthetic


def hand_bundle(value, allow_synthetic=False):
    """(the --bundle value the runner gets, {copy path: bytes} to upload, synthetic or None).

    A local folder is checked and uploaded into the copy's bundle folder; an absolute board path
    is only named, so whether it is synthetic is not known here (None)."""
    if Path(value).is_dir():
        files, synthetic = bundle_files(value, allow_synthetic)
        return BUNDLE, files, synthetic
    if not (value.startswith("/") and value.isprintable() and " " not in value):
        raise ValueError("--bundle {!r} is not a local folder, and a board path must be absolute "
                         "(start with /, no spaces)".format(value) + _git_bash_hint([value]))
    return value, {}, None


def _upload(board, folder, files):
    """Write the files under a new board folder through SFTP."""
    sftp = board.open_sftp()
    try:
        made = set()
        for name in sorted(files):
            parts = (folder + "/" + name).split("/")[:-1]
            for end in range(folder.count("/") + 1, len(parts) + 1):
                path = "/".join(parts[:end])
                if path not in made:
                    try:
                        sftp.stat(path)
                    except IOError:
                        sftp.mkdir(path)
                    made.add(path)
            sftp.putfo(io.BytesIO(files[name]), folder + "/" + name, confirm=True)
    finally:
        sftp.close()


def _run(board, command):
    _, out, err = board.exec_command(command, timeout=300)
    result = out.read().decode()
    errors = err.read().decode()
    return result, errors, out.channel.recv_exit_status()


def stage(args):
    runner = RUNNERS[args.runner]
    reads_bundle = runner.weights is None
    if reads_bundle:
        weights = None
        if args.weights is not None:
            raise ValueError("the {} runner reads a model bundle (--bundle), not --weights".format(
                args.runner))
        if args.source is not None:
            raise ValueError("the {} runner is uploaded from a commit (--from-git), not copied from "
                             "a board folder (--source)".format(args.runner))
        if args.bundle is None:
            raise ValueError(NO_BUNDLE)
        # The deploy tool's own check, so the printed command is never one it refuses.
        replace_entries(args.replace_runner)
    else:
        if args.bundle is not None or args.allow_synthetic or args.replace_runner:
            raise ValueError("--bundle, --allow-synthetic and --replace-runner are for the hand12 "
                             "runner only; the {} runner reads --weights".format(args.runner))
        weights = runner.weights if args.weights is None else args.weights
        if not weights or not weights.isprintable() or _git_bash_hint([weights]):
            raise ValueError("--weights must be a path relative to the runner folder, or an absolute "
                             "board path" + _git_bash_hint([weights]))
    expected = runner.expected(weights)
    git_ref = args.from_git or (runner.git_ref if args.source is None else None)
    if args.from_git and args.source is not None:
        raise ValueError("give --source (a board folder) or --from-git (a commit), not both")
    if git_ref is not None and runner.folder:
        raise ValueError("--from-git needs a runner laid out as the repository; the {} runner is "
                         "copied from its board folder (--source)".format(args.runner))
    source = args.source or runner.source
    if git_ref is None and not (source.startswith("/") and source.isprintable()):
        raise ValueError("--source must be an absolute path on the board" + _git_bash_hint([source]))
    dest = destination(args.name or runner.name)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bundle = synthetic = None
    uploads = {}
    if reads_bundle:
        bundle, uploads, synthetic = hand_bundle(args.bundle, args.allow_synthetic)
        # A checked local bundle needs the flag only when it is synthetic.
        command = deploy_command(dest, args.runner, bundle=bundle,
                                 allow_synthetic=args.allow_synthetic if synthetic is None else synthetic,
                                 replace=args.replace_runner)
    else:
        command = deploy_command(dest, args.runner, weights)
    files = None
    if git_ref is not None:
        files = git_files(git_ref, expected)
        check_code(runner, files, git_ref)
        check_overlay(runner, files)
        files.update(uploads)
        print("Upload from {} of this checkout -> {}".format(git_ref, dest))
        for name, (size, digest) in sorted(listing(files).items()):
            print("  {:>10}  {}  {}".format(size, digest[:16], name))
    else:
        print("Copy on the board: {} -> {} (without __pycache__; the original stays)".format(source, dest))
        print("Required files: " + ", ".join(expected))
    if reads_bundle and uploads:
        print("Bundle: {} passed src.hand_runtime.ModelBundle ({}) and goes into {}/{}".format(
            args.bundle, "a synthetic test bundle" if synthetic else "trained LEFT and RIGHT models",
            dest, BUNDLE))
    elif reads_bundle:
        print("Bundle: board path {}, only named in the deploy command (not uploaded or checked "
              "here; the runner checks it when it starts)".format(bundle))
    for name in runner.folders:
        print("The copy gets an empty folder {}/{}; the runner writes its records there".format(dest, name))
    if synthetic:
        print("SYNTHETIC test bundle allowed (--allow-synthetic): it is not a trained model, so its "
              "gestures mean nothing.")
    elif synthetic is None and args.allow_synthetic:
        # A board path is not read here: the flag only lets the runner accept a synthetic bundle.
        print("--allow-synthetic passed on: the runner will also accept a synthetic test bundle at {}; "
              "a synthetic bundle is not a trained model.".format(bundle))
    print("Then deploy from the copy:")
    print("  " + command)
    if args.dry_run:
        if reads_bundle:
            print(HAND_STARTED)
        print("Dry run: nothing was connected or changed.")
        return 0
    jump, board, _ = _connect(args.jump_user, args.board_user)
    try:
        if files is None:
            result, errors, code = _run(board, remote_command(source, dest, stamp, expected))
        else:
            upload = posixpath.dirname(dest) + "/.upload-" + posixpath.basename(dest) + "-" + stamp
            try:
                _upload(board, upload, files)
            except BaseException:
                _run(board, "rm -rf -- " + shlex.quote(upload))
                raise
            result, errors, code = _run(board, remote_command(upload, dest, stamp, listing(files),
                                                              move=True, folders=runner.folders))
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
    record["runner"] = args.runner
    if git_ref is not None:
        record["git_ref"] = git_ref
    if reads_bundle:
        record.update(bundle=bundle, bundle_source=args.bundle, bundle_synthetic=synthetic)
    RECORDS.mkdir(parents=True, exist_ok=True)
    path = RECORDS / "model-runner-stage-{}.json".format(stamp)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    copied = record.get("files") or {}
    for name in sorted(copied):
        print("  {:>10}  {}  {}".format(copied[name][0], copied[name][1][:16], name))
    print("Local record: " + str(path))
    if code or record.get("status") not in ("staged", "already_staged"):
        print("FAILED: " + str(record.get("error", record.get("status"))), file=sys.stderr)
        return 1
    print(("Copied" if record["status"] == "staged" else "Already copied") +
          ": {} files in {}, each SHA-256 equal to the original.".format(len(copied), dest))
    print("Deploy from the copy (PowerShell, NUS VPN on):\n  " + command)
    if reads_bundle:
        print(HAND_STARTED)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runner", choices=sorted(RUNNERS), default="conv1d",
                        help="which runner (default %(default)s)")
    parser.add_argument("--source", help="the runner folder on the board (default for conv1d: {}; "
                                         "the full runner uploads from --from-git instead; not for "
                                         "hand12)".format(SOURCE))
    parser.add_argument("--from-git", metavar="REF",
                        help="full and hand12 runners: upload the files from this commit of the "
                             "checkout (default upstream/main for full, HEAD for hand12)")
    parser.add_argument("--weights", help="conv1d and full: weights for the deploy command: relative "
                                          "to the runner folder (copied too) or an absolute board "
                                          "path (default: " + WEIGHTS + ")")
    parser.add_argument("--bundle", help="hand12 only, needed: the model bundle the runner reads: a "
                                         "local folder (checked, then uploaded into <copy>/bundle) or "
                                         "an absolute board path (only printed)")
    parser.add_argument("--allow-synthetic", action="store_true",
                        help="hand12 only: accept a synthetic test bundle (not a trained model)")
    parser.add_argument("--replace-runner", action="append", default=[], metavar="PID:START_TICKS",
                        help="hand12 only: passed on to the deploy command, which replaces this "
                             "runner started by hand (repeat for several)")
    parser.add_argument("--name", help="the copy's folder name under BOARD_ROOT/model-runner "
                                       "(default: cnn_conv1d, cnn_full_20261020 or hand12_v1)")
    parser.add_argument("--jump-user", default=default_jump_user(),
                        help="your NUS login for the jump host; default: LTC_JUMP_USER when set, else yanjie")
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
