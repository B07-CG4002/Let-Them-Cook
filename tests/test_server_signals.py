"""CLI shutdown with real Linux signals; never contact the deployed service."""
import asyncio
import contextlib
import io
import json
import selectors
import signal
import socket
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from common.tls import TLS_SERVER_NAME, client_context
from common.wire import encode_frame
from tools.generate_comms_pki import generate_pki
from ultra96.server import _run


ROOT = Path(__file__).resolve().parents[1]


def read_message(connection):
    def exact(length):
        result = b""
        while len(result) < length:
            part = connection.recv(length - len(result))
            if not part:
                raise EOFError("service closed before its response")
            result += part
        return result
    length, = struct.unpack("!I", exact(4))
    if not 1 <= length <= 16384:
        raise ValueError("invalid service frame length")
    return json.loads(exact(length))


@unittest.skipUnless(sys.platform.startswith("linux"), "actual POSIX signals require Linux/WSL")
class LinuxSignalTests(unittest.TestCase):
    def check_signal(self, stop_signal):
        with tempfile.TemporaryDirectory(prefix="comms-signals-") as folder:
            generate_pki(folder)
            pki = Path(folder)
            process = subprocess.Popen([
                sys.executable, "-m", "ultra96.server",
                "--cert", str(pki / "server-cert.pem"),
                "--key", str(pki / "server-key.pem"),
                "--ingest-port", "0", "--gateway-port", "0"],
                cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True)
            connections = []
            try:
                with selectors.DefaultSelector() as ready:
                    ready.register(process.stdout, selectors.EVENT_READ)
                    self.assertTrue(ready.select(timeout=5), "server did not report startup")
                listening = json.loads(process.stdout.readline())
                self.assertEqual(listening["event"], "listening")
                ports = listening["ingest_port"], listening["gateway_port"]
                context = client_context(pki / "ca-cert.pem")
                for port in ports:
                    raw = socket.create_connection(("127.0.0.1", port), timeout=3)
                    connections.append(context.wrap_socket(raw, server_hostname=TLS_SERVER_NAME))
                ingest, viewer = connections
                viewer.sendall(encode_frame(dict(v=1, type="SUBSCRIBE", session_id="ltc-comms")))
                self.assertEqual(read_message(viewer)["type"], "SUBSCRIBED")
                ingest.sendall(encode_frame(dict(v=1, type="SENSOR_BATCH", session_id="ltc-comms",
                    device_id=1, boot_id=42, seq=0, uptime_ms=0,
                    values=[-1000 + 10 * index for index in range(8)])))
                self.assertEqual(read_message(ingest)["status"], "accepted")
                self.assertEqual(read_message(viewer)["result_id"], "1:42:0")
                # Include an owned connection stalled before its TLS handshake.
                connections.append(socket.create_connection(("127.0.0.1", ports[0]), timeout=3))

                process.send_signal(stop_signal)
                output, errors = process.communicate(timeout=5)

                self.assertEqual(process.returncode, 0, (output, errors))
                stopped = [json.loads(line) for line in output.splitlines()]
                self.assertEqual(len(stopped), 1, (output, errors))
                self.assertEqual(stopped[0]["event"], "stopped")
                self.assertEqual(stopped[0]["metrics"]["accepted"], 1)
                self.assertEqual(stopped[0]["metrics"]["subscribers"], 1)
                self.assertNotIn("Traceback", errors)
                for connection in connections:
                    try:
                        self.assertEqual(connection.recv(1), b"")
                    except ConnectionResetError:
                        pass  # A pre-handshake socket can close with a TCP reset.
                # Restartability includes Linux TIME_WAIT on the exact old ports.
                for port in ports:
                    with socket.socket() as listener:
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        listener.bind(("127.0.0.1", port))
                        listener.listen(1)
            finally:
                for connection in connections:
                    connection.close()
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    def test_sigterm_reports_metrics_and_releases_live_tls_and_pending_handshake(self):
        self.check_signal(signal.SIGTERM)

    def test_sigint_retains_graceful_ctrl_c_behavior(self):
        self.check_signal(signal.SIGINT)


class SignalFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsupported_signal_handlers_preserve_cancellation_cleanup(self):
        waiting = asyncio.Event()

        class Service:
            ingest_port = 8888
            gateway_port = 9999
            metrics = {}
            closed = False

            async def start(self):
                pass

            async def wait_failure(self):
                waiting.set()
                await asyncio.Event().wait()

            async def close(self):
                self.closed = True

        service = Service()
        args = SimpleNamespace(cert="unused", key="unused", session_id="ltc-comms",
                               ingest_port=8888, gateway_port=9999)
        loop = asyncio.get_running_loop()
        output = io.StringIO()
        with patch.object(loop, "add_signal_handler", side_effect=NotImplementedError), \
                patch.object(loop, "remove_signal_handler") as remove, \
                patch("ultra96.server.CommsServer", return_value=service), \
                patch("ultra96.server.server_context"), contextlib.redirect_stdout(output):
            task = asyncio.create_task(_run(args))
            await asyncio.wait_for(waiting.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        self.assertTrue(service.closed)
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["event"], "stopped")
        remove.assert_not_called()
