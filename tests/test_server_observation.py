"""Server-side result fates; a drained write is never a Phone receipt."""
import asyncio
from collections import Counter
import json
import ssl
import struct
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from common.wire import encode_frame
from ultra96.server import ResultQueue, CommsServer


def sensor(seq, device=1):
    return dict(v=1, type="SENSOR_BATCH", session_id="ltc-comms",
                device_id=device, boot_id=7, seq=seq, uptime_ms=seq * 100,
                values=[seq % 2000 - 1000 + 10 * i for i in range(8)])


def result(seq):
    return dict(v=1, type="GESTURE_RESULT", session_id="ltc-comms",
                device_id=1, boot_id=7, seq=seq, result_id="1:7:" + str(seq),
                gesture=("REST", "FIST", "OPEN", "POINT")[seq % 4], confidence=1.0)


def reader_for(*messages, eof=False):
    reader = asyncio.StreamReader()
    for message in messages:
        reader.feed_data(encode_frame(message))
    if eof:
        reader.feed_eof()
    return reader


class Writer:
    def __init__(self, reader=None, block_results=False, fail_type=None):
        self.reader = reader
        self.block_results = block_results
        self.fail_type = fail_type
        self.messages = []
        self.result_started = asyncio.Event()
        self.subscribed = asyncio.Event()

    def write(self, data):
        length = struct.unpack("!I", data[:4])[0]
        self.messages.append(json.loads(data[4:4 + length]))

    async def drain(self):
        kind = self.messages[-1]["type"]
        if kind == "SUBSCRIBED":
            self.subscribed.set()
        if kind == self.fail_type:
            raise ConnectionResetError("private error text must not reach observer")
        if kind == "GESTURE_RESULT":
            self.result_started.set()
            if self.block_results:
                await asyncio.Event().wait()

    def close(self):
        if self.reader is not None:
            self.reader.feed_eof()


def observed_server(events):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    return CommsServer(context, observer=lambda event, **fields: events.append((event, fields)))


def fates(events):
    terminal = {"result_no_subscriber", "result_drop_oldest", "result_drop_stale",
                "result_write_complete", "result_send_failed", "result_send_cancelled",
                "result_abandoned"}
    return [(event, fields["seq"]) for event, fields in events if event in terminal]


def test_absent_subscriber_and_duplicate_preserve_wire_and_exact_ids():
    async def check():
        events = []
        server = observed_server(events)
        reader = reader_for(sensor(0), sensor(0, 2), sensor(0), eof=True)
        writer = Writer()
        with pytest.raises(asyncio.IncompleteReadError):
            await server._ingest(reader, writer)
        assert [item["status"] for item in writer.messages] == ["accepted", "accepted", "duplicate"]
        assert [fields["device_id"] for event, fields in events
                if event == "result_no_subscriber"] == [1, 2]
        assert len([event for event, _ in events if event == "result_accepted"]) == 2
        assert len([event for event, _ in events if event == "result_duplicate"]) == 1
        assert all("values" not in fields and "gesture" not in fields for _, fields in events)
    asyncio.run(check())


def test_queue_reports_evicted_identity_and_both_stale_and_remaining_ids():
    async def check():
        events = []
        queue = ResultQueue(observer=lambda event, **fields: events.append((event, fields)),
                            subscriber_id=9)
        for seq in range(34):
            queue.put(result(seq), received_at=100.0)
        assert queue.dropped == 2
        queue.put(result(34), received_at=103.0)
        assert (await queue.get_timed(now=103.0))[1]["seq"] == 34
        assert fates(events) == [("result_drop_oldest", seq) for seq in range(3)] + [
            ("result_drop_stale", seq) for seq in range(3, 34)]
        assert all(fields["subscriber_id"] == 9 for _, fields in events)
        queue.put(result(35))
        queue.retire("disconnected")
        queue.retire("disconnected")
        assert fates(events)[-1] == ("result_abandoned", 35)
        assert sum(event == "result_abandoned" for event, _ in events) == 1
    asyncio.run(check())


def test_replacement_accounts_current_and_pending_once_without_retiring_new_owner():
    async def check():
        events = []
        server = observed_server(events)
        subscribe = dict(v=1, type="SUBSCRIBE", session_id="ltc-comms")
        old_reader = reader_for(subscribe)
        old_writer = Writer(old_reader, block_results=True)
        old = asyncio.create_task(server._gateway(old_reader, old_writer))
        await asyncio.wait_for(old_writer.subscribed.wait(), 1)
        old_queue = server._subscriber[1]
        old_queue.put(result(1))
        old_queue.put(result(2))
        await asyncio.wait_for(old_writer.result_started.wait(), 1)
        new_reader = reader_for(subscribe)
        new_writer = Writer(new_reader)
        new = asyncio.create_task(server._gateway(new_reader, new_writer))
        await asyncio.wait_for(new_writer.subscribed.wait(), 1)
        await asyncio.wait_for(old, 1)
        assert server._subscriber[0] is new_writer
        server._subscriber[1].put(result(3))
        await asyncio.wait_for(new_writer.result_started.wait(), 1)
        new_writer.close()
        await asyncio.wait_for(new, 1)
        assert fates(events) == [("result_send_cancelled", 1), ("result_abandoned", 2),
                                 ("result_write_complete", 3)]
        claims = [fields["subscriber_id"] for event, fields in events if event == "subscriber_claimed"]
        assert claims == [1, 2]
        replaced = [fields for event, fields in events if event == "subscriber_replaced"]
        assert replaced[0]["previous_subscriber_id"] == 1
        ends = [fields for event, fields in events if event == "subscriber_ended"]
        assert ends[0]["reason"] == "replaced"
        assert next(fields for event, fields in events if event == "result_write_complete")["phone_receipt_confirmed"] is False
    asyncio.run(check())


@pytest.mark.parametrize("failure", ["GESTURE_RESULT", "SUBSCRIBED"])
def test_send_or_subscribe_failure_records_queued_retirement(failure):
    async def check():
        events = []
        server = observed_server(events)
        # A synchronous observer sees ownership before SUBSCRIBED is written.
        def observer(event, **fields):
            events.append((event, fields))
            if event == "subscriber_claimed":
                server._subscriber[1].put(result(1))
                server._subscriber[1].put(result(2))
        server.observer = observer
        reader = reader_for(dict(v=1, type="SUBSCRIBE", session_id="ltc-comms"))
        writer = Writer(reader, fail_type=failure)
        with pytest.raises(ConnectionResetError):
            await asyncio.wait_for(server._gateway(reader, writer), 1)
        expected = [("result_send_failed", 1), ("result_abandoned", 2)] if failure == "GESTURE_RESULT" else [
            ("result_abandoned", 1), ("result_abandoned", 2)]
        assert fates(events) == expected
        assert server._subscriber is None
        assert all("private error" not in str(fields) for _, fields in events)
    asyncio.run(check())


def test_observer_exception_does_not_change_ack_or_delivery():
    async def check():
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        def broken(event, **fields):
            raise RuntimeError("sink failed")
        server = CommsServer(context, observer=broken)
        reader = reader_for(sensor(0), eof=True)
        writer = Writer()
        with pytest.raises(asyncio.IncompleteReadError):
            await server._ingest(reader, writer)
        assert writer.messages[0]["status"] == "accepted"
        assert server.metrics["accepted"] == 1
        assert server.metrics["observer_errors"] >= 1
        subscription = reader_for(dict(v=1, type="SUBSCRIBE", session_id="ltc-comms"))
        phone = Writer(subscription)
        gateway = asyncio.create_task(server._gateway(subscription, phone))
        await asyncio.wait_for(phone.subscribed.wait(), 1)
        with pytest.raises(asyncio.IncompleteReadError):
            await server._ingest(reader_for(sensor(1), eof=True), Writer())
        await asyncio.wait_for(phone.result_started.wait(), 1)
        phone.close()
        await asyncio.wait_for(gateway, 1)
        assert phone.messages[-1]["result_id"] == "1:7:1"
    asyncio.run(check())


def test_cancelled_gateway_accounts_current_and_pending_with_stable_owner():
    async def check():
        events = []
        server = observed_server(events)
        reader = reader_for(dict(v=1, type="SUBSCRIBE", session_id="ltc-comms"))
        writer = Writer(reader, block_results=True)
        task = asyncio.create_task(server._gateway(reader, writer))
        await asyncio.wait_for(writer.subscribed.wait(), 1)
        server._subscriber[1].put(result(8))
        server._subscriber[1].put(result(9))
        await asyncio.wait_for(writer.result_started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert fates(events) == [("result_send_cancelled", 8), ("result_abandoned", 9)]
        assert server._subscriber is None
        assert [fields["reason"] for event, fields in events if event == "subscriber_ended"] == ["cancelled"]
    asyncio.run(check())


def test_result_expiring_after_dequeue_has_stale_fate_without_write():
    async def check():
        events = []
        stale = asyncio.Event()
        server = observed_server(events)
        class OldQueue(ResultQueue):
            async def get_timed(self, now=None):
                if stale.is_set():
                    await asyncio.Event().wait()
                return time.monotonic() - 3, result(10)
        def observe(event, **fields):
            events.append((event, fields))
            if event == "result_drop_stale":
                stale.set()
        queue = OldQueue(observer=observe, subscriber_id=1)
        writer = Writer()
        task = asyncio.create_task(server._send_results(writer, queue))
        await asyncio.wait_for(stale.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fates(events) == [("result_drop_stale", 10)]
        assert writer.messages == []
        assert events[0][1]["reason"] == "before_write"
    asyncio.run(check())


def test_default_metrics_schema_is_unchanged():
    server = CommsServer(ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER))
    assert "observer_errors" not in server.metrics


def test_cli_exposes_opt_in_event_directory():
    process = subprocess.run([sys.executable, "-m", "ultra96.server", "--help"],
                             capture_output=True, text=True, timeout=5)
    assert process.returncode == 0
    assert "--event-log-dir" in process.stdout


def test_cli_closes_observer_with_bounded_timeout_when_start_fails(monkeypatch):
    from ultra96 import server as module
    calls = []
    class Sink:
        def __init__(self, directory):
            calls.append(("directory", directory))
        def close(self, timeout):
            calls.append(("close", timeout))
            return {"complete": True}
    class Service:
        def __init__(self, *args, **kwargs):
            assert isinstance(kwargs["observer"], Sink)
        async def start(self):
            raise OSError("cannot bind")
    monkeypatch.setitem(sys.modules, "ultra96.diagnostics", SimpleNamespace(BoundedEventLog=Sink))
    monkeypatch.setattr(module, "CommsServer", Service)
    monkeypatch.setattr(module, "server_context", lambda *args: None)
    args = SimpleNamespace(cert="unused", key="unused", session_id="ltc-comms",
                           ingest_port=0, gateway_port=0, event_log_dir="new-evidence")
    with pytest.raises(OSError, match="cannot bind"):
        asyncio.run(module._run(args))
    assert calls == [("directory", "new-evidence"), ("close", 2.0)]


def test_every_accepted_id_has_one_fate_through_overflow_duplicate_and_server_close():
    async def check():
        events = []
        server = observed_server(events)
        async def ingest(*messages):
            with pytest.raises(asyncio.IncompleteReadError):
                await server._ingest(reader_for(*messages, eof=True), Writer())
        await ingest(sensor(0))
        reader = reader_for(dict(v=1, type="SUBSCRIBE", session_id="ltc-comms"))
        writer = Writer(reader, block_results=True)
        gateway = asyncio.create_task(server._gateway(reader, writer))
        server._tasks.add(gateway)
        await asyncio.wait_for(writer.subscribed.wait(), 1)
        await ingest(sensor(1))
        await asyncio.wait_for(writer.result_started.wait(), 1)
        await ingest(sensor(1), *(sensor(seq) for seq in range(2, 37)))
        await asyncio.wait_for(server.close(), 1)
        assert gateway.cancelled()
        assert server._subscriber is None
        accepted = [fields["seq"] for event, fields in events if event == "result_accepted"]
        outcomes = fates(events)
        assert Counter(seq for _, seq in outcomes) == Counter(accepted)
        assert len(accepted) == 37
        assert Counter(event for event, _ in outcomes) == {
            "result_no_subscriber": 1, "result_send_cancelled": 1,
            "result_drop_oldest": 3, "result_abandoned": 32}
        assert [fields["reason"] for event, fields in events
                if event == "subscriber_ended"] == ["server_shutdown"]
    asyncio.run(check())


def test_real_sink_accepts_server_events_and_finalizes_exact_safe_ledger(tmp_path):
    from ultra96.diagnostics import BoundedEventLog
    sink = BoundedEventLog(tmp_path / "new-log")
    async def check():
        complete = asyncio.Event()
        written_count = 0
        def observer(event, **fields):
            nonlocal written_count
            sink(event, **fields)
            if event == "result_write_complete":
                written_count += 1
                if written_count == 2:
                    complete.set()
        server = CommsServer(ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER), observer=observer)
        reader = reader_for(dict(v=1, type="SUBSCRIBE", session_id="ltc-comms"))
        phone = Writer(reader)
        gateway = asyncio.create_task(server._gateway(reader, phone))
        await asyncio.wait_for(phone.subscribed.wait(), 1)
        with pytest.raises(asyncio.IncompleteReadError):
            await server._ingest(reader_for(sensor(0), sensor(0, 2), eof=True), Writer())
        await asyncio.wait_for(complete.wait(), 1)
        assert len(phone.messages) == 3
        phone.close()
        await asyncio.wait_for(gateway, 1)
        assert server.metrics["observer_errors"] == 0
    try:
        asyncio.run(check())
    finally:
        state = sink.close(timeout=2)
    assert state["complete"], state
    events = [json.loads(line) for line in (tmp_path / "new-log" / "events.jsonl").read_text().splitlines()]
    accepted = [(item["device_id"], item["seq"]) for item in events if item["event"] == "result_accepted"]
    written = [(item["device_id"], item["seq"]) for item in events if item["event"] == "result_write_complete"]
    assert accepted == written == [(1, 0), (2, 0)]
    assert all(item["phone_receipt_confirmed"] is False for item in events
               if item["event"] == "result_write_complete")
    assert all("values" not in item and "gesture" not in item for item in events)
