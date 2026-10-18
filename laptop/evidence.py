"""Bounded background evidence writing; sampling never changes packet counters."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import sys
import threading
import time


class PacketEvidence:
    # Stream-only notices (status, log, finished) waiting for the writer thread.
    NOTICE_CAPACITY = 256

    def __init__(self, path=None, *, capacity=4096, sample_every=10, stream=None, color=None,
                 stream_all=False, text_log=True):
        if capacity < 1 or sample_every < 1:
            raise ValueError("evidence capacity and sample interval must be positive")
        self.queue = queue.Queue(maxsize=capacity)
        self.sample_every = sample_every
        self.stream = stream if stream is not None else sys.stderr
        # stream_all: every record goes to the stream as its JSON line, with no
        # sampling and no colour, followed by notify() notices. The stream is
        # best effort: a failed write stops the stream, never the evidence file.
        self.stream_all = bool(stream_all)
        self.color = False if self.stream_all else (
            self.stream.isatty() if color is None else color)
        self.json_file = self.text_file = None
        if path is not None:
            path = Path(path)
            self.json_file = path.open("x", encoding="utf-8")
            if text_log:
                try:
                    self.text_file = path.with_suffix(".log").open("x", encoding="utf-8")
                except BaseException:
                    self.json_file.close()
                    raise
        self._closing = threading.Event()
        self._records_done = threading.Event()
        self._finished = threading.Event()
        self.dropped = self.written = self.write_errors = 0
        self.stream_errors = self.notify_dropped = 0
        self._stream_failed = False
        self._notices = deque()
        self._notice_lock = threading.Lock()
        self._notices_closed = False
        # finish()'s last notice: held apart so nothing can be written after it.
        self._final = None
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

    @staticmethod
    def _notice(kind, fields):
        return dict(timestamp_utc=datetime.now(timezone.utc).isoformat(),
                    monotonic_seconds=time.monotonic(), type=kind, **fields)

    def notify(self, kind, **fields):
        """Queue one stream-only notice; never blocks and is never evidence."""
        if not self.stream_all:
            return False
        item = self._notice(kind, fields)
        with self._notice_lock:
            if self._notices_closed:
                return False
            if len(self._notices) >= self.NOTICE_CAPACITY:
                try:
                    self._notices.popleft()
                    self.notify_dropped += 1
                except IndexError:
                    pass
            self._notices.append(item)
        return True

    def _emit(self, serialized):
        if self._stream_failed:
            return
        try:
            # One write per line, so a reader never sees a torn line.
            self.stream.write(serialized + "\n")
            self.stream.flush()
        except (OSError, ValueError):
            self._stream_failed = True
            self.stream_errors += 1

    def _emit_notice(self, item):
        try:
            serialized = json.dumps(item, sort_keys=True, ensure_ascii=True,
                                    allow_nan=False)
        except (TypeError, ValueError):
            with self._notice_lock:
                self.notify_dropped += 1
            return
        self._emit(serialized)

    def _drain_notices(self):
        while True:
            try:
                item = self._notices.popleft()
            except IndexError:
                return
            self._emit_notice(item)

    def _write(self):
        try:
            while not self._closing.is_set() or not self.queue.empty():
                try:
                    item = self.queue.get(timeout=0.05)
                except queue.Empty:
                    if self.stream_all:
                        self._drain_notices()
                    continue
                if self.stream_all:
                    self._drain_notices()
                try:
                    serialized = json.dumps(item, sort_keys=True, ensure_ascii=True)
                    line = None
                    if self.text_file is not None or not self.stream_all:
                        line = " ".join(f"{key}={value}" for key, value in item.items())
                    if self.json_file is not None:
                        self.json_file.write(serialized + "\n")
                        if self.text_file is not None:
                            self.text_file.write(line + "\n")
                    if self.stream_all:
                        self._emit(serialized)
                    else:
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
            self._records_done.set()
        if self.stream_all:
            # The records are complete; notices keep flowing until finish().
            while not self._finished.is_set():
                self._drain_notices()
                self._finished.wait(0.05)
            # finish() closed the notices before setting _finished, so this
            # drain is complete and the final notice is the last line.
            self._drain_notices()
            if self._final is not None:
                self._emit_notice(self._final)

    def close(self, timeout=2.0):
        self._closing.set()
        if self.stream_all:
            self._records_done.wait(timeout)
        else:
            self._thread.join(timeout)

    def finish(self, kind="finished", timeout=2.0, **fields):
        """Close the records, write one last notice (none if kind is None), and
        stop the writer thread. The last notice follows every record and every
        earlier notice, even when close() timed out; later notices are ignored."""
        self.close(timeout)
        if self.stream_all:
            with self._notice_lock:
                if kind is not None and not self._notices_closed:
                    self._final = self._notice(kind, fields)
                self._notices_closed = True
            self._finished.set()
        self._thread.join(timeout)

    def summary(self):
        unfinished = self.queue.qsize() + int(
            self._thread.is_alive() and not self._records_done.is_set())
        result = {"written": self.written, "dropped": self.dropped,
                  "write_errors": self.write_errors,
                  "unfinished": unfinished,
                  "console_sample_every": self.sample_every}
        if self.stream_all:
            result.update(stream_errors=self.stream_errors,
                          notify_dropped=self.notify_dropped)
        return result
