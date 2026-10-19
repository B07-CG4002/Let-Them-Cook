"""Compile and execute the firmware's sensor send queue, then split its output with the laptop parser."""

from pathlib import Path
import shutil
import subprocess

import pytest

from common.sensor import MAX_PACKETS_PER_NOTIFICATION, PACKET_SIZE, decode_packet, split_notification


ROOT = Path(__file__).resolve().parents[1]


def test_send_queue_batches_and_accounting(tmp_path):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("A native C++ compiler is required for the firmware send-queue check")

    executable = tmp_path / "comms-send-queue-test.exe"
    subprocess.run(
        [
            compiler,
            "-std=c++11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            str(ROOT / "firmware/esp32/include"),
            str(ROOT / "firmware/esp32/test/host_send_queue.cpp"),
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
    notification_hex, verdict = completed.stdout.splitlines()
    assert verdict == "PASS send queue order, batching, overflow, discard and wrap"

    # The firmware's two-packet notification, cut by the laptop's parser.
    packets = [decode_packet(piece) for piece in split_notification(bytes.fromhex(notification_hex))]
    assert [(p.device_id, p.boot_id, p.seq, p.uptime_ms) for p in packets] == [
        (2, 0xA1B2C3D4, 41, 5000), (2, 0xA1B2C3D4, 42, 5020)]
    assert packets[1].values == tuple(5020 + channel for channel in range(11))


def test_laptop_and_firmware_agree_on_the_batch_limit():
    header = (ROOT / "firmware/esp32/include/comms_send_queue.h").read_text(encoding="utf-8")
    assert "constexpr size_t kMaxPacketsPerNotification = {};".format(
        MAX_PACKETS_PER_NOTIFICATION) in header
    assert PACKET_SIZE == 38
