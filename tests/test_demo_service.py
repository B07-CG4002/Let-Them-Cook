"""Exercise service controls locally; never contact SSH or real devices."""
import importlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess

import pytest


@pytest.fixture
def service(monkeypatch, tmp_path):
    assert importlib.util.find_spec("tools.demo_service") is not None, "Service helper is missing"
    module = importlib.import_module("tools.demo_service")
    # Never depend on this machine's real deployment records.
    monkeypatch.setattr(module, "DEPLOYMENTS", tmp_path / "no-deployments")
    return module


def write_record(folder, stamp, record):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ("deployment-" + stamp + ".json")).write_text(json.dumps(record), encoding="utf-8")


def running(folder, *extra):
    return {"cwd": folder, "argv": ["/usr/bin/python3", "-u", "-m", "ultra96.server", *extra]}


def test_board_source_follows_the_newest_deployment_record(service, monkeypatch, tmp_path):
    records = tmp_path / "deployments"
    monkeypatch.setattr(service, "DEPLOYMENTS", records)
    assert service.board_source() == service.BOARD_SOURCE
    deployed = service.BOARD_ROOT + "/source-co-v3-20261015T010000Z"
    write_record(records, "20261015T010000Z", {"status": "deployed", "current": running(deployed)})
    assert service.board_source() == deployed
    rollback = service.BOARD_ROOT + "/rollback-co-20261015T020000Z"
    write_record(records, "20261015T020000Z", {"status": "rolled_back", "current": running(rollback)})
    assert service.board_source() == rollback
    # Nothing left running, unreadable, or outside the deployment root: use an older record.
    write_record(records, "20261015T030000Z", {"status": "failed_nothing_running"})
    (records / "deployment-20261015T040000Z.json").write_text("{", encoding="utf-8")
    write_record(records, "20261015T050000Z", {"status": "deployed", "current": running("/tmp/x")})
    assert service.board_source() == rollback
    assert "cd " + rollback in service.start_script("ltc-comms")
    assert "--model" not in launch_arguments(service.start_script("ltc-comms"))


def test_start_restarts_the_deployed_model_too(service, monkeypatch, tmp_path):
    records = tmp_path / "deployments"
    monkeypatch.setattr(service, "DEPLOYMENTS", records)
    folder = service.BOARD_ROOT + "/source-co-v3-20261016T010000Z"
    model_path = "/var/tmp/wen ree/model dir"
    write_record(records, "20261016T010000Z", {"status": "deployed", "current": running(
        folder, "--session-id", "ltc-comms", "--model", "wen_model.board", "--model-path", model_path)})
    arguments = launch_arguments(service.start_script("ltc-comms"))
    assert arguments[arguments.index("--model") + 1] == "wen_model.board"
    assert arguments[arguments.index("--model-path") + 1] == model_path
    # A record whose model cannot be a module name is not trusted: fall back to an older one.
    write_record(records, "20261016T020000Z", {"status": "deployed", "current": running(
        service.BOARD_ROOT + "/source-co-v3-20261016T020000Z", "--model", "x; rm -rf /")})
    assert service.newest_deployment() == (folder, "wen_model.board", model_path, 3)


def test_start_restarts_the_deployed_sensor_version(service, monkeypatch, tmp_path):
    records = tmp_path / "deployments"
    monkeypatch.setattr(service, "DEPLOYMENTS", records)
    v3 = service.BOARD_ROOT + "/source-co-v3-20261027T010000Z"
    write_record(records, "20261027T010000Z", {"status": "deployed", "current": running(
        v3, "--session-id", "ltc-comms", "--model", "ultra96.model_bridge")})
    arguments = launch_arguments(service.start_script("ltc-comms"))
    assert "--sensor-version" not in arguments and arguments[-2:] == ["--model", "ultra96.model_bridge"]
    v4 = service.BOARD_ROOT + "/source-co-v4-20261027T020000Z"
    write_record(records, "20261027T020000Z", {"status": "deployed", "current": running(
        v4, "--session-id", "ltc-comms", "--model", "ultra96.model_bridge", "--sensor-version", "4")})
    arguments = launch_arguments(service.start_script("ltc-comms"))
    assert arguments[-4:] == ["--model", "ultra96.model_bridge", "--sensor-version", "4"]
    assert service.newest_deployment() == (v4, "ultra96.model_bridge", None, 4)
    # Version 5 (the gloves with the wide gyro range) is restarted the same way.
    v5 = service.BOARD_ROOT + "/source-co-v5-20261029T020000Z"
    write_record(records, "20261029T020000Z", {"status": "deployed", "current": running(
        v5, "--session-id", "ltc-comms", "--model", "ultra96.model_bridge", "--sensor-version", "5")})
    arguments = launch_arguments(service.start_script("ltc-comms"))
    assert arguments[-4:] == ["--model", "ultra96.model_bridge", "--sensor-version", "5"]
    assert service.newest_deployment() == (v5, "ultra96.model_bridge", None, 5)
    # A record whose sensor version is not 3, 4 or 5 is not trusted: fall back to an older one.
    for stamp, bad in (("20261029T030000Z", "6"), ("20261029T040000Z", "5; reboot"),
                       ("20261029T050000Z", "2")):
        write_record(records, stamp, {"status": "deployed", "current": running(
            service.BOARD_ROOT + "/source-co-v5-" + stamp, "--sensor-version", bad)})
        assert service.newest_deployment() == (v5, "ultra96.model_bridge", None, 5)


@pytest.mark.parametrize("extra, version", [((), 3), (("--sensor-version", "4"), 4),
                                            (("--sensor-version", "5"), 5)])
def test_start_prints_the_sensor_version_it_restarts(service, monkeypatch, tmp_path, capsys, extra, version):
    records = tmp_path / "deployments"
    monkeypatch.setattr(service, "DEPLOYMENTS", records)
    write_record(records, "20261027T050000Z", {"status": "deployed", "current": running(
        service.BOARD_ROOT + "/source-co-v4-20261027T050000Z", *extra)})
    monkeypatch.setattr("builtins.input", lambda _: "q")
    assert service.main(start=True) == 130
    assert "Sensor protocol: v{} ".format(version) in capsys.readouterr().out


@pytest.fixture
def local_shell():
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("A local POSIX sh is required to exercise the remote script")
    probe = subprocess.run([shell, "-c", "exit 0"], capture_output=True, timeout=5)
    if probe.returncode:
        pytest.skip("The installed sh cannot execute a local POSIX script")
    return shell


def launch_arguments(script):
    launch, = [line for line in script.splitlines() if line.startswith("exec ")]
    return shlex.split(launch)


def test_start_script_launches_the_requested_session(service):
    arguments = launch_arguments(service.start_script("rehearsal-2"))
    assert arguments[arguments.index("--session-id") + 1] == "rehearsal-2"


def test_start_script_keeps_a_hostile_session_as_one_argument(service):
    session = "two words; quote'$(id)"
    arguments = launch_arguments(service.start_script(session))
    assert arguments[arguments.index("--session-id") + 1] == session
    assert arguments[-4:] == ["--ingest-port", "8888", "--gateway-port", "9999"]


@pytest.mark.parametrize("session", ["", "x" * 129, "two\nlines", None])
def test_start_script_rejects_an_invalid_session(service, session):
    with pytest.raises(ValueError):
        service.start_script(session)


@pytest.mark.parametrize("environment, extra, expected", [
    (None, [], "ltc-comms"),
    ("env-session", [], "env-session"),
    ("env-session", ["--session-id", "cli-session"], "cli-session"),
])
def test_service_start_uses_the_same_session_rule_as_capture(service, monkeypatch, capsys,
                                                            environment, extra, expected):
    import demo

    if environment is None:
        monkeypatch.delenv("LTC_COMMS_SESSION", raising=False)
    else:
        monkeypatch.setenv("LTC_COMMS_SESSION", environment)
    scripts = []

    def run(command, **kwargs):
        scripts.append(shlex.split(command[-1])[2])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(service.subprocess, "run", run)
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert demo.main(["service", "--start"] + extra) == 0
    script, = scripts
    arguments = launch_arguments(script)
    assert arguments[arguments.index("--session-id") + 1] == expected
    assert "Session: " + expected in capsys.readouterr().out
    assert demo._parser().parse_args(["run"] + extra).session_id == expected


def run_start_guard(module, shell, monkeypatch, listeners="", ss_status=0):
    monkeypatch.setattr(module, "BOARD_SOURCE", "/")
    lines = module.start_script("ltc-comms").splitlines()
    launch_lines = [index for index, line in enumerate(lines) if line.startswith("exec ")]
    assert len(launch_lines) == 1, "Refuse to run an unrecognized server-launch script"
    launch = shlex.split(lines[launch_lines[0]])
    assert launch[:5] == ["exec", "/usr/bin/python3", "-u", "-m", "ultra96.server"]
    # Replace only the external listener query and final service launch.
    lines[launch_lines[0]] = "printf 'START_REACHED\\n'"
    fake_ss = """ss() {
  case "$1" in
    -ltnH) printf '%s' "$B07_TEST_LISTENERS"; return "$B07_TEST_SS_STATUS" ;;
    -ltnp) printf '%s' "$B07_TEST_LISTENERS" ;;
    *) return 98 ;;
  esac
}
"""
    environment = dict(os.environ, B07_TEST_LISTENERS=listeners, B07_TEST_SS_STATUS=str(ss_status))
    return subprocess.run([shell, "-c", fake_ss + "\n".join(lines)], env=environment,
                          text=True, capture_output=True, timeout=5)


def test_listener_query_failure_never_starts_service(service, local_shell, monkeypatch):
    result = run_start_guard(service, local_shell, monkeypatch, ss_status=1)
    assert "START_REACHED" not in result.stdout
    assert result.returncode != 0


@pytest.mark.parametrize("address", ["127.0.0.1:8888", "[::]:9999"])
def test_either_occupied_service_port_blocks_start(service, local_shell, monkeypatch, address):
    listeners = "LISTEN 0 128 127.0.0.1:22 0.0.0.0:*\n" + f"LISTEN 0 128 {address} *:*\n"
    result = run_start_guard(service, local_shell, monkeypatch, listeners)
    assert "START_REACHED" not in result.stdout
    assert result.returncode != 0


def test_free_service_ports_reach_start(service, local_shell, monkeypatch):
    listeners = "LISTEN 0 128 127.0.0.1:22 0.0.0.0:*\nLISTEN 0 128 [::]:18888 [::]:*\n"
    result = run_start_guard(service, local_shell, monkeypatch, listeners)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "START_REACHED"


@pytest.mark.parametrize("start", [False, True])
def test_service_retains_strict_interactive_ssh_and_returns_its_exit_code(service, monkeypatch, start):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(service.subprocess, "run", run)
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert service.main(start=start) == 7
    (command, kwargs), = calls
    options = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-o"]
    assert "StrictHostKeyChecking=yes" in options and "BatchMode=no" in options
    proxy = next(option for option in options if option.startswith("ProxyCommand="))
    assert "-o StrictHostKeyChecking=yes" in proxy and "-o BatchMode=no" in proxy
    assert "StrictHostKeyChecking=no" not in " ".join(command)
    assert "-N" not in command and "-T" not in command and "-L" not in command
    assert "-tt" in command
    assert command[-2] == "xilinx@makerslab-fpga-35.ddns.comp.nus.edu.sg"
    assert shlex.split(command[-1])[:2] == ["sh", "-c"]
    # Authentication prompts and foreground service output must use this terminal.
    assert not {"stdin", "stdout", "stderr", "capture_output", "creationflags", "shell"} & kwargs.keys()


def test_cancelled_start_does_not_contact_ssh(service, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _: "q")
    monkeypatch.setattr(service.subprocess, "run", lambda *args, **kwargs: pytest.fail("Cancelled start contacted SSH"))
    assert service.main(start=True) == 130
    assert service.BOARD_SOURCE in capsys.readouterr().out


def test_default_status_reads_ports_processes_and_working_directory(service, local_shell, monkeypatch):
    run = subprocess.run
    fake_queries = """ss() { [ "$1" = -ltnp ] || return 97; printf 'LISTENERS\\n'; }
id() { [ "$1" = -u ] || return 97; printf '1000\\n'; }
ps() { [ "$1 $2 $3 $4" = '-u 1000 -o pid=,args=' ] || return 97; printf '456 ultra96.server\\n'; }
pgrep() { [ "$1 $2 $3 $4" = '-u 1000 -f [u]ltra96.server' ] || return 97; printf '456\\n'; }
readlink() { [ "$1" = /proc/456/cwd ] || return 97; printf '/deployed/source\\n'; }
"""
    outputs = []

    def local_remote(command, **kwargs):
        remote = shlex.split(command[-1])
        assert remote[:2] == ["sh", "-c"]
        result = run([local_shell, "-c", fake_queries + remote[2]], capture_output=True,
                     text=True, timeout=5)
        outputs.append(result.stdout)
        return result

    monkeypatch.setattr(service.subprocess, "run", local_remote)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Read-only status requested start confirmation"))
    assert service.main() == 0
    output, = outputs
    assert "LISTENERS" in output and "456 ultra96.server" in output
    assert "Server PID 456, working directory: /deployed/source" in output
    assert "Model runner processes of any user, as PID:TICKS" in output


def fake_process(proc, pid, argv, ticks, name="python"):
    folder = proc / str(pid)
    folder.mkdir(parents=True)
    (folder / "cmdline").write_bytes(b"".join(part.encode() + bytes([0]) for part in argv))
    # Fields 3..21, then the start ticks (field 22), then more fields.
    fields = ["S"] + [str(number) for number in range(4, 22)] + [ticks, "4096", "17"]
    (folder / "stat").write_text("{} ({}) {}\n".format(pid, name, " ".join(fields)))


def test_status_lists_every_model_runner_process_as_pid_and_start_ticks(service, local_shell, tmp_path):
    proc = tmp_path / "proc dir"
    fake_process(proc, 177087, ["/usr/local/share/pynq-venv/bin/python", "-m", "src.socket_worker",
                                "--socket", "/var/tmp/x.sock"], "611890431", name="py (thon) x")
    fake_process(proc, 2001, ["sudo", "-E", "python3", "-m", "src.socket_worker_hand", "--socket",
                              "/var/tmp/x.sock"], "700")
    fake_process(proc, 3001, ["/usr/bin/python3", "-u", "-m", "ultra96.server"], "800")
    fake_process(proc, 4001, [], "900", name="kworker/0:1")
    (proc / "5001").mkdir()  # a process that ended while the list was read
    (proc / "self").mkdir()
    script = service.runner_processes_script(proc.as_posix())
    result = subprocess.run([local_shell, "-c", "set -eu\n" + script], capture_output=True, text=True,
                            timeout=10)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[1].startswith("Model runner processes of any user, as PID:TICKS")
    assert sorted(lines[2:]) == [
        "  177087:611890431 /usr/local/share/pynq-venv/bin/python -m src.socket_worker --socket /var/tmp/x.sock",
        "  2001:700 sudo -E python3 -m src.socket_worker_hand --socket /var/tmp/x.sock"]


def test_status_says_none_when_no_model_runner_runs_and_never_lists_itself(service, local_shell, tmp_path):
    proc = tmp_path / "proc"
    script = service.runner_processes_script(proc.as_posix())
    # The shell that runs the status script holds the whole script in its own command line.
    fake_process(proc, 6001, ["sh", "-c", script], "1000", name="sh")
    result = subprocess.run([local_shell, "-c", "set -eu\n" + script], capture_output=True, text=True,
                            timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[2:] == ["  none"]
    assert "socket_worker" not in script
    assert service.runner_processes_script().count("/proc/[0-9]*") == 1


def test_service_ssh_uses_the_given_jump_login(service, monkeypatch):
    calls = []
    monkeypatch.setattr(service.subprocess, "run",
                        lambda command, **kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0))
    assert service.main(jump="e0123456@stujump.comp.nus.edu.sg") == 0
    proxy = next(o for o in calls[0] if o.startswith("ProxyCommand="))
    assert "e0123456@stujump.comp.nus.edu.sg" in proxy and "yanjie" not in proxy
