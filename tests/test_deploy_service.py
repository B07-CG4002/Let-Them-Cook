import hashlib
import io
import json
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
OLD_SERVICE_COMMIT = "5a004c6"


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


def test_runner_options_need_the_bridge_and_absolute_board_paths():
    runner = dict(model="ultra96.model_bridge", runner="/home/xilinx/w/runner.py",
                  runner_python=deploy_service.PYNQ_PYTHON, runner_arg=["--bit", "a.bit"])
    assert deploy_service.options_from(_args(**runner))["runner"] == dict(
        script="/home/xilinx/w/runner.py", python=deploy_service.PYNQ_PYTHON, args=["--bit", "a.bit"])
    assert "runner" not in deploy_service.options_from(_args(model="ultra96.model_bridge"))
    for bad in (dict(model=None), dict(model="wen_model"), dict(runner="runner.py"),
                dict(runner_python="python3"), dict(runner_arg=["a\nb"])):
        with pytest.raises(ValueError):
            deploy_service.options_from(_args(**dict(runner, **bad)))


def test_a_runner_module_runs_from_its_board_folder():
    runner = dict(model="ultra96.model_bridge", runner_module="src.socket_worker",
                  runner_cwd="/tmp/cnn_conv1d/ai_integration", runner_python=deploy_service.PYNQ_PYTHON,
                  runner_arg=["--weights", "fpga/hls/w.npz"])
    assert deploy_service.options_from(_args(**runner))["runner"] == dict(
        module="src.socket_worker", cwd="/tmp/cnn_conv1d/ai_integration",
        python=deploy_service.PYNQ_PYTHON, args=["--weights", "fpga/hls/w.npz"])
    for bad in (dict(runner_cwd=None), dict(runner_cwd="tmp/ai"), dict(runner_module="src/socket_worker.py"),
                dict(runner_module="src..x"), dict(runner_module="-m src.x"), dict(model="wen_model"),
                dict(runner="/home/xilinx/w/runner.py")):
        with pytest.raises(ValueError):
            deploy_service.options_from(_args(**dict(runner, **bad)))
    with pytest.raises(ValueError, match="need --runner or --runner-module"):
        deploy_service.options_from(_args(model="ultra96.model_bridge", runner_cwd="/tmp/ai"))


def test_a_runner_script_may_run_in_another_folder():
    runner = dict(model="ultra96.model_bridge", runner="/home/xilinx/w/runner.py", runner_cwd="/home/xilinx",
                  runner_python=deploy_service.PYNQ_PYTHON, runner_arg=[])
    assert deploy_service.options_from(_args(**runner))["runner"]["cwd"] == "/home/xilinx"


def test_the_plan_shows_the_runner_module_command(monkeypatch, capsys):
    monkeypatch.setattr(deploy_service, "_connect", lambda *a: pytest.fail("connected"))
    assert deploy_service.main(["--model", "ultra96.model_bridge", "--runner-module", "src.socket_worker",
                                "--runner-cwd", "/tmp/cnn_conv1d/ai_integration",
                                "--runner-arg=--weights", "--runner-arg", "fpga/hls/w.npz", "--dry-run"]) == 0
    assert ("cd /tmp/cnn_conv1d/ai_integration && " + deploy_service.PYNQ_PYTHON
            + " -m src.socket_worker --weights fpga/hls/w.npz") in capsys.readouterr().out


def test_board_paths_that_git_bash_rewrote_are_refused():
    runner = dict(model="ultra96.model_bridge", runner="/home/xilinx/w/runner.py",
                  runner_python=deploy_service.PYNQ_PYTHON, runner_arg=[])
    for bad in (dict(runner="D:/Git/home/xilinx/w/runner.py"),
                dict(runner_python="C:/Program Files/Git/usr/local/share/pynq-venv/bin/python"),
                dict(runner_arg=["--socket", "D:/Git/var/tmp/x.sock"]),
                dict(runner_arg=["--socket=D:/Git/var/tmp/x.sock"])):
        with pytest.raises(ValueError, match="Git Bash"):
            deploy_service.options_from(_args(**dict(runner, **bad)))


def test_runner_arguments_that_start_with_a_dash_use_the_equals_form(monkeypatch, capsys):
    monkeypatch.setattr(deploy_service, "_connect", lambda *a: pytest.fail("connected"))
    assert deploy_service.main(["--model", "ultra96.model_bridge", "--runner", "/home/xilinx/r.py",
                                "--runner-arg=--bit", "--runner-arg", "a b.bit", "--dry-run"]) == 0
    assert "/home/xilinx/r.py --bit 'a b.bit'" in capsys.readouterr().out


class _Stream:
    def __init__(self, data=b"", code=0):
        self.data, self.written, self.channel = data, "", self
        self.code = code

    def read(self):
        return self.data

    def write(self, text):
        self.written += text

    def flush(self):
        pass

    def shutdown_write(self):
        self.closed = True

    def recv_exit_status(self):
        return self.code


class _Board:
    def __init__(self, record, code):
        self.stdin = _Stream()
        self.out = _Stream((json.dumps(record) + "\n").encode(), code)

    def open_sftp(self):
        return type("Sftp", (), dict(mkdir=lambda *a, **k: None, put=lambda *a: None,
                                    chmod=lambda *a: None, close=lambda self: None,
                                    open=lambda *a: io.StringIO()))()

    def exec_command(self, command, timeout):
        assert "runner" in command
        return self.stdin, self.out, _Stream()

    def close(self):
        pass


@pytest.mark.parametrize("code, runner, result, message", [
    (0, dict(status="running", pid=7, connected=True, log="/b/l"), 0, "Model runner running (PID 7)"),
    (3, dict(status="failed", error="runner script not found: /x"), 1, "the new model runner is not"),
    (3, dict(status="failed", previous="untouched", previous_running=41, error="sudo: incorrect password"),
     1, "previous --runner deploy (PID 41) is still running"),
    (3, dict(status="conflict", error="another model runner is connected"), 1, "another model runner"),
])
def test_deploy_hands_the_sudo_password_to_the_board_on_stdin(monkeypatch, tmp_path, capsys,
                                                              code, runner, result, message):
    board = _Board(dict(status="deployed", runner=runner), code)
    monkeypatch.setattr(deploy_service, "RECORDS", tmp_path)
    monkeypatch.setattr(deploy_service, "_connect",
                        lambda jump_user, board_user, want_sudo: (board, board, "sudo-pw"))
    assert deploy_service.main(["--model", "ultra96.model_bridge", "--runner", "/home/xilinx/r.py"]) == result
    assert board.stdin.written == "sudo-pw\n" and board.stdin.closed
    output = capsys.readouterr()
    assert message in output.out + output.err and "sudo-pw" not in output.out + output.err


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
    # The running service on the board is version-2 code; the team main 5a004c6 (the last
    # team commit before version 3, also in every clone of the team repository) has it.
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
    assert "RESULT pass=44 fail=0" in output


def test_ssh_errors_end_in_a_failed_line(monkeypatch, tmp_path, capsys):
    paramiko = pytest.importorskip("paramiko")

    def refuse(*_args):
        raise paramiko.AuthenticationException("Authentication failed.")
    monkeypatch.setattr(deploy_service, "_connect", refuse)
    assert deploy_service.main([]) == 2
    assert "FAILED: AuthenticationException: Authentication failed." in capsys.readouterr().err


def test_a_jump_host_without_password_login_points_at_the_vpn(monkeypatch, capsys):
    paramiko = pytest.importorskip("paramiko")

    def refuse(*_args):
        raise paramiko.BadAuthenticationType("Bad authentication type", ["publickey"])
    monkeypatch.setattr(deploy_service, "_connect", refuse)
    assert deploy_service.main([]) == 2
    err = capsys.readouterr().err
    assert "FAILED: BadAuthenticationType" in err
    assert "connect the NUS VPN" in err
