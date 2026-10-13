"""Bounded, fail-closed evidence logging for Ultra96 observer events."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import time

import pytest

import ultra96.diagnostics as diagnostics
from ultra96.diagnostics import BoundedEventLog


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_events(path: Path):
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def test_records_safe_events_flushes_and_finalizes_sha_bound_receipt(tmp_path):
    directory = tmp_path / "capture"
    sink = BoundedEventLog(directory, capacity=8, max_bytes=64 * 1024)
    initial = read_json(directory / "status.json")
    assert initial["complete"] is False
    assert initial["incomplete"] is True
    assert initial["lost_event_count"] == 0
    assert initial["finalized"] is False

    fields = {"session_id": "ltc-comms", "device_id": 1, "boot_id": 7, "seq": 42}
    sink("result_accepted", **fields)
    fields["seq"] = 99

    live_events = directory / initial["partial_events_file"]
    deadline = time.monotonic() + 1.0
    while not live_events.exists() or not live_events.stat().st_size:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    events = read_events(live_events)
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "result_accepted"
    assert event["event_seq"] == 0
    assert event["seq"] == 42
    assert event["run_id"] == initial["run_id"]
    assert event["utc"].endswith("Z")
    assert type(event["monotonic"]) is float

    state = sink.close()
    raw = (directory / "events.jsonl").read_bytes()
    assert state["complete"] is True
    assert state["incomplete"] is False
    assert state["lost_event_count"] == 0
    assert state["finalized"] is True
    assert state["recorded_event_count"] == 1
    assert state["dropped_event_count"] == 0
    assert state["events_sha256"] == hashlib.sha256(raw).hexdigest()
    assert read_json(directory / "status.json") == state
    assert sink.close() == state
    assert sink.stats() == state


def test_directory_creation_is_exclusive(tmp_path):
    directory = tmp_path / "capture"
    first = BoundedEventLog(directory)
    try:
        with pytest.raises(FileExistsError):
            BoundedEventLog(directory)
    finally:
        first.close()


class BlockingStream:
    def __init__(self, entered: threading.Event, release: threading.Event):
        self.entered = entered
        self.release = release
        self.buffer = bytearray()

    def write(self, data):
        self.entered.set()
        self.release.wait()
        self.buffer.extend(data)
        return len(data)

    def flush(self):
        pass

    def close(self):
        pass


def test_emit_is_nonblocking_and_overflow_is_explicit(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    stream = BlockingStream(entered, release)
    monkeypatch.setattr(diagnostics, "_open_events", lambda _path: stream)
    sink = BoundedEventLog(tmp_path / "capture", capacity=2)
    sink("result_accepted", device_id=1, boot_id=7, seq=0, session_id="ltc-comms")
    assert entered.wait(1.0)

    started = time.monotonic()
    for seq in range(1, 101):
        sink("result_accepted", device_id=1, boot_id=7, seq=seq, session_id="ltc-comms")
    elapsed = time.monotonic() - started
    assert elapsed < 0.2
    snapshot = sink.stats()
    assert snapshot["overflow_count"] > 0
    assert snapshot["dropped_event_count"] == snapshot["overflow_count"]
    assert snapshot["lost_event_count"] == snapshot["overflow_count"]
    assert snapshot["complete"] is False

    release.set()
    final = sink.close()
    assert final["finalized"] is True
    assert final["complete"] is False
    assert final["recorded_event_count"] <= 3


def test_file_cap_drops_whole_event_and_marks_evidence_incomplete(tmp_path):
    sink = BoundedEventLog(tmp_path / "capture", max_bytes=1)
    sink("subscriber_claimed", subscriber_id=1, queue_size=0)
    state = sink.close()
    assert state["file_cap_reached"] is True
    assert state["file_cap_drop_count"] == 1
    assert state["recorded_event_count"] == 0
    assert state["dropped_event_count"] == 1
    assert state["complete"] is False
    assert (tmp_path / "capture" / "events.jsonl").read_bytes() == b""


class FailingStream:
    def write(self, _data):
        raise OSError("synthetic write failure")

    def flush(self):
        pass

    def close(self):
        pass


class ShortWriteStream(FailingStream):
    def write(self, data):
        return len(data) - 1


def test_writer_failure_is_contained_and_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(diagnostics, "_open_events", lambda _path: FailingStream())
    sink = BoundedEventLog(tmp_path / "capture")
    sink("result_accepted", device_id=1, boot_id=7, seq=1, session_id="ltc-comms")
    state = sink.close()
    assert state["writer_error_count"] == 1
    assert state["writer_error_type"] == "OSError"
    assert state["recorded_event_count"] == 0
    assert state["dropped_event_count"] == 1
    assert state["complete"] is False


def test_short_write_is_a_writer_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(diagnostics, "_open_events", lambda _path: ShortWriteStream())
    sink = BoundedEventLog(tmp_path / "capture")
    sink("result_accepted", device_id=1, boot_id=7, seq=1, session_id="ltc-comms")
    state = sink.close()
    assert state["writer_error_count"] == 1
    assert state["writer_error_type"] == "OSError"
    assert state["recorded_event_count"] == 0
    assert state["lost_event_count"] == 1
    assert state["complete"] is False


def test_internal_callback_failure_does_not_escape_or_deadlock(monkeypatch, tmp_path):
    sink = BoundedEventLog(tmp_path / "capture")
    monkeypatch.setattr(diagnostics, "_utc_now", lambda: (_ for _ in ()).throw(OSError()))
    sink("result_accepted", device_id=1, boot_id=7, seq=1, session_id="ltc-comms")
    snapshot = sink.stats()
    assert snapshot["callback_error_count"] == 1
    assert snapshot["dropped_event_count"] == 1
    monkeypatch.undo()
    assert sink.close()["complete"] is False


@pytest.mark.parametrize("fields", [
    {"password": "secret"},
    {"payload": b"raw"},
    {"peer": "192.0.2.1"},
    {"device_id": True},
    {"reason": "x" * 257},
])
def test_unsafe_or_invalid_fields_are_rejected_without_raising(fields, tmp_path):
    sink = BoundedEventLog(tmp_path / ("capture-" + str(len(list(tmp_path.iterdir())))))
    sink("result_send_failed", **fields)
    state = sink.close()
    assert state["callback_error_count"] == 1
    assert state["dropped_event_count"] == 1
    assert state["recorded_event_count"] == 0
    assert state["complete"] is False


def test_close_timeout_is_bounded_idempotent_and_latches_incomplete(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    stream = BlockingStream(entered, release)
    monkeypatch.setattr(diagnostics, "_open_events", lambda _path: stream)
    sink = BoundedEventLog(tmp_path / "capture")
    sink("result_accepted", device_id=1, boot_id=7, seq=0, session_id="ltc-comms")
    assert entered.wait(1.0)
    started = time.monotonic()
    first = sink.close(timeout=0.01)
    assert time.monotonic() - started < 0.2
    assert first["close_timed_out"] is True
    assert first["finalized"] is False
    assert first["complete"] is False
    assert first["incomplete"] is True
    assert first["events_file"] is None
    assert first["events_sha256"] is None
    assert not (tmp_path / "capture" / "events.jsonl").exists()
    release.set()
    time.sleep(0.02)
    assert sink.close() == first
    assert sink.stats() == first
    assert not (tmp_path / "capture" / "events.jsonl").exists()


def test_close_deadline_includes_status_persistence(monkeypatch, tmp_path):
    sink = BoundedEventLog(tmp_path / "capture")
    sink("result_accepted", device_id=1, boot_id=7, seq=0, session_id="ltc-comms")
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    original = sink._prepare_status

    def blocking_status(state):
        entered.set()
        release.wait()
        original(state)

    monkeypatch.setattr(sink, "_prepare_status", blocking_status)
    result = {}

    def close():
        result.update(sink.close(timeout=0.01))
        returned.set()

    thread = threading.Thread(target=close)
    thread.start()
    try:
        assert entered.wait(1.0)
        assert returned.wait(0.2)
        assert result["close_timed_out"] is True
        assert result["finalized"] is False
        assert result["complete"] is False
    finally:
        release.set()
        thread.join(1.0)


def test_late_atomic_publication_does_not_upgrade_timeout_receipt(monkeypatch, tmp_path):
    sink = BoundedEventLog(tmp_path / "capture")
    sink("result_accepted", device_id=1, boot_id=7, seq=0, session_id="ltc-comms")
    entered, release = threading.Event(), threading.Event()
    replace = os.replace

    def blocking_final_status(source, destination):
        if Path(destination) == sink.status_path:
            entered.set()
            release.wait()
        return replace(source, destination)

    monkeypatch.setattr(diagnostics.os, "replace", blocking_final_status)
    # Reach the publication race before starting the close deadline. Disk flush
    # and writer scheduling need not finish within the 10 ms timeout under test.
    sink._stop.set()
    try:
        assert entered.wait(2.0)
        first = sink.close(timeout=0.01)
        assert first["close_timed_out"] is True
        assert first["finalized"] is False
        assert first["complete"] is False
        assert sink.close() == first
    finally:
        release.set()
        assert sink._done.wait(2.0)
    assert sink.stats() == first
    assert sink.close() == first

    persisted = read_json(sink.status_path)
    raw = sink.events_path.read_bytes()
    assert persisted["finalized"] is True
    assert persisted["complete"] is True
    assert persisted["events_sha256"] == hashlib.sha256(raw).hexdigest()
    assert persisted["recorded_event_count"] == 1


def test_event_sequence_is_exact_for_multiple_producers(tmp_path):
    sink = BoundedEventLog(tmp_path / "capture", capacity=256)
    threads = [threading.Thread(target=lambda base=base: [
        sink("result_accepted", device_id=1, boot_id=7, seq=base + offset,
             session_id="ltc-comms") for offset in range(20)
    ]) for base in range(0, 100, 20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    state = sink.close()
    events = read_events(tmp_path / "capture" / "events.jsonl")
    assert state["complete"] is True
    assert [event["event_seq"] for event in events] == list(range(100))
    assert len({event["seq"] for event in events}) == 100
