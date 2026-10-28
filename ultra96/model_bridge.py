"""A model that forwards samples to a separate runner program and emits its results.

For a model that cannot run inside the service process (for example, the FPGA
runner needs sudo and PYNQ's Python). Start the service with
`--model ultra96.model_bridge`; the runner, started on its own, connects to the
Unix socket below. One JSON object per line, UTF-8, in both directions:

    service -> runner   {"type": "hello", "format": 1, "channels": [11 names]}   (once, on connect)
                        {"type": "sample", "device_id": 1, "boot_id": 7, "seq": 42,
                         "uptime_ms": 12345, "values": [11 floats]}
    runner -> service   {"type": "result", "gesture": "CHOP", "confidence": 0.93,
                         "device_id": 1, "boot_id": 7, "seq": 42}

A sample carries exactly the arguments of on_sample, a result exactly those of
emit. One runner at a time: a new connection replaces the old one. Samples that
arrive while no runner is connected, or while the runner's queue is full, are
dropped and counted. Bad result lines are counted and skipped; they never close
the connection.

Under sensor protocol version 4 (12 channels) the hello also offers hand events,
and the runner may send two more kinds of line:

    service -> runner   {"type": "hello", "format": 1, "channels": [12 names], "hand_events": 1}
    runner -> service   {"type": "hand_hello", "hello": {the worker's hello}}      (once per connection,
                                                                                 before any hand_event)
                        {"type": "hand_event", "event": {one complete HAND_EVENT}}

The bridge hands them unchanged to the service's emit_hand with the number of the
runner connection (1, 2, ... in connection order), and tells it when that
connection ends ("closed"). It does not check the event schema; the service does
(ultra96.hand_gate). In version 3 mode the hello is unchanged and hand lines are
bad lines.

The socket is COMMS_MODEL_SOCKET, or model-runner.sock in the deployment folder
(the parent of this source folder), created with mode 0600.
"""
import json
import logging
import os
from pathlib import Path
import queue
import socket
import stat
import struct
import threading
import time

from common.sensor import CHANNELS

LOG = logging.getLogger(__name__)
FORMAT = 1
HAND_EVENTS = 1  # the hello's "hand_events" value in sensor version 4 mode
QUEUE_SIZE = 1024  # about 10 s of both gloves at 50 Hz
MAX_LINE_BYTES = 4096
_WARNING_INTERVAL = 1.0
# Runner line type -> (its payload key, which is also the emit_hand kind; the count it adds to).
_HAND_LINES = {"hand_hello": ("hello", "hand_hellos"), "hand_event": ("event", "hand_events")}


def socket_path():
    configured = os.environ.get("COMMS_MODEL_SOCKET")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "model-runner.sock"


class Bridge:
    """Owns the listener, the current runner connection and its send queue."""

    def __init__(self, queue_size=QUEUE_SIZE):
        self.channels = tuple(CHANNELS)
        # Offered in the hello, and hand lines accepted, only in sensor version 4 mode.
        self.hand_events = False
        self.queue_size = queue_size
        self.counts = dict(samples_sent=0, samples_dropped_no_runner=0, samples_dropped_full=0,
                           results=0, results_refused=0, bad_lines=0, connections=0,
                           hand_hellos=0, hand_events=0, hand_refused=0)
        self._emit = None
        self._emit_hand = None
        self._lock = threading.Lock()
        self._current = None  # (socket, queue) of the connected runner
        self._listener = None
        self._warned = {}

    def configure_sensor_version(self, version):
        """Before start: version 4 selects the 12 channels and offers hand events to the runner."""
        if type(version) is not int or version not in (3, 4):
            raise ValueError("sensor version must be 3 or 4")
        if self._listener is not None or self._current is not None:
            raise RuntimeError("cannot change sensor version after bridge start")
        if version == 4:
            from common.sensor_v4 import CHANNELS as selected
        else:
            selected = CHANNELS
        self.channels = tuple(selected)
        self.hand_events = version == 4

    def start(self, emit, path=None):
        """Remember emit; with a path, listen there for the runner."""
        self._emit = emit
        if path is not None:
            self._listener = listen(Path(path))
            threading.Thread(target=self._accept_loop, name="model-bridge-accept", daemon=True).start()
            LOG.info("model bridge waiting for the runner on %s", path)

    def start_hand_events(self, emit_hand):
        """Remember emit_hand(kind, payload, connection); the service calls this right after start."""
        self._emit_hand = emit_hand

    def on_sample(self, device_id, boot_id, seq, uptime_ms, values):
        """Event-loop thread: queue the sample for the runner and return at once."""
        current = self._current
        if current is None:
            self._count("samples_dropped_no_runner")
            return
        line = json.dumps(dict(type="sample", device_id=device_id, boot_id=boot_id, seq=seq,
                               uptime_ms=uptime_ms, values=list(values)), separators=(",", ":"))
        try:
            current[1].put_nowait(line)
        except queue.Full:
            self._count("samples_dropped_full")
            self._warn("full", "model runner is not reading; samples dropped")

    def attach(self, conn):
        """Make conn the runner connection (replacing any other) and serve it on two threads."""
        sends = queue.Queue(self.queue_size)
        hello = dict(type="hello", format=FORMAT, channels=list(self.channels))
        if self.hand_events:
            hello["hand_events"] = HAND_EVENTS
        # The hello is queued before on_sample can see this queue, so it is always first.
        sends.put(json.dumps(hello, separators=(",", ":")))
        with self._lock:
            previous, self._current = self._current, (conn, sends)
            self.counts["connections"] += 1
            number = self.counts["connections"]  # this connection's number for emit_hand
        if previous is not None:
            LOG.warning("a new model runner connected; closing the previous one")
            _close(previous)
        threading.Thread(target=self._send_loop, args=(conn, sends), name="model-bridge-send",
                         daemon=True).start()
        threading.Thread(target=self._receive_loop, args=(conn, sends, number),
                         name="model-bridge-receive", daemon=True).start()

    def close(self):
        with self._lock:
            current, self._current = self._current, None
        if current is not None:
            _close(current)
        if self._listener is not None:
            self._listener.close()

    def _accept_loop(self):
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return  # listener closed
            LOG.info("model runner connected (pid %s)", _peer_pid(conn))
            self.attach(conn)

    def _send_loop(self, conn, sends):
        while True:
            line = sends.get()
            if line is None:
                return
            try:
                conn.sendall(line.encode("utf-8") + b"\n")
            except OSError:
                self._detach(conn, sends)
                return
            if line.startswith('{"type":"sample"'):
                self._count("samples_sent")

    def _receive_loop(self, conn, sends, number=0):
        reader = conn.makefile("rb")
        try:
            while True:
                try:
                    raw = reader.readline(MAX_LINE_BYTES + 1)
                except OSError:
                    break
                if not raw:
                    break
                if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
                    self._count("bad_lines")
                    self._warn("bad", "model runner line longer than %d bytes", MAX_LINE_BYTES)
                    _skip_to_newline(reader)
                    continue
                if raw.strip():
                    self._handle(raw, number)
        finally:
            reader.close()
            self._detach(conn, sends)
            # After this connection's last line, on the same thread, so the service sees it last.
            emit_hand = self._emit_hand
            if self.hand_events and emit_hand is not None:
                emit_hand("closed", None, number)

    def _handle(self, raw, number=0):
        try:
            message = json.loads(raw.decode("utf-8"))
            kind = message.get("type") if isinstance(message, dict) else None
            if isinstance(kind, str) and kind in _HAND_LINES and self.hand_events:
                payload = message[_HAND_LINES[kind][0]]
                if not isinstance(payload, dict):
                    raise ValueError("hand line payload is not an object")
            elif kind == "result":
                fields = [message[key] for key in ("gesture", "confidence", "device_id", "boot_id", "seq")]
            else:
                raise ValueError("not a result")
        except (ValueError, KeyError, UnicodeDecodeError, RecursionError):  # deep nesting recurses
            self._count("bad_lines")
            self._warn("bad", "model runner sent a line that is not a result; skipped")
            return
        if kind != "result":
            emit_kind, count = _HAND_LINES[kind]
            emit_hand = self._emit_hand
            if emit_hand is not None and emit_hand(emit_kind, payload, number):
                self._count(count)
            else:
                self._count("hand_refused")
                self._warn("hand_refused", "model runner hand line refused (no hand events or service stopping)")
            return
        if self._emit(*fields):
            self._count("results")
        else:
            self._count("results_refused")
            self._warn("refused", "model result refused (invalid field or service stopping)")

    def _detach(self, conn, sends):
        with self._lock:
            if self._current is not None and self._current[0] is conn:
                self._current = None
                LOG.warning("model runner disconnected; counts so far: %s", json.dumps(self.counts))
        _close((conn, sends))

    def _count(self, key):
        with self._lock:
            self.counts[key] += 1

    def _warn(self, kind, message, *args):
        now = time.monotonic()
        with self._lock:
            if now - self._warned.get(kind, -_WARNING_INTERVAL) < _WARNING_INTERVAL:
                return
            self._warned[kind] = now
        LOG.warning(message, *args)


def listen(path):
    """A Unix listener at path, readable and writable by this user only (root can still connect).

    A lock file beside it, held while the listener is open, keeps a second service
    from replacing the socket of one that is still running.
    """
    lock = _lock(path)
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise FileExistsError("{} exists and is not a socket".format(path))
        path.unlink()  # left over from an earlier service (the lock says none holds it now)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_mask = os.umask(0o177)
    try:
        listener.bind(str(path))
    finally:
        os.umask(old_mask)
    os.chmod(str(path), 0o600)
    listener.listen(1)
    return _Listener(listener, lock)


class _Listener:
    """The listening socket and its lock; closing it releases both."""

    def __init__(self, sock, lock):
        self._sock = sock
        self._lock = lock

    def accept(self):
        return self._sock.accept()

    def close(self):
        self._sock.close()
        self._lock.close()


def _lock(path):
    import fcntl  # Unix only, like the socket itself
    lock = open(str(path) + ".lock", "a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise FileExistsError("another service is using {}".format(path)) from None
    return lock


def _peer_pid(conn):
    """The connecting process's PID (Linux SO_PEERCRED), or None where it is not available."""
    try:
        pid, _, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                        struct.calcsize("3i")))
        return pid
    except (AttributeError, OSError, struct.error):
        return None


def _skip_to_newline(reader):
    while True:
        chunk = reader.readline(MAX_LINE_BYTES)
        if not chunk or chunk.endswith(b"\n"):
            return


def _close(current):
    conn, sends = current
    try:
        sends.put_nowait(None)
    except queue.Full:
        pass
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    conn.close()


_BRIDGE = Bridge()


def start(emit):
    _BRIDGE.start(emit, socket_path())


def start_hand_events(emit_hand):
    _BRIDGE.start_hand_events(emit_hand)


def on_sample(device_id, boot_id, seq, uptime_ms, values):
    _BRIDGE.on_sample(device_id, boot_id, seq, uptime_ms, values)


def configure_sensor_version(version):
    """Called before model start; never change a connected runner's contract."""
    _BRIDGE.configure_sensor_version(version)
