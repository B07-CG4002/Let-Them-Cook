"""Regressions for shutdown interrupted during an already pending TLS close."""
import asyncio

from common.tls import TLS_SERVER_NAME, client_context, server_context
from common.wire import write_frame
from tools.generate_comms_pki import generate_pki
from ultra96.server import CommsServer


def test_shutdown_releases_client_already_waiting_for_tls_close(tmp_path):
    generate_pki(tmp_path)

    async def check():
        service = CommsServer(server_context(tmp_path / "server-cert.pem", tmp_path / "server-key.pem"),
                              ingest_port=0, gateway_port=0)
        await service.start()
        _, writer = await asyncio.open_connection("127.0.0.1", service.ingest_port,
            ssl=client_context(tmp_path / "ca-cert.pem"), server_hostname=TLS_SERVER_NAME)
        writer.transport.pause_reading()
        try:
            # Invalid schema makes the server enter its finally block. Pausing
            # TLS reads withholds close_notify so graceful close stays pending.
            await write_frame(writer, {})
            for _ in range(200):
                if service.metrics["rejected"] == 1 and any(
                        owned.is_closing() for owned in service._writers):
                    break
                await asyncio.sleep(0.001)
            assert service.metrics["rejected"] == 1
            assert any(owned.is_closing() for owned in service._writers)
            owned_sockets = tuple(service._sockets)
            assert len(owned_sockets) == 1

            await asyncio.wait_for(service.close(), timeout=1)

            # Cancelling cleanup must neither retain ownership nor open FDs.
            assert not service._sockets
            assert not service._writers
            assert all(connection.fileno() == -1 for connection in owned_sockets)
        finally:
            writer.transport.resume_reading()
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (OSError, asyncio.TimeoutError):
                writer.transport.abort()
            await service.close()
            # Clean up the pre-fix leak when proving this regression is RED.
            for remaining in tuple(service._writers):
                remaining.transport.abort()
            for remaining in tuple(service._sockets):
                remaining.close()

    asyncio.run(check())


def test_same_ports_restart_after_stopping_pending_handshake(tmp_path):
    generate_pki(tmp_path)

    async def check():
        context = server_context(tmp_path / "server-cert.pem", tmp_path / "server-key.pem")
        original = CommsServer(context, ingest_port=0, gateway_port=0)
        replacement = None
        writer = None
        await original.start()
        ports = original.ingest_port, original.gateway_port
        try:
            # No TLS ClientHello: stopping this accepted socket can leave the
            # listener's port in TCP TIME_WAIT on Linux.
            reader, writer = await asyncio.open_connection("127.0.0.1", ports[0])
            for _ in range(200):
                if original._sockets:
                    break
                await asyncio.sleep(0.001)
            assert original._sockets
            await asyncio.sleep(0)
            await original.close()
            assert await asyncio.wait_for(reader.read(), 1) == b""
            writer.close()
            await writer.wait_closed()

            replacement = CommsServer(context, ingest_port=ports[0], gateway_port=ports[1])
            await replacement.start()
            assert (replacement.ingest_port, replacement.gateway_port) == ports
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            await original.close()
            if replacement is not None:
                await replacement.close()

    asyncio.run(check())
