"""Compile and execute the glove module switches and the dummy module's portable parts."""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _compiler():
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("A native C++ compiler is required for the firmware glove module check")
    return compiler


def _build(compiler, defines, executable):
    return subprocess.run([
        compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", *defines,
        "-I", str(ROOT / "firmware/esp32/include"),
        str(ROOT / "firmware/esp32/test/host_glove.cpp"), "-o", str(executable),
    ], capture_output=True, text=True)


@pytest.mark.parametrize("name,defines,summary", [
    ("dummy", [], "module=dummy gated=0 moving_ms=3000 idle_ms=0"),
    ("dummy-gated", ["-DCOMMS_DUMMY_IDLE_MS=7000"],
     "module=dummy gated=1 moving_ms=3000 idle_ms=7000"),
    ("dummy-gated-explicit", ["-DCOMMS_DUMMY_MOVING_MS=3000", "-DCOMMS_DUMMY_IDLE_MS=7000"],
     "module=dummy gated=1 moving_ms=3000 idle_ms=7000"),
    ("dummy-moving-only", ["-DCOMMS_DUMMY_MOVING_MS=1500"],
     "module=dummy gated=0 moving_ms=1500 idle_ms=0"),
    ("external", ["-DCOMMS_GLOVE_MODULE=1"], "module=external gated=1 moving_ms=3000 idle_ms=0"),
])
def test_dummy_cycle_fixture_round_trip_and_gated_flag(tmp_path, name, defines, summary):
    compiler = _compiler()
    executable = tmp_path / "comms-glove-{}-test.exe".format(name)
    built = _build(compiler, defines, executable)
    assert built.returncode == 0, built.stdout + built.stderr
    result = subprocess.run([str(executable)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "PASS glove " + summary


@pytest.mark.parametrize("defines", [
    ["-DCOMMS_GLOVE_MODULE=2"],
    ["-DCOMMS_DUMMY_IDLE_MS=-1"],
    ["-DCOMMS_DUMMY_MOVING_MS=4294967296"],
])
def test_invalid_glove_switches_do_not_compile(tmp_path, defines):
    compiler = _compiler()
    built = _build(compiler, defines, tmp_path / "comms-glove-invalid.exe")
    assert built.returncode != 0
    assert "static assertion failed" in built.stderr or "static_assert" in built.stderr
