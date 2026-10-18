"""python demo.py start end to end on loopback: a local service, a phone, synthetic gloves.

No SSH and no BLE: --no-tunnel points the run at a CommsServer on this machine and --mock
gives the capture synthetic input, so the run ends in CAPTURE NOT PASSED by design. What
these tests check is the run itself: the board view, the live view, run.log and the stop.
"""
import asyncio
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

import demo
from laptop import board_monitor
from tools import live_session
from tools.ssh_tunnel import tunnel_command

LEFT, RIGHT = "38:18:2B:19:82:AE", "38:18:2B:18:9D:6A"


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    from tools.generate_comms_pki import generate_pki
    folder = tmp_path_factory.mktemp("live-session-pki")
    generate_pki(folder)
    other = tmp_path_factory.mktemp("live-session-untrusted-pki")
    generate_pki(other)
    return folder, other


class LocalBoard:
    """A CommsServer and one subscribed phone, on their own event loop thread."""

    def __init__(self, folder):
        self.folder = folder
        self.loop = asyncio.new_event_loop()
        self.results = 0
        self.server = None
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._start(), self.loop).result(10)
        return self

    async def _start(self):
        from common.tls import TLS_SERVER_NAME, client_context, server_context
        from common.wire import read_frame, write_frame
        from ultra96.server import CommsServer
        self.server = CommsServer(server_context(self.folder / "server-cert.pem",
                                                 self.folder / "server-key.pem"),
                                  ingest_port=0, gateway_port=0)
        await self.server.start()
        reader, self.phone = await asyncio.open_connection(
            "127.0.0.1", self.server.gateway_port, ssl=client_context(self.folder / "ca-cert.pem"),
            server_hostname=TLS_SERVER_NAME)
        await write_frame(self.phone, {"v": 1, "type": "SUBSCRIBE", "session_id": "ltc-comms"})
        assert (await read_frame(reader))["type"] == "SUBSCRIBED"

        async def receive():
            while True:
                await read_frame(reader, timeout=3600)
                self.results += 1
        self.receiver = asyncio.ensure_future(receive())

    async def _stop(self):
        self.receiver.cancel()
        self.phone.close()
        await self.server.close()

    def __exit__(self, *exc):
        asyncio.run_coroutine_threadsafe(self._stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)


class FakeConsole(live_session.Console):
    """A live console without a screen: it keeps the frames and types q when asked."""

    def __init__(self, quit_after=None):
        super().__init__()
        self.frames = []
        self.quit_after = quit_after
        self.entered_at = None

    def enter(self):
        self.live = True
        self.entered_at = time.monotonic()
        return True

    def draw(self, lines):
        self.frames.append(list(lines))

    def leave(self):
        self.live = False

    def keys(self):
        if (self.quit_after is not None and self.entered_at is not None
                and time.monotonic() - self.entered_at >= self.quit_after):
            self.quit_after = None
            return ["q"]
        return []

    @staticmethod
    def size():
        return 120, 60


def start_args(pki, board, root, *extra):
    args = demo._parser().parse_args([
        "start", "--no-tunnel", "--mock", "--rate", "20", "--port", str(board.server.ingest_port),
        "--monitor-port", str(board.server.gateway_port), "--ca", str(pki[0] / "ca-cert.pem"),
        "--output-root", str(root), "--left-address", LEFT, "--right-address", RIGHT, *extra])
    args.argv = ["start", "--no-tunnel", "--mock"]
    return args


def run_folder(root):
    folders = [path for path in Path(root).glob("B07-*") if path.is_dir()]
    assert len(folders) == 1
    return folders[0]


def test_start_parser_defaults_and_help():
    args = demo._parser().parse_args(["start"])
    assert (args.duration, args.rate, args.keyboard, args.no_tunnel) == (None, 50, False, False)
    assert (args.port, args.monitor_port) == (18889, 19999)
    assert args.output_root == demo.ROOT / ".comms-local"


def test_capture_command_adds_the_supervised_stream_at_the_end(tmp_path):
    args = demo._parser().parse_args(["start", "--left-address", LEFT, "--right-address", RIGHT,
                                      "--seed", "4"])
    command = live_session.capture_command(args, tmp_path / "report.json")
    assert command[:4] == [sys.executable, "-u", "-m", "laptop.dual_bridge"]
    assert command[-3:] == ["--progress-interval", "0", "--supervised"]
    options = dict(zip(command[4:-3:2], command[5:-3:2]))
    assert options["--rate"] == "50" and options["--duration"] == "86400.0"
    assert options["--report"] == str(tmp_path / "report.json")
    assert options["--evidence"] == str(tmp_path / "packets.jsonl")
    assert options["--seed"] == "4" and "--mock" not in command and "--keyboard" not in command


def test_tunnel_command_can_add_the_board_view_forward():
    command = tunnel_command("yanjie@stujump.comp.nus.edu.sg", "xilinx@board.example", 18889, 8888,
                             forwards=((19999, 9999),), ssh="C:/OpenSSH/ssh.exe")
    assert command[0] == "C:/OpenSSH/ssh.exe" and command[-1] == "xilinx@board.example"
    forwards = [command[index + 1] for index, part in enumerate(command) if part == "-L"]
    assert forwards == ["127.0.0.1:18889:127.0.0.1:8888", "127.0.0.1:19999:127.0.0.1:9999"]
    proxy = [part for part in command if part.startswith("ProxyCommand=")]
    assert len(proxy) == 1 and "C:/OpenSSH/ssh.exe" in proxy[0]
    for bad in (((19999,),), ((18889, 9999),), ((0, 9999),)):
        with pytest.raises(ValueError):
            tunnel_command("a@b", "c@d", 18889, 8888, forwards=bad)


def test_precheck_refuses_a_busy_port_and_names_the_way_out(tmp_path, pki):
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    busy = holder.getsockname()[1]
    try:
        assert live_session.port_in_use(busy)
        args = demo._parser().parse_args([
            "start", "--port", str(busy), "--ca", str(pki[0] / "ca-cert.pem"),
            "--left-address", LEFT, "--right-address", RIGHT, "--output-root", str(tmp_path)])
        problem = live_session._precheck(args)
        assert "already in use" in problem and "--no-tunnel" in problem
    finally:
        holder.close()
    assert not live_session.port_in_use(busy)
    args.no_tunnel = True
    assert "Nothing listens" in live_session._precheck(args)
    args.ca = tmp_path / "missing.pem"
    assert "CA file not found" in live_session._precheck(args)
    assert list(tmp_path.iterdir()) == []  # nothing is created before the checks pass


def test_a_timed_run_writes_one_log_with_every_step_and_a_summary(pki, tmp_path, capsys):
    with LocalBoard(pki[0]) as board:
        console = FakeConsole()
        code = live_session.main(start_args(pki, board, tmp_path, "--duration", "3"),
                                 console=console)
        phone_results = board.results
    out = capsys.readouterr().out
    folder = run_folder(tmp_path)
    assert sorted(path.name for path in folder.iterdir()) == [
        "exit-code.txt", "packets.jsonl", "report.json", "run.log"]
    assert code != 0  # synthetic input never passes the physical check
    report = json.loads((folder / "report.json").read_text())
    # exit-code.txt is the capture's own exit code (on Windows the synthetic source runs a
    # little slow, so its coverage, and with it clean, may fail; that is not checked here).
    assert (folder / "exit-code.txt").read_text().strip() == ("0" if report["clean"] else "1")
    assert report["mock_input"] is True and report["run"]["stop"] == "duration"
    for device in ("1", "2"):
        assert report["devices"][device]["source"]["complete"] is True
        assert report["devices"][device]["runtime_failures"] == []
    log = (folder / "run.log").read_text(encoding="utf-8")
    assert log.startswith("Let Them Cook (B07): Communications run log")
    for text in ("board  board view connected", "service up", "phone subscribed",
                 "LEFT   packet #", "RIGHT  packet #", "LEFT   ack    #", "LEFT   result #",
                 "LEFT   counts sent", "to phone", "Report (report.json)", "Summary",
                 "Written to the phone (Ultra96)", "CAPTURE NOT PASSED"):
        assert text in log, text
    assert log.rstrip().splitlines()[-1].startswith("CAPTURE NOT PASSED")
    written = [line for line in log.splitlines() if "results to the phone during this run" in line]
    assert len(written) == 1
    assert int(written[0].split("wrote ")[1].split()[0]) == phone_results > 0
    assert "Run log:" in out and "Summary" in out
    frames = ["\n".join(frame) for frame in console.frames]
    assert any("Sent to phone" in frame and "Latest packet #" in frame for frame in frames)
    assert any("LEFT glove" in frame and "RIGHT glove" in frame for frame in frames)


def test_q_stops_the_capture_gracefully_and_the_summary_says_so(pki, tmp_path):
    with LocalBoard(pki[0]) as board:
        started = time.monotonic()
        code = live_session.main(start_args(pki, board, tmp_path), console=FakeConsole(quit_after=3))
        elapsed = time.monotonic() - started
    folder = run_folder(tmp_path)
    report = json.loads((folder / "report.json").read_text())
    assert report["run"]["stop"] == "operator"
    for device in ("1", "2"):
        assert report["devices"][device]["source"]["complete"] is True
    assert (folder / "exit-code.txt").read_text().strip() == ("0" if report["clean"] else "1")
    assert code != 0 and elapsed < 60
    log = (folder / "run.log").read_text(encoding="utf-8")
    assert "stopping (stopped by the operator (q))" in log
    assert "Ended" in log and "stopped by the operator (q)" in log.split("Summary")[-1]


def test_board_monitor_states(pki, tmp_path):
    def run_until(monitor, wanted, limit=20.0):
        end = time.monotonic() + limit
        while monitor.state not in wanted and time.monotonic() < end:
            time.sleep(0.05)
        return monitor.state

    events = []
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    monitor = board_monitor.BoardMonitor(port, pki[0] / "ca-cert.pem", "ltc-comms",
                                         lambda kind, payload: events.append(kind)).start()
    try:
        assert run_until(monitor, {board_monitor.WAITING}) == board_monitor.WAITING
    finally:
        monitor.stop()

    with LocalBoard(pki[0]) as board:
        monitor = board_monitor.BoardMonitor(board.server.gateway_port, pki[0] / "ca-cert.pem",
                                             "ltc-comms", lambda kind, payload: events.append(kind))
        monitor.start()
        try:
            assert run_until(monitor, {board_monitor.CONNECTED}) == board_monitor.CONNECTED
            end = time.monotonic() + 5
            while "update" not in events and time.monotonic() < end:
                time.sleep(0.05)
            assert "update" in events
        finally:
            monitor.stop()
        untrusted = board_monitor.BoardMonitor(board.server.gateway_port, pki[1] / "ca-cert.pem",
                                               "ltc-comms", lambda *_: None).start()
        wrong_session = board_monitor.BoardMonitor(board.server.gateway_port, pki[0] / "ca-cert.pem",
                                                   "another-session", lambda *_: None).start()
        try:
            assert run_until(untrusted, {board_monitor.TLS_FAILED}) == board_monitor.TLS_FAILED
            assert run_until(wrong_session, {board_monitor.UNSUPPORTED}) == board_monitor.UNSUPPORTED
        finally:
            untrusted.stop()
            wrong_session.stop()
        assert board.server._subscriber is not None  # the phone kept its subscription


def test_board_monitor_reports_no_service_when_the_forward_closes(pki):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def close_everything():
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
                connection.close()  # like sshd when nothing listens on the board
            except OSError:
                pass

    thread = threading.Thread(target=close_everything, daemon=True)
    thread.start()
    monitor = board_monitor.BoardMonitor(port, pki[0] / "ca-cert.pem", "ltc-comms",
                                         lambda *_: None).start()
    try:
        end = time.monotonic() + 20
        while monitor.state != board_monitor.NO_SERVICE and time.monotonic() < end:
            time.sleep(0.05)
        assert monitor.state == board_monitor.NO_SERVICE
    finally:
        monitor.stop()
        stop.set()
        thread.join(2)
        listener.close()


def test_precheck_loads_the_ca_so_a_wrong_file_is_named_before_any_login(tmp_path, pki):
    wrong = tmp_path / "server-key.pem"
    wrong.write_text("not a certificate\n", encoding="utf-8")
    args = demo._parser().parse_args([
        "start", "--ca", str(wrong), "--left-address", LEFT, "--right-address", RIGHT,
        "--output-root", str(tmp_path / "runs")])
    problem = live_session._precheck(args)
    assert "could not be loaded as a CA certificate" in problem
    assert not (tmp_path / "runs").exists()


def test_after_a_connection_an_outage_is_lost_and_retried_not_final(pki):
    states = []
    with LocalBoard(pki[0]) as board:
        monitor = board_monitor.BoardMonitor(board.server.gateway_port, pki[0] / "ca-cert.pem",
                                             "ltc-comms", lambda kind, payload: states.append(
                                                 payload.get("state")))
        monitor.start()
        try:
            end = time.monotonic() + 10
            while monitor.state != board_monitor.CONNECTED and time.monotonic() < end:
                time.sleep(0.05)
            assert monitor.state == board_monitor.CONNECTED
            asyncio.run_coroutine_threadsafe(board.server.close(), board.loop).result(10)
            end = time.monotonic() + 15
            while monitor.state != board_monitor.LOST and time.monotonic() < end:
                time.sleep(0.05)
            time.sleep(3.0)  # long enough for several reconnect attempts to be refused
            assert monitor.state == board_monitor.LOST
            assert monitor._thread.is_alive()  # still trying
            assert not set(states) & board_monitor.FINAL
        finally:
            monitor.stop()
