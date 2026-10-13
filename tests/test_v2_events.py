"""V2 random events, command correlation and bounded replay safety (no hardware)."""
import asyncio
import json
import random
import ssl

import pytest

from common.wire import ProtocolError, encode_frame
from phone.receiver import validate_result as phone_result
from ultra96.protocol import validate_message
from ultra96.server import ResultQueue, CommsServer


def sensor(seq=0, request_id=None, device=1, boot=7):
    return dict(v=2, type="SENSOR_BATCH", session_id="ltc-comms", device_id=device,
                boot_id=boot, seq=seq, uptime_ms=42,
                values=[-32768, 32767, 0, 1, -1, 100, 200, 300], request_id=request_id)


def result(request_id=None):
    seq = 0 if request_id is None else request_id
    return dict(v=2, type="GESTURE_RESULT", session_id="ltc-comms", device_id=1,
                boot_id=7, seq=seq, result_id="1:7:0" if request_id is None else "cmd:1:7:" + str(seq),
                gesture="POINT", confidence=1.0, request_id=request_id)


class Writer:
    def __init__(self):
        self.messages = []

    def write(self, data):
        self.messages.append(json.loads(data[4:]))

    async def drain(self):
        pass


async def ingest(server, *messages):
    reader = asyncio.StreamReader()
    for message in messages:
        reader.feed_data(encode_frame(message))
    reader.feed_eof()
    writer = Writer()
    try:
        await server._ingest(reader, writer)
    except asyncio.IncompleteReadError:
        pass
    return writer.messages


def server(**kwargs):
    return CommsServer(ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER), rng=random.Random(23), **kwargs)


def test_v2_stream_and_command_validate_independently_of_sequence_gesture():
    for request in (None, 1):
        assert validate_message(sensor(seq=request or 0, request_id=request))['request_id'] == request
        message = result(request)
        assert validate_message(message) == message
        assert phone_result(message, "ltc-comms") == message


@pytest.mark.parametrize("change", [dict(request_id=True), dict(request_id=-1), dict(request_id=0),
    dict(request_id=2**32), dict(request_id=1), dict(extra=1), dict(v=3),
    dict(values=[0] * 7), dict(values=[32768] * 8)])
def test_v2_rejects_ambiguous_or_invalid_correlation_and_payload(change):
    with pytest.raises(ProtocolError):
        validate_message(dict(sensor(), **change))


def test_v2_requires_request_field_and_v1_does_not_accept_it():
    message = sensor()
    del message['request_id']
    with pytest.raises(ProtocolError):
        validate_message(message)
    with pytest.raises(ProtocolError):
        validate_message(dict(sensor(), v=1))


def test_random_events_preserve_command_trace_and_duplicate_has_no_second_effect():
    async def check():
        board = server()
        queue = ResultQueue()
        board._subscriber = (None, queue)
        acks = await ingest(board, sensor(), sensor(), sensor(1, request_id=1))
        assert [ack['status'] for ack in acks] == ['accepted', 'duplicate', 'accepted']
        assert [ack['request_id'] for ack in acks] == [None, None, 1]
        assert all(ack['v'] == 2 for ack in acks)
        stream, command = await queue.get(), await queue.get()
        assert stream['result_id'] == '1:7:0' and stream['request_id'] is None
        assert command['result_id'] == 'cmd:1:7:1' and command['request_id'] == 1
        control = server()
        other = ResultQueue()
        control._subscriber = (None, other)
        await ingest(control, sensor(), sensor(1, request_id=1))
        assert stream == await other.get()
        assert command == await other.get()
        assert board.metrics['accepted'] == 2
        assert board.metrics['duplicates'] == 1
    asyncio.run(check())


def test_seeded_random_generation_can_produce_all_allowed_labels():
    async def check():
        board, queue = server(), ResultQueue()
        board._subscriber = (None, queue)
        labels = set()
        for seq in range(24):
            await ingest(board, sensor(seq))
            event = await queue.get()
            labels.add(event['gesture'])
            validate_message(event)
        assert labels == {'REST', 'FIST', 'OPEN', 'POINT'}
    asyncio.run(check())


def test_v2_old_identity_cannot_create_another_event_after_recent_cache_eviction():
    async def check():
        board = server()
        await ingest(board, *(sensor(seq) for seq in range(4097)))
        with pytest.raises(ProtocolError, match="stale"):
            await ingest(board, sensor())
        assert board.metrics['accepted'] == 4097
    asyncio.run(check())


def test_v2_conflicting_duplicate_payload_is_rejected():
    async def check():
        board = server()
        await ingest(board, sensor())
        with pytest.raises(ProtocolError, match="conflict"):
            await ingest(board, dict(sensor(), values=[0] * 8))
        assert board.metrics['accepted'] == 1
    asyncio.run(check())


def test_v2_namespace_capacity_rejects_new_boot_without_forgetting_old_identity():
    async def check():
        board = server(max_v2_namespaces=2)
        await ingest(board, sensor(), sensor(device=2))
        with pytest.raises(ProtocolError, match="capacity"):
            await ingest(board, sensor(boot=8))
        assert (await ingest(board, sensor()))[0]['status'] == 'duplicate'
    asyncio.run(check())


def test_command_ledger_accepts_unordered_ids_and_never_evicts_at_capacity():
    async def check():
        board = server(max_v2_commands=2)
        await ingest(board, sensor(90, request_id=90), sensor(4, request_id=4))
        with pytest.raises(ProtocolError, match="capacity"):
            await ingest(board, sensor(50, request_id=50))
        assert (await ingest(board, sensor(90, request_id=90)))[0]['status'] == 'duplicate'
        await ingest(board, sensor(1))  # Command capacity does not block telemetry.
    asyncio.run(check())


def test_observation_log_preserves_stream_and_command_correlation(tmp_path):
    from ultra96.diagnostics import BoundedEventLog

    async def check():
        sink = BoundedEventLog(tmp_path / 'events')
        board = server(observer=sink)
        await ingest(board, sensor(), sensor(1, request_id=1))
        receipt = sink.close()
        assert receipt['complete']
        records = [json.loads(line) for line in sink.events_path.read_text().splitlines()]
        assert [item['request_id'] for item in records if item['event'] == 'result_accepted'] == [None, 1]
    asyncio.run(check())
