"""Offline packet examples validate one explicit capture without phone-count entry."""
import importlib
import importlib.util
import json
import struct

import pytest


def api():
    assert importlib.util.find_spec("tools.video_evidence") is not None, "video evidence helpers are missing"
    return importlib.import_module("tools.video_evidence")


def capture(tmp_path, name="capture", *, completed=(0, 0)):
    folder = tmp_path / name
    folder.mkdir()
    devices = {}
    for device, count, commands in ((1, 3, completed[0]), (2, 4, completed[1])):
        devices[str(device)] = {
            "clean": True, "protected_ble": True, "unfinished": False,
            "input": "physical", "received": count, "sent": count, "acked": count,
            "source": {"device_id": device, "boot_id": 100 + device,
                       "complete": True, "clean": True, "generated": count,
                       "source_submitted": count, "received": count, "acked": count,
                       "missing_received": 0, "missing_acked": 0},
            "commands": {"accepted": commands, "rejected": 0, "completed": commands,
                         "failed": 0, "pending": 0},
        }
    report = {"clean": True, "mock_input": False, "report_saved": True,
              "session_id": "video-session", "devices": devices,
              "run": {"mode": "physical", "session_id": "video-session"}}
    write_json(folder / "report.json", report)
    (folder / "exit-code.txt").write_text("0\n", encoding="utf-8")
    return folder


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def sensor(device, *, boot=None, seq=7):
    boot = 100 + device if boot is None else boot
    values = [32767, -32768, -1, 0, 1, 123, -456, 789]
    return {"timestamp_utc": "2026-09-29T00:00:00+00:00", "monotonic_seconds": 1.0,
            "type": "sensor", "device_id": device, "direction": "ESP->laptop",
            "version": 2, "boot_id": boot, "seq": seq, "uptime_ms": 100,
            "values": values, "raw_hex": struct.pack("<2sBBIII8h", b"LC", 2, device,
                boot, seq, 100, *values).hex(), "validation": "decoded",
            "received_monotonic": 1.0}


def ack(device, *, boot=None, seq=7):
    return {"timestamp_utc": "2026-09-29T00:00:00.1+00:00", "monotonic_seconds": 1.1,
            "type": "sensor_ack", "device_id": device, "direction": "Ultra96->laptop",
            "seq": seq, "boot_id": 100 + device if boot is None else boot,
            "validation": "accepted"}


def packets(folder, rows):
    (folder / "packets.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_packet_examples_pair_exact_boot_and_sequence_and_show_all_values(tmp_path, capsys):
    folder = capture(tmp_path)
    packets(folder, [sensor(1, boot=99), ack(1), sensor(2), sensor(1), ack(2)])
    assert api().show_packet_examples(folder) is True
    output = capsys.readouterr().out
    assert str(folder.resolve()) in output
    assert "32767, -32768, -1, 0, 1, 123, -456, 789" in output
    assert '"device_id": 1' in output and '"device_id": 2' in output
    assert '"boot_id": 99' not in output
    assert "sensor_ack" in output and "accepted" in output
    assert "video-session" in output and "report.json" in output
    assert "request_id" in output and "null" in output


@pytest.mark.parametrize("change", ["wrong_boot", "wrong_seq", "wrong_device"])
def test_packet_examples_do_not_match_partial_identity(tmp_path, capsys, change):
    folder = capture(tmp_path)
    wrong = ack(1)
    wrong[{"wrong_boot": "boot_id", "wrong_seq": "seq", "wrong_device": "device_id"}[change]] += 1
    packets(folder, [sensor(1), wrong, sensor(2), ack(2)])
    assert api().show_packet_examples(folder) is False
    assert "NOT PASSED" in capsys.readouterr().out


@pytest.mark.parametrize("extra", [
    {"session_id": "another-session"}, {"request_id": 7}, {"version": 1},
])
def test_packet_examples_reject_conflicting_extended_ack_identity(tmp_path, capsys, extra):
    folder = capture(tmp_path)
    packets(folder, [sensor(1), dict(ack(1), **extra), sensor(2), ack(2)])
    assert api().show_packet_examples(folder) is False
    assert "NOT PASSED" in capsys.readouterr().out


@pytest.mark.parametrize("tail", ["{broken\n", "[]\n", '{"type":"sensor","device_id":1}\n',
                                    '{"type":"sensor_ack","device_id":1,"device_id":2}\n'])
def test_packet_examples_reject_corruption_even_after_both_examples(tmp_path, capsys, tail):
    folder = capture(tmp_path)
    packets(folder, [sensor(1), ack(1), sensor(2), ack(2)])
    with (folder / "packets.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(tail)
    assert api().show_packet_examples(folder) is False
    assert "NOT PASSED" in capsys.readouterr().out


def test_packet_examples_never_fill_a_missing_device_from_another_capture(tmp_path, capsys):
    requested, other = capture(tmp_path, "requested"), capture(tmp_path, "other")
    packets(requested, [sensor(1), ack(1)])
    packets(other, [sensor(2), ack(2)])
    assert api().show_packet_examples(requested) is False
    assert "NOT PASSED" in capsys.readouterr().out


def test_packet_examples_reject_raw_and_decoded_value_disagreement(tmp_path, capsys):
    folder = capture(tmp_path)
    row = sensor(1)
    row["values"][7] = 800
    packets(folder, [row, ack(1), sensor(2), ack(2)])
    assert api().show_packet_examples(folder) is False
    assert "NOT PASSED" in capsys.readouterr().out


def test_packet_examples_need_a_session_context_and_both_files(tmp_path, capsys):
    folder = capture(tmp_path)
    packets(folder, [sensor(1), ack(1), sensor(2), ack(2)])
    write_json(folder / "report.json", {"session_id": ""})
    assert api().show_packet_examples(folder) is False
    assert "NOT PASSED" in capsys.readouterr().out


def test_packet_examples_reject_overflowing_json_number_in_metadata(tmp_path, capsys):
    folder = capture(tmp_path)
    packets(folder, [sensor(1), ack(1), sensor(2), ack(2)])
    with (folder / "packets.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('{"type":"BLE_disconnected","monotonic_seconds":1e999}\n')
    assert api().show_packet_examples(folder) is False
    assert "NOT PASSED" in capsys.readouterr().out
