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


def message(seq=0, session="ltc-comms"):
    return {"v": 1, "type": "SENSOR_BATCH", "session_id": session,
            "device_id": 1, "boot_id": 42, "seq": seq, "uptime_ms": seq * 100,
            "values": [-1000 + seq % 2000 + 10 * i for i in range(8)]}


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
                    "v": 1, "type": "INGEST_ACK", "session_id": "ltc-comms",
                    "device_id": 1, "boot_id": 42, "seq": seq, "status": "accepted"}
                assert await read_frame(phone) == {
                    "v": 1, "type": "GESTURE_RESULT", "session_id": "ltc-comms",
                    "device_id": 1, "boot_id": 42, "seq": seq,
                    "result_id": "1:42:" + str(seq),
                    "gesture": ["REST", "FIST", "OPEN", "POINT"][seq % 4], "confidence": 1.0}
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
                {"v": True}, {"session_id": "wrong"}, {"device_id": 3},
                {"boot_id": -1}, {"seq": 4294967296}, {"uptime_ms": False},
                {"values": [0] * 8}, {"extra": 1}, {"type": "SUBSCRIBE"})]
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


def test_dedup_evicts_oldest_trace_at_4096(pki):
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
            await write_frame(writer, message(0))
            assert (await read_frame(reader))["status"] == "accepted"
            await disconnect(writer)
        finally:
            await server.close()
    run(check())


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
            await write_frame(writer, {"v": 1, "type": "GESTURE_RESULT", "session_id": "ltc-comms",
                "device_id": 1, "boot_id": 42, "seq": 0, "result_id": "wrong", "gesture": "REST", "confidence": 1.0})
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
