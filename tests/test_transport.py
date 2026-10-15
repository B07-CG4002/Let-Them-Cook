"""Real framed TLS contract tests; PKI stays in pytest temporary directories."""
import asyncio
import importlib.util
import json
import ssl
import struct
import socket

import pytest


def test_transport_modules_exist():
    try:
        spec = importlib.util.find_spec("common.wire")
    except ModuleNotFoundError:
        spec = None
    assert spec is not None, "framed transport is missing"


DUMMY_GESTURES = ("REST", "FIST", "OPEN", "POINT")


def message(seq=0, session="ltc-comms"):
    return {"v": 3, "type": "SENSOR_BATCH", "session_id": session,
            "device_id": 1, "boot_id": 42, "seq": seq, "uptime_ms": seq * 100,
            "values": [(seq + 10 * i) % 32768 for i in range(11)], "request_id": None}


def run(coro):
    return asyncio.run(coro)


def test_framing_split_coalesced_and_rejects_invalid_json():
    from common.wire import ProtocolError, encode_frame, read_frame

    async def check():
        reader = asyncio.StreamReader(limit=16388)
        encoded = encode_frame({"hello": "世界"})
        async def feed():
            for part in (encoded[:1], encoded[1:3], encoded[3:] + encoded):
                reader.feed_data(part)
                await asyncio.sleep(0)
        await asyncio.gather(feed(), read_twice(reader))
        for body in (b"", b"[]", b"null", b"\xff", b'{"x":NaN}',
                     b'{"x":Infinity}', b'{"x":1,"x":2}', b'{"x":',
                     b'{"nested":{"x":1,"x":2}}', b'{"x":1e999}'):
            invalid = asyncio.StreamReader()
            invalid.feed_data(struct.pack("!I", len(body)) + body)
            invalid.feed_eof()
            with pytest.raises(ProtocolError):
                await read_frame(invalid)
        for raw in (b"\x00\x00", struct.pack("!I", 16385), b"\x00\x00\x00\x09{}"):
            invalid = asyncio.StreamReader()
            invalid.feed_data(raw)
            invalid.feed_eof()
            with pytest.raises((ProtocolError, asyncio.IncompleteReadError)):
                await read_frame(invalid)
        idle = asyncio.StreamReader()
        with pytest.raises(asyncio.TimeoutError):
            await read_frame(idle, timeout=0.02)

    async def read_twice(reader):
        assert await read_frame(reader) == {"hello": "世界"}
        assert await read_frame(reader) == {"hello": "世界"}

    run(check())
    for value in ([], {"x": float("nan")}, {"x": "x" * 16384}):
        with pytest.raises(ProtocolError):
            encode_frame(value)


def test_read_frame_idle_timeout_waits_for_the_first_byte_then_applies_the_frame_deadline():
    from common.wire import ProtocolError, encode_frame, read_frame

    frame = encode_frame({"v": 1})

    async def feed_later(reader, parts):
        for delay, data in parts:
            await asyncio.sleep(delay)
            if data is None:
                reader.feed_eof()
            else:
                reader.feed_data(data)

    async def timed(reader, parts, **kwargs):
        loop = asyncio.get_running_loop()
        feeder = asyncio.create_task(feed_later(reader, parts))
        started = loop.time()
        try:
            return await read_frame(reader, **kwargs), loop.time() - started
        finally:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)

    async def check():
        # Quiet for longer than the frame deadline, then a whole frame: accepted.
        message, _ = await timed(asyncio.StreamReader(), [(0.3, frame)],
                                 timeout=0.1, idle_timeout=0.5)
        assert message == {"v": 1}
        # Nothing at all: the idle limit, not the frame deadline, ends the wait.
        with pytest.raises(asyncio.TimeoutError):
            await timed(asyncio.StreamReader(), [], timeout=0.02, idle_timeout=0.2)
        # A frame that has begun gets only the frame deadline, counted from its first byte.
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(asyncio.TimeoutError):
            await timed(asyncio.StreamReader(), [(0.2, frame[:2])], timeout=0.1, idle_timeout=5.0)
        assert 0.2 <= loop.time() - started < 1.0
        started = loop.time()
        with pytest.raises(asyncio.TimeoutError):
            await timed(asyncio.StreamReader(), [(0.0, frame[:6])], timeout=0.1, idle_timeout=5.0)
        assert loop.time() - started < 1.0
        # The first byte late, the rest within the frame deadline: one frame.
        message, _ = await timed(asyncio.StreamReader(), [(0.15, frame[:1]), (0.15, frame[1:])],
                                 timeout=0.2, idle_timeout=0.5)
        assert message == {"v": 1}
        # The same timing with idle_timeout=None misses the single deadline, as before.
        with pytest.raises(asyncio.TimeoutError):
            await timed(asyncio.StreamReader(), [(0.15, frame[:1]), (0.15, frame[1:])],
                        timeout=0.2)
        # A clean EOF before the first byte stays IncompleteReadError, with or without idle.
        for kwargs in ({"timeout": 1.0, "idle_timeout": 1.0}, {"timeout": 1.0}):
            with pytest.raises(asyncio.IncompleteReadError) as caught:
                await timed(asyncio.StreamReader(), [(0.0, None)], **kwargs)
            assert caught.value.partial == b""
        # EOF inside a frame stays a ProtocolError: after one header byte, mid-header, mid-body.
        for prefix in (frame[:1], frame[:3], frame[:6]):
            with pytest.raises(ProtocolError):
                await timed(asyncio.StreamReader(), [(0.0, prefix), (0.0, None)],
                            timeout=1.0, idle_timeout=1.0)
        # Two frames back to back are both read in idle mode.
        reader = asyncio.StreamReader()
        reader.feed_data(frame + encode_frame({"v": 2}))
        assert await read_frame(reader, timeout=0.5, idle_timeout=0.5) == {"v": 1}
        assert await read_frame(reader, timeout=0.5, idle_timeout=0.5) == {"v": 2}

    run(check())


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    from tools.generate_comms_pki import generate_pki
    folder = tmp_path_factory.mktemp("comms-transport-pki")
    generate_pki(folder)
    other = tmp_path_factory.mktemp("comms-untrusted-pki")
    generate_pki(other)
    return folder, other


async def server_fixture(pki):
    from common.tls import server_context
    from ultra96.server import CommsServer
    folder, _ = pki
    server = CommsServer(server_context(folder / "server-cert.pem", folder / "server-key.pem"),
                         ingest_port=0, gateway_port=0)
    await server.start()
    return server


async def connect(pki, port, name="ultra96.week7.internal", other=False):
    from common.tls import client_context
    return await asyncio.open_connection("127.0.0.1", port,
        ssl=client_context(pki[bool(other)] / "ca-cert.pem"), server_hostname=name)


async def disconnect(writer):
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, ssl.SSLError):
        pass


async def subscribe(pki, server):
    from common.wire import read_frame, write_frame
    reader, writer = await connect(pki, server.gateway_port)
    await write_frame(writer, {"v": 1, "type": "SUBSCRIBE", "session_id": "ltc-comms"})
    assert await read_frame(reader) == {"v": 1, "type": "SUBSCRIBED", "session_id": "ltc-comms"}
    return reader, writer


def test_real_tls_100_correlated_ack_and_independent_gateway_results(pki):
    from common.wire import read_frame, write_frame

    async def check():
        server = await server_fixture(pki)
        try:
            phone, phone_writer = await subscribe(pki, server)
            laptop, laptop_writer = await connect(pki, server.ingest_port)
            for seq in range(100):
                await write_frame(laptop_writer, message(seq))
                assert await read_frame(laptop) == {
                    "v": 3, "type": "INGEST_ACK", "session_id": "ltc-comms",
                    "device_id": 1, "boot_id": 42, "seq": seq, "request_id": None,
                    "status": "accepted"}
                # The built-in dummy model answers every accepted sample once.
                result = await read_frame(phone)
                assert result.pop("gesture") in DUMMY_GESTURES
                assert result == {
                    "v": 3, "type": "GESTURE_RESULT", "session_id": "ltc-comms",
                    "device_id": 1, "boot_id": 42, "seq": seq, "request_id": None,
                    "result_id": "1:42:" + str(seq), "confidence": 1.0}
            await write_frame(laptop_writer, message(99))
            assert (await read_frame(laptop))["status"] == "duplicate"
            with pytest.raises(asyncio.TimeoutError):
                await read_frame(phone, timeout=0.03)
            await disconnect(laptop_writer)
            await disconnect(phone_writer)
        finally:
            await server.close()
    run(check())


def test_tls_rejects_wrong_hostname_wrong_ca_and_plaintext(pki):
    async def check():
        server = await server_fixture(pki)
        try:
            for name, other in (("localhost", False), ("ultra96.week7.internal", True)):
                with pytest.raises(ssl.SSLCertVerificationError):
                    await connect(pki, server.ingest_port, name=name, other=other)
            reader, writer = await asyncio.open_connection("127.0.0.1", server.ingest_port)
            writer.write(b"\x00\x00\x00\x02{}")
            await writer.drain()
            try:
                assert await asyncio.wait_for(reader.read(1), 2) == b""
            except ConnectionError:
                pass
            await disconnect(writer)
        finally:
            await server.close()
    run(check())


def test_server_rejects_schema_session_values_and_oversize_frames(pki):
    from common.wire import encode_frame, read_frame, write_frame

    async def check():
        server = await server_fixture(pki)
        try:
            bad_messages = [dict(message(), **patch) for patch in (
                {"v": True}, {"v": 2}, {"v": 1}, {"session_id": "wrong"}, {"device_id": 3},
                {"boot_id": -1}, {"seq": 4294967296}, {"uptime_ms": False},
                {"values": [0] * 8}, {"values": [0] * 10}, {"values": [0] * 12},
                {"values": [32768] + [0] * 10}, {"request_id": 5}, {"extra": 1},
                {"type": "SUBSCRIBE"})]
            bad_messages.append({key: value for key, value in message().items() if key != "request_id"})
            for invalid in bad_messages:
                reader, writer = await connect(pki, server.ingest_port)
                await write_frame(writer, invalid)
                assert await asyncio.wait_for(reader.read(), 1) == b""
                await disconnect(writer)
            for raw in (struct.pack("!I", 16385), b"\x00\x00\x00\x00",
                        b"\x00\x00\x00\x02[]", b"\x00\x00\x00\x09{\"x\":NaN}"):
                reader, writer = await connect(pki, server.ingest_port)
                writer.write(raw)
                await writer.drain()
                assert await asyncio.wait_for(reader.read(), 1) == b""
                await disconnect(writer)
            reader, writer = await connect(pki, server.gateway_port)
            await write_frame(writer, {"v": 1, "type": "SUBSCRIBE", "session_id": "wrong"})
            assert await asyncio.wait_for(reader.read(), 1) == b""
            await disconnect(writer)
        finally:
            await server.close()
    run(check())


def test_subscriber_replacement_reconnect_and_service_close(pki):
    from common.wire import read_frame, write_frame

    async def check():
        server = await server_fixture(pki)
        old, old_writer = await subscribe(pki, server)
        current, current_writer = await subscribe(pki, server)
        assert await asyncio.wait_for(old.read(), 1) == b""
        laptop, laptop_writer = await connect(pki, server.ingest_port)
        await write_frame(laptop_writer, message(0))
        await read_frame(laptop)
        assert (await read_frame(current))["seq"] == 0
        await disconnect(current_writer)
        await asyncio.sleep(0.03)
        await write_frame(laptop_writer, message(1))
        await read_frame(laptop)
        resumed, resumed_writer = await subscribe(pki, server)
        with pytest.raises(asyncio.TimeoutError):
            await read_frame(resumed, timeout=0.03)
        await write_frame(laptop_writer, message(2))
        await read_frame(laptop)
        assert (await read_frame(resumed))["seq"] == 2
        await asyncio.wait_for(server.close(), timeout=2)
        assert await asyncio.wait_for(laptop.read(), 1) == b""
        assert await asyncio.wait_for(resumed.read(), 1) == b""
        for writer in (old_writer, laptop_writer, resumed_writer):
            await disconnect(writer)
    run(check())


def test_identity_evicted_from_the_4096_fingerprints_is_refused_as_stale(pki):
    from common.wire import read_frame, write_frame

    async def check():
        server = await server_fixture(pki)
        try:
            reader, writer = await connect(pki, server.ingest_port)
            for seq in range(4097):
                await write_frame(writer, message(seq))
                assert (await read_frame(reader))["status"] == "accepted"
            await write_frame(writer, message(4096))
            assert (await read_frame(reader))["status"] == "duplicate"
            # seq 1 is still among the last 4096 fingerprints: an exact retry is a duplicate.
            await write_frame(writer, message(1))
            assert (await read_frame(reader))["status"] == "duplicate"
            # seq 0 left the fingerprints but is below the high-water mark: it can never be
            # accepted again, so the service refuses it and closes ingestion without an ACK.
            await write_frame(writer, message(0))
            assert await asyncio.wait_for(reader.read(), 1) == b""
            await disconnect(writer)
            assert server.metrics["accepted"] == 4097
            assert server.metrics["duplicates"] == 2
            assert server.metrics["rejected"] == 1
            assert server.metrics["model_samples"] == 4097
        finally:
            await server.close()
    run(check())


def test_ingest_waits_its_idle_limit_between_frames_and_closes_after_it(pki, monkeypatch):
    from common import wire
    from common.tls import server_context
    from common.wire import read_frame, write_frame
    import ultra96.server as service

    # Scale the five-second frame deadline down so the idle limit can be shown
    # to outlast it in a short test. Only the ingest path passes idle_timeout.
    seen = []

    async def scaled(reader, timeout=5.0, idle_timeout=None):
        seen.append(idle_timeout)
        return await wire.read_frame(reader, timeout=0.2, idle_timeout=idle_timeout)

    monkeypatch.setattr(service, "read_frame", scaled)

    async def check():
        folder, _ = pki
        server = service.CommsServer(
            server_context(folder / "server-cert.pem", folder / "server-key.pem"),
            ingest_port=0, gateway_port=0, ingest_idle_timeout=1.0)
        await server.start()
        loop = asyncio.get_running_loop()
        try:
            reader, writer = await connect(pki, server.ingest_port)
            await write_frame(writer, message(0))
            assert (await read_frame(reader, timeout=2))["status"] == "accepted"
            # Quiet for three scaled frame deadlines, as a still glove would be.
            await asyncio.sleep(0.6)
            await write_frame(writer, message(1))
            assert (await read_frame(reader, timeout=2))["status"] == "accepted"
            started = loop.time()
            assert await asyncio.wait_for(reader.read(), 4) == b""
            assert 0.8 <= loop.time() - started < 4
            await disconnect(writer)

            # After a quiet spell, a frame that begins must still finish within the deadline.
            reader, writer = await connect(pki, server.ingest_port)
            await asyncio.sleep(0.4)
            writer.write(wire.encode_frame(message(2))[:2])
            await writer.drain()
            started = loop.time()
            assert await asyncio.wait_for(reader.read(), 4) == b""
            assert loop.time() - started < 0.8
            await disconnect(writer)
            assert server.metrics["accepted"] == 2
            assert set(seen) == {1.0}
        finally:
            await server.close()

    run(check())


def test_ingest_idle_timeout_defaults_to_ten_minutes_and_must_be_positive(pki):
    from common.tls import server_context
    from ultra96.server import CommsServer, INGEST_IDLE_TIMEOUT
    folder, _ = pki
    context = server_context(folder / "server-cert.pem", folder / "server-key.pem")
    assert INGEST_IDLE_TIMEOUT == 600.0
    assert CommsServer(context).ingest_idle_timeout == 600.0
    assert CommsServer(context, ingest_idle_timeout=2).ingest_idle_timeout == 2.0
    for value in (0, -1, float("nan"), float("inf"), True, "600", None):
        with pytest.raises(ValueError, match="idle timeout"):
            CommsServer(context, ingest_idle_timeout=value)


@pytest.mark.parametrize("value,expected", [
    (None, 600.0), ("5", 5.0), ("30.5", 30.5), ("86400", 86400.0)])
def test_ingest_idle_timeout_option_accepts_5_to_86400_seconds(value, expected):
    from ultra96.server import _parser
    argv = ["--cert", "c.pem", "--key", "k.pem"]
    if value is not None:
        argv += ["--ingest-idle-timeout", value]
    assert _parser().parse_args(argv).ingest_idle_timeout == expected


@pytest.mark.parametrize("value", ["4.9", "0", "-5", "86401", "nan", "inf", "ten"])
def test_ingest_idle_timeout_option_refuses_values_outside_5_to_86400(value, capsys):
    from ultra96.server import _parser
    with pytest.raises(SystemExit):
        _parser().parse_args(["--cert", "c.pem", "--key", "k.pem",
                              "--ingest-idle-timeout", value])
    assert "5..86400" in capsys.readouterr().err


def test_service_passes_the_ingest_idle_timeout_to_the_server():
    from types import SimpleNamespace
    from unittest.mock import patch
    import contextlib
    import io
    from ultra96.server import _run

    created = []

    class Service:
        ingest_port, gateway_port, metrics = 8888, 9999, {}
        model = SimpleNamespace(name="dummy")
        model_start_seconds = 0.0

        async def start(self):
            pass

        async def wait_failure(self):
            raise RuntimeError("listener failed")

        async def close(self):
            pass

    def factory(*args, **kwargs):
        created.append(kwargs)
        return Service()

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    base = dict(cert="unused", key="unused", session_id="ltc-comms",
                ingest_port=8888, gateway_port=9999)
    with patch("ultra96.server.CommsServer", factory), \
            patch("ultra96.server.server_context", return_value=context), \
            contextlib.redirect_stdout(io.StringIO()):
        for args in (SimpleNamespace(**base, ingest_idle_timeout=42.0), SimpleNamespace(**base)):
            with pytest.raises(RuntimeError, match="listener failed"):
                run(_run(args))
    assert [kwargs["ingest_idle_timeout"] for kwargs in created] == [42.0, 600.0]


def test_slow_result_queue_drops_oldest_and_expired_results():
    from ultra96.server import ResultQueue

    async def check():
        queue = ResultQueue()
        for seq in range(40):
            queue.put({"seq": seq}, received_at=100.0)
        assert queue.dropped == 8
        assert (await queue.get(now=100.1))["seq"] == 8
        queue.put({"seq": 40}, received_at=103.0)
        assert (await queue.get(now=103.0))["seq"] == 40
        assert queue.stale == 31
    run(check())


def test_real_tls_split_coalesced_partial_timeout_and_connection_limit(pki):
    from common.wire import encode_frame, read_frame

    async def check():
        server = await server_fixture(pki)
        writers = []
        try:
            reader, writer = await connect(pki, server.ingest_port)
            writers.append(writer)
            frame = encode_frame(message(0))
            writer.write(frame[:2])
            await writer.drain()
            await asyncio.sleep(0)
            writer.write(frame[2:] + encode_frame(message(1)) + encode_frame(message(2)))
            await writer.drain()
            assert [(await read_frame(reader))["seq"] for _ in range(3)] == [0, 1, 2]
            idle = []
            for _ in range(7):
                idle_reader, idle_writer = await connect(pki, server.ingest_port)
                idle.append(idle_reader)
                writers.append(idle_writer)
            try:
                rejected, rejected_writer = await connect(pki, server.ingest_port)
                writers.append(rejected_writer)
                assert await asyncio.wait_for(rejected.read(), 1) == b""
            except ConnectionError:
                pass
            writers[1].write(b"\x00\x00")
            writers[2].write(b"\x00\x00\x00\x10{}")
            await asyncio.gather(*(writer.drain() for writer in writers[1:3]))
            assert await asyncio.gather(*(asyncio.wait_for(r.read(), 6) for r in idle[:2])) == [b"", b""]
        finally:
            await asyncio.gather(*(disconnect(writer) for writer in writers))
            await server.close()
    run(check())


def test_shutdown_cancels_pending_tls_handshakes(pki):
    async def check():
        server = await server_fixture(pki)
        clients = [await asyncio.open_connection("127.0.0.1", server.ingest_port) for _ in range(8)]
        await asyncio.sleep(0.03)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.ingest_port)
            assert await asyncio.wait_for(reader.read(), 1) == b""
            await disconnect(writer)
            assert server.metrics["client_limit"] == 1
            await asyncio.wait_for(server.close(), timeout=1)
            assert await asyncio.gather(*(reader.read() for reader, _ in clients)) == [b""] * 8
        finally:
            await asyncio.gather(*(disconnect(writer) for _, writer in clients))
    run(check())


def test_full_server_closes_the_longest_idle_ingest_connection_to_admit_a_subscriber(
        pki, monkeypatch):
    from common.wire import read_frame, write_frame
    import ultra96.server as service

    async def eof(reader):
        try:
            return await asyncio.wait_for(reader.read(), 3)
        except ConnectionError:
            return b""

    async def check():
        server = await server_fixture(pki)
        readers, writers = [], []
        try:
            # Eight silent ingest connections fill every slot, as sshd can leave
            # them for a laptop that vanished from the network.
            for _ in range(service.MAX_CLIENTS):
                reader, writer = await connect(pki, server.ingest_port)
                readers.append(reader)
                writers.append(writer)
            for _ in range(300):
                if len(server._waiting_ingest) == service.MAX_CLIENTS:
                    break
                await asyncio.sleep(0.01)
            assert len(server._waiting_ingest) == service.MAX_CLIENTS

            # None has been quiet long enough to be displaced: refused as before.
            monkeypatch.setattr(service, "INGEST_EVICTABLE_AFTER", 60.0)
            try:
                rejected, rejected_writer = await connect(pki, server.gateway_port)
                writers.append(rejected_writer)
                assert await eof(rejected) == b""
            except (ConnectionError, ssl.SSLError):
                pass
            for _ in range(100):
                if server.metrics["client_limit"]:
                    break
                await asyncio.sleep(0.01)
            assert server.metrics["client_limit"] == 1
            assert server.metrics["idle_evicted"] == 0

            # Seven of them stream a frame; only the first stays silent.
            monkeypatch.setattr(service, "INGEST_EVICTABLE_AFTER", 0.3)
            await asyncio.sleep(0.4)
            for seq in range(1, service.MAX_CLIENTS):
                await write_frame(writers[seq], message(seq))
                assert (await read_frame(readers[seq]))["status"] == "accepted"
            phone, phone_writer = await subscribe(pki, server)
            writers.append(phone_writer)
            assert await eof(readers[0]) == b""
            assert server.metrics["idle_evicted"] == 1
            assert server.metrics["client_limit"] == 1
            assert server.metrics["subscribers"] == 1

            # The other seven are untouched and the new subscriber gets results.
            await write_frame(writers[1], message(8))
            assert (await read_frame(readers[1]))["status"] == "accepted"
            assert (await read_frame(phone, timeout=2))["seq"] == 8
            assert len(server._sockets) == service.MAX_CLIENTS
        finally:
            await asyncio.gather(*(disconnect(writer) for writer in writers))
            await server.close()
    run(check())


def test_clean_ingestion_eof_is_normal_but_partial_body_eof_is_rejected(pki):
    async def check():
        server = await server_fixture(pki)
        try:
            _, writer = await connect(pki, server.ingest_port)
            await disconnect(writer)
            for _ in range(100):
                if not server._tasks:
                    break
                await asyncio.sleep(0.001)
            assert server.metrics["rejected"] == 0
            _, writer = await connect(pki, server.ingest_port)
            writer.write(b"\x00\x00\x00\x10")
            await writer.drain()
            await disconnect(writer)
            for _ in range(100):
                if not server._tasks:
                    break
                await asyncio.sleep(0.001)
            assert server.metrics["rejected"] == 1
        finally:
            await server.close()
    run(check())


def test_real_tls_stalled_phone_bounds_output_and_keeps_ingestion_live(pki):
    from common.wire import read_frame, write_frame

    async def check():
        server = await server_fixture(pki)
        try:
            phone, phone_writer = await subscribe(pki, server)
            phone_writer.transport.pause_reading()
            # Shrink the real socket send buffer to expose backpressure promptly.
            server._subscriber[0].get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
            laptop, laptop_writer = await connect(pki, server.ingest_port)
            queue = server._subscriber[1]
            for seq in range(6000):
                await write_frame(laptop_writer, message(seq))
                assert (await read_frame(laptop))["status"] == "accepted"
            assert queue.dropped > 0
            assert queue._queue.qsize() <= 32
            phone_writer.transport.resume_reading()
            await disconnect(phone_writer)
            await disconnect(laptop_writer)
        finally:
            await server.close()
    run(check())


def test_pki_refuses_git_and_overwrites_and_has_selected_certificate_extensions(pki, tmp_path):
    from tools.generate_comms_pki import generate_pki
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID
    folder, _ = pki
    with pytest.raises(ValueError, match="overwrite"):
        generate_pki(folder)
    (tmp_path / ".git").write_text("gitdir: elsewhere")
    with pytest.raises(ValueError, match="outside Git"):
        generate_pki(tmp_path / "secrets")
    ca = x509.load_pem_x509_certificate((folder / "ca-cert.pem").read_bytes())
    cert = x509.load_pem_x509_certificate((folder / "server-cert.pem").read_bytes())
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert ExtendedKeyUsageOID.SERVER_AUTH in cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) == ["ultra96.week7.internal"]
    assert (cert.not_valid_after_utc - cert.not_valid_before_utc).days == 30


def test_renewed_server_certificate_keeps_the_ca_and_is_trusted_by_existing_clients(pki, tmp_path_factory):
    from common.tls import client_context, server_context
    from common.wire import read_frame, write_frame
    from cryptography import x509
    from tools.generate_comms_pki import renew_server_identity
    from ultra96.server import CommsServer
    folder, other = pki
    renewed = renew_server_identity(folder, tmp_path_factory.mktemp("comms-renewed-pki"))
    assert sorted(path.name for path in renewed.iterdir()) == ["ca-cert.pem", "server-cert.pem", "server-key.pem"]
    assert (renewed / "ca-cert.pem").read_bytes() == (folder / "ca-cert.pem").read_bytes()
    assert (renewed / "server-key.pem").read_bytes() != (folder / "server-key.pem").read_bytes()
    ca = x509.load_pem_x509_certificate((folder / "ca-cert.pem").read_bytes())
    cert = x509.load_pem_x509_certificate((renewed / "server-cert.pem").read_bytes())
    assert cert.issuer == ca.subject
    assert cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
        x509.DNSName) == ["ultra96.week7.internal"]
    assert (cert.not_valid_after_utc - cert.not_valid_before_utc).days == 30

    async def check():
        server = CommsServer(server_context(renewed / "server-cert.pem", renewed / "server-key.pem"),
                             ingest_port=0, gateway_port=0)
        await server.start()
        try:
            # A client that only holds the original CA file accepts the renewed server.
            reader, writer = await asyncio.open_connection("127.0.0.1", server.gateway_port,
                ssl=client_context(folder / "ca-cert.pem"), server_hostname="ultra96.week7.internal")
            await write_frame(writer, {"v": 1, "type": "SUBSCRIBE", "session_id": "ltc-comms"})
            assert await read_frame(reader) == {"v": 1, "type": "SUBSCRIBED", "session_id": "ltc-comms"}
            await disconnect(writer)
            with pytest.raises(ssl.SSLError):
                await asyncio.open_connection("127.0.0.1", server.gateway_port,
                    ssl=client_context(other / "ca-cert.pem"), server_hostname="ultra96.week7.internal")
        finally:
            await server.close()
    run(check())


def test_renewal_refuses_a_mismatched_key_an_occupied_folder_and_git(pki, tmp_path):
    from tools.generate_comms_pki import renew_server_identity
    folder, other = pki
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    (mixed / "ca-cert.pem").write_bytes((folder / "ca-cert.pem").read_bytes())
    (mixed / "ca-key.pem").write_bytes((other / "ca-key.pem").read_bytes())
    with pytest.raises(ValueError, match="does not belong"):
        renew_server_identity(mixed, tmp_path / "out")
    assert not (tmp_path / "out").exists()
    with pytest.raises(ValueError, match="overwrite"):
        renew_server_identity(folder, folder)
    with pytest.raises(FileNotFoundError):
        renew_server_identity(tmp_path / "missing", tmp_path / "out")
    (tmp_path / ".git").write_text("gitdir: elsewhere")
    with pytest.raises(ValueError, match="outside Git"):
        renew_server_identity(folder, tmp_path / "secrets")


def test_phone_simulator_receives_results_from_independent_gateway(pki):
    from laptop.phone_simulator import receive_results
    from common.wire import read_frame, write_frame

    async def check():
        server = await server_fixture(pki)
        received = []
        try:
            phone = asyncio.create_task(receive_results("127.0.0.1", server.gateway_port,
                pki[0] / "ca-cert.pem", count=3, on_result=received.append))
            for _ in range(100):
                if server.metrics["subscribers"]:
                    break
                await asyncio.sleep(0.01)
            laptop, writer = await connect(pki, server.ingest_port)
            for seq in range(3):
                await write_frame(writer, message(seq))
                await read_frame(laptop)
            assert await asyncio.wait_for(phone, 2) == 3
            assert [result["result_id"] for result in received] == ["1:42:0", "1:42:1", "1:42:2"]
            await disconnect(writer)
        finally:
            await server.close()
    run(check())


def test_phone_simulator_rejects_bad_result_schema_and_bad_ca(pki):
    from laptop.phone_simulator import receive_results
    from common.tls import server_context
    from common.wire import ProtocolError, read_frame, write_frame

    async def check():
        async def bad_peer(reader, writer):
            await read_frame(reader)
            await write_frame(writer, {"v": 1, "type": "SUBSCRIBED", "session_id": "ltc-comms"})
            # Valid in every field except result_id, so the receiver refuses it for that reason.
            await write_frame(writer, {"v": 3, "type": "GESTURE_RESULT", "session_id": "ltc-comms",
                "device_id": 1, "boot_id": 42, "seq": 0, "request_id": None, "result_id": "wrong",
                "gesture": "REST", "confidence": 1.0})
            await disconnect(writer)
        folder, _ = pki
        server = await asyncio.start_server(bad_peer, "127.0.0.1", 0,
            ssl=server_context(folder / "server-cert.pem", folder / "server-key.pem"))
        port = server.sockets[0].getsockname()[1]
        try:
            with pytest.raises(ProtocolError):
                await receive_results("127.0.0.1", port, folder / "ca-cert.pem", count=1)
            with pytest.raises(ssl.SSLCertVerificationError):
                await receive_results("127.0.0.1", port, pki[1] / "ca-cert.pem", count=1)
        finally:
            server.close()
            await server.wait_closed()
    run(check())
