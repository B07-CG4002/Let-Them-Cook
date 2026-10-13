"""Listener error recovery without BLE, SSH, serial or external connections."""
import asyncio
import contextlib
import errno
import io
import ssl
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from ultra96.server import CommsServer, _run


def context():
    value = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    value.minimum_version = ssl.TLSVersion.TLSv1_2
    return value


class AcceptRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_accept_errors_retry_with_capped_delay_and_preserve_cancel(self):
        service = CommsServer(context())
        failures = [ConnectionAbortedError("queued peer aborted")]
        failures.extend(OSError(errno.EMFILE, "temporary descriptors exhausted") for _ in range(8))
        failures.append(asyncio.CancelledError())
        accept = AsyncMock(side_effect=failures)
        delays = []
        async def sleep(delay):
            delays.append(delay)
        loop = asyncio.get_running_loop()
        with patch.object(loop, "sock_accept", accept), patch("ultra96.server.asyncio.sleep", sleep):
            with self.assertRaises(asyncio.CancelledError):
                await service._accept(object(), False)
        self.assertEqual(accept.await_count, 10)
        self.assertEqual(len(delays), 9)
        self.assertTrue(all(0 < delay <= 1.0 for delay in delays))
        self.assertEqual(delays[-1], 1.0)
        self.assertEqual(service.metrics["accept_retries"], 9)

    async def test_unexpected_listener_failure_is_observable_and_closes_cleanly(self):
        service = CommsServer(context(), ingest_port=0, gateway_port=0)
        async def failed_accept(*args):
            raise OSError(errno.EINVAL, "unexpected listener failure")
        with patch.object(service, "_accept", failed_accept):
            await service.start()
            try:
                with self.assertRaisesRegex(RuntimeError, "listener failed"):
                    await asyncio.wait_for(service.wait_failure(), 1.0)
                self.assertGreaterEqual(service.metrics["accept_failures"], 1)
            finally:
                await service.close()
        self.assertFalse(service._listeners)
        self.assertFalse(service._accept_tasks)

    async def test_cli_awaits_listener_failure_instead_of_waiting_forever(self):
        class Service:
            ingest_port = 8888
            gateway_port = 9999
            metrics = {}
            closed = False
            async def start(self):
                pass
            async def wait_failure(self):
                raise RuntimeError("listener failed")
            async def close(self):
                self.closed = True
        service = Service()
        args = SimpleNamespace(cert="unused", key="unused", session_id="ltc-comms", ingest_port=8888, gateway_port=9999)
        with patch("ultra96.server.CommsServer", return_value=service), patch("ultra96.server.server_context", return_value=context()), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "listener failed"):
                await asyncio.wait_for(_run(args), 0.1)
        self.assertTrue(service.closed)
