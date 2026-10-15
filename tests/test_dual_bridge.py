import asyncio
from functools import wraps
import json
import subprocess
import struct
import sys
import textwrap
from types import MethodType, SimpleNamespace

import pytest

from common.sensor import DUMMY_FIXTURES, SensorPacket, encode_packet
from laptop.bridge import Bridge, BridgeConfig, SENSOR_UUID, SERVICE_UUID
from laptop.dual_bridge import DualBridge, _write_progress, board_coverage
from laptop.source_audit import SOURCE_STATS_MAGIC, SOURCE_STATS_UUID, SourceStats


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


class Writer:
    def close(self):
        pass

    async def wait_closed(self):
        pass


def fixture(seq):
    return DUMMY_FIXTURES[seq % len(DUMMY_FIXTURES)]


def source_bytes(device, boot, next_seq, submitted, failures=0, *, gated=False, idle=0,
                 unread=0, refused=0):
    return struct.pack("<4sBB2xIIIIIII", SOURCE_STATS_MAGIC, device, int(gated), boot,
                       next_seq, submitted, failures, idle, unread, refused)


def config(device, *, address=None, capacity=64, freshness=2.0):
    return BridgeConfig(
        ca_file="unused", address=address, queue_capacity=capacity,
        freshness=freshness, diagnostic_unprotected=True,
        expected_device_id=device, source_audit=True)


def install_ack_transport(bridge, *, gate=None, progress=None, progress_count=1):
    messages = []

    async def connect():
        return object(), Writer()

    async def write(_writer, message, timeout):
        messages.append(message)

    async def read(_reader, timeout):
        if gate is not None:
            await gate.wait()
        message = messages[-1]
        if progress is not None and len(messages) >= progress_count:
            progress.set()
        return {
            "v": message["v"], "type": "INGEST_ACK", "session_id": message["session_id"],
            "device_id": message["device_id"], "boot_id": message["boot_id"],
            "seq": message["seq"], "status": "accepted",
            "request_id": message["request_id"],
        }

    bridge._connector = connect
    bridge._write_frame = write
    bridge._read_frame = read
    return messages


@async_test
async def test_two_bridge_paths_progress_independently_with_overlapping_sequences():
    left_gate = asyncio.Event()
    right_progress = asyncio.Event()
    left, right = Bridge(config(1)), Bridge(config(2))
    left_messages = install_ack_transport(left, gate=left_gate)
    right_messages = install_ack_transport(
        right, progress=right_progress, progress_count=2)
    dual = DualBridge(left, right, expected_rate=50, startup_timeout=1, drain_timeout=1)

    running = asyncio.create_task(dual.run(duration=0.12, mock=True, mock_rate=50))
    await asyncio.wait_for(right_progress.wait(), 1)

    assert left.metrics.acked == 0
    assert right.metrics.acked >= 2
    left_gate.set()
    report = await running

    assert report["devices"]["1"]["acked"] > 0
    assert report["devices"]["2"]["acked"] > 0
    assert left_messages[0]["seq"] == right_messages[0]["seq"] == 0
    assert {message["device_id"] for message in left_messages} == {1}
    assert {message["device_id"] for message in right_messages} == {2}


@async_test
async def test_normal_stop_drains_tail_and_reconciles_both_mock_sources():
    left, right = Bridge(config(1)), Bridge(config(2))
    install_ack_transport(left)
    install_ack_transport(right)
    dual = DualBridge(left, right, expected_rate=20, startup_timeout=1, drain_timeout=1)

    report = await dual.run(duration=0.12, mock=True, mock_rate=100)

    assert report["clean"] is True
    assert (report["devices"]["1"]["sensor_goodput"]["elapsed_seconds"] ==
            report["devices"]["2"]["sensor_goodput"]["elapsed_seconds"] ==
            report["common_observation_seconds"])
    for device in ("1", "2"):
        item = report["devices"][device]
        assert item["source"]["generated"] >= 3
        assert item["source"]["generated"] == item["received"] == item["acked"]
        assert item["source"]["clean"] is True
        assert item["queue_size"] == 0
        assert item["unfinished"] is False


@async_test
async def test_disabled_progress_stays_quiet_and_callback_failure_still_cleans_up():
    left, right = Bridge(config(1)), Bridge(config(2))
    install_ack_transport(left)
    install_ack_transport(right)
    dual = DualBridge(left, right, expected_rate=10, startup_timeout=1, drain_timeout=1)

    def must_not_run(*_args):
        raise AssertionError("disabled progress callback ran")

    quiet = await dual.run(
        duration=0.04, mock=True, mock_rate=100,
        progress_interval=0, progress_callback=must_not_run)
    assert quiet["clean"] is True
    assert quiet["progress_error"] is False

    failed_left, failed_right = Bridge(config(1)), Bridge(config(2))
    install_ack_transport(failed_left)
    install_ack_transport(failed_right)

    def fail_progress(*_args):
        raise OSError("private logging detail")

    failed = await DualBridge(
        failed_left, failed_right, expected_rate=10,
        startup_timeout=1, drain_timeout=1).run(
            duration=0.04, mock=True, mock_rate=100,
            progress_interval=0.01, progress_callback=fail_progress)

    assert failed["clean"] is False
    assert failed["progress_error"] is True
    for device in ("1", "2"):
        assert failed["devices"][device]["source"]["clean"] is True
        assert failed["devices"][device]["unfinished"] is False


@async_test
async def test_startup_packets_do_not_count_toward_common_window_coverage():
    left, right = Bridge(config(1)), Bridge(config(2))
    install_ack_transport(left)
    install_ack_transport(right)

    async def startup_burst(self, rate, *, stop_event, active_event):
        device = self.config.expected_device_id
        boot = 100 + device
        self.inbox.activate(1)
        self.source_audit.start(SourceStats(device, boot, 0, 0, 0))
        for seq in range(5):
            self.enqueue(1, encode_packet(
                SensorPacket(device, boot, seq, seq * 100, fixture(seq))))
        active_event.set()
        await stop_event.wait()
        self.source_audit.finish(SourceStats(device, boot, 5, 5, 0))
        self.inbox.deactivate(preserve=True)
        active_event.clear()

    left.dummy_loop = MethodType(startup_burst, left)
    right.dummy_loop = MethodType(startup_burst, right)
    report = await DualBridge(
        left, right, expected_rate=10, startup_timeout=1,
        drain_timeout=1).run(duration=0.03, mock=True)

    assert report["devices"]["1"]["source"]["clean"] is True
    assert report["devices"]["1"]["observation_received"] == 0
    assert report["devices"]["1"]["sustained_coverage"] is False
    assert report["clean"] is False


@async_test
async def test_final_quiet_tail_is_included_in_silence_verdict():
    left = Bridge(config(1, freshness=0.02))
    right = Bridge(config(2, freshness=0.02))
    install_ack_transport(left)
    install_ack_transport(right)
    common_started = asyncio.Event()
    starts = [0]

    for bridge in (left, right):
        original = bridge.begin_observation

        def begin(now=None, *, original=original):
            original(now)
            starts[0] += 1
            if starts[0] == 2:
                common_started.set()

        bridge.begin_observation = begin

    async def one_then_silent(self, rate, *, stop_event, active_event):
        device = self.config.expected_device_id
        boot = 200 + device
        self.inbox.activate(1)
        self.source_audit.start(SourceStats(device, boot, 0, 0, 0))
        active_event.set()
        await common_started.wait()
        self.enqueue(1, encode_packet(
            SensorPacket(device, boot, 0, 0, fixture(0))))
        await stop_event.wait()
        self.source_audit.finish(SourceStats(device, boot, 1, 1, 0))
        self.inbox.deactivate(preserve=True)
        active_event.clear()

    left.dummy_loop = MethodType(one_then_silent, left)
    right.dummy_loop = MethodType(one_then_silent, right)
    report = await DualBridge(
        left, right, expected_rate=10, startup_timeout=1,
        drain_timeout=1).run(duration=0.08, mock=True)

    for device in ("1", "2"):
        assert report["devices"][device]["sustained_coverage"] is True
        assert report["devices"][device]["silence_ok"] is False
    assert report["clean"] is False


def ticking_source(*, gated, rate, moving_ticks, quiet_ticks, delay=0.0, ticking_for=None):
    """A board that ticks at a fixed rate and sends only on its moving ticks.

    A gated board counts its quiet ticks as idle; an ungated board can only
    miss a tick when a read fails, so it counts them as unread. Ticks follow
    the loop clock, so a coarse timer cannot make the board look slow. The
    board subscribes after delay seconds, and with ticking_for its loop stalls
    that many seconds after it subscribed (it stays connected).
    """
    async def source(self, _rate, *, stop_event, active_event):
        device = self.config.expected_device_id
        boot = 500 + device
        clock = asyncio.get_running_loop().time
        counts = {"ticks": 0, "seq": 0, "quiet": 0}
        if delay:
            try:
                await asyncio.wait_for(stop_event.wait(), delay)
            except asyncio.TimeoutError:
                pass
        self.inbox.activate(1)
        self.source_audit.start(SourceStats(device, boot, 0, 0, 0, gated=gated))
        started = clock()
        self.source_ticking_since = self.clock()

        def catch_up():
            elapsed = clock() - started
            if ticking_for is not None:
                elapsed = min(elapsed, ticking_for)
            due = int(elapsed * rate)
            while counts["ticks"] < due:
                tick = counts["ticks"]
                if tick % (moving_ticks + quiet_ticks) < moving_ticks:
                    seq = counts["seq"]
                    self.enqueue(1, encode_packet(SensorPacket(
                        device, boot, seq, tick * 1000 // rate, fixture(seq))))
                    counts["seq"] += 1
                else:
                    counts["quiet"] += 1
                counts["ticks"] += 1

        active_event.set()
        while not stop_event.is_set():
            catch_up()
            try:
                await asyncio.wait_for(stop_event.wait(), 0.005)
            except asyncio.TimeoutError:
                pass
        catch_up()
        skipped = {"idle" if gated else "unread": counts["quiet"]}
        self.source_audit.finish(SourceStats(device, boot, counts["seq"], counts["seq"], 0,
                                             gated=gated, **skipped))
        self.source_ticking_until = self.clock()
        self.inbox.deactivate(preserve=True)
        active_event.clear()
    return source


@async_test
async def test_gated_board_idle_half_the_time_passes_and_ungated_board_keeps_old_rules():
    # 100 Hz, 0.25 s moving then 0.25 s quiet: half the ticks send nothing, and
    # every quiet span is longer than the 0.1 s freshness limit.
    left = Bridge(config(1, freshness=0.1))
    right = Bridge(config(2, freshness=0.1))
    install_ack_transport(left)
    install_ack_transport(right)
    left.dummy_loop = MethodType(
        ticking_source(gated=True, rate=100, moving_ticks=25, quiet_ticks=25), left)
    right.dummy_loop = MethodType(
        ticking_source(gated=False, rate=100, moving_ticks=25, quiet_ticks=25), right)
    progress = []
    report = await DualBridge(
        left, right, expected_rate=100, startup_timeout=1, drain_timeout=1).run(
            duration=1.0, mock=True, progress_interval=0.05,
            progress_callback=lambda phase, mode, snapshots: progress.append((phase, snapshots)))

    gated, ungated = report["devices"]["1"], report["devices"]["2"]
    assert gated["gated"] is True and gated["source"]["gated"] is True
    assert gated["source_idle"] == gated["source"]["source_idle"] > 0
    source = gated["source"]
    assert source["clean"] is True
    assert source["generated"] == gated["received"] == gated["acked"]
    # Fewer than 90% of the expected samples were sent, but the board kept ticking.
    assert gated["observation_received"] < 0.9 * gated["expected_samples"]
    assert source["generated"] + source["source_idle"] >= 0.9 * gated["expected_samples"]
    assert source["source_ticks"] == (source["generated"] + source["source_idle"]
                                      + source["source_unread"] + source["source_refused"])
    assert gated["observation_max_silence"] > 0.1
    assert gated["sustained_coverage"] is True
    # Judged over the time its notifications were on, not the common window.
    assert gated["coverage_expected"] == pytest.approx(gated["source_ticking_seconds"] * 100)
    assert gated["coverage_ratio"] >= 0.9
    assert gated["silence_ok"] is True
    assert gated["clean"] is True

    assert ungated["gated"] is False and ungated["source_idle"] == 0
    assert ungated["source"]["source_unread"] > 0
    assert ungated["source"]["clean"] is True
    assert ungated["sustained_coverage"] is False
    assert ungated["silence_ok"] is False
    assert ungated["clean"] is False
    assert report["clean"] is False

    # The start snapshot carries the flag, so it is known from the observation on.
    observed = [item for phase, snapshots in progress if phase == "observation"
                for item in snapshots]
    assert {item["gated"] for item in observed if item["device_id"] == 1} == {True}
    assert {item["gated"] for item in observed if item["device_id"] == 2} == {False}


def coverage_item(*, gated, generated, idle, received, silence, ticking=2.0):
    return {"source": {"gated": gated, "generated": generated, "source_idle": idle},
            "observation_received": received, "observation_max_silence": silence,
            "source_ticking_seconds": ticking}


# Common window: 100 expected samples. Rate 50 Hz, so 2 s of ticking is 100 ticks.
@pytest.mark.parametrize("item,expected", [
    # Gated: idle ticks count toward coverage, and silence is expected once idle.
    (coverage_item(gated=True, generated=50, idle=50, received=50, silence=0.5),
     (100, 100.0, True, True)),
    (coverage_item(gated=True, generated=40, idle=40, received=40, silence=0.5),
     (80, 100.0, False, True)),
    # Gated but never idle: the ordinary silence rule applies.
    (coverage_item(gated=True, generated=100, idle=0, received=100, silence=0.5),
     (100, 100.0, True, False)),
    # The same 100 ticks over 6 s of notifications (the board subscribed long
    # before the other one): the expectation covers the counters' interval.
    (coverage_item(gated=True, generated=50, idle=50, received=50, silence=0.5, ticking=6.0),
     (100, 300.0, False, True)),
    # No ticking interval (no final snapshot): nothing to judge the ticks against.
    (coverage_item(gated=True, generated=50, idle=50, received=50, silence=0.5, ticking=None),
     (100, None, False, True)),
    # No same-boot source counts: nothing proves the board kept ticking.
    (coverage_item(gated=True, generated=None, idle=None, received=100, silence=0.0),
     (0, 100.0, False, True)),
    # Ungated: unchanged, judged on samples received in the common window.
    (coverage_item(gated=False, generated=100, idle=0, received=50, silence=0.5),
     (50, 100.0, False, False)),
    (coverage_item(gated=False, generated=100, idle=0, received=95, silence=0.05, ticking=6.0),
     (95, 100.0, True, True)),
    (coverage_item(gated=None, generated=None, idle=None, received=0, silence=0.0),
     (0, 100.0, False, True)),
])
def test_board_coverage_rules_for_gated_and_ungated_boards(item, expected):
    assert board_coverage(item, 100.0, 0.1, 50.0) == expected


@async_test
@pytest.mark.parametrize("stalls", [False, True])
async def test_gated_board_that_subscribed_early_is_judged_over_its_own_ticking_interval(stalls):
    # LEFT (gated) subscribes 0.6 s before RIGHT, so the common window starts
    # about 0.6 s into LEFT's counters. When LEFT's loop stalls at 0.7 s it has
    # about 70 ticks, more than 90% of the window's 40 expected samples, yet it
    # ticked through only about 70% of the time its notifications were on.
    left = Bridge(config(1))
    right = Bridge(config(2))
    install_ack_transport(left)
    install_ack_transport(right)
    left.dummy_loop = MethodType(ticking_source(
        gated=True, rate=100, moving_ticks=5, quiet_ticks=5,
        ticking_for=0.7 if stalls else None), left)
    right.dummy_loop = MethodType(ticking_source(
        gated=False, rate=100, moving_ticks=1, quiet_ticks=0, delay=0.6), right)
    report = await DualBridge(
        left, right, expected_rate=100, startup_timeout=2, drain_timeout=1).run(
            duration=0.4, mock=True)

    item = report["devices"]["1"]
    source = item["source"]
    assert item["gated"] is True and source["clean"] is True
    assert item["source_idle"] > 0 and item["silence_ok"] is True
    assert item["source_ticking_seconds"] >= 0.9
    assert item["coverage_expected"] == pytest.approx(item["source_ticking_seconds"] * 100)
    covered = source["generated"] + source["source_idle"]
    assert item["coverage_ratio"] == pytest.approx(covered / item["coverage_expected"])
    if stalls:
        # The old rule compared these ticks with the window alone and passed.
        assert covered >= 0.9 * item["expected_samples"]
        assert item["observation_received"] < 0.9 * item["expected_samples"]
        assert item["coverage_ratio"] < 0.9
        assert item["sustained_coverage"] is False
        assert item["clean"] is False
        assert report["clean"] is False
    else:
        assert item["coverage_ratio"] >= 0.9
        assert item["sustained_coverage"] is True
        assert item["clean"] is True
        assert report["devices"]["2"]["coverage_expected"] == item["expected_samples"]


def test_progress_line_names_whether_each_board_is_gated(capsys):
    goodput = {"rolling_kbps": 0.0, "average_kbps": 0.0}
    snapshots = [dict(device_id=device, gated=gated, received=0, processed=0, acked=0,
                      queue=0, drops=0, errors=0, sensor_goodput=goodput)
                 for device, gated in ((1, True), (2, None))]
    _write_progress("observation", "physical", snapshots)
    lines = capsys.readouterr().err.splitlines()
    assert "device=1 gated=yes " in lines[0]
    assert "device=2 gated=unknown " in lines[1]


@async_test
async def test_source_snapshot_order_is_read_subscribe_stop_read_disconnect():
    events = []
    bridge = Bridge(config(2, address="AA:BB"))
    stopped = asyncio.Event()
    active = asyncio.Event()

    class Scanner:
        @staticmethod
        async def find_device_by_filter(predicate, timeout):
            device = SimpleNamespace(address="AA:BB")
            assert predicate(device, SimpleNamespace(service_uuids=[SERVICE_UUID]))
            return device

    class Client:
        mtu_size = 517

        def __init__(self, device, disconnected_callback):
            self.is_connected = False
            sensor = SimpleNamespace(properties=["notify"])
            source = SimpleNamespace(properties=["read"])
            service = SimpleNamespace(get_characteristic=lambda uuid:
                                      sensor if uuid == SENSOR_UUID else
                                      source if uuid == SOURCE_STATS_UUID else None)
            self.services = SimpleNamespace(get_service=lambda uuid:
                                            service if uuid == SERVICE_UUID else None)

        async def connect(self):
            self.is_connected = True
            events.append("connect")

        async def read_gatt_char(self, uuid):
            events.append("read")
            seq = 0 if events.count("read") == 1 else 1
            return source_bytes(2, 7, seq, seq)

        async def start_notify(self, uuid, callback):
            events.append("subscribe")
            callback(None, encode_packet(SensorPacket(2, 7, 0, 0, fixture(0))))

        async def stop_notify(self, uuid):
            events.append("stop")

        async def disconnect(self):
            events.append("disconnect")
            self.is_connected = False

    worker = asyncio.create_task(bridge.ble_loop(
        scanner=Scanner, client_factory=Client, stop_event=stopped,
        active_event=active))
    await asyncio.wait_for(active.wait(), 1)
    stopped.set()
    await asyncio.wait_for(worker, 1)

    assert events == ["connect", "read", "subscribe", "stop", "read", "disconnect"]
    assert bridge.source_audit.start_stats.next_seq == 0
    assert bridge.source_audit.end_stats.next_seq == 1


@async_test
@pytest.mark.parametrize("source_characteristic,stats", [
    (False, source_bytes(2, 7, 0, 0)),
    (True, source_bytes(1, 7, 0, 0)),
])
async def test_missing_source_stats_or_wrong_identity_never_becomes_active(
        source_characteristic, stats):
    bridge = Bridge(config(2, address="AA:BB"))
    stop = asyncio.Event()
    active = asyncio.Event()

    class Scanner:
        @staticmethod
        async def find_device_by_filter(predicate, timeout):
            if stop.is_set():
                return None
            return SimpleNamespace(address="AA:BB")

    class Client:
        mtu_size = 517

        def __init__(self, device, disconnected_callback):
            self.is_connected = False
            sensor = SimpleNamespace(properties=["notify"])
            source = SimpleNamespace(properties=["read"])
            service = SimpleNamespace(get_characteristic=lambda uuid:
                                      sensor if uuid == SENSOR_UUID else
                                      source if uuid == SOURCE_STATS_UUID and source_characteristic
                                      else None)
            self.services = SimpleNamespace(get_service=lambda uuid: service)

        async def connect(self):
            self.is_connected = True

        async def read_gatt_char(self, uuid):
            stop.set()
            return stats

        async def start_notify(self, uuid, callback):
            raise AssertionError("invalid source must fail before subscription")

        async def disconnect(self):
            self.is_connected = False

    worker = asyncio.create_task(bridge.ble_loop(
        scanner=Scanner, client_factory=Client, stop_event=stop,
        active_event=active))
    while bridge.metrics.source_stats_errors == 0:
        await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(worker, 1)

    assert not active.is_set()
    assert bridge.metrics.source_stats_errors == 1
    assert bridge.source_audit.report()["clean"] is False
    assert "updated dual-source firmware" in bridge.summary()["source_issue"]


@async_test
async def test_physical_dual_configuration_requires_two_distinct_addresses():
    left = Bridge(config(1, address="AA:BB"))
    right = Bridge(config(2, address="aa:bb"))
    with pytest.raises(ValueError, match="distinct"):
        DualBridge(left, right)


@async_test
async def test_ble_disconnect_reconnect_is_per_device_and_keeps_fault_verdict():
    left = Bridge(config(1, address="AA:01"))
    right = Bridge(config(2, address="AA:02"))
    install_ack_transport(left)
    install_ack_transport(right)

    class Environment:
        def __init__(self, device, address, disconnect_first=False):
            self.device = device
            self.address = address
            self.disconnect_first = disconnect_first
            self.boot = 300 + device
            self.seq = 0
            self.connections = 0

        async def find_device_by_filter(self, predicate, timeout):
            device = SimpleNamespace(address=self.address)
            assert predicate(device, SimpleNamespace(service_uuids=[SERVICE_UUID]))
            return device

        def client(self, device, disconnected_callback):
            environment = self

            class Client:
                mtu_size = 517

                def __init__(self):
                    self.number = environment.connections
                    environment.connections += 1
                    self.is_connected = False
                    self.sender = None
                    sensor = SimpleNamespace(properties=["notify"])
                    source = SimpleNamespace(properties=["read"])
                    service = SimpleNamespace(get_characteristic=lambda uuid:
                                              sensor if uuid == SENSOR_UUID else
                                              source if uuid == SOURCE_STATS_UUID else None)
                    self.services = SimpleNamespace(get_service=lambda uuid: service)

                async def connect(self):
                    self.is_connected = True

                async def read_gatt_char(self, uuid):
                    return source_bytes(environment.device, environment.boot,
                                        environment.seq, environment.seq)

                async def start_notify(self, uuid, callback):
                    async def send():
                        while self.is_connected:
                            seq = environment.seq
                            callback(None, encode_packet(SensorPacket(
                                environment.device, environment.boot, seq,
                                seq * 100, fixture(seq))))
                            environment.seq += 1
                            if environment.disconnect_first and self.number == 0:
                                self.is_connected = False
                                disconnected_callback(self)
                                return
                            await asyncio.sleep(0.01)
                    self.sender = asyncio.create_task(send())

                async def stop_notify(self, uuid):
                    if self.sender is not None:
                        self.sender.cancel()
                        await asyncio.gather(self.sender, return_exceptions=True)

                async def disconnect(self):
                    self.is_connected = False
                    if self.sender is not None:
                        self.sender.cancel()
                        await asyncio.gather(self.sender, return_exceptions=True)

            return Client()

    left_ble = Environment(1, "AA:01", disconnect_first=True)
    right_ble = Environment(2, "AA:02")
    progress = []
    report = await DualBridge(
        left, right, expected_rate=5, startup_timeout=1,
        drain_timeout=1, shutdown_timeout=2).run(
            duration=0.75, mock=False,
            ble_options={
                1: {"scanner": left_ble, "client_factory": left_ble.client},
                2: {"scanner": right_ble, "client_factory": right_ble.client},
            }, progress_interval=0.05,
            progress_callback=lambda phase, mode, snapshots:
                progress.append((phase, mode, snapshots)))

    assert left_ble.connections >= 2
    assert report["devices"]["1"]["disconnects"] >= 1
    assert report["devices"]["2"]["acked"] > report["devices"]["1"]["acked"]
    assert report["devices"]["2"]["source"]["clean"] is True
    assert report["devices"]["2"]["protected_ble"] is False
    assert report["devices"]["2"]["clean"] is False
    assert report["devices"]["1"]["clean"] is False
    assert report["clean"] is False
    assert any(item["errors"] >= 1 for _, _, snapshots in progress
               for item in snapshots if item["device_id"] == 1)


def test_cli_exits_after_bounded_cleanup_when_ack_read_resists_cancellation():
    program = textwrap.dedent(r"""
        import asyncio
        import sys
        from laptop import dual_bridge

        original_run = dual_bridge.DualBridge.run

        class Writer:
            def close(self): pass
            async def wait_closed(self): pass

        async def injected_run(self, duration=600, **kwargs):
            messages = {1: [], 2: []}
            for device, bridge in self.bridges.items():
                async def connect():
                    return object(), Writer()
                async def write(_writer, message, timeout, device=device):
                    messages[device].append(message)
                bridge._connector = connect
                bridge._write_frame = write
                if device == 1:
                    async def resist_cancellation(_reader, timeout):
                        while True:
                            try:
                                await asyncio.Event().wait()
                            except asyncio.CancelledError:
                                continue
                    bridge._read_frame = resist_cancellation
                else:
                    async def acknowledge(_reader, timeout, device=device):
                        message = messages[device][-1]
                        return {
                            "v": message["v"], "type": "INGEST_ACK",
                            "session_id": message["session_id"],
                            "device_id": message["device_id"],
                            "boot_id": message["boot_id"], "seq": message["seq"],
                            "status": "accepted",
                            "request_id": message["request_id"],
                        }
                    bridge._read_frame = acknowledge
            self.drain_timeout = 0.03
            self.shutdown_timeout = 0.1
            return await original_run(
                self, duration=0.04, mock=True, mock_rate=50)

        dual_bridge.DualBridge.run = injected_run
        sys.argv = ["dual_bridge", "--ca", "unused", "--mock", "--duration", "0.04"]
        raise SystemExit(dual_bridge.main())
    """)

    completed = subprocess.run(
        [sys.executable, "-c", program], cwd=str(__import__("pathlib").Path(__file__).parents[1]),
        capture_output=True, text=True, timeout=2)

    report = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert report["clean"] is False
    assert "writer:timeout" in report["devices"]["1"]["runtime_failures"]
