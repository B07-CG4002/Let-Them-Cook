"""The runner launcher's decisions that need no root and no /proc (the rest runs in tests/linux)."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from ultra96 import model_runner_launch as launcher


def test_a_runner_that_cannot_start_leaves_the_previous_one_untouched_and_names_it(tmp_path, monkeypatch,
                                                                                     capsys):
    pidfile = tmp_path / "model-runner.json"
    pidfile.write_text(json.dumps(dict(pid=4242, start_ticks="99")))
    monkeypatch.setattr(launcher, "alive", lambda pid, ticks: (pid, ticks) == (4242, "99"))
    monkeypatch.setattr(launcher, "stop_previous", lambda *a: (_ for _ in ()).throw(AssertionError))
    options = dict(script=str(tmp_path / "runnr.py"), python=str(tmp_path), args=[],
                   log=str(tmp_path / "runner.log"), pidfile=str(pidfile))
    assert launcher.main([json.dumps(options)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed" and "runner script not found" in result["error"]
    assert result["previous"] == "untouched" and result["previous_running"] == 4242


def test_the_recorded_runner_counts_only_while_its_start_time_matches(tmp_path, monkeypatch):
    pidfile = tmp_path / "model-runner.json"
    assert launcher.recorded_runner(pidfile) is None
    pidfile.write_text(json.dumps(dict(pid=7, start_ticks="5")))
    monkeypatch.setattr(launcher, "alive", lambda pid, ticks: ticks == "5")
    assert launcher.recorded_runner(pidfile) == 7
    pidfile.write_text(json.dumps(dict(pid=7, start_ticks="6")))
    assert launcher.recorded_runner(pidfile) is None
    pidfile.write_text("not json")
    assert launcher.recorded_runner(pidfile) is None


def test_the_launcher_reads_the_bridge_lines_it_waits_for(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("old line: " + launcher.REPLACED + "\n")
    offset = log.stat().st_size
    with open(log, "a") as stream:
        stream.write("INFO model runner connected (pid 12)\n")
    text = launcher._log_since(str(log), offset)
    assert "{} (pid 12)".format(launcher.CONNECTED) in text and launcher.REPLACED not in text
    assert launcher._log_since(str(tmp_path / "missing.log"), 0) == ""


def test_a_runner_module_is_found_in_its_folder_and_run_with_dash_m(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "socket_worker.py").write_text("")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__main__.py").write_text("")
    result = {}
    assert launcher.runner_command(dict(module="src.socket_worker", cwd=str(tmp_path)), result) == (
        ["-m", "src.socket_worker"], tmp_path)
    assert result == dict(module="src.socket_worker", cwd=str(tmp_path))
    assert launcher.runner_command(dict(module="pkg", cwd=str(tmp_path)), {})[0] == ["-m", "pkg"]
    for options, message in ((dict(module="src.missing", cwd=str(tmp_path)), "runner module not found"),
                             (dict(module="src.socket_worker", cwd=str(tmp_path / "no")), "runner folder not found")):
        try:
            launcher.runner_command(options, {})
        except FileNotFoundError as error:
            assert message in str(error)
        else:
            raise AssertionError(options)


def test_a_runner_script_runs_in_its_own_folder_unless_told_otherwise(tmp_path):
    script = tmp_path / "runner.py"
    script.write_text("")
    assert launcher.runner_command(dict(script=str(script)), {}) == ([str(script)], tmp_path)
    assert launcher.runner_command(dict(script=str(script), cwd="/"), {})[1] == Path("/")


# --- --replace-runner: a runner started by hand, named by PID and start ticks ----------------

class Processes:
    """A fake /proc: pid -> dict(ticks, argv, state, ignores=signals, reused_by=process)."""

    def __init__(self):
        self.table = {}
        self.signals = []

    def stat(self, pid):
        process = self.table.get(pid)
        # The fields after the command name: the state first, the start ticks at index 19.
        return None if process is None else [process.get("state", "S")] + ["0"] * 18 + [process["ticks"]]

    def cmdline(self, pid):
        process = self.table.get(pid)
        return None if process is None else list(process["argv"])

    def signal(self, pid, sig):
        self.signals.append((pid, sig))
        process = self.table.get(pid)
        if process is None:
            raise ProcessLookupError(pid)
        if sig in process.get("ignores", ()):
            return
        if process.get("reused_by") is None:
            del self.table[pid]
        else:  # it exits, and another process gets its PID
            self.table[pid] = process["reused_by"]


SIGTERM, SIGKILL = 15, 9
RUNNER = ["/usr/local/share/pynq-venv/bin/python", "-m", "src.socket_worker", "--socket", "/x.sock"]


@pytest.fixture
def processes(monkeypatch):
    fake = Processes()
    monkeypatch.setattr(launcher, "_stat", fake.stat)
    monkeypatch.setattr(launcher, "cmdline", fake.cmdline)
    monkeypatch.setattr(launcher, "_signal", fake.signal)
    # Windows has no SIGKILL; the launcher itself runs only on the board.
    monkeypatch.setattr(launcher, "signal", SimpleNamespace(SIGTERM=SIGTERM, SIGKILL=SIGKILL))
    return fake


def test_a_hand_started_runner_gets_sigterm_on_its_own_pid_only(processes):
    processes.table[177087] = dict(ticks="611890431", argv=RUNNER)
    report = launcher.replace_runner(177087, "611890431", wait=0.3)
    assert report == dict(pid=177087, start_ticks="611890431", argv=RUNNER, stop="sigterm")
    assert processes.signals == [(177087, SIGTERM)]


def test_a_runner_that_ignores_sigterm_gets_sigkill_while_it_is_still_the_same_process(processes):
    processes.table[42] = dict(ticks="7", argv=RUNNER, ignores={SIGTERM})
    assert launcher.replace_runner(42, "7", wait=0.3)["stop"] == "sigkill"
    assert processes.signals == [(42, SIGTERM), (42, SIGKILL)]


def test_a_pid_reused_during_the_wait_never_gets_sigkill(processes):
    other = dict(ticks="8", argv=["/usr/bin/python3", "-m", "src.socket_worker"])
    processes.table[42] = dict(ticks="7", argv=RUNNER, reused_by=other)
    assert launcher.replace_runner(42, "7", wait=0.3)["stop"] == "sigterm"
    assert processes.signals == [(42, SIGTERM)] and processes.table[42] is other


def test_a_runner_that_survives_sigkill_is_an_error(processes):
    processes.table[42] = dict(ticks="7", argv=RUNNER, ignores={SIGTERM, SIGKILL})
    with pytest.raises(RuntimeError, match="did not exit after SIGKILL"):
        launcher.replace_runner(42, "7", wait=0.2)


@pytest.mark.parametrize("table", [{}, {42: dict(ticks="8", argv=RUNNER)},
                                   {42: dict(ticks="7", argv=RUNNER, state="Z")}])
def test_a_gone_reused_or_zombie_pid_is_already_gone_and_never_signalled(processes, table):
    processes.table.update(table)
    assert launcher.replace_runner(42, "7", wait=0.2) == dict(pid=42, start_ticks="7", argv=None,
                                                              stop="already_gone")
    launcher.check_replacements([(42, "7")])
    assert processes.signals == []


@pytest.mark.parametrize("pid, argv, reason", [
    (1, RUNNER, "PID 1 is init"),
    (os.getpid(), RUNNER, "this launcher"),
    (42, ["/usr/bin/sudo", "-E", "python", "-m", "src.socket_worker"], "not a Python program"),
    (42, [], "not a Python program"),
    (42, ["/usr/bin/socket_worker", "--socket", "/x.sock"], "not a Python program"),
    (42, ["/usr/bin/python3", "-u", "-m", "ultra96.server", "--model-path", "/x/model_runner"],
     "an ultra96.server"),
    (42, ["/usr/bin/python3", "/home/xilinx/w/ultra96/server.py", "--socket_worker"], "an ultra96.server"),
    (42, ["/usr/bin/python3", "-m", "http.server", "8888"], "no argument names"),
    (42, ["/usr/bin/python3_socket_worker"], "no argument names"),
    # The Comms adapter runner is gone (Wen Ree's worker delivers); its name is no longer a marker.
    (42, ["/usr/bin/python3", "-m", "ultra96.hand_runner"], "no argument names socket_worker, model_runner"),
])
def test_a_process_that_is_not_a_python_model_runner_is_refused_and_never_signalled(
        processes, pid, argv, reason):
    processes.table[pid] = dict(ticks="7", argv=argv)
    with pytest.raises(RuntimeError, match=reason):
        launcher.check_replacements([(pid, "7")])
    with pytest.raises(RuntimeError, match=reason):
        launcher.replace_runner(pid, "7", wait=0.2)
    assert processes.signals == []


@pytest.mark.parametrize("argv", [
    RUNNER, ["python3", "-m", "src.socket_worker_full"], ["python", "-m", "src.socket_worker_hand"],
    ["/usr/bin/python3.10", "/home/xilinx/w/ultra96/model_runner_example.py", "--every", "1"],
])
def test_python_model_runners_may_be_replaced(argv):
    assert launcher.not_a_runner(42, argv) is None


@pytest.mark.parametrize("entry", [
    dict(pid=0, start_ticks="1"), dict(pid=-3, start_ticks="1"), dict(pid=True, start_ticks="1"),
    dict(pid="42", start_ticks="1"), dict(pid=42, start_ticks=1), dict(pid=42, start_ticks="1a"),
    dict(pid=42, start_ticks=""), dict(pid=42, start_ticks=chr(0x661)), dict(pid=42), "42:1",
    dict(pid=42, start_ticks="1", argv=[]),
])
def test_replace_entries_are_a_positive_pid_and_decimal_start_ticks(entry):
    with pytest.raises(ValueError):
        launcher.replacement(entry)


def test_replace_entries_keep_the_start_ticks_as_proc_writes_them():
    assert launcher.replacement(dict(pid=42, start_ticks="0611")) == (42, "611")


def _launch_options(tmp_path, replace):
    script = tmp_path / "runner.py"
    script.write_text("")
    return dict(script=str(script), python=sys.executable, args=[], log=str(tmp_path / "runner.log"),
                pidfile=str(tmp_path / "model-runner.json"), settle=0, replace=replace)


def test_the_launcher_stops_its_own_runner_then_the_named_ones_then_starts_the_new_one(
        tmp_path, monkeypatch, processes, capsys):
    order = []
    processes.table[177087] = dict(ticks="611890431", argv=RUNNER)
    signal_process = processes.signal
    monkeypatch.setattr(launcher, "_signal",
                        lambda pid, sig: order.append(("signal", pid)) or signal_process(pid, sig))
    monkeypatch.setattr(launcher, "stop_previous", lambda pidfile: order.append("stop_previous") or "sigterm")

    def start(argv, **kwargs):
        order.append("start")
        return SimpleNamespace(pid=5000, poll=lambda: None)
    monkeypatch.setattr(launcher.subprocess, "Popen", start)
    options = _launch_options(tmp_path, [dict(pid=177087, start_ticks="611890431"),
                                         dict(pid=4242, start_ticks="1")])
    assert launcher.main([json.dumps(options)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert order == ["stop_previous", ("signal", 177087), "start"]
    assert result["status"] == "running" and result["previous"] == "sigterm" and result["pid"] == 5000
    assert result["replaced"] == [
        dict(pid=177087, start_ticks="611890431", argv=RUNNER, stop="sigterm"),
        dict(pid=4242, start_ticks="1", argv=None, stop="already_gone")]


def test_a_refused_replacement_stops_nothing_and_starts_nothing(tmp_path, monkeypatch, processes, capsys):
    processes.table[177087] = dict(ticks="611890431", argv=RUNNER)
    processes.table[300] = dict(ticks="9", argv=["/usr/bin/python3", "-u", "-m", "ultra96.server"])
    monkeypatch.setattr(launcher, "stop_previous", lambda *a: pytest.fail("stopped the recorded runner"))
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("started a runner"))
    options = _launch_options(tmp_path, [dict(pid=177087, start_ticks="611890431"),
                                         dict(pid=300, start_ticks="9")])
    assert launcher.main([json.dumps(options)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed" and "--replace-runner 300:9 refused" in result["error"]
    assert result["previous"] == "untouched" and "replaced" not in result
    assert processes.signals == [] and 177087 in processes.table


def test_a_replacement_that_fails_after_the_recorded_runner_stopped_starts_nothing(
        tmp_path, monkeypatch, processes, capsys):
    processes.table[42] = dict(ticks="7", argv=RUNNER, ignores={SIGTERM, SIGKILL})
    monkeypatch.setattr(launcher, "stop_previous", lambda pidfile: "not_running")
    monkeypatch.setattr(launcher, "_wait_gone", lambda pid, ticks, seconds: False)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("started a runner"))
    assert launcher.main([json.dumps(_launch_options(tmp_path, [dict(pid=42, start_ticks="7")]))]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed" and "did not exit after SIGKILL" in result["error"]
    assert result["previous"] == "not_running"
    assert result["replaced"] == [dict(pid=42, start_ticks="7", stop="failed")]


def test_without_replace_entries_the_launcher_reports_no_replaced_list(tmp_path, monkeypatch, processes,
                                                                       capsys):
    monkeypatch.setattr(launcher, "stop_previous", lambda pidfile: "none")
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=7, poll=lambda: None))
    options = _launch_options(tmp_path, [])
    del options["replace"]
    assert launcher.main([json.dumps(options)]) == 0
    assert "replaced" not in json.loads(capsys.readouterr().out)


def test_the_launcher_stays_standard_library_only():
    source = Path(launcher.__file__).read_text(encoding="utf-8")
    imports = [line.split()[1] for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert imports == ["json", "os", "pathlib", "re", "signal", "subprocess", "sys", "time"]
