"""Compile and execute the portable source-statistics helper used by firmware."""

from pathlib import Path
import shutil
import struct
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
    gated_hex, verdict = completed.stdout.splitlines()
    assert verdict == (
        "PASS source statistics accounting, serialization, wrap, and validation"
    )
    # The 36-byte layout of the specification, unpacked independently of the
    # laptop parser: marker, device ID, flags, reserved, then seven uint32.
    raw = bytes.fromhex(gated_hex)
    assert len(raw) == 36 and raw[6:8] == b"\0\0"
    assert struct.unpack("<4sBB2xIIIIIII", raw) == (
        b"LCS3", 1, 0x03, 0x78563412, 2, 1, 1, 0x04030201, 0x0D0C0B0A, 0x11223344)
