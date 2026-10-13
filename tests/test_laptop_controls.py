"""Control paths use actual wire frames while BLE and TLS endpoints are local fakes."""
import asyncio
import hashlib
import importlib.util
import struct

import pytest


def test_control_implementation_exists():
    assert importlib.util.find_spec("laptop.controls") is not None


def test_command_loop_correlates_and_verifies_modified_payload():
    from common.control import COMMAND, ControlFrame, decode_control, encode_control, transformed_values
    from common.sensor import SensorPacket, decode_packet, encode_packet
    from laptop.controls import ControlChannel, CommandPipeline

    async def run():
        original = SensorPacket(1, 7, 101, 0, (32767, -32768, 0, 1, 2, 3, 4, 5), version=2)
        sent = []
        class Client:
            mtu_size = 64
            async def write_gatt_char(self, uuid, data, response):
                request = decode_control(data, mtu=64)
                sent.append(request)
                packet = decode_packet(request.payload)
                modified = SensorPacket(1, 7, packet.seq, 50, transformed_values(packet.values), version=2)
                channel.receive(None, encode_control(ControlFrame(COMMAND | 128, 1,
                    request.request_id, payload=encode_packet(modified)), mtu=64))
        channel = ControlChannel(1, timeout=0.1)
        channel.attach(Client(), 7)
        forwarded = []
        async def forward(packet, request_id):
            forwarded.append((packet, request_id))
        pipeline = CommandPipeline(channel, forward, capacity=1)
        task = asyncio.create_task(pipeline.run())
        assert pipeline.submit(original)
        await asyncio.wait_for(pipeline.join(), 0.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert len(sent) == len(forwarded) == 1
        assert forwarded[0][0].values == transformed_values(original.values)
        assert forwarded[0][1] == 101
        assert pipeline.summary()["completed"] == 1
    asyncio.run(run())


@pytest.mark.parametrize("observed_magic, expected_magic", [
    (None, b"LC"),
    (b"W7", b"W7"),
    (b"LC", b"LC"),
])
def test_bridge_command_matches_observed_board_packet_magic(observed_magic, expected_magic):
    from common.control import COMMAND, ControlFrame, decode_control, encode_control, transformed_values
    from common.sensor import SensorPacket, encode_packet
    from laptop.bridge import Bridge, BridgeConfig

    async def run():
        bridge = Bridge(BridgeConfig(ca_file="unused", expected_device_id=1,
                                     source_audit=True, controls_enabled=True))
        sent = []

        class Client:
            mtu_size = 64

            async def write_gatt_char(self, _uuid, data, response):
                request = decode_control(data, mtu=64)
                sent.append(request.payload[:2])
                original = SensorPacket(1, 7, request.request_id, 0, (0,) * 8, version=2)
                modified = SensorPacket(1, 7, request.request_id, 50,
                                        transformed_values(original.values), version=2)
                bridge.control.receive(None, encode_control(ControlFrame(COMMAND | 128, 1,
                    request.request_id, payload=encode_packet(modified, magic=expected_magic)), mtu=64))

        bridge.control.attach(Client(), 7)
        bridge.inbox.activate(1)
        if observed_magic is not None:
            sensor = SensorPacket(1, 7, 1, 100, (0,) * 8, version=2)
            assert bridge.enqueue(1, encode_packet(sensor, magic=observed_magic))
        command = SensorPacket(1, 7, 101, 0, (0,) * 8, version=2)
        await bridge.control.command(command)
        assert sent == [expected_magic]

    asyncio.run(run())


@pytest.mark.parametrize("stats_magic, command_magic", [
    (b"W7S1", b"W7"),
    (b"LCS1", b"LC"),
])
def test_command_uses_validated_source_stats_magic_before_first_sensor(
        monkeypatch, stats_magic, command_magic):
    from types import SimpleNamespace

    from common.control import (COMMAND, CONTROL_UUID, RESPONSE_UUID, ControlFrame,
                                decode_control, encode_control, transformed_values)
    from common.sensor import SensorPacket, decode_packet, encode_packet
    from laptop.bridge import Bridge, BridgeConfig, SENSOR_UUID, SERVICE_UUID
    from laptop.source_audit import SOURCE_STATS_UUID
    import laptop.windows_pairing as pairing

    async def paired(_client):
        pass

    monkeypatch.setattr(pairing, "require_authenticated_bond", paired)

    async def run():
        bridge = Bridge(BridgeConfig(ca_file="unused", address="AA:BB",
                                     expected_device_id=1, source_audit=True,
                                     controls_enabled=True))
        active = asyncio.Event()
        sent = []

        class Scanner:
            @staticmethod
            async def find_device_by_filter(predicate, timeout):
                device = SimpleNamespace(address="AA:BB")
                assert predicate(device, SimpleNamespace(service_uuids=[SERVICE_UUID]))
                return device

        class Client:
            mtu_size = 64

            def __init__(self, device, disconnected_callback):
                self.is_connected = False
                characteristics = {
                    SENSOR_UUID: SimpleNamespace(properties=["notify"]),
                    SOURCE_STATS_UUID: SimpleNamespace(properties=["read"]),
                    CONTROL_UUID: SimpleNamespace(properties=["write"]),
                    RESPONSE_UUID: SimpleNamespace(properties=["notify"]),
                }
                service = SimpleNamespace(get_characteristic=characteristics.get)
                self.services = SimpleNamespace(get_service=lambda uuid: service)

            async def connect(self):
                self.is_connected = True

            async def disconnect(self):
                self.is_connected = False

            async def read_gatt_char(self, uuid, **kwargs):
                assert uuid == SOURCE_STATS_UUID
                return struct.pack("<4sB3xIIII", stats_magic, 1, 7, 0, 0, 0)

            async def start_notify(self, uuid, callback):
                if uuid == RESPONSE_UUID:
                    self.response_callback = callback

            async def stop_notify(self, uuid):
                pass

            async def write_gatt_char(self, uuid, data, response):
                assert uuid == CONTROL_UUID
                request = decode_control(data)
                assert request.opcode == COMMAND
                sent.append(request.payload[:2])
                original = decode_packet(request.payload)
                modified = SensorPacket(1, 7, request.request_id, 50,
                                        transformed_values(original.values), version=2)
                self.response_callback(None, encode_control(ControlFrame(COMMAND | 128, 1,
                    request.request_id, payload=encode_packet(modified, magic=request.payload[:2]))))

        worker = asyncio.create_task(bridge.ble_loop(scanner=Scanner, client_factory=Client,
                                                     active_event=active))
        try:
            await asyncio.wait_for(active.wait(), 1)
            command = SensorPacket(1, 7, 101, 0, (0,) * 8, version=2)
            await bridge.control.command(command)
            assert sent == [command_magic]
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())


def test_bounded_acceptance_and_disconnect_fail_every_accepted_command():
    from common.sensor import SensorPacket
    from laptop.controls import ControlChannel, CommandPipeline
    async def run():
        channel = ControlChannel(1)
        pipeline = CommandPipeline(channel, lambda *_: None, capacity=1)
        packet = SensorPacket(1, 7, 1, 0, (0,) * 8, version=2)
        assert not pipeline.submit(packet)
        channel.attach(type("Client", (), {"mtu_size": 64})(), 7)
        assert pipeline.submit(packet)
        assert not pipeline.submit(packet)
        await pipeline.stop()
        assert pipeline.summary() == {"accepted": 1, "rejected": 2, "completed": 0,
                                      "failed": 1, "pending": 0}
    asyncio.run(run())


def test_timeout_never_retries_command_and_disconnect_unblocks_pending():
    from common.sensor import SensorPacket
    from laptop.controls import ControlChannel
    async def run():
        writes = []
        class Client:
            mtu_size = 64
            async def write_gatt_char(self, *_args, **_kwargs):
                writes.append(1)
        channel = ControlChannel(1, timeout=0.1)
        channel.attach(Client(), 7)
        packet = SensorPacket(1, 7, 1, 0, (0,) * 8, version=2)
        with pytest.raises(asyncio.TimeoutError):
            await channel.command(packet)
        assert len(writes) == 1
        pending = asyncio.create_task(channel.command(SensorPacket(1, 7, 2, 0, (0,) * 8, version=2)))
        await asyncio.sleep(0)
        channel.detach()
        with pytest.raises(ConnectionError):
            await pending
    asyncio.run(run())


def test_file_chunks_fit_mtu_retry_identical_and_verify_receiver_digest():
    from common.control import (ControlFrame, decode_control, encode_control, FILE_BEGIN,
                                FILE_CHUNK, FILE_END, file_metadata)
    from laptop.controls import ControlChannel
    async def run():
        data = bytes(range(255)) * 3
        received = bytearray()
        requests = []
        evidence = []
        class Evidence:
            def record(self, kind, **fields): evidence.append(dict(type=kind, **fields))
        dropped = False
        class Client:
            mtu_size = 64
            async def write_gatt_char(self, _uuid, encoded, response):
                nonlocal dropped
                assert len(encoded) <= 61
                frame = decode_control(encoded, mtu=64)
                requests.append(frame)
                if frame.opcode == FILE_CHUNK:
                    if frame.offset == len(received):
                        received.extend(frame.payload)
                    if not dropped:
                        dropped = True
                        return
                payload = file_metadata(bytes(received)) if frame.opcode == FILE_END else b""
                offset = len(received)
                channel.receive(None, encode_control(ControlFrame(frame.opcode | 128, 1,
                    frame.request_id, offset=offset, payload=payload), mtu=64))
        channel = ControlChannel(1, timeout=0.1, evidence=Evidence())
        channel.attach(Client(), 7)
        result = await channel.transfer_file(data, transfer_id=100)
        assert bytes(received) == data
        assert result["sha256"] == hashlib.sha256(data).hexdigest()
        chunks = [frame for frame in requests if frame.opcode == FILE_CHUNK]
        assert chunks[0] == chunks[1]
        assert result["bytes"] == len(data)
        begin = next(item for item in evidence if item["type"] == "file_begin")
        complete = next(item for item in evidence if item["type"] == "file_complete")
        assert begin["sender_bytes"] == complete["receiver_bytes"] == len(data)
        assert begin["sender_sha256"] == complete["receiver_sha256"] == result["sha256"]
        assert result["sender_sha256"] == result["receiver_sha256"]
    asyncio.run(run())


def test_keyboard_dispatches_each_key_without_newline():
    from laptop.controls import dispatch_key
    seen = []
    for key in "12x1\n":
        dispatch_key(key, lambda device: seen.append(device))
    assert seen == [1, 2, 1]


def test_new_launcher_modes_reach_child(tmp_path):
    import demo
    args = demo._parser().parse_args(["run", "--keyboard", "--rate", "80",
        "--seed", "23", "--file", str(tmp_path / "file.bin"), "--file-device", "2"])
    command = demo.capture_command(args, tmp_path / "report.json")
    assert "--keyboard" in command
    for flag, value in (("--rate", "80"), ("--seed", "23"), ("--file-device", "2")):
        assert command[command.index(flag) + 1] == value
    assert command[command.index("--file") + 1] == str(tmp_path / "file.bin")


def test_control_forward_uses_separate_tls_and_checks_request_ack():
    from common.sensor import SensorPacket
    from laptop.bridge import Bridge, BridgeConfig
    async def run():
        class Writer:
            def close(self): pass
            async def wait_closed(self): pass
        opened, messages = [], []
        async def connect():
            writer = Writer()
            opened.append(writer)
            return object(), writer
        bridge = Bridge(BridgeConfig(ca_file="unused"), connector=connect)
        bridge.writer = "sensor connection"
        packet = SensorPacket(1, 7, 12, 200, (1,) * 8, version=2)
        async def write(writer, message, timeout):
            assert writer is not bridge.writer
            messages.append(message)
        async def read(reader, timeout):
            return dict(v=2, type="INGEST_ACK", session_id="ltc-comms", device_id=1,
                        boot_id=7, seq=12, request_id=12, status="accepted")
        bridge._write_frame, bridge._read_frame = write, read
        await bridge.forward_command(packet, 12)
        assert len(opened) == 1
        assert messages[0]["request_id"] == 12
        assert bridge.metrics.sent == bridge.metrics.acked == 0
    asyncio.run(run())


def test_reconnect_does_not_replay_accepted_old_generation_commands():
    from common.sensor import SensorPacket
    from laptop.controls import ControlChannel, CommandPipeline
    async def run():
        writes = []
        class Client:
            mtu_size = 64
            async def write_gatt_char(self, *_args, **_kwargs):
                writes.append(1)
                raise AssertionError("old-generation command must not be written")
        channel = ControlChannel(1)
        channel.attach(Client(), 7)
        pipeline = CommandPipeline(channel, lambda *_: None)
        assert pipeline.submit(SensorPacket(1, 7, 1, 0, (0,) * 8, version=2))
        channel.attach(Client(), 7)
        worker = asyncio.create_task(pipeline.run())
        await asyncio.wait_for(pipeline.join(), 0.5)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        assert pipeline.summary()["failed"] == 1
        assert channel.errors == 0
        assert writes == []
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["wrong_request", "wrong_device", "wrong_values", "duplicate"])
def test_bad_and_duplicate_ble_response_cannot_forward_an_unverified_command(failure):
    from common.control import COMMAND, ControlFrame, encode_control, transformed_values
    from common.sensor import SensorPacket, encode_packet
    from laptop.controls import ControlChannel, CommandPipeline
    async def run():
        packet = SensorPacket(1, 7, 10, 0, (0,) * 8, version=2)
        writes = []
        class Client:
            mtu_size = 64
            async def write_gatt_char(self, *_args, **_kwargs):
                identity = 11 if failure == "wrong_request" else 10
                device = 2 if failure == "wrong_device" else 1
                values = (77,) * 8 if failure == "wrong_values" else transformed_values(packet.values)
                response = SensorPacket(device, 7, identity, 1, values, version=2)
                encoded = encode_control(ControlFrame(COMMAND | 128, device, identity,
                                        payload=encode_packet(response)), mtu=64)
                channel.receive(None, encoded)
                if failure == "duplicate":
                    channel.receive(None, encoded)
        channel = ControlChannel(1, timeout=0.2)
        channel.attach(Client(), 7)
        async def forward(*args): writes.append(args)
        pipeline = CommandPipeline(channel, forward)
        worker = asyncio.create_task(pipeline.run())
        assert pipeline.submit(packet)
        await asyncio.wait_for(pipeline.join(), 1)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        assert len(writes) == (1 if failure == "duplicate" else 0)
        assert channel.duplicates == (1 if failure == "duplicate" else 0)
    asyncio.run(run())


def test_workspace_provenance_records_dirty_state_and_content_fingerprint(tmp_path):
    from laptop.reporting import source_provenance
    (tmp_path / "laptop").mkdir()
    source = tmp_path / "laptop" / "bridge.py"
    source.write_text("first", encoding="utf-8")
    first = source_provenance(tmp_path)
    source.write_text("changed", encoding="utf-8")
    second = source_provenance(tmp_path)
    assert len(first["workspace_source_sha256"]) == 64
    assert first["workspace_source_sha256"] != second["workspace_source_sha256"]
    assert first["worktree_dirty"] is None


@pytest.mark.parametrize("relative", ["common/dummy_fixtures.json",
    "phone/unity/CommsPhoneCore.cs", "ios-visualizer/CommsNative/Sources/CommsCore/Protocol.swift",
    "tools/generate_dummy_fixtures.py", "firmware/esp32/platformio.ini"])
def test_workspace_fingerprint_covers_all_extended_protocol_production_inputs(tmp_path, relative):
    from laptop.reporting import source_provenance
    source = tmp_path / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("original", encoding="utf-8")
    before = source_provenance(tmp_path)["workspace_source_sha256"]
    source.write_text("edited", encoding="utf-8")
    assert source_provenance(tmp_path)["workspace_source_sha256"] != before


def test_corrupt_file_digest_fails_and_sends_abort():
    from common.control import (ControlFrame, decode_control, encode_control, FILE_END,
                                FILE_CHUNK, FILE_ABORT, file_metadata)
    from common.wire import ProtocolError
    from laptop.controls import ControlChannel
    async def run():
        received = bytearray()
        operations = []
        class Client:
            mtu_size = 64
            async def write_gatt_char(self, _uuid, encoded, response):
                frame = decode_control(encoded, mtu=64)
                operations.append(frame.opcode)
                if frame.opcode == FILE_CHUNK:
                    received.extend(frame.payload)
                payload = file_metadata(b"x" * len(received)) if frame.opcode == FILE_END else b""
                offset = 0 if frame.opcode == FILE_ABORT else len(received)
                channel.receive(None, encode_control(ControlFrame(frame.opcode | 128, 1,
                    frame.request_id, offset=offset, payload=payload), mtu=64))
        channel = ControlChannel(1, timeout=0.2)
        channel.attach(Client(), 7)
        with pytest.raises(ProtocolError, match="SHA-256"):
            await channel.transfer_file(b"actual file bytes")
        assert operations[-1] == FILE_ABORT
    asyncio.run(run())


def test_rate_ack_must_echo_requested_rate_and_command_ack_request_is_strict():
    from common.control import ControlFrame, decode_control, encode_control
    from common.sensor import SensorPacket
    from common.wire import ProtocolError
    from laptop.controls import ControlChannel
    from laptop.bridge import Bridge, BridgeConfig
    async def run():
        class Client:
            mtu_size = 64
            async def write_gatt_char(self, _uuid, encoded, response):
                frame = decode_control(encoded, mtu=64)
                channel.receive(None, encode_control(ControlFrame(frame.opcode | 128, 1,
                    frame.request_id, payload=struct.pack("<H", 10)), mtu=64))
        channel = ControlChannel(1, timeout=0.2)
        channel.attach(Client(), 7)
        with pytest.raises(ProtocolError, match="requested source rate"):
            await channel.set_rate(80)
        bridge = Bridge(BridgeConfig(ca_file="unused"))
        packet = SensorPacket(1, 7, 12, 5, (0,) * 8, version=2)
        ack = dict(v=2, type="INGEST_ACK", session_id="ltc-comms", device_id=1,
                   boot_id=7, seq=12, request_id=13, status="accepted")
        with pytest.raises(ProtocolError, match="uncorrelated"):
            bridge._check_ack(ack, packet, request_id=12)
        ack.pop("request_id")
        with pytest.raises(ProtocolError, match="malformed"):
            bridge._check_ack(ack, packet, request_id=12)
    asyncio.run(run())


def test_old_generation_command_waiting_for_file_lock_is_never_sent_after_reconnect():
    from common.sensor import SensorPacket
    from laptop.controls import ControlChannel, CommandPipeline
    async def run():
        writes = []
        class Client:
            mtu_size = 64
            async def write_gatt_char(self, *_args, **_kwargs):
                writes.append(1)
                raise OSError("must never write")
        channel = ControlChannel(1)
        channel.attach(Client(), 7)
        pipeline = CommandPipeline(channel, lambda *_: None)
        await channel._serial.acquire()
        assert pipeline.submit(SensorPacket(1, 7, 1, 0, (0,) * 8, version=2))
        worker = asyncio.create_task(pipeline.run())
        await asyncio.sleep(0)
        channel.attach(Client(), 7)
        channel._serial.release()
        await asyncio.wait_for(pipeline.join(), 0.5)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        assert writes == []
        assert pipeline.summary()["failed"] == 1
    asyncio.run(run())


def test_native_write_cancellation_resistance_is_bounded_and_prevents_overlap():
    from common.sensor import SensorPacket
    from laptop.controls import ControlChannel
    async def run():
        release = asyncio.Event()
        class Client:
            mtu_size = 64
            async def write_gatt_char(self, *_args, **_kwargs):
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
        channel = ControlChannel(1, timeout=0.03)
        channel.attach(Client(), 7)
        task = asyncio.create_task(channel.command(SensorPacket(1, 7, 1, 0, (0,) * 8, version=2)))
        try:
            done, _ = await asyncio.wait({task}, timeout=0.2)
            assert task in done, "a resistant native write must not trap the command owner"
            with pytest.raises(asyncio.TimeoutError):
                await task
            assert channel.summary()["native_pending"] == 1
            with pytest.raises(RuntimeError, match="pending"):
                channel.attach(Client(), 7)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_file_retry_can_accept_ack_already_received_before_att_write_timeout():
    from common.control import (ControlFrame, decode_control, encode_control, FILE_CHUNK,
                                FILE_END, FILE_ABORT, file_metadata)
    from laptop.controls import ControlChannel
    async def run():
        data = b"abc"
        received = bytearray()
        chunk_attempts = 0
        class Client:
            mtu_size = 64
            async def write_gatt_char(self, _uuid, encoded, response):
                nonlocal chunk_attempts
                request = decode_control(encoded)
                if request.opcode == FILE_CHUNK:
                    chunk_attempts += 1
                    if not received:
                        received.extend(request.payload)
                offset = 0 if request.opcode == FILE_ABORT else len(received)
                payload = file_metadata(bytes(received)) if request.opcode == FILE_END else b""
                channel.receive(None, encode_control(ControlFrame(request.opcode | 128, 1,
                                request.request_id, offset, payload)))
                if request.opcode == FILE_CHUNK and chunk_attempts == 1:
                    await asyncio.sleep(0.2)
        channel = ControlChannel(1, timeout=0.05)
        channel.attach(Client(), 7)
        result = await channel.transfer_file(data)
        assert result["verified"] is True
        assert bytes(received) == data
        assert chunk_attempts == 2
    asyncio.run(run())


def test_launcher_resolves_relative_file_before_changing_child_directory(tmp_path, monkeypatch):
    import demo
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "input.bin"
    source.write_bytes(b"actual content")
    args = demo._parser().parse_args(["run", "--file", "input.bin"])
    command = demo.capture_command(args, tmp_path / "report.json")
    assert command[command.index("--file") + 1] == str(source)


def test_bridge_supervised_resistant_write_also_prevents_next_control_write():
    from common.sensor import SensorPacket
    from laptop.bridge import Bridge, BridgeConfig
    async def run():
        release = asyncio.Event()
        writes = []
        class Client:
            mtu_size = 64
            async def write_gatt_char(self, *_args, **_kwargs):
                writes.append(1)
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
        bridge = Bridge(BridgeConfig(ca_file="unused", expected_device_id=1,
                        source_audit=True, controls_enabled=True, io_timeout=0.03))
        bridge.control.attach(Client(), 7)
        try:
            with pytest.raises(asyncio.TimeoutError):
                await bridge.control.command(SensorPacket(1, 7, 1, 0, (0,) * 8, version=2))
            with pytest.raises(RuntimeError, match="pending"):
                await bridge.control.command(SensorPacket(1, 7, 2, 0, (0,) * 8, version=2))
            assert writes == [1]
            assert bridge.control.summary()["native_pending"] == 1
            assert len(bridge._retained) == 1
        finally:
            release.set()
            await asyncio.gather(*bridge._retained, return_exceptions=True)
    asyncio.run(run())
