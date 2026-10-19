import json
from pathlib import Path
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
