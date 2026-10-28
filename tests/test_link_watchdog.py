"""The starved-link watchdog (journal W10-8), alone and through the bridge.

The pure tests feed laptop.link_watchdog arrival times and (boot_id, seq) lists.
The bridge tests run Bridge.ble_loop against a fake glove whose notifications
go through the real Bridge.enqueue path, with seq gaps simulated the way the
board's full queue makes them. The fake glove advances the bridge clock by the
BLE time it simulates, so seconds of glove time pass in milliseconds.
"""
import asyncio
from itertools import count
import json
import math
import struct
import sys
import time
from types import SimpleNamespace

import pytest

import demo
from common import sensor, sensor_v4
from laptop import dual_bridge
from laptop.bridge import Bridge, BridgeConfig, SENSOR_UUID, SERVICE_UUID
from laptop.dual_bridge import DualBridge, _parser
from laptop.link_watchdog import Starvation, StarvedLinkWatchdog, full_notification_packets
from laptop.source_audit import SOURCE_STATS_MAGIC, SOURCE_STATS_UUID
from tools import live_session

from test_dual_bridge import async_test, install_ack_transport

# 8 samples lost in 7 notifications: about 8 a second at 7 notifications a second.
STARVED_GAPS = (1, 1, 1, 1, 1, 1, 2)
TICK = 1 / 64  # time.monotonic on Windows: 15.6 ms


def tick(t):
    """An arrival time as the Windows clock reports it."""
    return math.floor(t / TICK) * TICK


class Seqs:
    """One glove's seq numbering: count packets after missing ones were lost."""

    def __init__(self, boot=7, seq=0):
        self.boot, self.seq = boot, seq

    def __call__(self, packets, missing=0):
        self.seq += missing
        batch = [(self.boot, seq) for seq in range(self.seq, self.seq + packets)]
        self.seq += packets
        return batch


def feed(watchdog, notifications):
    """notifications: (received_at, batch_size, packets); returns {index: Starvation}."""
    results = {}
    for index, (received_at, batch_size, packets) in enumerate(notifications):
        result = watchdog.observe(received_at, batch_size, packets)
        if result is not None:
            results[index] = result
    return results


def starved(seqs, start, seconds, *, rate=7, clock=lambda t: t):
    """Full notifications at rate per second, each after STARVED_GAPS samples were lost."""
    return [(clock(start + index / rate), 6, seqs(6, STARVED_GAPS[index % 7]))
            for index in range(round(seconds * rate))]


def armed(full_packets=6, **settings):
    watchdog = StarvedLinkWatchdog(**settings)
    watchdog.connection_started(full_packets)
    return watchdog


# --- laptop.link_watchdog -------------------------------------------------------------------

@pytest.mark.parametrize("mtu, packet_size, expected", [
    (517, 38, 6), (247, 38, 6), (247, 40, 6), (185, 38, 4), (185, 40, 4),
    (79, 38, 2), (78, 38, 1), (83, 40, 2), (82, 40, 1), (41, 38, 1), (23, 38, 0)])
def test_a_full_notification_holds_what_the_mtu_fits_up_to_six(mtu, packet_size, expected):
    assert full_notification_packets(mtu, packet_size) == expected


def test_a_healthy_single_link_with_occasional_single_gaps_never_fires():
    seqs = Seqs()
    stream = [(index / 50, 1, seqs(1, 1 if index % 100 == 99 else 0))
              for index in range(50 * 30)]
    assert feed(armed(), stream) == {}
    assert seqs.seq == 50 * 30 + 15  # the gaps were there


def test_healthy_two_links_and_controller_pauses_without_a_gap_never_fire():
    seqs, stream, t = Seqs(), [], 0.0
    for second in range(30):
        for index in range(29):  # about 30 a second of about 1.5 samples
            t += 1 / 30
            stream.append((tick(t), 2 - index % 2, seqs(2 - index % 2)))
        t += 0.12  # a controller pause: a few full notifications back to back, no gap
        stream += [(tick(t), 6, seqs(6)) for _ in range(4)]
    assert feed(armed(), stream) == {}


def test_full_notifications_without_any_gap_never_fire():
    seqs = Seqs()
    assert feed(armed(), [(index / 7, 6, seqs(6)) for index in range(7 * 30)]) == {}


def test_a_starved_link_fires_once_the_streak_spans_the_window_with_eight_notifications():
    seqs = Seqs()
    stream = starved(seqs, 1000.0, 4)
    results = feed(armed(), stream)
    # Notification 14 arrives 14 / 7 = 2.0 s after the first: the 15th of the streak.
    assert list(results) == [14]
    result = results[14]
    assert result == Starvation("reconnect", 2.0, 15, sum(
        STARVED_GAPS[index % 7] for index in range(1, 15)), 6)


def test_eight_notifications_are_needed_even_when_the_window_has_passed():
    seqs = Seqs()
    stream = [(index * 0.5, 6, seqs(6, 1)) for index in range(12)]
    results = feed(armed(), stream)
    assert list(results) == [7]  # 3.5 s, the 8th; at 2.0 s only 5 had arrived
    assert results[7].notifications == 8 and results[7].streak_seconds == 3.5


def test_one_notification_below_full_ends_the_streak():
    seqs = Seqs()
    stream = starved(seqs, 0.0, 13 / 7)  # 13 full, 1.71 s
    stream.append((13 / 7, 5, seqs(5, 1)))  # one below full, with its own gap
    stream += starved(seqs, 14 / 7, 3)
    results = feed(armed(), stream)
    assert list(results) == [14 + 14]
    assert results[28].notifications == 15


def test_a_boot_change_a_duplicate_and_an_older_seq_are_not_gaps():
    seqs = Seqs(boot=7, seq=500)
    stream = [(index / 7, 6, seqs(6)) for index in range(5)]
    seqs.boot, seqs.seq = 8, 0  # the glove restarted: seq starts again
    stream += [(index / 7, 6, seqs(6)) for index in range(5, 9)]
    stream.append((9 / 7, 6, list(stream[-1][2])))  # the same packets again
    stream.append((10 / 7, 6, list(stream[-3][2])))  # older ones
    stream += [(index / 7, 6, seqs(6)) for index in range(11, 40)]
    watchdog = armed()
    assert feed(watchdog, stream) == {}
    # The high mark survived the older packets: the next step is not a gap either.
    assert watchdog.observe(40 / 7, 6, seqs(6)) is None


def test_it_fires_at_most_once_per_connection():
    seqs, watchdog = Seqs(), armed()
    results = feed(watchdog, starved(seqs, 0.0, 15))
    assert [result.action for result in results.values()] == ["reconnect"]
    assert not watchdog.armed
    # A new connection after the cooldown fires again.
    watchdog.connection_started(6)
    results = feed(watchdog, starved(seqs, 15.0, 4))
    assert list(results) == [14] and results[14].action == "reconnect"


def test_inside_the_cooldown_it_reports_held_once_per_streak_then_fires_when_it_expires():
    seqs, watchdog = Seqs(), armed()
    assert feed(watchdog, starved(seqs, 0.0, 3))[14].action == "reconnect"  # at 2.0 s
    watchdog.connection_started(6)  # reconnected; starved again
    # Start times are exact binary fractions, so a 2 s span compares exactly.
    first = starved(seqs, 3.0, 4)  # 3.0 to 6.9 s
    below = [(7.0, 3, seqs(3, 1))]  # the streak ends
    second = starved(seqs, 7.25, 6)  # 7.25 to 13.1 s
    results = feed(watchdog, first + below + second)
    actions = {index: result.action for index, result in results.items()}
    held_first, held_second = 14, len(first) + len(below) + 14
    fire = len(first) + len(below) + next(
        index for index, (t, _, _) in enumerate(second) if t - 2.0 >= 10.0)
    assert actions == {held_first: "held", held_second: "held", fire: "reconnect"}
    assert results[fire].streak_seconds >= 2.0 and results[fire].missing > 0


def test_the_cooldown_is_kept_across_connections():
    seqs, watchdog = Seqs(), armed(cooldown=30.0)
    feed(watchdog, starved(seqs, 0.0, 3))
    for start in (5.0, 12.0, 19.0):
        watchdog.connection_started(6)
        results = feed(watchdog, starved(seqs, start, 4))
        assert [result.action for result in results.values()] == ["held"]
    watchdog.connection_started(6)
    results = feed(watchdog, starved(seqs, 26.0, 7))
    assert [result.action for result in results.values()] == ["held", "reconnect"]


@pytest.mark.parametrize("mtu, packet_size", [(78, 38), (82, 40)])
def test_it_is_disarmed_at_a_small_mtu_and_before_the_first_connection(mtu, packet_size):
    seqs = Seqs()
    watchdog = StarvedLinkWatchdog()
    assert not watchdog.armed
    assert feed(watchdog, starved(seqs, 0.0, 5)) == {}
    watchdog.connection_started(full_notification_packets(mtu, packet_size))
    assert not watchdog.armed
    # One packet per notification at 7 a second, with gaps: the starved rate, but
    # nothing marks it as starved.
    assert feed(watchdog, [(index / 7, 1, seqs(1, 1)) for index in range(70)]) == {}
    watchdog.connection_started(full_notification_packets(mtu + 1, packet_size))
    assert watchdog.armed and watchdog.full_packets == 2
    results = feed(watchdog, [(index / 7, 2, seqs(2, 1)) for index in range(20)])
    assert list(results) == [14]


def test_equal_arrival_times_from_the_windows_clock_are_counted_not_ordered():
    seqs, watchdog = Seqs(), armed()
    # 20 full notifications with gaps in one 15.6 ms tick span no time.
    assert feed(watchdog, [(tick(5.0), 6, seqs(6, 1)) for _ in range(20)]) == {}
    watchdog.observe(tick(5.01), 1, seqs(1))
    start = 6.0
    stream = starved(seqs, start, 4, rate=50, clock=tick)  # several share each tick
    results = feed(watchdog, stream)
    first = next(index for index, (t, _, _) in enumerate(stream) if t - stream[0][0] >= 2.0)
    assert list(results) == [first]
    assert results[first].notifications == first + 1
    assert results[first].streak_seconds == stream[first][0] - stream[0][0]


def test_settings_are_checked():
    for bad in (0, -1, float("nan"), float("inf"), True, "2"):
        with pytest.raises(ValueError):
            StarvedLinkWatchdog(window=bad)
        with pytest.raises(ValueError):
            StarvedLinkWatchdog(cooldown=bad)
    for bad in (0, 1.5, True):
        with pytest.raises(ValueError):
            StarvedLinkWatchdog(min_notifications=bad)
    with pytest.raises(ValueError):
        StarvedLinkWatchdog().connection_started(-1)
    watchdog = StarvedLinkWatchdog()
    assert (watchdog.window, watchdog.cooldown, watchdog.min_notifications) == (2.0, 10.0, 8)


# --- laptop.bridge: BridgeConfig and a fake glove -------------------------------------------

def test_bridge_config_checks_the_watchdog_settings_and_none_turns_it_off():
    assert BridgeConfig(ca_file="unused").starvation_window == 2.0
    assert BridgeConfig(ca_file="unused").starvation_cooldown == 10.0
    for bad in (0, -1.0, float("nan"), float("inf"), True, "2"):
        with pytest.raises(ValueError, match="starvation window"):
            BridgeConfig(ca_file="unused", starvation_window=bad)
        with pytest.raises(ValueError, match="starvation cooldown"):
            BridgeConfig(ca_file="unused", starvation_cooldown=bad)
    with pytest.raises(ValueError, match="starvation cooldown"):
        BridgeConfig(ca_file="unused", starvation_cooldown=None)
    BridgeConfig(ca_file="unused", starvation_window=None)


class SimulatedClock:
    """The bridge clock: real monotonic time plus the BLE time the fake glove simulates."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.monotonic() + self.offset


def notifications(pattern):
    """Yield (simulated seconds before it, packets, samples lost before it) for ever."""
    for index in count():
        if pattern == "starved":
            yield 1 / 7, 6, STARVED_GAPS[index % 7]
        elif pattern == "healthy":  # one glove alone, an occasional single gap
            yield 1 / 50, 1, int(index % 100 == 99)
        elif pattern == "two_links":  # 30 a second of 1 or 2, a 120 ms pause each second
            step = index % 33
            if step < 29:
                yield 1 / 30, 2 - step % 2, 0
            else:
                yield (0.12 if step == 29 else 0.0), 6, 0
        else:
            raise ValueError(pattern)


class FakeGlove:
    """A board behind a fake scanner and BLE client; patterns[n] is what connection n sends."""

    def __init__(self, patterns, *, sensor_version=3, device=1, boot=41, mtu=247):
        self.codec = sensor_v4 if sensor_version == 4 else sensor
        self.magic = b"LCS4" if sensor_version == 4 else SOURCE_STATS_MAGIC
        self.patterns = list(patterns)
        self.device, self.boot, self.mtu = device, boot, mtu
        self.address = "AA:0{}".format(device)
        self.clock = SimulatedClock()
        self.seq = self.failures = self.scans = 0
        self.clients, self.first_seq = [], []
        self.scan_gate = None  # scans after the first wait on it
        self.after_notification = None

    async def find_device_by_filter(self, predicate, timeout):
        self.scans += 1
        if self.clients and self.scan_gate is not None:
            await self.scan_gate.wait()
        device = SimpleNamespace(address=self.address)
        assert predicate(device, SimpleNamespace(service_uuids=[SERVICE_UUID]))
        return device

    def client(self, _device, disconnected_callback):
        client = FakeClient(self, len(self.clients), disconnected_callback)
        self.clients.append(client)
        return client

    def stats(self):
        return struct.pack("<4sBB2xIIIIIII", self.magic, self.device, 0, self.boot, self.seq,
                           self.seq - self.failures, self.failures, 0, 0, 0)

    def notification(self, packets, missing):
        # The board's full queue dropped its oldest samples: each took a seq.
        self.seq += missing
        self.failures += missing
        data = b"".join(self.codec.encode_packet(self.codec.SensorPacket(
            self.device, self.boot, seq, seq * 20,
            self.codec.DUMMY_FIXTURES[seq % len(self.codec.DUMMY_FIXTURES)]))
            for seq in range(self.seq, self.seq + packets))
        self.seq += packets
        return data


class FakeClient:
    def __init__(self, glove, number, disconnected_callback):
        self.glove, self.number = glove, number
        self.on_disconnect = disconnected_callback
        self.mtu_size = glove.mtu
        self.is_connected = False
        self.sender = None
        self.sent = self.stop_calls = self.disconnect_calls = 0
        sensor_characteristic = SimpleNamespace(properties=["notify"])
        source = SimpleNamespace(properties=["read"])
        service = SimpleNamespace(get_characteristic=lambda uuid:
                                  sensor_characteristic if uuid == SENSOR_UUID else
                                  source if uuid == SOURCE_STATS_UUID else None)
        self.services = SimpleNamespace(get_service=lambda uuid: service)

    async def connect(self):
        self.is_connected = True

    async def read_gatt_char(self, uuid, **kwargs):
        return self.glove.stats()

    async def start_notify(self, uuid, callback):
        patterns = self.glove.patterns
        self.glove.first_seq.append(self.glove.seq)
        self.sender = asyncio.create_task(
            self.send(patterns[min(self.number, len(patterns) - 1)], callback))

    async def send(self, pattern, callback):
        for interval, packets, missing in notifications(pattern):
            if not self.is_connected:
                return
            data = self.glove.notification(packets, missing)
            self.glove.clock.offset += interval
            callback(None, data)
            self.sent += 1
            if self.glove.after_notification is not None:
                self.glove.after_notification(self)
            await asyncio.sleep(0.001)

    async def _stop_sender(self):
        if self.sender is not None and not self.sender.done():
            self.sender.cancel()
            await asyncio.gather(self.sender, return_exceptions=True)

    async def stop_notify(self, uuid):
        self.stop_calls += 1
        await self._stop_sender()

    async def disconnect(self):
        self.disconnect_calls += 1
        self.is_connected = False
        await self._stop_sender()


def glove_bridge(glove, *, sensor_version=3, address=None, **settings):
    events = []
    bridge = Bridge(BridgeConfig(
        ca_file="unused", diagnostic_unprotected=True, expected_device_id=glove.device,
        source_audit=True, sensor_version=sensor_version, address=address, **settings),
        clock=glove.clock,
        evidence=SimpleNamespace(record=lambda kind, **fields: events.append((kind, fields))))
    return bridge, events, install_ack_transport(bridge)


async def wait_until(predicate, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached in time"
        await asyncio.sleep(0.005)


async def capture(bridge, glove, until, *, writer=True, stop=None):
    """Run ble_loop (and the writer) until until() holds, then stop it normally."""
    stop = stop or asyncio.Event()
    tasks = [asyncio.create_task(bridge.writer_loop())] if writer else []
    worker = asyncio.create_task(bridge.ble_loop(
        scanner=glove, client_factory=glove.client, stop_event=stop))
    try:
        await wait_until(lambda: until() or worker.done())
        stop.set()
        await asyncio.wait_for(asyncio.shield(worker), 5)
    finally:
        stop.set()
        for task in tasks + [worker]:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, worker, return_exceptions=True)
        await bridge.close_transport()
    return worker


def kinds(events, *names):
    return [(kind, fields.get("generation")) for kind, fields in events if kind in names]


@pytest.mark.parametrize("sensor_version", [3, 4])
@async_test
async def test_a_starved_link_is_dropped_and_rebuilt_and_samples_flow_again(sensor_version):
    glove = FakeGlove(["starved", "healthy"], sensor_version=sensor_version)
    bridge, events, messages = glove_bridge(glove, sensor_version=sensor_version)
    await capture(bridge, glove, lambda: len(glove.first_seq) == 2 and sum(
        message["seq"] >= glove.first_seq[1] for message in messages) >= 20)

    assert len(glove.clients) == 2 and bridge.metrics.ble_connections == 2
    assert bridge.metrics.starved_reconnects == 1
    assert bridge.metrics.starved_held == 0
    assert (bridge.metrics.disconnects, bridge.metrics.ble_errors,
            bridge.metrics.cleanup_errors) == (0, 0, 0)
    # The starved link ended like a link loss: cleanup ran, the source audit is
    # interrupted (once when the link was dropped, once when the new
    # connection's snapshot was read) and the run's own stop was still normal.
    first = glove.clients[0]
    assert (first.stop_calls, first.disconnect_calls) == (1, 1)
    assert bridge.source_audit.interruptions == 2
    assert bridge.source_audit.end_stats is not None
    starved_record = next(fields for kind, fields in events if kind == "BLE_link_starved")
    assert starved_record["action"] == "reconnect" and starved_record["generation"] == 1
    assert starved_record["full_packets"] == 6
    assert (starved_record["window"], starved_record["cooldown"]) == (2.0, 10.0)
    assert starved_record["notifications"] >= 8 and starved_record["missing"] >= 1
    assert starved_record["streak_seconds"] >= 2.0
    assert kinds(events, "BLE_link_starved", "BLE_starved_reconnect", "BLE_disconnected",
                 "BLE_connected") == [
        ("BLE_connected", 1), ("BLE_link_starved", 1), ("BLE_starved_reconnect", 1),
        ("BLE_disconnected", 1), ("BLE_connected", 2), ("BLE_disconnected", 2)]
    assert all(len(bytes.fromhex(fields["raw_hex"])) == glove.codec.PACKET_SIZE
               for kind, fields in events if kind == "sensor")
    summary = bridge.summary()
    assert (summary["starved_reconnects"], summary["starved_held"]) == (1, 0)
    assert summary["configuration"]["starvation_window_seconds"] == 2.0
    assert summary["configuration"]["starvation_cooldown_seconds"] == 10.0


@pytest.mark.parametrize("sensor_version", [3, 4])
@async_test
async def test_without_a_stop_event_a_starved_link_is_still_dropped_and_rebuilt(sensor_version):
    # Bridge.run (python -m laptop.bridge) calls ble_loop without a stop_event:
    # the starved wakeup is then the only thing besides a link loss that ends a link.
    glove = FakeGlove(["starved", "healthy"], sensor_version=sensor_version)
    bridge, events, messages = glove_bridge(glove, sensor_version=sensor_version)
    writer = asyncio.create_task(bridge.writer_loop())
    worker = asyncio.create_task(bridge.ble_loop(scanner=glove, client_factory=glove.client))
    try:
        await wait_until(lambda: worker.done() or len(glove.first_seq) == 2 and sum(
            message["seq"] >= glove.first_seq[1] for message in messages) >= 20)
        assert not worker.done()
    finally:
        for task in (writer, worker):
            task.cancel()
        await asyncio.gather(writer, worker, return_exceptions=True)
        await bridge.close_transport()

    assert len(glove.clients) == 2 and bridge.metrics.ble_connections == 2
    assert (bridge.metrics.starved_reconnects, bridge.metrics.starved_held) == (1, 0)
    assert (bridge.metrics.disconnects, bridge.metrics.ble_errors,
            bridge.metrics.cleanup_errors) == (0, 0, 0)
    first = glove.clients[0]
    assert (first.stop_calls, first.disconnect_calls) == (1, 1)
    assert bridge.source_audit.interruptions == 2
    assert kinds(events, "BLE_link_starved", "BLE_starved_reconnect", "BLE_disconnected",
                 "BLE_connected")[:5] == [
        ("BLE_connected", 1), ("BLE_link_starved", 1), ("BLE_starved_reconnect", 1),
        ("BLE_disconnected", 1), ("BLE_connected", 2)]


@pytest.mark.parametrize("pattern", ["healthy", "two_links"])
@async_test
async def test_a_healthy_link_longer_than_the_window_is_never_dropped(pattern):
    glove = FakeGlove([pattern])
    bridge, events, _ = glove_bridge(glove)
    await capture(bridge, glove, lambda: glove.clock.offset >= 3 * bridge.config.starvation_window)

    assert len(glove.clients) == 1
    assert (bridge.metrics.starved_reconnects, bridge.metrics.starved_held) == (0, 0)
    assert not kinds(events, "BLE_link_starved", "BLE_starved_reconnect")
    assert bridge.source_audit.interruptions == 0
    batches = {fields["batch_size"] for kind, fields in events if kind == "sensor"}
    if pattern == "healthy":
        assert bridge.tracker.gaps > 0 and batches == {1}  # single gaps alone do not count
    else:
        assert bridge.tracker.gaps == 0 and 6 in batches  # full bursts without a gap


@async_test
async def test_a_window_of_none_turns_the_watchdog_off():
    glove = FakeGlove(["starved"])
    bridge, events, _ = glove_bridge(glove, starvation_window=None)
    assert bridge.starvation is None
    await capture(bridge, glove, lambda: glove.clock.offset >= 5.0)

    assert len(glove.clients) == 1
    assert (bridge.metrics.starved_reconnects, bridge.metrics.starved_held) == (0, 0)
    assert not kinds(events, "BLE_link_starved", "BLE_starved_reconnect")
    assert bridge.tracker.gaps > 0
    assert bridge.summary()["configuration"]["starvation_window_seconds"] is None


class LateStop:
    """A stop request whose is_set() is true before its waiters wake, as when it
    arrives from another thread in the same step as the watchdog's wakeup."""

    def __init__(self):
        self._requested = False
        self._event = asyncio.Event()

    def is_set(self):
        return self._requested

    def set(self):
        if not self._requested:
            self._requested = True
            asyncio.get_running_loop().call_later(0.05, self._event.set)

    async def wait(self):
        return await self._event.wait()


@pytest.mark.parametrize("moment", ["mid_streak", "with_the_fire"])
@async_test
async def test_an_operator_stop_during_a_starved_streak_still_ends_normally(moment):
    glove = FakeGlove(["starved"])
    bridge, events, _ = glove_bridge(glove)
    stop = asyncio.Event() if moment == "mid_streak" else LateStop()

    def stop_now(client):
        if moment == "mid_streak" and glove.clock.offset >= 0.5:
            stop.set()
        if moment == "with_the_fire" and bridge.last_starvation is not None:
            stop.set()  # in the same loop step as the watchdog's wakeup

    glove.after_notification = stop_now
    worker = await capture(bridge, glove, lambda: False, stop=stop)

    assert worker.done() and not worker.cancelled() and worker.exception() is None
    assert len(glove.clients) == 1
    assert (bridge.metrics.starved_reconnects, bridge.metrics.disconnects) == (0, 0)
    assert not kinds(events, "BLE_starved_reconnect")
    assert bridge.source_audit.interruptions == 0
    assert bridge.source_audit.end_stats is not None  # the final source read ran
    if moment == "with_the_fire":
        assert bridge.last_starvation.action == "reconnect"


@async_test
async def test_a_starved_stop_keeps_the_accepted_tail_for_the_writer():
    glove = FakeGlove(["starved", "healthy"])
    glove.scan_gate = asyncio.Event()
    bridge, _, _ = glove_bridge(glove, queue_capacity=8)
    stop = asyncio.Event()
    worker = asyncio.create_task(bridge.ble_loop(
        scanner=glove, client_factory=glove.client, stop_event=stop))
    try:
        # No writer: the inbox holds the newest 8 samples when the link is dropped.
        await wait_until(lambda: glove.scans == 2)
        assert bridge.metrics.starved_reconnects == 1
        assert bridge.inbox.size == 8 and bridge.inbox.generation == 1
        dropped = bridge.inbox.generation_dropped
        glove.scan_gate.set()
        await wait_until(lambda: bridge.metrics.ble_connections == 2)
        # The next connection discards, and counts, what the writer left.
        assert bridge.inbox.generation_dropped >= dropped + 8
        assert bridge.inbox.generation == 2
    finally:
        glove.scan_gate.set()
        stop.set()
        await asyncio.wait_for(worker, 5)


@async_test
async def test_inside_the_cooldown_a_starved_reconnect_is_held_then_made():
    glove = FakeGlove(["starved", "starved", "healthy"])
    bridge, events, messages = glove_bridge(glove)
    await capture(bridge, glove, lambda: len(glove.first_seq) == 3 and sum(
        message["seq"] >= glove.first_seq[2] for message in messages) >= 10)

    assert (bridge.metrics.starved_reconnects, bridge.metrics.starved_held) == (2, 1)
    assert [(fields["generation"], fields["action"]) for kind, fields in events
            if kind == "BLE_link_starved"] == [(1, "reconnect"), (2, "held"), (2, "reconnect")]
    assert len(glove.clients) == 3 and bridge.metrics.disconnects == 0


# --- laptop.dual_bridge ---------------------------------------------------------------------

@async_test
async def test_a_dual_run_reports_the_starved_glove_like_a_disconnect():
    left, right = FakeGlove(["starved", "healthy"], device=1), FakeGlove(["healthy"], device=2)
    bridges = [glove_bridge(glove, address=glove.address)[0] for glove in (left, right)]
    progress, statuses = [], []
    report = await DualBridge(*bridges, expected_rate=50, startup_timeout=2, drain_timeout=1,
                              shutdown_timeout=2).run(
        duration=1.5, ble_options={
            glove.device: {"scanner": glove, "client_factory": glove.client}
            for glove in (left, right)},
        progress_interval=0.05,
        progress_callback=lambda phase, mode, snapshots: progress.append(snapshots),
        status_callback=statuses.append)

    one, two = report["devices"]["1"], report["devices"]["2"]
    assert (one["starved_reconnects"], one["starved_held"], one["disconnects"]) == (1, 0, 0)
    assert (two["starved_reconnects"], two["starved_held"]) == (0, 0)
    assert one["clean"] is False and report["clean"] is False
    assert one["configuration"]["starvation_window_seconds"] == 2.0
    assert any(item["errors"] >= 1 for snapshots in progress
               for item in snapshots if item["device_id"] == 1)
    errors = statuses[-1]["devices"]["1"]["errors"]
    assert (errors["starved_reconnects"], errors["starved_held"]) == (1, 0)
    assert statuses[-1]["devices"]["2"]["errors"]["starved_reconnects"] == 0


def test_the_cli_options_parse_and_zero_turns_the_watchdog_off():
    base = ["--ca", "unused", "--mock"]
    args = _parser().parse_args(base)
    assert (args.starvation_window, args.starvation_cooldown) == (2.0, 10.0)
    args = _parser().parse_args(base + ["--starvation-window", "0",
                                        "--starvation-cooldown", "30"])
    assert (args.starvation_window, args.starvation_cooldown) == (None, 30.0)
    for option, bad in (("--starvation-window", "-1"), ("--starvation-window", "nan"),
                        ("--starvation-window", "inf"), ("--starvation-window", "x"),
                        ("--starvation-cooldown", "0"), ("--starvation-cooldown", "-2"),
                        ("--starvation-cooldown", "inf")):
        with pytest.raises(SystemExit):
            _parser().parse_args(base + [option, bad])


@pytest.mark.parametrize("options, expected", [
    ([], (2.0, 10.0)),
    (["--starvation-window", "3.5", "--starvation-cooldown", "30"], (3.5, 30.0)),
    (["--starvation-window", "0"], (None, 10.0))])
def test_the_cli_gives_both_gloves_the_watchdog_settings(monkeypatch, capsys, options, expected):
    seen = {}

    async def observe(self, duration=600.0, **kwargs):
        for device, bridge in self.bridges.items():
            seen[device] = bridge
        return {"clean": False, "devices": {}}

    monkeypatch.setattr(dual_bridge.DualBridge, "run", observe)
    monkeypatch.setattr(sys, "argv", ["dual_bridge", "--ca", "unused", "--mock",
                                      "--duration", "1"] + options)
    assert dual_bridge.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert (report["run"]["starvation_window_seconds"],
            report["run"]["starvation_cooldown_seconds"]) == expected
    for device in (1, 2):
        config = seen[device].config
        assert (config.starvation_window, config.starvation_cooldown) == expected
        assert (seen[device].starvation is None) == (expected[0] is None)


def test_demo_run_and_start_keep_the_watchdog_at_its_default(tmp_path):
    addresses = ["--left-address", "38:18:2B:19:82:AE", "--right-address", "38:18:2B:18:9D:6A"]
    commands = [demo.capture_command(demo._parser().parse_args(["run"] + addresses),
                                     tmp_path / "report.json"),
                live_session.capture_command(demo._parser().parse_args(["start"] + addresses),
                                             tmp_path / "report.json")]
    for command in commands:
        assert command[1:4] == ["-u", "-m", "laptop.dual_bridge"]
        args = _parser().parse_args(command[4:])
        assert (args.starvation_window, args.starvation_cooldown) == (2.0, 10.0)
        assert not any(part.startswith("--starvation") for part in command)
