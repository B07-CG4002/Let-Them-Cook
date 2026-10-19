"""The runner launcher's decisions that need no root and no /proc (the rest runs in tests/linux)."""
import json
from pathlib import Path

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
