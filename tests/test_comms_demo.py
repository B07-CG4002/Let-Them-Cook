"""Teacher-facing evidence must describe actual bytes and correlated identities."""
import asyncio
import json
import subprocess
import sys

import pytest

from common.sensor import SensorPacket, dummy_values, encode_packet
from common.tls import client_context, server_context
from common.wire import ProtocolError, read_frame, write_frame
from tools.comms_demo import DemoBridge, audit_logs, packet_example
from laptop.bridge import BridgeConfig


def test_packet_example_matches_literal_bytes_and_real_protocol():
    example = packet_example()
    assert example["kind"] == "illustrative example; not captured hardware evidence"
    assert example["ble_hex"] == "4c430101070000002a0000006810000042fc4cfc56fc60fc6afc74fc7efc88fc"
    assert example["ble_bytes"] == 32
    assert example["sensor_batch"]["values"] == [-958, -948, -938, -928, -918, -908, -898, -888]
    assert example["gesture_result"]["result_id"] == "1:7:42"
    assert example["gesture_result"]["gesture"] == "OPEN"
    for frame in example["frames"].values():
        assert int(frame["length_prefix_hex"], 16) == len(frame["json_utf8"].encode())


def records():
    return [
        {"event": "subscribed", "session_id": "ltc-comms"},
        {"event": "result", "session_id": "ltc-comms", "result_id": "1:7:42", "gesture": "OPEN", "confidence": 1.0},
        {"event": "ack", "session_id": "ltc-comms", "result_id": "1:7:42", "status": "accepted"},
        {"event": "summary", "session_id": "ltc-comms", "passed": True, "source": "real BLE", "bridge": {"acked": 1}},
    ]


def save(tmp_path, rows, name="events.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(row) for row in rows)+"\n", encoding="utf-8-sig")
    return path


def test_audit_recomputes_ids_not_counts_and_labels_offline(tmp_path):
    path = save(tmp_path, records())
    result = audit_logs(path, minimum_count=1)
    assert result["audit_passed"]
    assert result["matched"] == 1
    assert result["evidence_kind"] == "offline log correlation; no live connection or physical provenance verified"
    rows = records()
    rows[1]["result_id"] = "1:8:42"
    result = audit_logs(save(tmp_path, rows), minimum_count=1)
    assert not result["audit_passed"]
    assert result["missing_results"] == result["unexpected_results"] == 1


@pytest.mark.parametrize("change", ["duplicate", "empty", "no_summary", "fault", "minimum", "wrong_count"])
def test_incomplete_duplicate_fault_or_short_evidence_cannot_pass(tmp_path, change):
    rows = records()
    minimum = 1
    if change == "duplicate":
        rows.insert(2, rows[1])
    elif change == "empty":
        rows = [rows[-1]]
        rows[0]["bridge"]["acked"] = 0
    elif change == "no_summary":
        rows.pop()
    elif change == "fault":
        rows[-1]["passed"] = False
    elif change == "minimum":
        minimum = 100
    elif change == "wrong_count":
        rows[-1]["bridge"]["acked"] = 100
    assert not audit_logs(save(tmp_path, rows), minimum_count=minimum)["audit_passed"]


@pytest.mark.parametrize("change", ["wrong_gesture", "wrong_session", "bad_id", "duplicate_json", "truncated", "after_summary", "summary_session", "missing_summary_session", "deep_json"])
def test_invalid_evidence_is_rejected(tmp_path, change):
    rows = records()
    if change == "wrong_gesture":
        rows[1]["gesture"] = "FIST"
    elif change == "wrong_session":
        rows[1]["session_id"] = "another-demo"
    elif change == "bad_id":
        rows[1]["result_id"] = "1:7:4294967296"
    elif change == "after_summary":
        rows.append(rows[2])
    elif change == "summary_session":
        rows[-1]["session_id"] = "another-demo"
    elif change == "missing_summary_session":
        del rows[-1]["session_id"]
    path = save(tmp_path, rows)
    if change == "duplicate_json":
        path.write_text('{"event":"context","event":"summary"}\n')
    elif change == "truncated":
        path.write_text('{"event":')
    elif change == "deep_json":
        path.write_text('{"event":"context","value":' + '['*10000 + '0' + ']'*10000 + '}')
    with pytest.raises(ValueError):
        audit_logs(path, minimum_count=1)


def test_raw_phone_results_match_sender_ack_without_desktop_subscriber(tmp_path):
    rows = records()
    rows.pop(1)
    phone_result = dict(v=1, type="GESTURE_RESULT", session_id="ltc-comms", device_id=1,
                        boot_id=7, seq=42, result_id="1:7:42", gesture="OPEN", confidence=1.0)
    phone = save(tmp_path, [phone_result], "phone.jsonl")
    result = audit_logs(save(tmp_path, rows), phone_results=phone, minimum_count=1)
    assert result["audit_passed"] and result["matched"] == 1
    # Combining two subscribers must not silently select the convenient result set.
    with pytest.raises(ValueError):
        audit_logs(save(tmp_path, records()), phone_results=phone, minimum_count=1)


def test_recorded_reconnect_is_retained_as_failed_fault_not_invalid_log(tmp_path):
    rows = records()
    rows.insert(1, dict(event="subscriber_reconnect", session_id="ltc-comms",
                        error_type="ConnectionResetError", delay_seconds=.5))
    result = audit_logs(save(tmp_path, rows), minimum_count=1)
    assert result["matched"] == 1
    assert result["interruption_events"] == 1
    assert not result["audit_passed"]


def test_sender_timing_exposes_stall_and_trailing_silence():
    async def check():
        now = [100.0]
        bridge = DemoBridge(BridgeConfig(ca_file="unused.pem"), lambda e: None, clock=lambda: now[0])
        packet = SensorPacket(1, 7, 42, 4200, dummy_values(42))
        ack = dict(v=1, type="INGEST_ACK", session_id="ltc-comms", device_id=1, boot_id=7, seq=42, status="accepted")
        now[0] = 103.0
        bridge._check_ack(ack, packet)
        now[0] = 109.0
        bridge._check_ack(ack, packet)
        timing = bridge.ack_timing()
        assert timing["first_activity_seconds"] == 3.0
        assert timing["max_gap_seconds"] == 6.0
        assert not timing["activity_met"]
        now[0] = 120.0
        assert bridge.ack_timing()["max_gap_seconds"] == 11.0
    asyncio.run(check())


def test_sender_observes_actual_tls_ack_and_does_not_create_a_subscriber(tmp_path):
    from tools.generate_comms_pki import generate_pki
    from ultra96.server import CommsServer
    generate_pki(tmp_path / "pki")
    async def check():
        pki = tmp_path / "pki"
        server = CommsServer(server_context(pki / "server-cert.pem", pki / "server-key.pem"),
                             ingest_port=0, gateway_port=0)
        await server.start()
        events = []
        bridge = DemoBridge(BridgeConfig(ca_file=str(pki / "ca-cert.pem"), port=server.ingest_port), events.append)
        writer = None
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.gateway_port,
                ssl=client_context(pki / "ca-cert.pem"), server_hostname="ultra96.week7.internal")
            await write_frame(writer, dict(v=1, type="SUBSCRIBE", session_id="ltc-comms"))
            assert (await read_frame(reader))["type"] == "SUBSCRIBED"
            packet = SensorPacket(1, 7, 42, 4200, dummy_values(42))
            bridge.inbox.activate(1)
            bridge.inbox.put(1, encode_packet(packet), bridge.clock())
            await bridge.forward_one()
            assert (await read_frame(reader))["result_id"] == "1:7:42"
            assert [e["event"] for e in events] == ["packet", "ack"]
            assert events[0]["ble_hex"] == packet_example()["ble_hex"]
            assert events[1]["result_id"] == "1:7:42"
            assert bridge.metrics.acked == 1
            before = len(events)
            with pytest.raises(ProtocolError):
                bridge._check_ack(dict(v=1, type="INGEST_ACK", session_id="ltc-comms",
                    device_id=1, boot_id=8, seq=42, status="accepted"), packet)
            assert len(events) == before
        finally:
            await bridge.close_transport()
            if writer:
                writer.close()
                await writer.wait_closed()
            await server.close()
    asyncio.run(check())


def test_cli_returns_failure_for_mismatched_saved_results(tmp_path):
    rows = records()
    rows[1]["result_id"] = "1:8:42"
    result = subprocess.run([sys.executable, "-m", "tools.comms_demo", "audit",
        str(save(tmp_path, rows)), "--minimum-count", "1"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert not json.loads(result.stdout)["audit_passed"]
