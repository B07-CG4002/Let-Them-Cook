"""Execute the exact portable event parser called by firmware's GATTS hook."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_non_write_union_and_malformed_mtu_control_requests(tmp_path):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("A native C++ compiler is required for the firmware event check")
    executable = tmp_path / "comms-events-test.exe"
    subprocess.run([
        compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror",
        "-I", str(ROOT / "firmware/esp32/include"),
        str(ROOT / "firmware/esp32/test/host_events.cpp"), "-o", str(executable),
    ], check=True, capture_output=True, text=True)
    completed = subprocess.run([str(executable)], check=True, capture_output=True, text=True)
    assert completed.stdout.strip() == "PASS GATT event/union and malformed-write regression"
