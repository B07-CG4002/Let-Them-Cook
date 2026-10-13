"""Exercise service controls locally; never contact SSH or real devices."""
import importlib
import importlib.util
import os
import shlex
import shutil
import subprocess

import pytest


@pytest.fixture
def service():
    assert importlib.util.find_spec("tools.demo_service") is not None, "Service helper is missing"
    return importlib.import_module("tools.demo_service")


@pytest.fixture
def local_shell():
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("A local POSIX sh is required to exercise the remote script")
    probe = subprocess.run([shell, "-c", "exit 0"], capture_output=True, timeout=5)
    if probe.returncode:
        pytest.skip("The installed sh cannot execute a local POSIX script")
    return shell


def run_start_guard(module, shell, monkeypatch, listeners="", ss_status=0):
    monkeypatch.setattr(module, "BOARD_SOURCE", "/")
    lines = module.start_script().splitlines()
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
