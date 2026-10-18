"""Compile and execute the portable sensor scaling and tick policy under both policies."""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("policy,defines", [
    ("clamp", []),
    ("drop", ["-DCOMMS_OUT_OF_RANGE_DROP=1"]),
])
def test_sample_scaling_rounding_limits_and_refusals(tmp_path, policy, defines):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("A native C++ compiler is required for the firmware sample scaling check")
    executable = tmp_path / "comms-sample-{}-test.exe".format(policy)
    built = subprocess.run([
        compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", *defines,
        "-I", str(ROOT / "firmware/esp32/include"),
        str(ROOT / "firmware/esp32/test/host_sample.cpp"), "-o", str(executable),
    ], capture_output=True, text=True)
    assert built.returncode == 0, built.stdout + built.stderr
    result = subprocess.run([str(executable)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == (
        "PASS sample scaling, rounding, limits, refusals and tick outcomes ({} policy)".format(policy))
