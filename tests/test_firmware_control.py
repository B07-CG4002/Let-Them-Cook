"""Run the same bounded binary command/file state machine used on the ESP."""

from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_firmware_command_file_rate_and_validation(tmp_path):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("A native C++ compiler is required for firmware controls")
    executable = tmp_path / "b07-control-test.exe"
    built = subprocess.run([
        compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror",
        "-I", str(ROOT / "firmware/esp32/include"),
        str(ROOT / "firmware/esp32/test/host_control.cpp"), "-o", str(executable),
    ], capture_output=True, text=True)
    assert built.returncode == 0, built.stdout + built.stderr
    result = subprocess.run([str(executable)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "PASS B07 command, rate, bounded file and validation"
