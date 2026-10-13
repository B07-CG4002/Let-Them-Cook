"""Flashing with one programming USB cable identifies each board afresh and fails fast."""
import importlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
LEFT = {"port": "COM4", "description": "USB serial", "hwid": "USB VID:PID=10C4:EA60 SER=LEFT"}
RIGHT = {"port": "COM7", "description": "USB serial", "hwid": "USB VID:PID=10C4:EA60 SER=RIGHT"}


def environment(monkeypatch, *, snapshots=None, answers=("", ""), fail=None):
    assert importlib.util.find_spec("flash") is not None, "Root flash.py is not implemented"
    module = importlib.import_module("flash")
    monkeypatch.setattr(module, "_pio", lambda: ["platformio"])
    identities = iter(['38:18:2B:19:82:AE', '38:18:2B:18:9D:6A'])
    monkeypatch.setattr(module, '_read_board_address', lambda port: next(identities))
    monkeypatch.setattr(module, 'save_boards', lambda *args: None)
    monkeypatch.setattr(module, 'load_boards', lambda: (('left','38:18:2B:19:82:AE'), ('right','38:18:2B:18:9D:6A')))
    snapshots = iter([[LEFT], [RIGHT]] if snapshots is None else snapshots)
    replies = iter(answers)
    commands, prompts = [], []

    def read(prompt):
        prompts.append((prompt, len(commands)))
        return next(replies)

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["cwd"] == module.ROOT and kwargs["check"] is True
        if fail is not None and fail(command):
            raise subprocess.CalledProcessError(7, command)
        if command[1:3] == ["device", "list"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(next(snapshots)))
        # Only public port metadata is captured. Uploads and PIN prompts inherit the console.
        assert not kwargs.get("capture_output")
        assert not any(key in kwargs for key in ("stdin", "stdout", "stderr", "input"))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("builtins.input", read)
    monkeypatch.setattr(module.subprocess, "run", run)
    return module, commands, prompts


def uploads(commands):
    return [command for command in commands if "upload" in command]


def test_one_programming_cable_builds_both_first_then_redetects_after_each_physical_prompt(monkeypatch):
    module, commands, prompts = environment(monkeypatch)
    assert module.main([]) == 0
    assert commands[0][1:] == ["-m", "tools.generate_dummy_fixtures"]
    assert commands[1][-2:] == ["-e", "firebeetle32-left"]
    assert commands[2][-2:] == ["-e", "firebeetle32-right"]
    assert "LEFT" in prompts[0][0] and "labels" in prompts[0][0].lower()
    assert "RIGHT" in prompts[1][0] and "disconnect" in prompts[1][0].lower()
    assert [count for _, count in prompts] == [3, 5]
    assert commands[3][1:3] == commands[5][1:3] == ["device", "list"]
    assert uploads(commands)[0][-5:] == ["firebeetle32-left", "-t", "upload", "--upload-port", "COM4"]
    assert uploads(commands)[1][-5:] == ["firebeetle32-right", "-t", "upload", "--upload-port", "COM7"]


def test_same_com_for_successive_boards_is_valid_without_additional_input(monkeypatch):
    module, commands, prompts = environment(monkeypatch, snapshots=[[LEFT], [{**RIGHT, "port": "COM4"}]])
    assert module.main([]) == 0
    assert len(prompts) == 2
    assert [command[-1] for command in uploads(commands)] == ["COM4", "COM4"]


def test_ambiguous_ports_require_explicit_selection_instead_of_enumeration_order(monkeypatch):
    module, commands, prompts = environment(monkeypatch, snapshots=[[LEFT, RIGHT], [LEFT, RIGHT]],
                                             answers=("", "com7", "", "COM4"))
    assert module.main([]) == 0
    assert len(prompts) == 4
    assert [command[-1] for command in uploads(commands)] == ["COM7", "COM4"]


@pytest.mark.parametrize("answer", ["COM9", "--erase", ""])
def test_ambiguous_invalid_selection_never_uploads(monkeypatch, answer):
    module, commands, _ = environment(monkeypatch, snapshots=[[LEFT, RIGHT]], answers=("", answer))
    assert module.main([]) != 0 and uploads(commands) == []
    assert len(commands) == 4


@pytest.mark.parametrize("answers, expected_uploads", [(("q",), 0), (("", "q"), 1)])
def test_cancel_stops_at_requested_board_without_claiming_completion(monkeypatch, capsys, answers, expected_uploads):
    module, commands, _ = environment(monkeypatch, answers=answers)
    assert module.main([]) != 0
    assert len(uploads(commands)) == expected_uploads
    assert "Both uploads succeeded" not in capsys.readouterr().out


def test_cancel_ambiguous_selection_does_not_upload(monkeypatch):
    module, commands, _ = environment(monkeypatch, snapshots=[[LEFT, RIGHT]], answers=("", "q"))
    assert module.main([]) != 0 and uploads(commands) == []


@pytest.mark.parametrize("bad_rows", [[], {}, [LEFT, LEFT], [{"port": "--erase"}]])
def test_missing_or_invalid_port_data_stops_without_upload(monkeypatch, bad_rows):
    module, commands, _ = environment(monkeypatch, snapshots=[bad_rows], answers=("",))
    assert module.main([]) != 0 and uploads(commands) == []


@pytest.mark.parametrize("failure", ["generate", "build", "detect", "upload"])
def test_command_failure_stops_subsequent_work_and_returns_real_exit_code(monkeypatch, failure, capsys):
    def fail(command):
        if failure == "generate":
            return "tools.generate_dummy_fixtures" in command
        if failure == "build":
            return "firebeetle32-right" in command and "upload" not in command
        if failure == "detect":
            return command[1:3] == ["device", "list"]
        return "upload" in command
    module, commands, prompts = environment(monkeypatch, fail=fail)
    assert module.main([]) == 7
    assert len(commands) == {"generate": 1, "build": 3, "detect": 4, "upload": 5}[failure]
    assert len(prompts) == (0 if failure in ("generate", "build") else 1)
    assert "Both uploads succeeded" not in capsys.readouterr().out


def test_ports_mode_only_lists_actual_metadata(monkeypatch, capsys):
    module, commands, prompts = environment(monkeypatch, snapshots=[[LEFT, RIGHT]])
    assert module.main(["--ports"]) == 0
    assert len(commands) == 1 and prompts == []
    output = capsys.readouterr().out
    assert "COM4" in output and "COM7" in output and "SER=RIGHT" in output


@pytest.mark.parametrize("selection, addresses", [
    ([], ["38:18:2B:19:82:AE", "38:18:2B:18:9D:6A"]),
    (["left"], ["38:18:2B:19:82:AE"]),
    (["right"], ["38:18:2B:18:9D:6A"]),
])
def test_pair_modes_reuse_authenticated_flow_without_platformio(monkeypatch, selection, addresses):
    module, commands, prompts = environment(monkeypatch)
    monkeypatch.setattr(module, "_pio", lambda: pytest.fail("Pairing should not require PlatformIO"))
    assert module.main(["--pair"] + selection) == 0
    assert [command[-1] for command in commands] == addresses
    assert all(command[1:4] == ["-m", "laptop.windows_pairing", "--address"] for command in commands)
    assert prompts == []


def test_pair_stops_on_first_failure(monkeypatch):
    module, commands, _ = environment(monkeypatch, fail=lambda command: True)
    assert module.main(["--pair"]) == 7 and len(commands) == 1


def test_monitor_inherits_terminal_without_log_capture(monkeypatch):
    module, commands, prompts = environment(monkeypatch)
    assert module.main(["--monitor", "com4"]) == 0
    assert commands == [["platformio", "device", "monitor", "--port", "COM4",
                         "--baud", "115200", "--no-reconnect"]]
    assert prompts == []


def test_invalid_monitor_port_does_not_start_serial(monkeypatch):
    module, commands, _ = environment(monkeypatch)
    assert module.main(["--monitor", "bad-port"]) != 0 and commands == []


def test_modes_are_mutually_exclusive_before_operations(monkeypatch):
    module, commands, _ = environment(monkeypatch)
    with pytest.raises(SystemExit) as stopped:
        module.main(["--ports", "--pair"])
    assert stopped.value.code != 0 and commands == []


def test_help_runs_outside_repository_without_hardware(tmp_path):
    assert (ROOT / "flash.py").is_file(), "Root flash.py is not implemented"
    result = subprocess.run([sys.executable, str(ROOT / "flash.py"), "--help"], cwd=tmp_path,
                            input="", text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert all(flag in result.stdout for flag in ("--ports", "--pair", "--monitor"))
