import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from tools import deploy_service, stage_model_runner as stage


def make_source(root):
    files = {name: name.encode() * 3 for name in stage.EXPECTED}
    files["ai_integration/src/ai_common.py"] = b"CLASS_NAMES = []\n"
    files["ai_integration/src/__pycache__/ai_common.cpython-310.pyc"] = b"\0compiled"
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def run_remote(source, dest, evidence, stamp="20261019T100000Z"):
    done = subprocess.run([sys.executable, "-I", "-c", stage.REMOTE_PROGRAM, str(source), str(dest),
                           stamp, json.dumps(list(stage.EXPECTED)), str(evidence)],
                          capture_output=True, text=True, timeout=60)
    record = json.loads(done.stdout.strip().splitlines()[-1])
    return done.returncode, record


def test_copies_every_file_except_pycache_and_keeps_the_original(tmp_path):
    source = make_source(tmp_path / "tmp" / "cnn_conv1d")
    dest = tmp_path / "board" / "model-runner" / "cnn_conv1d"
    code, record = run_remote(source, dest, tmp_path / "evidence")
    assert code == 0 and record["status"] == "staged"
    assert sorted(record["files"]) == sorted(list(stage.EXPECTED) + ["ai_integration/src/ai_common.py"])
    for name in record["files"]:
        assert (dest / name).read_bytes() == (source / name).read_bytes()
    assert not (dest / "ai_integration" / "src" / "__pycache__").exists()
    assert (source / "ai_integration" / "src" / "__pycache__").is_dir()
    assert not list(dest.parent.glob("*.partial-*"))
    saved = json.loads((tmp_path / "evidence" / "model-runner-stage-20261019T100000Z.json").read_text())
    assert saved["status"] == "staged" and saved["dest"] == str(dest)


def test_a_second_run_reports_the_copy_already_there(tmp_path):
    source = make_source(tmp_path / "src")
    dest = tmp_path / "copy"
    assert run_remote(source, dest, tmp_path / "e")[0] == 0
    code, record = run_remote(source, dest, tmp_path / "e", stamp="20261019T100100Z")
    assert code == 0 and record["status"] == "already_staged"


def test_a_destination_with_other_files_is_never_overwritten(tmp_path):
    source = make_source(tmp_path / "src")
    dest = tmp_path / "copy"
    assert run_remote(source, dest, tmp_path / "e")[0] == 0
    (source / stage.EXPECTED[0]).write_bytes(b"changed by the model owner")
    code, record = run_remote(source, dest, tmp_path / "e", stamp="20261019T100200Z")
    assert code == 1 and record["status"] == "failed" and "--name" in record["error"]
    assert (dest / stage.EXPECTED[0]).read_bytes() == stage.EXPECTED[0].encode() * 3


def test_a_missing_runner_file_stops_before_copying(tmp_path):
    source = make_source(tmp_path / "src")
    (source / stage.EXPECTED[3]).unlink()  # the .hwh beside the bitstream
    dest = tmp_path / "copy"
    code, record = run_remote(source, dest, tmp_path / "e")
    assert code == 1 and "missing in the source" in record["error"] and stage.EXPECTED[3] in record["error"]
    assert not dest.exists()


def test_a_missing_source_folder_says_the_board_may_have_restarted(tmp_path):
    code, record = run_remote(tmp_path / "gone", tmp_path / "copy", tmp_path / "e")
    assert code == 1 and "restarted" in record["error"]
    assert not (tmp_path / "copy").exists()


def test_the_printed_deploy_command_is_one_the_deploy_tool_accepts():
    dest = stage.destination(stage.NAME)
    assert dest == deploy_service.BOARD_ROOT + "/model-runner/cnn_conv1d"
    argv = stage.deploy_command(dest).split()[3:]
    parser_args = {}

    def capture(args):
        parser_args["options"] = deploy_service.options_from(args)
        return 0

    original = deploy_service.deploy
    deploy_service.deploy = capture
    try:
        assert deploy_service.main(argv) == 0
    finally:
        deploy_service.deploy = original
    runner = parser_args["options"]["runner"]
    assert runner["module"] == "src.socket_worker"
    assert runner["cwd"] == dest + "/ai_integration"
    assert runner["args"] == ["--weights", "fpga/hls/eightclass_weights_20261018_v2.npz",
                              "--bit", dest + "/cnn_conv1d_fixed28_v2.bit"]


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a b", "../x"])
def test_the_copy_name_is_a_plain_folder_name(name):
    with pytest.raises(ValueError):
        stage.destination(name)


def test_dry_run_connects_nothing(monkeypatch, capsys):
    monkeypatch.setattr(stage, "_connect", lambda *a, **k: pytest.fail("connected"))
    assert stage.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Dry run" in out and "/tmp/cnn_conv1d -> " in out and "--runner-cwd " in out


def test_a_windows_source_path_is_refused(capsys):
    assert stage.main(["--dry-run", "--source", "D:/Git/tmp/cnn_conv1d"]) == 2
    assert "Git Bash" in capsys.readouterr().err


FULL = stage.RUNNERS["full"]


def full_files():
    return {name: ("full " + name).encode() for name in FULL.expected()}


def run_move(upload, dest, evidence, expected, stamp="20261022T100000Z"):
    done = subprocess.run([sys.executable, "-I", "-c", stage.REMOTE_PROGRAM, str(upload), str(dest),
                           stamp, json.dumps(expected), str(evidence), "move"],
                          capture_output=True, text=True, timeout=60)
    return done.returncode, json.loads(done.stdout.strip().splitlines()[-1])


def write_upload(root, files):
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    return root


def test_the_full_runner_needs_its_code_weights_overlay_and_hwh_in_repository_layout():
    assert FULL.expected() == (
        "src/ai_common.py", "src/export_full_cnn.py", "src/full_cnn_backend.py",
        "src/socket_worker_full.py", "fpga/hls/eightclass_weights_20261018_v2.npz",
        "fpga/overlays/full_cnn/cnn_full_20261020.bit", "fpga/overlays/full_cnn/cnn_full_20261020.hwh")
    # Weights staged on their own are only named in the command, not copied.
    assert "fpga/hls/eightclass_weights_20261018_v2.npz" not in FULL.expected("/var/tmp/w/x.npz")
    # The Conv1-only runner keeps the files and names it had.
    assert stage.EXPECTED == ("ai_integration/src/socket_worker.py",
                              "ai_integration/fpga/hls/eightclass_weights_20261018_v2.npz",
                              "cnn_conv1d_fixed28_v2.bit", "cnn_conv1d_fixed28_v2.hwh")


def parsed_runner(command):
    captured = {}
    original = deploy_service.deploy
    deploy_service.deploy = lambda args: captured.update(options=deploy_service.options_from(args)) or 0
    try:
        assert deploy_service.main(command.split()[3:]) == 0
    finally:
        deploy_service.deploy = original
    return captured["options"]["runner"]


@pytest.mark.parametrize("weights", [None, deploy_service.BOARD_ROOT +
    "/model-runner/weights_standard_20261021/eightclass_weights_standard_20261021.npz"])
def test_the_full_runner_command_is_the_one_deployed_on_10_21_and_10_22(weights):
    dest = stage.destination(FULL.name)
    runner = parsed_runner(stage.deploy_command(dest, "full", weights))
    assert runner["module"] == "src.socket_worker_full" and runner["cwd"] == dest
    assert runner["args"] == ["--weights", weights or "fpga/hls/eightclass_weights_20261018_v2.npz",
                              "--bit", dest + "/fpga/overlays/full_cnn/cnn_full_20261020.bit"]
    assert "--control-address" not in stage.deploy_command(dest, "full", weights)


def test_an_upload_matching_the_commit_is_moved_into_place(tmp_path):
    files = full_files()
    upload = write_upload(tmp_path / "model-runner" / ".upload-cnn_full-1", files)
    dest = tmp_path / "model-runner" / "cnn_full_20261020"
    code, record = run_move(upload, dest, tmp_path / "e", stage.listing(files))
    assert code == 0 and record["status"] == "staged"
    assert {name: (dest / name).read_bytes() for name in files} == files
    assert not upload.exists()


def test_an_upload_that_differs_from_the_commit_is_removed_and_nothing_staged(tmp_path):
    files = full_files()
    upload = write_upload(tmp_path / ".upload", dict(files, **{"src/ai_common.py": b"damaged"}))
    dest = tmp_path / "cnn_full_20261020"
    code, record = run_move(upload, dest, tmp_path / "e", stage.listing(files))
    assert code == 1 and "src/ai_common.py" in record["error"]
    assert not dest.exists() and not upload.exists()


def test_an_existing_copy_with_the_same_files_and_more_counts_as_staged(tmp_path):
    files = full_files()
    dest = write_upload(tmp_path / "cnn_full_20261020", dict(files, **{"extra.npz": b"other weights"}))
    upload = write_upload(tmp_path / ".upload", files)
    code, record = run_move(upload, dest, tmp_path / "e", stage.listing(files))
    assert code == 0 and record["status"] == "already_staged"
    assert not upload.exists() and (dest / "extra.npz").exists()
    changed = write_upload(tmp_path / ".upload2", dict(files, **{"src/ai_common.py": b"new"}))
    code, record = run_move(changed, dest, tmp_path / "e", stage.listing(
        dict(files, **{"src/ai_common.py": b"new"})))
    assert code == 1 and "--name" in record["error"]
    assert (dest / "src/ai_common.py").read_bytes() == files["src/ai_common.py"]
    assert not changed.exists()


def test_git_files_reads_each_file_of_a_commit_and_names_a_missing_one(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
    git("init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_bytes(b"\x00binary\r\n")
    git("add", "src/a.py")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "a")
    assert stage.git_files("HEAD", ["src/a.py"], root=tmp_path) == {"src/a.py": b"\x00binary\r\n"}
    with pytest.raises(ValueError, match="git fetch upstream"):
        stage.git_files("upstream/main", ["src/a.py"], root=tmp_path)
    # A file missing from HEAD (the hand12 default) is one not committed yet.
    with pytest.raises(ValueError, match="commit it first"):
        stage.git_files("HEAD", ["src/missing.py"], root=tmp_path)


def test_full_dry_run_reads_the_commit_and_connects_nothing(monkeypatch, capsys):
    monkeypatch.setattr(stage, "_connect", lambda *a, **k: pytest.fail("connected"))
    seen = []
    monkeypatch.setattr(stage, "git_files", lambda ref, names: seen.append(ref) or
                        {name: b"x" for name in names})
    assert stage.main(["--runner", "full", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert seen == ["upstream/main"]
    assert "Upload from upstream/main" in out and "--runner-module src.socket_worker_full" in out
    assert "Dry run" in out


def test_from_git_is_refused_for_the_conv1d_folder_layout(capsys):
    assert stage.main(["--dry-run", "--from-git", "HEAD"]) == 2
    assert "--source" in capsys.readouterr().err


def test_the_jump_user_defaults_to_ltc_jump_user(monkeypatch):
    seen = []
    monkeypatch.setenv("LTC_JUMP_USER", "e0123456")
    monkeypatch.setattr(stage, "stage", lambda args: seen.append(args.jump_user) or 0)
    assert stage.main([]) == 0 and seen == ["e0123456"]


class FakeBoard:
    """SFTP writes and commands run on this machine, under the test's BOARD_ROOT."""

    def __init__(self):
        self.closed = False
        board = self

        class Sftp:
            def stat(self, path):
                if not Path(path).exists():
                    raise IOError(path)

            def mkdir(self, path):
                Path(path).mkdir()

            def putfo(self, stream, path, confirm=True):
                Path(path).write_bytes(stream.read())

            def close(self):
                pass
        self.sftp = Sftp

    def open_sftp(self):
        return self.sftp()

    def exec_command(self, command, timeout=None):
        argv = shlex.split(command)
        if argv[0] == "/usr/bin/python3":
            argv[0] = sys.executable
        done = subprocess.run(argv, capture_output=True, timeout=60)

        class Out(io.BytesIO):
            class channel:
                @staticmethod
                def recv_exit_status():
                    return done.returncode
        return None, Out(done.stdout), io.BytesIO(done.stderr)

    def close(self):
        self.closed = True


@pytest.mark.skipif(os.name == "nt", reason="runs the board program with POSIX paths")
def test_full_staging_uploads_checks_and_reports_end_to_end(tmp_path, monkeypatch, capsys):
    board_root = tmp_path / "board"
    (board_root / "model-runner").mkdir(parents=True)
    monkeypatch.setattr(stage, "BOARD_ROOT", str(board_root))
    monkeypatch.setattr(stage, "RECORDS", tmp_path / "records")
    files = full_files()
    monkeypatch.setattr(stage, "git_files", lambda ref, names: {n: files[n] for n in names})
    users = []
    monkeypatch.setattr(stage, "_connect", lambda jump, board: users.append((jump, board)) or
                        (FakeBoard(), FakeBoard(), None))
    assert stage.main(["--runner", "full", "--jump-user", "e0123456"]) == 0
    dest = board_root / "model-runner" / "cnn_full_20261020"
    assert {name: (dest / name).read_bytes() for name in files} == files
    assert sorted(p.name for p in (board_root / "model-runner").iterdir()) == ["cnn_full_20261020"]
    assert users == [("e0123456", "xilinx")]
    record = json.loads(next((tmp_path / "records").glob("model-runner-stage-*.json")).read_text())
    assert record["status"] == "staged" and record["runner"] == "full"
    assert record["git_ref"] == "upstream/main"
    assert "Copied: 7 files" in capsys.readouterr().out
    assert stage.main(["--runner", "full"]) == 0
    assert "Already copied" in capsys.readouterr().out


HAND12 = stage.RUNNERS["hand12"]
REPO = Path(stage.__file__).resolve().parents[1]
OVERLAY = "fpga/overlays/independent_hand_v1/"
BOARD_BUNDLE = deploy_service.BOARD_ROOT + "/model-runner/bundles/hand12_trained"


def no_connect(*args, **kwargs):
    pytest.fail("connected")


@pytest.fixture(scope="module")
def overlay():
    """The overlay files of this checkout: Wen Ree's board-verified pair and its manifest."""
    return {name: (REPO / name).read_bytes() for name in HAND12.expected() if name.startswith(OVERLAY)}


@pytest.fixture(scope="module")
def synthetic_bundle(tmp_path_factory):
    from tools import hand_hls_prepare
    folder = tmp_path_factory.mktemp("hand12") / "synthetic"
    hand_hls_prepare.prepare(folder, synthetic=True)
    return folder


WORKER = "src/socket_worker_hand.py"
SOCKET = deploy_service.BOARD_ROOT + "/model-runner.sock"


def hand12_commit(overlay, changed=None):
    """git_files for a commit with placeholder code (the worker naming its compat delivery), this
    checkout's overlay and the changed files."""
    def git_files(ref, names):
        files = {name: overlay.get(name, ("hand12 " + name).encode()) for name in names}
        files[WORKER] = b"DELIVERY='compat_v3_gesture_result'\n"
        files.update(changed or {})
        return files
    return git_files


def hand12_command(dest, bundle="bundle", extra=""):
    return ("python -m tools.deploy_service --sensor-version 4 --model ultra96.model_bridge "
            "--runner-module src.socket_worker_hand --runner-cwd " + dest +
            " --runner-arg=--bundle --runner-arg " + bundle +
            " --runner-arg=--backend --runner-arg fpga --runner-arg=--bit --runner-arg " + dest +
            "/fpga/overlays/independent_hand_v1/cnn_hand12_v1.bit --runner-arg=--socket --runner-arg " +
            SOCKET + " --runner-arg=--out --runner-arg " + dest + "/logs" + extra)


def test_the_hand12_runner_stages_wen_rees_worker_and_runtime_and_the_verified_overlay(overlay):
    assert HAND12.expected() == (
        "src/socket_worker_hand.py", "src/hand_runtime.py", "src/hand_backend.py", "src/hand_events.py",
        "src/hand_ai_flex3/__init__.py", "src/hand_ai_flex3/core.py",
        "common/__init__.py", "common/sensor.py", "common/dummy_fixtures.json",
        "common/hand_contract.py", "common/wire.py", OVERLAY + "cnn_hand12_v1.bit",
        OVERLAY + "cnn_hand12_v1.hwh", OVERLAY + "artifact_manifest.json")
    assert (HAND12.module, HAND12.name, HAND12.git_ref, HAND12.folder) == (
        "src.socket_worker_hand", "hand12_v1", "HEAD", "")
    assert HAND12.overlay_sha256 == {
        OVERLAY + "cnn_hand12_v1.bit": "b8d0407300c913fbde24143b6a632f62c0bf4021290d80fad575d32f59d6523d",
        OVERLAY + "cnn_hand12_v1.hwh": "935975fe769d0fda1a802c43c9a65430406a61dc02802f637df40f935660a790"}
    stage.check_overlay(HAND12, overlay)  # this checkout carries that pair
    assert HAND12.markers == {WORKER: "compat_v3_gesture_result"} and HAND12.folders == ("logs",)
    # This checkout's worker is the one with the compat delivery.
    stage.check_code(HAND12, {WORKER: (REPO / WORKER).read_bytes()}, "HEAD")
    # The other runners pin no overlay, need no marker and copy nothing extra.
    assert FULL.overlay_sha256 == {} == stage.RUNNERS["conv1d"].overlay_sha256
    assert FULL.extra == () == stage.RUNNERS["conv1d"].extra
    assert FULL.markers == {} == stage.RUNNERS["conv1d"].markers
    assert FULL.folders == () == stage.RUNNERS["conv1d"].folders


def test_wen_rees_observe_only_worker_is_refused_before_connecting(overlay, monkeypatch, capsys):
    monkeypatch.setattr(stage, "_connect", no_connect)
    observe_only = b'"""Observe/replay a twelve-channel bridge and write LOCAL diagnostic events."""\n'
    monkeypatch.setattr(stage, "git_files", hand12_commit(overlay, {WORKER: observe_only}))
    assert stage.main(["--runner", "hand12", "--bundle", BOARD_BUNDLE, "--from-git", "upstream/main"]) == 2
    err = capsys.readouterr().err
    assert WORKER + " at upstream/main has no 'compat_v3_gesture_result'" in err
    assert "observe-only worker" in err and "such as HEAD" in err and "nothing was uploaded" in err


def test_the_board_program_makes_the_runners_empty_folders_and_keeps_their_files(tmp_path):
    files = {name: ("hand12 " + name).encode() for name in HAND12.expected()}
    upload = write_upload(tmp_path / ".upload", files)
    dest = tmp_path / "hand12_v1"
    done = subprocess.run([sys.executable, "-I", "-c", stage.REMOTE_PROGRAM, str(upload), str(dest),
                           "20261028T100000Z", json.dumps(stage.listing(files)), str(tmp_path / "e"),
                           "move", json.dumps(["logs"])], capture_output=True, text=True, timeout=60)
    record = json.loads(done.stdout.strip().splitlines()[-1])
    assert done.returncode == 0 and record["status"] == "staged" and (dest / "logs").is_dir()
    assert "logs" not in json.dumps(record["files"])
    # The runner's records do not stop a second staging from counting as already staged.
    (dest / "logs" / "hand-worker-20261028T100100Z-77.jsonl").write_text("{}\n")
    upload = write_upload(tmp_path / ".upload2", files)
    done = subprocess.run([sys.executable, "-I", "-c", stage.REMOTE_PROGRAM, str(upload), str(dest),
                           "20261028T100200Z", json.dumps(stage.listing(files)), str(tmp_path / "e"),
                           "move", json.dumps(["logs"])], capture_output=True, text=True, timeout=60)
    record = json.loads(done.stdout.strip().splitlines()[-1])
    assert done.returncode == 0 and record["status"] == "already_staged"
    assert (dest / "logs" / "hand-worker-20261028T100100Z-77.jsonl").read_text() == "{}\n"


def test_the_remote_command_passes_the_folders_only_when_there_are_some():
    def argv(**kwargs):
        return shlex.split(stage.remote_command("/s", "/d", "T", ["a"], **kwargs))[4:]
    assert argv() == ["/s", "/d", "T", '["a"]', stage.BOARD_ROOT + "/evidence"]
    assert argv(move=True)[-1] == "move"
    assert argv(move=True, folders=("logs",))[-2:] == ["move", '["logs"]']
    assert argv(folders=("logs",))[-2:] == ["copy", '["logs"]']


def test_the_hand12_deploy_command_reads_v4_the_bundle_and_the_overlay():
    dest = stage.destination(HAND12.name)
    assert dest == deploy_service.BOARD_ROOT + "/model-runner/hand12_v1"
    assert stage.deploy_command(dest, "hand12") == hand12_command(dest)
    assert stage.deploy_command(dest, "hand12", bundle=BOARD_BUNDLE, allow_synthetic=True,
                                replace=["177087:611890431", "2:3"]) == hand12_command(
        dest, BOARD_BUNDLE, " --runner-arg=--allow-synthetic --replace-runner 177087:611890431"
                            " --replace-runner 2:3")


def test_the_deploy_tool_accepts_the_hand12_command(monkeypatch):
    dest = stage.destination(HAND12.name)
    captured = {}
    monkeypatch.setattr(deploy_service, "deploy", lambda args: captured.update(
        args=args, options=deploy_service.options_from(args)) or 0)
    command = stage.deploy_command(dest, "hand12", replace=["177087:611890431"])
    assert deploy_service.main(command.split()[3:]) == 0
    runner = captured["options"]["runner"]
    assert runner["module"] == "src.socket_worker_hand" and runner["cwd"] == dest
    assert runner["args"] == ["--bundle", "bundle", "--backend", "fpga", "--bit",
                              dest + "/fpga/overlays/independent_hand_v1/cnn_hand12_v1.bit",
                              "--socket", "/var/tmp/cg4002-week7-yanjie-20260907/model-runner.sock",
                              "--out", dest + "/logs"]
    assert captured["options"]["sensor_version"] == 4
    assert [entry["pid"] for entry in runner["replace"]] == [177087]


def test_hand12_dry_run_checks_a_local_bundle_and_plans_its_upload(synthetic_bundle, overlay,
                                                                   monkeypatch, capsys):
    monkeypatch.setattr(stage, "_connect", no_connect)
    seen = []
    commit = hand12_commit(overlay)
    monkeypatch.setattr(stage, "git_files", lambda ref, names: seen.append((ref, tuple(names))) or
                        commit(ref, names))
    capsys.readouterr()
    assert stage.main(["--runner", "hand12", "--bundle", str(synthetic_bundle), "--allow-synthetic",
                       "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert seen == [("HEAD", HAND12.expected())]
    manifest = json.loads((synthetic_bundle / "manifest.json").read_text())
    names = sorted(manifest["generated_sha256"]) + ["manifest.json"]
    assert {"left/parameter_words.bin", "right/normalization.npz", "vectors/hand_test.bin"} <= set(names)
    lines = out.splitlines()
    for name in names:
        data = (synthetic_bundle / name).read_bytes()
        assert "  {:>10}  {}  bundle/{}".format(
            len(data), hashlib.sha256(data).hexdigest()[:16], name) in lines
    dest = stage.destination("hand12_v1")
    assert "  " + hand12_command(dest, extra=" --runner-arg=--allow-synthetic") in lines
    assert "a synthetic test bundle" in out and "not a trained model" in out
    assert "PID 177087, start_ticks 611890431 on 2026-10-27" in out
    assert "--replace-runner 177087:611890431" in out and "python demo.py service" in out
    assert lines[-1] == "Dry run: nothing was connected or changed."


def test_a_synthetic_bundle_needs_allow_synthetic(synthetic_bundle, overlay, monkeypatch, capsys):
    monkeypatch.setattr(stage, "_connect", no_connect)
    monkeypatch.setattr(stage, "git_files", hand12_commit(overlay))
    assert stage.main(["--runner", "hand12", "--bundle", str(synthetic_bundle), "--dry-run"]) == 2
    err = capsys.readouterr().err
    assert "--allow-synthetic" in err and "not a trained model" in err


def test_a_damaged_local_bundle_is_refused(synthetic_bundle, overlay, tmp_path, monkeypatch, capsys):
    damaged = tmp_path / "damaged"
    shutil.copytree(str(synthetic_bundle), str(damaged))
    (damaged / "right" / "config.json").write_bytes(b"{}")
    monkeypatch.setattr(stage, "_connect", no_connect)
    monkeypatch.setattr(stage, "git_files", hand12_commit(overlay))
    assert stage.main(["--runner", "hand12", "--bundle", str(damaged), "--allow-synthetic"]) == 2
    err = capsys.readouterr().err
    assert "not a usable hand model bundle" in err and "right/config.json" in err


def test_hand12_with_a_board_bundle_path_only_names_it(overlay, monkeypatch, capsys):
    monkeypatch.setattr(stage, "_connect", no_connect)
    monkeypatch.setattr(stage, "git_files", hand12_commit(overlay))
    assert stage.main(["--runner", "hand12", "--bundle", BOARD_BUNDLE, "--replace-runner",
                       "177087:611890431", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert " bundle/" not in out and "board path " + BOARD_BUNDLE in out
    dest = stage.destination("hand12_v1")
    assert "  " + hand12_command(dest, BOARD_BUNDLE, " --replace-runner 177087:611890431") in (
        out.splitlines())
    assert "--allow-synthetic" not in out and "SYNTHETIC" not in out


def test_allow_synthetic_with_a_board_bundle_path_does_not_call_it_synthetic(overlay, monkeypatch,
                                                                            capsys):
    monkeypatch.setattr(stage, "_connect", no_connect)
    monkeypatch.setattr(stage, "git_files", hand12_commit(overlay))
    assert stage.main(["--runner", "hand12", "--bundle", BOARD_BUNDLE, "--allow-synthetic",
                       "--dry-run"]) == 0
    out = capsys.readouterr().out
    # Not read here, so whether it is synthetic is unknown: the flag is passed on, nothing claimed.
    assert "SYNTHETIC" not in out and "gestures mean nothing" not in out
    assert ("--allow-synthetic passed on: the runner will also accept a synthetic test bundle at " +
            BOARD_BUNDLE + "; a synthetic bundle is not a trained model.") in out.splitlines()
    dest = stage.destination("hand12_v1")
    assert "  " + hand12_command(dest, BOARD_BUNDLE, " --runner-arg=--allow-synthetic") in (
        out.splitlines())


def test_without_a_bundle_the_hand12_runner_says_where_one_comes_from(overlay, monkeypatch, capsys):
    monkeypatch.setattr(stage, "_connect", no_connect)
    monkeypatch.setattr(stage, "git_files", hand12_commit(overlay))
    assert stage.main(["--runner", "hand12", "--dry-run"]) == 2
    err = capsys.readouterr().err
    assert "needs --bundle" in err and "Wen Ree" in err and "--allow-synthetic" in err
    assert "python -m tools.hand_hls_prepare --synthetic --out <new folder>" in err


@pytest.mark.parametrize("argv, message", [
    (["--runner", "hand12", "--bundle", BOARD_BUNDLE, "--weights", "w.npz"], "not --weights"),
    (["--runner", "full", "--bundle", BOARD_BUNDLE], "hand12 runner only"),
    (["--bundle", BOARD_BUNDLE], "hand12 runner only"),
    (["--runner", "full", "--allow-synthetic"], "hand12 runner only"),
    (["--runner", "full", "--replace-runner", "177087:611890431"], "hand12 runner only"),
    (["--runner", "hand12", "--bundle", BOARD_BUNDLE, "--source", "/tmp/hand12"], "--source"),
    (["--runner", "hand12", "--bundle", BOARD_BUNDLE, "--replace-runner", "177087"], "PID:START_TICKS"),
    (["--runner", "hand12", "--bundle", BOARD_BUNDLE, "--replace-runner", "0:611890431"],
     "PID:START_TICKS"),
    (["--runner", "hand12", "--bundle", BOARD_BUNDLE, "--replace-runner", "177087:6118x"],
     "PID:START_TICKS"),
    # What the deploy tool refuses too: init, and one PID twice.
    (["--runner", "hand12", "--bundle", BOARD_BUNDLE, "--replace-runner", "1:5"], "names init"),
    (["--runner", "hand12", "--bundle", BOARD_BUNDLE, "--replace-runner", "177087:1",
      "--replace-runner", "177087:2"], "names PID 177087 twice"),
    (["--runner", "hand12", "--bundle", "D:/Git/var/tmp/hand12_bundle"], "Git Bash"),
    (["--runner", "hand12", "--bundle", "no/such/bundle"], "not a local folder"),
])
def test_hand12_refusals(argv, message, overlay, monkeypatch, capsys):
    monkeypatch.setattr(stage, "_connect", no_connect)
    monkeypatch.setattr(stage, "git_files", hand12_commit(overlay))
    assert stage.main(argv + ["--dry-run"]) == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("name", ["cnn_hand12_v1.bit", "cnn_hand12_v1.hwh"])
def test_an_overlay_other_than_the_board_verified_pair_is_refused_before_connecting(
        name, overlay, monkeypatch, capsys):
    monkeypatch.setattr(stage, "_connect", no_connect)
    monkeypatch.setattr(stage, "git_files", hand12_commit(overlay, {OVERLAY + name: b"another build"}))
    assert stage.main(["--runner", "hand12", "--bundle", BOARD_BUNDLE]) == 2
    err = capsys.readouterr().err
    assert OVERLAY + name in err and "board-verified" in err and "nothing was uploaded" in err


def test_hand12_uploads_the_code_overlay_and_bundle_for_the_board_to_check(
        synthetic_bundle, overlay, tmp_path, monkeypatch, capsys):
    class Connection:
        def close(self):
            pass
    monkeypatch.setattr(stage, "RECORDS", tmp_path / "records")
    monkeypatch.setattr(stage, "git_files", hand12_commit(overlay))
    monkeypatch.setattr(stage, "_connect", lambda jump, board: (Connection(), Connection(), None))
    uploaded, commands = {}, []
    monkeypatch.setattr(stage, "_upload", lambda board, folder, files: uploaded.update(files))

    def run(board, command):
        commands.append(shlex.split(command))
        return json.dumps({"status": "staged", "files": stage.listing(uploaded)}), "", 0
    monkeypatch.setattr(stage, "_run", run)
    capsys.readouterr()
    assert stage.main(["--runner", "hand12", "--bundle", str(synthetic_bundle), "--allow-synthetic",
                       "--replace-runner", "177087:611890431"]) == 0
    manifest = json.loads((synthetic_bundle / "manifest.json").read_text())
    bundle = ["bundle/" + name for name in list(manifest["generated_sha256"]) + ["manifest.json"]]
    assert sorted(uploaded) == sorted(list(HAND12.expected()) + bundle)
    for name in bundle:
        assert uploaded[name] == (synthetic_bundle / name[len("bundle/"):]).read_bytes()
    # What lands in the copy's bundle folder passes the runner's own check.
    from src.hand_runtime import ModelBundle
    assert ModelBundle(write_upload(tmp_path / "copy", uploaded) / "bundle", allow_synthetic=True).synthetic
    # The board program gets every size and SHA-256, moves the upload into place and makes logs/.
    [argv] = commands
    assert argv[-2:] == ["move", '["logs"]'] and json.loads(argv[7]) == stage.listing(uploaded)
    assert argv[5] == stage.destination("hand12_v1")
    record = json.loads(next((tmp_path / "records").glob("model-runner-stage-*.json")).read_text())
    assert (record["runner"], record["git_ref"], record["bundle"], record["bundle_synthetic"]) == (
        "hand12", "HEAD", "bundle", True)
    out = capsys.readouterr().out
    assert "Copied: {} files".format(len(uploaded)) in out
    assert "  " + hand12_command(stage.destination("hand12_v1"), extra=(
        " --runner-arg=--allow-synthetic --replace-runner 177087:611890431")) in out.splitlines()
    assert out.splitlines()[-1] == stage.HAND_STARTED


def test_the_staged_hand12_code_imports_on_its_own(tmp_path):
    """The copy runs from its own folder: everything it imports must be in the staged set."""
    copy = tmp_path / "hand12_v1"
    for name in HAND12.code:
        (copy / name).parent.mkdir(parents=True, exist_ok=True)
        (copy / name).write_bytes((REPO / name).read_bytes())
    env = {key: value for key, value in os.environ.items()
           if key not in ("PYTHONPATH", "PYTHONSAFEPATH")}
    done = subprocess.run(
        [sys.executable, "-c", "import src.socket_worker_hand, src.hand_runtime, src.hand_backend, "
         "src.hand_events, common.hand_contract; import json, sys; print(json.dumps(sorted("
         "m.__file__ for n, m in list(sys.modules.items()) if n.split('.')[0] in "
         "('ultra96', 'src', 'common') and getattr(m, '__file__', None))))"],
        cwd=str(copy), env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    loaded = [Path(path).resolve() for path in json.loads(done.stdout.strip().splitlines()[-1])]
    assert loaded and all(copy.resolve() in path.parents for path in loaded), loaded
    assert {path.relative_to(copy.resolve()).as_posix() for path in loaded} <= set(HAND12.code)
