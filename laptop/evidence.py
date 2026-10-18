"""Bounded background evidence writing; sampling never changes packet counters."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import sys
import threading
import time


class PacketEvidence:
    def __init__(self, path=None, *, capacity=4096, sample_every=10, stream=None, color=None):
        if capacity < 1 or sample_every < 1:
            raise ValueError("evidence capacity and sample interval must be positive")
        self.queue = queue.Queue(maxsize=capacity)
        self.sample_every = sample_every
        self.stream = stream if stream is not None else sys.stderr
        self.color = self.stream.isatty() if color is None else color
        self.json_file = self.text_file = None
        if path is not None:
            path = Path(path)
            self.json_file = path.open("x", encoding="utf-8")
            try:
                self.text_file = path.with_suffix(".log").open("x", encoding="utf-8")
            except BaseException:
                self.json_file.close()
                raise
        self._closing = threading.Event()
        self.dropped = self.written = self.write_errors = 0
        self._counts = {}
        self._thread = threading.Thread(target=self._write, name="packet-evidence", daemon=True)
        self._thread.start()

    def record(self, kind, **fields):
        item = dict(timestamp_utc=datetime.now(timezone.utc).isoformat(),
                    monotonic_seconds=time.monotonic(), type=kind, **fields)
        if self._closing.is_set():
            self.dropped += 1
            return False
        try:
            self.queue.put_nowait(item)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def _write(self):
        try:
            while not self._closing.is_set() or not self.queue.empty():
                try:
                    item = self.queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    serialized = json.dumps(item, sort_keys=True, ensure_ascii=True)
                    line = " ".join(f"{key}={value}" for key, value in item.items())
                    if self.json_file is not None:
                        self.json_file.write(serialized + "\n")
                        self.text_file.write(line + "\n")
                    device = item.get("device_id")
                    key = (device, item["type"])
                    count = self._counts.get(key, 0)
                    self._counts[key] = count + 1
                    if item["type"] not in ("sensor", "sensor_ack") or count % self.sample_every == 0:
                        prefix = "\x1b[36m" if device == 1 else "\x1b[35m"
                        print(prefix + line + "\x1b[0m" if self.color else line,
                              file=self.stream, flush=True)
                    self.written += 1
                except (OSError, ValueError):
                    self.write_errors += 1
                finally:
                    self.queue.task_done()
        finally:
            for handle in (self.json_file, self.text_file):
                if handle is not None:
                    try:
                        handle.close()
                    except OSError:
                        self.write_errors += 1

    def close(self, timeout=2.0):
        self._closing.set()
        self._thread.join(timeout)

    def summary(self):
        return {"written": self.written, "dropped": self.dropped,
                "write_errors": self.write_errors,
                "unfinished": self.queue.qsize() + int(self._thread.is_alive()),
                "console_sample_every": self.sample_every}
