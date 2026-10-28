"""Export the glove samples of one capture as CSV in the agreed physical units.

A capture (python demo.py run or live) records every sensor packet the laptop
received in packets.jsonl. This writes one CSV row per sample:
device_id, glove, boot_id, seq, uptime_ms, then ax ay az (g), gx gy gz (deg/s),
yaw pitch roll (deg), flex1 flex2 (0..1), restored as int16 / scale. A capture of
the three-flex boards (sensor version 4) adds a flex3 column. Use it to record
data through the real chain once the gloves run the Communications firmware
(their Bluetooth Classic recording scripts can no longer connect).

    python -m tools.export_capture                      # newest capture -> samples.csv beside it
    python -m tools.export_capture <capture folder or packets.jsonl> --out data.csv --device 2
"""
import argparse
import csv
import json
import os
from pathlib import Path
import sys
import tempfile

from common import sensor, sensor_v4

ROOT = Path(__file__).resolve().parents[1]
GLOVES = {1: "left", 2: "right"}
# Version 3 packets carry 11 values; version 4, the three-flex boards, 12 (flex3 added).
CODECS = {sensor.VERSION: sensor, sensor_v4.VERSION: sensor_v4}


def _evidence_path(source):
    path = Path(source)
    if path.is_dir():
        path = path / "packets.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"no packets.jsonl at {path}")
    return path


def newest_capture(output_root=ROOT / ".comms-local"):
    folders = [path for path in Path(output_root).glob("B07-*") if path.is_dir()]
    if not folders:
        raise FileNotFoundError(f"No B07 captures in {output_root}; run a capture first.")
    return max(folders, key=lambda path: (path.stat().st_mtime_ns, path.name))


def capture_version(path):
    """The capture's sensor version: what its report.json names, else its first sample's, else 3.

    A report that names two versions, or an invalid one, refuses the capture (ValueError),
    as python demo.py report does.
    """
    from tools.video_evidence import report_sensor_version
    report = Path(path).with_name("report.json")
    try:
        loaded = json.loads(report.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        loaded = None  # no report beside it, or one that cannot be read
    # None: no readable report, or one from before the option.
    named = report_sensor_version(loaded) if isinstance(loaded, dict) else None
    if named is not None:
        return named
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("type") == "sensor":
                return record.get("version", 3)
    return 3


def samples(path, device=None, version=3):
    """Yield (device_id, boot_id, seq, uptime_ms, floats) once per sample, in arrival order.

    Every sample must have the capture's version: a capture never mixes versions.
    """
    if version not in CODECS:
        raise ValueError("sensor version {!r} is not 3 or 4".format(version))
    codec = CODECS[version]
    seen = set()
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except ValueError:
                continue  # a torn last line of an interrupted capture
            if not isinstance(record, dict) or record.get("type") != "sensor":
                continue
            # The codec checks the version, identity ranges and the 11 or 12 int16 values.
            record_version = record.get("version", 3)
            if record_version != version:
                raise ValueError("a version {!r} sample in a version {} capture".format(
                    record_version, version))
            packet = codec.SensorPacket(record["device_id"], record["boot_id"], record["seq"],
                                        record["uptime_ms"], record["values"], record_version)
            if device is not None and packet.device_id != device:
                continue
            key = (packet.device_id, packet.boot_id, packet.seq)
            if key in seen:
                continue
            seen.add(key)
            yield (packet.device_id, packet.boot_id, packet.seq, packet.uptime_ms,
                   codec.physical_values(packet.values))


def export(source, out, device=None):
    """Write the CSV and return its row count. The rows go to a temporary file beside
    out, renamed to out only when every record was read, so a refused capture leaves
    no partial CSV behind."""
    version = capture_version(source)
    if version not in CODECS:
        raise ValueError("sensor version {!r} is not 3 or 4".format(version))
    out = Path(out)
    rows = 0
    partial = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=out.parent,
                                          prefix=out.name + ".", suffix=".partial", delete=False)
    try:
        with partial as stream:
            writer = csv.writer(stream)
            writer.writerow(["device_id", "glove", "boot_id", "seq", "uptime_ms",
                             *CODECS[version].CHANNELS])
            for device_id, boot_id, seq, uptime_ms, values in samples(source, device, version):
                writer.writerow([device_id, GLOVES[device_id], boot_id, seq, uptime_ms,
                                 *("{:.4f}".format(value) for value in values)])
                rows += 1
        os.replace(partial.name, out)
    except BaseException:
        Path(partial.name).unlink(missing_ok=True)
        raise
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("capture", nargs="?", help="capture folder or packets.jsonl (default: newest)")
    parser.add_argument("--out", help="CSV file to write (default: samples.csv beside the evidence)")
    parser.add_argument("--device", type=int, choices=(1, 2), help="only the left (1) or right (2) glove")
    args = parser.parse_args(argv)
    try:
        source = _evidence_path(args.capture if args.capture else newest_capture())
        out = Path(args.out) if args.out else source.with_name("samples.csv")
        if out.exists():
            raise FileExistsError(f"{out} exists; choose another --out")
        rows = export(source, out, args.device)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    print(f"{rows} samples written to {out}")
    report = source.with_name("report.json")
    try:
        dropped = json.loads(report.read_text(encoding="utf-8"))["evidence"]["dropped"]
    except (OSError, ValueError, KeyError, TypeError):
        dropped = None
    if dropped:
        print(f"Warning: the capture dropped {dropped} evidence records under load; "
              "those samples are missing from the CSV.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
