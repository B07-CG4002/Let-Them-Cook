import csv
import json

from common.sensor import DUMMY_FIXTURES, SCALES
from tools import export_capture


def _sensor(device, seq, values, boot=7):
    return {"type": "sensor", "device_id": device, "boot_id": boot, "seq": seq,
            "uptime_ms": 1000 + 20 * seq, "values": list(values), "version": 3,
            "direction": "ESP->laptop", "validation": "decoded"}


def _capture(tmp_path):
    folder = tmp_path / "B07-20261015T000000Z-test"
    folder.mkdir()
    lines = [json.dumps(_sensor(1, 0, DUMMY_FIXTURES[1])),
             json.dumps({"type": "sensor_invalid", "raw_hex": "00"}),
             json.dumps(_sensor(2, 5, DUMMY_FIXTURES[3])),
             json.dumps(_sensor(1, 0, DUMMY_FIXTURES[1])),  # the same sample twice
             json.dumps(_sensor(1, 1, DUMMY_FIXTURES[2])),
             '{"type": "sensor", "device_id"']  # torn last line of an interrupted capture
    (folder / "packets.jsonl").write_text("\n".join(lines), encoding="utf-8")
    return folder


def test_export_writes_one_physical_row_per_sample(tmp_path):
    folder = _capture(tmp_path)
    out = tmp_path / "samples.csv"
    assert export_capture.export(folder / "packets.jsonl", out) == 3
    rows = list(csv.reader(out.open(encoding="utf-8")))
    assert rows[0] == ["device_id", "glove", "boot_id", "seq", "uptime_ms", "ax", "ay", "az",
                       "gx", "gy", "gz", "yaw", "pitch", "roll", "flex1", "flex2"]
    assert [row[:5] for row in rows[1:]] == [["1", "left", "7", "0", "1000"],
                                             ["2", "right", "7", "5", "1100"],
                                             ["1", "left", "7", "1", "1020"]]
    expected = ["{:.4f}".format(value / scale) for value, scale in zip(DUMMY_FIXTURES[3], SCALES)]
    assert rows[2][5:] == expected


def test_device_filter_newest_capture_and_no_overwrite(tmp_path, monkeypatch, capsys):
    folder = _capture(tmp_path)
    monkeypatch.setattr(export_capture, "newest_capture", lambda: folder)
    assert export_capture.main(["--device", "1"]) == 0
    rows = list(csv.reader((folder / "samples.csv").open(encoding="utf-8")))
    assert [row[0] for row in rows[1:]] == ["1", "1"]
    assert "2 samples written" in capsys.readouterr().out
    assert export_capture.main([]) == 2  # samples.csv exists now
    assert "exists" in capsys.readouterr().err


def test_a_capture_with_a_bad_sample_fails_clearly(tmp_path, capsys):
    folder = tmp_path / "B07-bad"
    folder.mkdir()
    bad = _sensor(1, 0, DUMMY_FIXTURES[0])
    bad["values"] = bad["values"][:8]  # a version-2 era record
    (folder / "packets.jsonl").write_text(json.dumps(bad), encoding="utf-8")
    assert export_capture.main([str(folder), "--out", str(tmp_path / "x.csv")]) == 2
    assert "FAILED" in capsys.readouterr().err
