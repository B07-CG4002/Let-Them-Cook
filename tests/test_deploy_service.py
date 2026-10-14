import hashlib
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile

import pytest

from tools import deploy_service
from tools.generate_comms_pki import generate_pki, renew_server_identity

ROOT = Path(__file__).resolve().parents[1]
OLD_SERVICE_COMMIT = "c42b386"


def test_source_files_are_exactly_what_the_board_runs():
    names = [name for name, _ in deploy_service.source_files()]
    assert "common/sensor.py" in names and "common/dummy_fixtures.json" in names
    assert "ultra96/server.py" in names and "ultra96/model.py" in names
    assert all(name.split("/")[0] in ("common", "ultra96") for name in names)
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
    assert not any(name.startswith("ultra96/") and not name.endswith(".py") for name in names)


def test_manifest_hashes_each_file():
    files = deploy_service.source_files()
    manifest = deploy_service.manifest(files)
    name, path = files[0]
    assert manifest[name] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(manifest) == len(files)


def test_remote_program_compiles_and_command_round_trips():
    compile(deploy_service.REMOTE_PROGRAM, "remote", "exec")
    options = {"session_id": "ltc-comms", "event_log": True, "model": None, "model_path": None,
               "start_timeout": 30.0}
    command = deploy_service.remote_command("/var/tmp/root dir", "/var/tmp/root dir/src", "T1", options)
    parts = shlex.split(command)
    assert parts[:2] == ["/usr/bin/python3", "-c"]
    assert parts[2] == deploy_service.REMOTE_PROGRAM
    assert parts[3:6] == ["/var/tmp/root dir", "/var/tmp/root dir/src", "T1"]
    assert '"session_id": "ltc-comms"' in parts[6]


def _args(**overrides):
    values = dict(session_id="ltc-comms", no_event_log=False, model=None, model_path=None,
                  start_timeout=30.0, server_cert_dir=None, dry_run=True,
                  jump_user="yanjie", board_user="xilinx")
    values.update(overrides)
    return type("Args", (), values)()


def test_options_are_validated():
    assert deploy_service.options_from(_args(model="wen_model"))["model"] == "wen_model"
    with pytest.raises(ValueError):
        deploy_service.options_from(_args(model_path="/x"))
    with pytest.raises(ValueError):
        deploy_service.options_from(_args(start_timeout=1))
    with pytest.raises(ValueError):
        deploy_service.options_from(_args(session_id=""))


def test_certificate_folder_needs_both_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        deploy_service.certificate_files(tmp_path)
    (tmp_path / "server-cert.pem").write_text("x")
    (tmp_path / "server-key.pem").write_text("y")
    assert deploy_service.certificate_files(tmp_path)[1].name == "server-key.pem"
    assert deploy_service.certificate_files(None) is None


def test_dry_run_connects_nothing(monkeypatch, capsys):
    def refuse(*_args):
        raise AssertionError("dry run must not connect")
    monkeypatch.setattr(deploy_service, "_connect", refuse)
    assert deploy_service.main(["--dry-run", "--model", "wen_model"]) == 0
    out = capsys.readouterr().out
    assert "/source-co-v3-" in out and "Dry run" in out and '"model": "wen_model"' in out


def test_missing_certificate_folder_fails_before_connecting(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(deploy_service, "_connect", lambda *_: (_ for _ in ()).throw(AssertionError()))
    assert deploy_service.main(["--server-cert-dir", str(tmp_path / "absent")]) == 2
    assert "FAILED" in capsys.readouterr().err


def _linux_runner():
    if sys.platform.startswith("linux"):
        return ["bash"], lambda path: str(path)
    wsl = shutil.which("wsl.exe")
    if wsl is None:
        return None, None

    def convert(path):
        # wsl.exe passes arguments through a shell that drops backslashes.
        result = subprocess.run([wsl, "wslpath", "-u", Path(path).as_posix()], capture_output=True,
                                timeout=60)
        return result.stdout.decode("utf-8", "replace").replace("\0", "").strip()
    return [wsl, "--", "bash"], convert


@pytest.mark.skipif(os.environ.get("B07_LINUX_DEPLOY_TEST") != "1",
                    reason="opt-in: starts and stops local ultra96.server processes in Linux/WSL")
def test_remote_program_on_linux(tmp_path):
    """Deploy, rollback, certificate checks and port checks against real Linux processes."""
    runner, convert = _linux_runner()
    if runner is None:
        pytest.skip("needs Linux or WSL")
    material = tmp_path / "material"
    material.mkdir()
    (material / "remote_program.py").write_text(deploy_service.REMOTE_PROGRAM, encoding="utf-8",
                                                newline="\n")
    generate_pki(material / "pki-a")
    renew_server_identity(material / "pki-a", material / "pki-b")
    for folder in ("common", "ultra96"):
        shutil.copytree(ROOT / folder, material / "new" / folder,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # The running service on the board is the version-2 code of 28 September; c42b386 is
    # the last commit before version 3 (same service code as the team main 5a004c6).
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", OLD_SERVICE_COMMIT, "common", "ultra96"],
                             capture_output=True, timeout=60)
    if archive.returncode:
        pytest.skip("needs the Git history with " + OLD_SERVICE_COMMIT)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
        options = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        bundle.extractall(material / "old", **options)
    # A Windows checkout may have given the script CRLF endings, which bash rejects.
    script = material / "deploy_remote_test.sh"
    script.write_bytes((ROOT / "tests" / "linux" / "deploy_remote_test.sh").read_bytes()
                       .replace(bytes([13, 10]), bytes([10])))
    environment = dict(os.environ, MSYS_NO_PATHCONV="1")
    result = subprocess.run(runner + [convert(script), convert(material)], capture_output=True,
                            timeout=600, env=environment)
    output = result.stdout.decode("utf-8", "replace").replace("\0", "")
    assert result.returncode == 0, output[-4000:]
    assert "RESULT pass=27 fail=0" in output
