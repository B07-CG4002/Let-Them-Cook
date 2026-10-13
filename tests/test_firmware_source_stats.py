"""Compile and execute the portable source-statistics helper used by firmware."""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_source_statistics_accounting_and_wire_format(tmp_path):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("A native C++ compiler is required for the firmware source-statistics check")

    executable = tmp_path / "comms-source-stats-test.exe"
    subprocess.run(
        [
            compiler,
            "-std=c++11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            str(ROOT / "firmware/esp32/include"),
            str(ROOT / "firmware/esp32/test/host_source_stats.cpp"),
            "-o",
            str(executable),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    completed = subprocess.run(
        [str(executable)], check=True, capture_output=True, text=True
    )
    assert completed.stdout.strip() == (
        "PASS source statistics accounting, serialization, wrap, and validation"
    )
