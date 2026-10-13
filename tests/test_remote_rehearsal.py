"""Forward-only acceptance checks use externally owned TLS fixtures, never SSH/BLE."""
import asyncio
import importlib.util

import pytest


def test_remote_runner_exists():
    assert importlib.util.find_spec("tools.rehearse_remote_comms") is not None, (
        "forward-only remote rehearsal is missing")


def test_correlation_requires_exact_ids_and_bounds_the_ledger():
    from tools.rehearse_remote_comms import Correlation
    ledger = Correlation(capacity=2)
    ledger.result("1:2:4")  # Delivery may precede receipt of its ingestion ACK.
    ledger.ack("1:2:4", "accepted")
    assert ledger.summary()["matched"] == 1
    ledger.ack("1:2:5", "accepted")
    ledger.result("1:2:6")
    report = ledger.summary()
    assert report["missing_results"] == 1
    assert report["unexpected_results"] == 1
    assert not report["exact_match"]  # Equal counts do not prove correlation.
    ledger.result("1:2:4")
    assert ledger.summary()["duplicate_results"] == 1
    ledger.result("1:2:7")
    assert ledger.summary()["overflow"] == 1
    assert len(ledger.result_ids) == 2
    ledger.ack("1:2:4", "duplicate")
    assert ledger.summary()["duplicate_acks"] == 1


def test_idle_wait_is_separate_from_partial_frame_deadline():
    from tools.rehearse_remote_comms import read_live_frame
    from common.wire import encode_frame
    async def check():
        idle = asyncio.StreamReader()
        async def delayed():
            await asyncio.sleep(0.06)
            idle.feed_data(encode_frame({"v": 1}))
        task = asyncio.create_task(delayed())
        assert await read_live_frame(idle, idle_timeout=0.5, frame_timeout=0.02) == {"v": 1}
        await task
        partial = asyncio.StreamReader()
        partial.feed_data(b"\x00")
        start = asyncio.get_running_loop().time()
        with pytest.raises(asyncio.TimeoutError):
            await read_live_frame(partial, idle_timeout=1.0, frame_timeout=0.03)
        assert asyncio.get_running_loop().time() - start < 0.3
    asyncio.run(check())


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    from tools.generate_comms_pki import generate_pki
    folder = tmp_path_factory.mktemp("remote-runner-pki")
    wrong = tmp_path_factory.mktemp("remote-runner-wrong-pki")
    generate_pki(folder)
    generate_pki(wrong)
    return folder, wrong


async def start_external_server(folder):
    from common.tls import server_context
    from ultra96.server import CommsServer
    server = CommsServer(server_context(folder / "server-cert.pem", folder / "server-key.pem"),
                         ingest_port=0, gateway_port=0)
    await server.start()
    return server


def test_external_tls_100_exact_pairs_and_second_run_has_new_boot(pki):
    from tools.rehearse_remote_comms import rehearse_remote
    async def check():
        server = await start_external_server(pki[0])
        events = []
        try:
            first = await rehearse_remote(str(pki[0] / "ca-cert.pem"), duration=15,
                target=100, ingest_port=server.ingest_port, gateway_port=server.gateway_port,
                on_event=events.append)
            assert first["passed"], first
            assert first["correlation"]["matched"] == 100
            assert events[0]["event"] == "subscribed"
            assert server.metrics["disconnected_results"] == 0
            assert "unverified" in first["topology"]
            second = await rehearse_remote(str(pki[0] / "ca-cert.pem"), duration=3,
                target=5, ingest_port=server.ingest_port, gateway_port=server.gateway_port)
            assert second["passed"], second
            assert first["synthetic_boot_id"] != second["synthetic_boot_id"]
            assert server.metrics["duplicates"] == 0
            assert len([item for item in events if item["event"] == "ack"]) == 100
            assert len([item for item in events if item["event"] == "result"]) == 100
            sustained = await rehearse_remote(str(pki[0] / "ca-cert.pem"), duration=1,
                target=0, ingest_port=server.ingest_port, gateway_port=server.gateway_port)
            assert sustained["passed"], sustained
            assert sustained["sustained"]["minimum_count"] == 9
            assert sustained["sustained"]["coverage_met"]
        finally:
            await server.close()
    asyncio.run(check())


def test_wrong_ca_stops_before_any_input(pki):
    from tools.rehearse_remote_comms import rehearse_remote
    async def check():
        server = await start_external_server(pki[0])
        try:
            report = await rehearse_remote(str(pki[1] / "ca-cert.pem"), duration=2,
                target=5, ingest_port=server.ingest_port, gateway_port=server.gateway_port)
            assert not report["passed"]
            assert report["subscriber"]["fatal_error"] == "SSLCertVerificationError"
            assert report["bridge"]["sent"] == 0
            assert server.metrics["accepted"] == 0
        finally:
            await server.close()
    asyncio.run(check())


def test_ready_subscription_survives_slow_input_setup(pki, monkeypatch):
    from tools.rehearse_remote_comms import ObservedBridge, rehearse_remote
    original = ObservedBridge.dummy_loop
    async def delayed(self):
        # Ordinary Phone simulator's five-second idle deadline would lose this
        # ready subscription while the producer is still discovering BLE.
        await asyncio.sleep(5.2)
        await original(self)
    monkeypatch.setattr(ObservedBridge, "dummy_loop", delayed)
    async def check():
        server = await start_external_server(pki[0])
        try:
            report = await rehearse_remote(str(pki[0] / "ca-cert.pem"), duration=8,
                target=2, ingest_port=server.ingest_port, gateway_port=server.gateway_port)
            assert report["passed"], report
            assert report["subscriber"]["connections"] == 1
            assert server.metrics["subscribers"] == 1
        finally:
            await server.close()
    asyncio.run(check())


@pytest.mark.parametrize("reboot", [False, True])
def test_silence_or_orderly_reboot_cannot_be_a_clean_pass(pki, monkeypatch, reboot):
    from common.sensor import SensorPacket, dummy_values, encode_packet
    from tools.rehearse_remote_comms import ObservedBridge, rehearse_remote
    async def source(self):
        self.inbox.activate(1)
        for boot in ([123, 124] if reboot else [123]):
            packet = SensorPacket(1, boot, 0, 0, dummy_values(0))
            self.inbox.put(1, encode_packet(packet), self.clock())
            await asyncio.sleep(0.1)
        await asyncio.Event().wait()
    monkeypatch.setattr(ObservedBridge, "dummy_loop", source)
    async def check():
        server = await start_external_server(pki[0])
        try:
            report = await rehearse_remote(str(pki[0] / "ca-cert.pem"), duration=0.5,
                target=2 if reboot else 0, ingest_port=server.ingest_port,
                gateway_port=server.gateway_port)
            assert report["correlation"]["exact_match"]
            assert report["bridge"]["acked"] == (2 if reboot else 1)
            assert not report["passed"], report
            if reboot:
                assert report["bridge"]["new_boots"] == 1
            else:
                assert not report["sustained"]["coverage_met"]
        finally:
            await server.close()
    asyncio.run(check())


def test_gateway_disconnect_resubscribes_and_reports_fault_without_clean_pass(pki):
    from tools.rehearse_remote_comms import rehearse_remote
    async def check():
        server = await start_external_server(pki[0])
        cut = False
        def event(item):
            nonlocal cut
            if item["event"] == "result" and not cut:
                cut = True
                server._subscriber[0].close()
        try:
            report = await rehearse_remote(str(pki[0] / "ca-cert.pem"), duration=3,
                target=0, ingest_port=server.ingest_port, gateway_port=server.gateway_port,
                on_event=event)
            assert not report["passed"]
            assert report["subscriber"]["connections"] >= 2
            assert report["subscriber"]["transport_errors"] >= 1
            assert report["correlation"]["matched"] >= 10
            assert report["correlation"]["missing_results"] > 0
            assert report["subscriber"]["gaps"] > 0
        finally:
            await server.close()
    asyncio.run(check())


def test_initial_burst_then_long_silence_fails_even_with_enough_exact_pairs(pki, monkeypatch):
    from common.sensor import SensorPacket, dummy_values, encode_packet
    from tools.rehearse_remote_comms import ObservedBridge, rehearse_remote
    async def burst(self):
        self.inbox.activate(1)
        for seq in range(60):
            packet = SensorPacket(1, self.synthetic_boot_id, seq, seq * 100, dummy_values(seq))
            self.inbox.put(1, encode_packet(packet), self.clock())
        await asyncio.Event().wait()
    monkeypatch.setattr(ObservedBridge, "dummy_loop", burst)
    async def check():
        server = await start_external_server(pki[0])
        try:
            report = await rehearse_remote(str(pki[0] / "ca-cert.pem"), duration=6.3,
                target=0, ingest_port=server.ingest_port, gateway_port=server.gateway_port)
            assert report["correlation"]["exact_match"]
            assert report["bridge"]["acked"] == 60
            assert report["sustained"]["count_met"]
            assert not report["sustained"]["activity_met"]
            assert report["timing"]["ack"]["max_gap_seconds"] > 5
            assert report["timing"]["result"]["max_gap_seconds"] > 5
            assert not report["passed"]
        finally:
            await server.close()
    asyncio.run(check())
