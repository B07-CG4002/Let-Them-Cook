"""Sensor protocol v4 and v5 on the Ultra96 service: samples and ACKs at v=4 or v=5, every result
still at v=3.

Both phone apps accept only v=3 results, so the service publishes v=3 GESTURE_RESULT
envelopes in either sensor mode; the monitor envelopes stay at v=1.
"""
import asyncio
import ssl
import threading

import pytest

from common import sensor, sensor_v4, sensor_versions
from common.wire import ProtocolError, read_frame, write_frame
from ultra96.protocol import DUMMY_GESTURES, RESULT_VERSION, validate_message

SESSION = "ltc-comms"


def result(**change):
    message = dict(v=3, type="GESTURE_RESULT", session_id=SESSION, device_id=1, boot_id=7, seq=0,
                   request_id=None, result_id="1:7:0", gesture="CHOP", confidence=0.5)
    message.update(change)
    return message


def ack(version, **change):
    message = dict(v=version, type="INGEST_ACK", session_id=SESSION, device_id=1, boot_id=7, seq=0,
                   request_id=None, status="accepted")
    message.update(change)
    return message


def sample(version, device=1, boot=42, seq=0, request_id=None):
    codec = sensor_versions.codec(version)
    fixtures = codec.DUMMY_FIXTURES
    return codec.SensorPacket(device, boot, seq, seq * 20, fixtures[seq % len(fixtures)]).to_message(
        SESSION, request_id)


# --- validate_message: only SENSOR_BATCH and INGEST_ACK follow the sensor version -------------

def test_results_are_version_3_whatever_the_sensor_version():
    assert RESULT_VERSION == 3
    for version in (3, 4, 5):
        assert validate_message(result(), "GESTURE_RESULT", SESSION, sensor_version=version) == result()
        for bad in (5, 4, 2, 1):
            with pytest.raises(ProtocolError, match="unsupported protocol version"):
                validate_message(result(v=bad), "GESTURE_RESULT", SESSION, sensor_version=version)
    command = result(seq=5, request_id=5, result_id="cmd:1:7:5")
    assert validate_message(command, sensor_version=4) is command


def test_samples_and_acks_follow_the_sensor_version():
    for version in (3, 4, 5):
        assert validate_message(sample(version), sensor_version=version)["v"] == version
        assert validate_message(ack(version), sensor_version=version)["v"] == version
        for other in {3, 4, 5} - {version}:
            with pytest.raises(ProtocolError):
                validate_message(sample(version), sensor_version=other)
            with pytest.raises(ProtocolError, match="unsupported protocol version"):
                validate_message(ack(version), sensor_version=other)
    # The default is still version 3.
    for version in (4, 5):
        with pytest.raises(ProtocolError):
            validate_message(ack(version))
    for bad in (6, 2, True, "5", None):
        with pytest.raises(ProtocolError, match="sensor_version must be 3, 4 or 5"):
            validate_message(result(), sensor_version=bad)


def test_monitor_and_subscription_envelopes_stay_at_version_1_in_v4():
    for message in (dict(v=1, type="SUBSCRIBE", session_id=SESSION),
                    dict(v=1, type="MONITORING", session_id=SESSION)):
        assert validate_message(message, sensor_version=4) is message
        with pytest.raises(ProtocolError):
            validate_message(dict(message, v=4), sensor_version=4)


# --- a real TLS service in each sensor mode ----------------------------------------------------

@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    from tools.generate_comms_pki import generate_pki
    folder = tmp_path_factory.mktemp("comms-v4-service-pki")
    generate_pki(folder)
    return folder


async def start_server(pki, **kwargs):
    from common.tls import server_context
    from ultra96.server import CommsServer
    server = CommsServer(server_context(pki / "server-cert.pem", pki / "server-key.pem"),
                         ingest_port=0, gateway_port=0, **kwargs)
    await server.start()
    return server


async def connect(pki, port):
    from common.tls import TLS_SERVER_NAME, client_context
    return await asyncio.open_connection("127.0.0.1", port, ssl=client_context(pki / "ca-cert.pem"),
                                         server_hostname=TLS_SERVER_NAME)


async def disconnect(writer):
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, ssl.SSLError):
        pass


async def subscribe(pki, server):
    reader, writer = await connect(pki, server.gateway_port)
    await write_frame(writer, {"v": 1, "type": "SUBSCRIBE", "session_id": SESSION})
    assert await read_frame(reader) == {"v": 1, "type": "SUBSCRIBED", "session_id": SESSION}
    return reader, writer


@pytest.mark.parametrize("version", [3, 4, 5])
def test_the_service_acks_at_the_sensor_version_and_publishes_v3_results(pki, version):
    async def check():
        server = await start_server(pki, sensor_version=version)
        try:
            assert server.sensor_version == version
            phone, phone_writer = await subscribe(pki, server)
            laptop, laptop_writer = await connect(pki, server.ingest_port)
            for device in (1, 2):
                for seq in range(3):
                    await write_frame(laptop_writer, sample(version, device, 42, seq))
                    assert await read_frame(laptop) == dict(
                        v=version, type="INGEST_ACK", session_id=SESSION, device_id=device, boot_id=42,
                        seq=seq, request_id=None, status="accepted")
                    # The built-in dummy model answers every accepted sample once, at v=3.
                    published = await read_frame(phone)
                    assert published.pop("gesture") in DUMMY_GESTURES
                    assert published == dict(
                        v=3, type="GESTURE_RESULT", session_id=SESSION, device_id=device, boot_id=42,
                        seq=seq, request_id=None, result_id="{}:42:{}".format(device, seq), confidence=1.0)
            # A command (request_id = seq) never reaches the model; its echo is a v=3 result too.
            await write_frame(laptop_writer, sample(version, 2, 43, 7, request_id=7))
            assert await read_frame(laptop) == dict(
                v=version, type="INGEST_ACK", session_id=SESSION, device_id=2, boot_id=43, seq=7,
                request_id=7, status="accepted")
            echo = await read_frame(phone)
            assert echo["v"] == 3 and echo["result_id"] == "cmd:2:43:7" and echo["request_id"] == 7
            assert validate_message(echo, "GESTURE_RESULT", SESSION) is echo
            assert server.metrics["model_results"] == 6
            await disconnect(laptop_writer)
            await disconnect(phone_writer)
        finally:
            await server.close()
    asyncio.run(check())


class ScriptedModel:
    """Emits one result for the sample, and a second (refused) one for the same identity."""
    name = "scripted-v4"

    def __init__(self):
        self.values = []

    def start(self, emit):
        self.emit = emit

    def on_sample(self, device_id, boot_id, seq, uptime_ms, values):
        self.values.append(values)
        self.emit("POURING", 0.75, device_id, boot_id, seq)
        self.emit("POURING", 0.75, device_id, boot_id, seq)


def test_a_model_result_in_a_v4_service_is_published_at_v3_once(pki):
    async def check():
        model = ScriptedModel()
        server = await start_server(pki, sensor_version=4, model=model)
        try:
            phone, phone_writer = await subscribe(pki, server)
            laptop, laptop_writer = await connect(pki, server.ingest_port)
            await write_frame(laptop_writer, sample(4, 1, 9, 1))
            assert (await read_frame(laptop))["v"] == 4
            assert await read_frame(phone) == dict(
                v=3, type="GESTURE_RESULT", session_id=SESSION, device_id=1, boot_id=9, seq=1,
                request_id=None, result_id="1:9:1", gesture="POURING", confidence=0.75)
            with pytest.raises(asyncio.TimeoutError):
                await read_frame(phone, timeout=0.2)
            assert server.metrics["model_results"] == 1 and server.metrics["model_results_duplicate"] == 1
            # The model saw the twelve physical values of the v4 sample.
            assert len(model.values) == 1 and len(model.values[0]) == 12
            await disconnect(laptop_writer)
            await disconnect(phone_writer)
        finally:
            await server.close()
    asyncio.run(check())


def test_the_phone_simulator_receives_the_results_of_a_v4_service(pki):
    from laptop.phone_simulator import receive_results

    async def check():
        server = await start_server(pki, sensor_version=4)
        received = []
        try:
            phone = asyncio.create_task(receive_results("127.0.0.1", server.gateway_port,
                                                        pki / "ca-cert.pem", count=3,
                                                        on_result=received.append))
            for _ in range(200):
                if server.metrics["subscribers"]:
                    break
                await asyncio.sleep(0.01)
            laptop, writer = await connect(pki, server.ingest_port)
            for seq in range(2):
                await write_frame(writer, sample(4, 1, 42, seq))
                await read_frame(laptop)
            await write_frame(writer, sample(4, 1, 42, 5, request_id=5))
            await read_frame(laptop)
            assert await asyncio.wait_for(phone, 5) == 3
            assert [item["result_id"] for item in received] == ["1:42:0", "1:42:1", "cmd:1:42:5"]
            assert {item["v"] for item in received} == {3}
            await disconnect(writer)
        finally:
            await server.close()
    asyncio.run(check())


def test_the_board_monitor_follows_a_v4_service(pki):
    from laptop.board_monitor import CONNECTED, BoardMonitor

    async def check():
        server = await start_server(pki, sensor_version=4)
        updates = []
        lock = threading.Lock()

        def on_event(kind, payload):
            with lock:
                updates.append((kind, payload))

        monitor = BoardMonitor(server.gateway_port, pki / "ca-cert.pem", SESSION, on_event,
                               retry_seconds=0.05).start()
        loop = asyncio.get_running_loop()
        try:
            for _ in range(500):
                if monitor.state == CONNECTED:
                    break
                await asyncio.sleep(0.01)
            assert monitor.state == CONNECTED
            phone, phone_writer = await subscribe(pki, server)
            laptop, writer = await connect(pki, server.ingest_port)
            await write_frame(writer, sample(4, 2, 42, 0))
            await read_frame(laptop)
            await write_frame(writer, sample(4, 2, 42, 3, request_id=3))
            await read_frame(laptop)
            events = []
            for _ in range(500):
                with lock:
                    events = [event for kind, payload in updates if kind == "update"
                              for event in payload["update"]["events"]]
                    last = [payload["update"] for kind, payload in updates if kind == "update"][-1:]
                if {"result", "command"} <= {event[0] for event in events}:
                    break
                await asyncio.sleep(0.01)
            assert [event[:4] for event in events if event[0] in ("result", "command")] == [
                ["result", 2, 42, 0], ["command", 2, 42, 3]]
            assert last and last[0]["v"] == 1
            await disconnect(writer)
            await disconnect(phone_writer)
        finally:
            await loop.run_in_executor(None, monitor.stop)
            await server.close()
    asyncio.run(check())


def test_the_default_service_is_still_version_3(pki):
    async def check():
        server = await start_server(pki)
        try:
            assert server.sensor_version == 3
            laptop, writer = await connect(pki, server.ingest_port)
            await write_frame(writer, sample(4, 1, 42, 0))
            # A v4 sample is refused by a v3 service: the connection closes without an ACK.
            with pytest.raises((asyncio.IncompleteReadError, ConnectionError, ssl.SSLError)):
                await read_frame(laptop, timeout=2.0)
            assert server.metrics["rejected"] == 1
            await disconnect(writer)
        finally:
            await server.close()
    asyncio.run(check())
