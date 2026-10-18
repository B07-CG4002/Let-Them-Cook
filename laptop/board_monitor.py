"""Read-only view of the Ultra96 service for the live view: counts, model results, the phone.

It connects to the gateway port through the laptop's SSH forward and sends MONITOR, never
SUBSCRIBE, so the phone keeps its subscription and every result. It runs in its own thread
and event loop, so nothing here touches the capture's data path.
"""
from __future__ import annotations

import asyncio
import ssl
import threading
import time

from common.tls import TLS_SERVER_NAME, client_context
from common.wire import STREAM_LIMIT, ProtocolError, read_frame, write_frame
from ultra96.protocol import validate_message, validate_session

# What the last connection attempt found, in the order a run meets them.
WAITING = "waiting"            # nothing listens on the local port yet (the tunnel is not up)
NO_SERVICE = "no_service"      # the forward is up, but nothing answered TLS on the board
TLS_FAILED = "tls_failed"      # the board answered with a certificate the CA does not accept
UNSUPPORTED = "unsupported"    # the service closed MONITOR: it predates the monitor, or another session
CONNECTED = "connected"
LOST = "lost"                  # a connected monitor stopped receiving updates
STOPPED = "stopped"

# A state that proves the local forward accepted the connection (the SSH login is done).
FORWARD_UP = frozenset({NO_SERVICE, TLS_FAILED, UNSUPPORTED, CONNECTED, LOST})
# Answers after which the monitor stops asking (an old service is not asked again and again).
FINAL = frozenset({NO_SERVICE, TLS_FAILED, UNSUPPORTED})


class BoardMonitor:
    """Keep one MONITOR connection open and hand every event to on_event(kind, payload).

    on_event runs on the monitor's own thread. kind is "state" (payload: state, detail)
    or "update" (payload: one validated MONITOR_UPDATE). Before the first connection an
    UNSUPPORTED, NO_SERVICE or TLS_FAILED answer is final: the monitor stops trying, so an
    old service is not asked again and again. After a connection, the same answers mean
    an outage (a restart, a redeploy) and the monitor keeps reconnecting.
    The CA is loaded here, so a file that is not a CA certificate raises at once.
    """

    def __init__(self, port, ca_file, session_id, on_event, *, host="127.0.0.1",
                 retry_seconds=0.5, update_timeout=5.0):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("port must be 1..65535")
        self.port, self.host = port, host
        self.ca_file = str(ca_file)
        self._context = client_context(self.ca_file)
        self.session_id = validate_session(session_id)
        self.on_event = on_event
        self.retry_seconds = retry_seconds
        self.update_timeout = update_timeout
        self.state = None
        self.detail = ""
        self._stop = threading.Event()
        self._loop = None
        self._task = None
        self._thread = threading.Thread(target=self._main, name="board-monitor", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self, timeout=3.0):
        self._stop.set()
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # the loop has already closed
        self._thread.join(timeout)

    def _emit(self, kind, **payload):
        try:
            self.on_event(kind, payload)
        except Exception:
            pass  # the view must never stop the monitor

    def _set_state(self, state, detail=""):
        if state != self.state:
            self.detail = detail
            self.state = state
            self._emit("state", state=state, detail=detail)

    def _main(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            self._task = loop.create_task(self._run())
            if self._stop.is_set():
                self._task.cancel()
            loop.run_until_complete(asyncio.gather(self._task, return_exceptions=True))
        finally:
            loop.close()
            # A final answer stays readable: the run decides what it means.
            if self.state not in FINAL:
                self._set_state(STOPPED)

    async def _run(self):
        delay = self.retry_seconds
        no_service = 0
        connected_once = False
        while not self._stop.is_set():
            state, detail = await self._session()
            connected_once = connected_once or self.state in (CONNECTED, LOST)
            if connected_once and (state in FINAL or state == WAITING):
                state = LOST  # the service answered before: this is an outage
            if state == NO_SERVICE:
                # A slow first handshake through both hops is not yet proof of no service.
                no_service += 1
                if no_service < 3:
                    await asyncio.sleep(1.0)
                    continue
            else:
                no_service = 0
            self._set_state(state, detail)
            if state in FINAL:
                return
            if state == LOST:
                delay = min(5.0, max(delay, 1.0) * 2)
            else:
                delay = self.retry_seconds
            await asyncio.sleep(delay)

    async def _session(self):
        """One connection: return the state it ended in and a short detail.

        Before the SSH login nothing listens on the local port, so the connection is
        refused locally and nothing reaches the board. Once the forward is up, sshd on
        the board connects to the service's port; if nothing listens there, the forward
        closes during the TLS handshake.
        """
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(
                self.host, self.port, ssl=self._context,
                server_hostname=TLS_SERVER_NAME, limit=STREAM_LIMIT,
                ssl_handshake_timeout=10.0), 12.0)
        except ssl.SSLCertVerificationError as exc:
            return TLS_FAILED, exc.verify_message or "certificate verification failed"
        except ConnectionRefusedError as exc:
            return WAITING, type(exc).__name__
        except (ssl.SSLError, ConnectionError, OSError, asyncio.TimeoutError,
                asyncio.IncompleteReadError) as exc:
            return NO_SERVICE, type(exc).__name__
        try:
            return await self._monitor(reader, writer)
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1.0)
            except (asyncio.TimeoutError, ConnectionError, OSError, ssl.SSLError):
                writer.transport.abort()

    async def _monitor(self, reader, writer):
        try:
            await write_frame(writer, {"v": 1, "type": "MONITOR", "session_id": self.session_id})
            validate_message(await read_frame(reader, timeout=5.0), "MONITORING", self.session_id)
        except (ProtocolError, asyncio.IncompleteReadError, asyncio.TimeoutError,
                ConnectionError, OSError, ssl.SSLError) as exc:
            return UNSUPPORTED, type(exc).__name__
        self._set_state(CONNECTED)
        while not self._stop.is_set():
            try:
                update = validate_message(
                    await read_frame(reader, timeout=self.update_timeout),
                    "MONITOR_UPDATE", self.session_id)
            except (ProtocolError, asyncio.IncompleteReadError, asyncio.TimeoutError,
                    ConnectionError, OSError, ssl.SSLError) as exc:
                return LOST, type(exc).__name__
            update["received_monotonic"] = time.monotonic()
            self._emit("update", update=update)
        return STOPPED, ""
