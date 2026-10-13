"""Bounded background evidence logging for safe Ultra96 observer events."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import threading
import time
import uuid


_EVENTS = frozenset({
    "result_accepted", "result_duplicate", "result_no_subscriber",
    "result_enqueued", "result_drop_oldest", "result_drop_stale",
    "result_send_started", "result_write_complete", "result_send_failed",
    "result_send_cancelled", "result_abandoned", "subscriber_claimed",
    "subscriber_replaced", "subscribed_write_complete", "subscriber_ended",
})
_INTEGER_FIELDS = frozenset({
    "device_id", "boot_id", "seq", "subscriber_id", "previous_subscriber_id",
    "queue_size", "queued_count",
})
_TEXT_FIELDS = frozenset({"session_id", "reason", "error_type"})
_BOOLEAN_FIELDS = frozenset({"phone_receipt_confirmed", "ambiguous"})
_ALLOWED_FIELDS = _INTEGER_FIELDS | _TEXT_FIELDS | _BOOLEAN_FIELDS | {"age_seconds", "request_id"}
_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_STATUS_SCHEMA = "ultra96-bounded-event-log-status-v1"
_EVENT_SCHEMA = "ultra96-observer-event-v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _open_events(path: Path):
    """Open an exclusive unbuffered binary stream; split out for fault testing."""
    return path.open("xb", buffering=0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BoundedEventLog:
    """A no-throw observer that moves JSON and file work off the server loop."""

    def __init__(self, directory, capacity=2048, max_bytes=50 * 1024 * 1024):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        self.events_path = self.directory / "events.jsonl"
        self.partial_path = self.directory / "events.partial.jsonl"
        self.status_path = self.directory / "status.json"
        self.capacity = capacity
        self.max_bytes = max_bytes
        self.run_id = uuid.uuid4().hex
        self._created_at_utc = _utc_now()
        self._queue = queue.Queue(maxsize=capacity)
        self._stop = threading.Event()
        self._abandon = threading.Event()
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._accepting = True
        self._finalized = False
        self._final_receipt = None
        self._timeout_receipt = None
        self._next_event_seq = 0
        self._accepted = 0
        self._recorded = 0
        self._dropped = 0
        self._overflow = 0
        self._callback_errors = 0
        self._file_cap_drops = 0
        self._file_cap_reached = False
        self._writer_errors = 0
        self._writer_error_type = None
        self._file_bytes = 0
        self._write_status(self._snapshot(finalized=False, complete=False))
        self._thread = threading.Thread(
            target=self._writer, name="ultra96-diagnostic-writer", daemon=True)
        self._thread.start()

    @staticmethod
    def _safe_fields(event, fields):
        if type(event) is not str or event not in _EVENTS:
            raise ValueError("unrecognized diagnostic event")
        if len(fields) > len(_ALLOWED_FIELDS) or not set(fields) <= _ALLOWED_FIELDS:
            raise ValueError("unsafe or unexpected diagnostic field")
        copied = dict(fields)
        for name, value in copied.items():
            if name == "request_id":
                if value is not None and (type(value) is not int or not 1 <= value <= 0xFFFFFFFF):
                    raise ValueError("diagnostic request ID is invalid")
            elif name in _INTEGER_FIELDS:
                if type(value) is not int or value < 0:
                    raise ValueError("diagnostic integer field is invalid")
                if name == "device_id" and value not in (1, 2):
                    raise ValueError("diagnostic device ID is invalid")
                if name in ("boot_id", "seq") and value > 0xFFFFFFFF:
                    raise ValueError("diagnostic uint32 field is invalid")
            elif name == "session_id":
                if (type(value) is not str or not 1 <= len(value) <= 128
                        or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF
                               for char in value)):
                    raise ValueError("diagnostic session ID is invalid")
            elif name in ("reason", "error_type"):
                if type(value) is not str or _TOKEN.fullmatch(value) is None:
                    raise ValueError("diagnostic token field is invalid")
            elif name == "age_seconds":
                if (type(value) not in (int, float) or isinstance(value, bool)
                        or not math.isfinite(value) or value < 0):
                    raise ValueError("diagnostic age is invalid")
                copied[name] = float(value)
            elif name == "phone_receipt_confirmed":
                if value is not False:
                    raise ValueError("transport write is not a Phone receipt")
            elif name == "ambiguous":
                if type(value) is not bool:
                    raise ValueError("diagnostic ambiguity flag is invalid")
        return copied

    def __call__(self, event: str, **fields) -> None:
        """Queue one safe event without encoding JSON, touching disk, or raising."""
        try:
            fields = self._safe_fields(event, fields)
            observed_utc = _utc_now()
            observed_monotonic = float(time.monotonic())
            with self._lock:
                if not self._accepting or self._finalized:
                    return
                sequence = self._next_event_seq
                self._next_event_seq += 1
                record = {
                    "schema": _EVENT_SCHEMA,
                    "run_id": self.run_id,
                    "event_seq": sequence,
                    "utc": observed_utc,
                    "monotonic": observed_monotonic,
                    "event": event,
                    **fields,
                }
                try:
                    self._queue.put_nowait(record)
                    self._accepted += 1
                except queue.Full:
                    self._overflow += 1
                    self._dropped += 1
        except BaseException:
            # Observer failures must never alter transport behavior.
            with self._lock:
                if not self._finalized:
                    self._callback_errors += 1
                    self._dropped += 1

    def _record_writer_error(self, error, current_lost=0):
        with self._lock:
            if self._finalized:
                return
            self._accepting = False
            if self._writer_errors == 0:
                self._writer_errors = 1
                self._writer_error_type = type(error).__name__
            self._dropped += current_lost
        pending = 0
        while True:
            try:
                self._queue.get_nowait()
                pending += 1
            except queue.Empty:
                break
        if pending:
            with self._lock:
                if not self._finalized:
                    self._dropped += pending

    def _writer(self):
        stream = None
        capped = False
        digest = hashlib.sha256()
        try:
            stream = _open_events(self.partial_path)
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    record = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    encoded = (json.dumps(record, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=True) + "\n").encode("ascii")
                    with self._lock:
                        if self._finalized:
                            return
                        exceeds = capped or self._file_bytes + len(encoded) > self.max_bytes
                        if exceeds:
                            capped = True
                            self._file_cap_reached = True
                            self._file_cap_drops += 1
                            self._dropped += 1
                    if exceeds:
                        continue
                    written = stream.write(encoded)
                    if written != len(encoded):
                        raise OSError("short diagnostic event write")
                    digest.update(encoded)
                    with self._lock:
                        if self._finalized:
                            return
                        self._recorded += 1
                        self._file_bytes += len(encoded)
                except BaseException as error:
                    self._record_writer_error(error, current_lost=1)
                    break
        except BaseException as error:
            self._record_writer_error(error)
        finally:
            if stream is not None:
                try:
                    stream.flush()
                    stream.close()
                except BaseException as error:
                    self._record_writer_error(error)
            try:
                self._finalize_from_writer(digest.hexdigest())
            except BaseException as error:
                self._record_writer_error(error)
            finally:
                self._done.set()

    def _snapshot(self, *, finalized, complete, close_timed_out=False,
                  events_sha256=None, closed_at_utc=None, events_file=None):
        return {
            "schema": _STATUS_SCHEMA,
            "run_id": self.run_id,
            "created_at_utc": self._created_at_utc,
            "closed_at_utc": closed_at_utc,
            "capacity": self.capacity,
            "max_bytes": self.max_bytes,
            "events_file": events_file,
            "partial_events_file": "events.partial.jsonl" if not finalized else None,
            "events_sha256": events_sha256,
            "file_bytes": self._file_bytes,
            "next_event_seq": self._next_event_seq,
            "accepted_event_count": self._accepted,
            "recorded_event_count": self._recorded,
            "dropped_event_count": self._dropped,
            "lost_event_count": self._dropped,
            "overflow_count": self._overflow,
            "callback_error_count": self._callback_errors,
            "file_cap_reached": self._file_cap_reached,
            "file_cap_drop_count": self._file_cap_drops,
            "writer_error_count": self._writer_errors,
            "writer_error_type": self._writer_error_type,
            "close_timed_out": close_timed_out,
            "finalized": finalized,
            "complete": complete,
            "incomplete": not complete,
        }

    def _prepare_status(self, state):
        temporary = self.directory / (".status-" + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(state, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            return temporary
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    def _write_status(self, state):
        temporary = self._prepare_status(state)
        try:
            os.replace(temporary, self.status_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _finalize_from_writer(self, digest):
        """Prepare and publish the final files before signalling close completion."""
        if self._abandon.is_set():
            return
        partial_exists = self.partial_path.is_file()
        closed_at_utc = _utc_now()
        with self._lock:
            publish_events = self._writer_errors == 0 and partial_exists
            complete = bool(
                self._dropped == 0
                and self._callback_errors == 0
                and self._writer_errors == 0
                and not self._file_cap_reached
                and self._recorded == self._accepted
                and publish_events
            )
            receipt = self._snapshot(
                finalized=True, complete=complete,
                events_sha256=digest if publish_events else None,
                closed_at_utc=closed_at_utc,
                events_file="events.jsonl" if publish_events else None)
        status_temporary = None
        try:
            status_temporary = self._prepare_status(receipt)
            if self._abandon.is_set():
                return
            if publish_events:
                os.replace(self.partial_path, self.events_path)
            if self._abandon.is_set():
                return
            os.replace(status_temporary, self.status_path)
            status_temporary = None
        except BaseException as error:
            self._record_writer_error(error)
            published_events = self.events_path.is_file()
            failed_at_utc = _utc_now()
            with self._lock:
                receipt = self._snapshot(
                    finalized=True, complete=False, closed_at_utc=failed_at_utc,
                    events_file="events.jsonl" if published_events else None)
                receipt["status_write_failed"] = True
        finally:
            if status_temporary is not None:
                try:
                    status_temporary.unlink()
                except FileNotFoundError:
                    pass
        if self._abandon.is_set():
            return
        with self._lock:
            if self._timeout_receipt is None:
                self._finalized = True
                self._final_receipt = dict(receipt)

    def stats(self):
        """Return a stable final receipt, or a lock-consistent live snapshot."""
        with self._lock:
            if self._final_receipt is not None:
                return dict(self._final_receipt)
            if self._timeout_receipt is not None:
                return dict(self._timeout_receipt)
            return self._snapshot(finalized=False, complete=False)

    def close(self, timeout=2.0):
        """Stop accepting events and bound the complete finalization attempt.

        A timeout receipt is latched and remains the authoritative result of this
        close call. An already-running atomic filesystem publication may finish
        later with internally valid files, but cannot upgrade that receipt.
        """
        if (type(timeout) not in (int, float) or isinstance(timeout, bool)
                or not math.isfinite(timeout) or timeout < 0):
            raise ValueError("close timeout must be finite and nonnegative")
        with self._lock:
            if self._final_receipt is not None:
                return dict(self._final_receipt)
            if self._timeout_receipt is not None:
                return dict(self._timeout_receipt)
            self._accepting = False
        self._stop.set()
        if self._done.wait(float(timeout)):
            with self._lock:
                if self._final_receipt is not None:
                    return dict(self._final_receipt)
        self._abandon.set()
        timed_out_at_utc = _utc_now()
        with self._lock:
            if self._final_receipt is not None:
                return dict(self._final_receipt)
            if self._timeout_receipt is None:
                self._timeout_receipt = self._snapshot(
                    finalized=False, complete=False, close_timed_out=True,
                    closed_at_utc=timed_out_at_utc)
            return dict(self._timeout_receipt)
