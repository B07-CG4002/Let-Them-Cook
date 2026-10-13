import asyncio

from common.tls import server_context
from laptop.bridge import Bridge, BridgeConfig
from laptop.dual_bridge import DualBridge
from tools.generate_comms_pki import generate_pki
from ultra96.server import CommsServer


def make_dual(ca, port, *, expected_rate=20):
    def bridge(device):
        return Bridge(BridgeConfig(
            ca_file=str(ca), port=port, expected_device_id=device,
            source_audit=True, diagnostic_unprotected=True))
    left, right = bridge(1), bridge(2)
    return left, right, DualBridge(
        left, right, expected_rate=expected_rate,
        startup_timeout=2, drain_timeout=2, shutdown_timeout=2)


def test_real_tls_receives_exact_device_tagged_sets_from_both_sources(tmp_path):
    generate_pki(tmp_path)

    async def check():
        server = CommsServer(server_context(
            tmp_path / "server-cert.pem", tmp_path / "server-key.pem"),
            ingest_port=0, gateway_port=0)
        await server.start()
        try:
            _, _, dual = make_dual(tmp_path / "ca-cert.pem", server.ingest_port)
            report = await dual.run(duration=0.2, mock=True, mock_rate=30)
            assert report["clean"] is True
            expected = set()
            for device in (1, 2):
                source = report["devices"][str(device)]["source"]
                expected.update((device, source["boot_id"], seq)
                                for seq in range(source["generated"]))
            assert set(server._v2_recent) == expected
            assert server.metrics["accepted"] == len(expected)
        finally:
            await server.close()

    asyncio.run(check())


def test_one_transport_disconnect_does_not_stop_healthy_peer_and_fails_verdict(tmp_path):
    generate_pki(tmp_path)

    async def wait_until(predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(wait(), 2)

    async def check():
        server = CommsServer(server_context(
            tmp_path / "server-cert.pem", tmp_path / "server-key.pem"),
            ingest_port=0, gateway_port=0)
        await server.start()
        try:
            left, right, dual = make_dual(
                tmp_path / "ca-cert.pem", server.ingest_port, expected_rate=10)
            running = asyncio.create_task(
                dual.run(duration=0.6, mock=True, mock_rate=30))
            await wait_until(lambda: left.metrics.acked >= 2 and right.metrics.acked >= 2)
            right_before = right.metrics.acked
            left.writer.transport.abort()
            await wait_until(lambda: right.metrics.acked >= right_before + 3)
            report = await running

            assert report["devices"]["2"]["acked"] >= right_before + 3
            assert report["devices"]["1"]["transport_errors"] >= 1
            assert report["devices"]["1"]["clean"] is False
            assert report["clean"] is False
        finally:
            await server.close()

    asyncio.run(check())
