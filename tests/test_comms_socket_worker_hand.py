"""Wen Ree's hand12 worker (src.socket_worker_hand) in compat delivery, tested by Communications.

Most tests drive his HandEngine and the worker's Delivery the way observe_socket does
(samples in, then run_due), on a fake clock with a scripted backend: both hands, no FPGA.
Socket tests run observe_socket and main() over a socket pair (Windows has no AF_UNIX),
and the loopback runs a real TLS version-4 service and the real model bridge. The hand
event tests have the bridge offer hand events ("hand_events":1 in its hello) and check
the hand_hello and hand_event lines against Wen Ree's common.hand_contract.
"""
import asyncio
import contextlib
import copy
import io
import json
from pathlib import Path
import re
import signal
import socket
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from common.hand_contract import ProtocolError, make_hello, validate_event, validate_hello
from src import socket_worker_hand as worker
from src.hand_runtime import CHANNELS, LABELS, HandEngine

HELLO = dict(type="hello", format=1, channels=list(CHANNELS))
HAND_HELLO = dict(HELLO, hand_events=1)  # a bridge in sensor-version-4 mode that offers hand events
LEFT, RIGHT = 1, 2
SESSION = "ltc-comms"
EPOCH = "{:032x}".format(1)
RESULT_KEYS = ["type", "gesture", "confidence", "device_id", "boot_id", "seq"]


class ScriptedBackend:
    """Scripts the gesture through the sensor input: the window's newest row picks it.

    The label is LABELS[round(flex3 * 10)] (flex3 is index 11) with confidence 0.94, or
    with flex2 (index 10) as the confidence when flex2 is not zero. delay advances the
    given clock during each inference, as a slow backend would.
    """

    def __init__(self, clock=None, delay=0.0):
        self.clock = clock
        self.delay = delay
        self.calls = []

    def probabilities(self, device, window):
        row = np.asarray(window)[-1]
        index = int(round(row[11] * 10))
        confidence = float(row[10]) or 0.94
        self.calls.append((device, LABELS[index]))
        if self.delay:
            self.clock.now += self.delay
        result = np.full(7, (1.0 - confidence) / 6)
        result[index] = confidence
        return result

    def close(self):
        pass


def values(label, confidence=None):
    row = [0.0] * 12
    row[11] = LABELS.index(label) / 10
    row[10] = confidence or 0.0
    return row


class Glove:
    """One hand's bridge samples at 50 Hz: seq and uptime advance 20 ms per sample."""

    def __init__(self, device, boot):
        self.device = device
        self.boot = boot
        self.seq = 0
        self.sent = []

    def sample(self, label, confidence=None, seq=None):
        if seq is None:
            seq = self.seq
            self.seq += 1
        message = dict(type="sample", device_id=self.device, boot_id=self.boot, seq=seq,
                       uptime_ms=(1000 + seq * 20) % 2 ** 32, values=values(label, confidence))
        self.sent.append(message)
        return message


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def hello(threshold=0.70, confirmations=3):
    return make_hello(SESSION, EPOCH, worker.LEASE_MS, threshold, confirmations)


class Run:
    """The engine and the delivery driven as observe_socket drives them: every 20 ms the new
    samples arrive (HandEngine.add, stamped with their arrival), then one run_due. With
    offered, the bridge offered hand events and the hand_hello has gone, as in observe_socket."""

    def __init__(self, delay=0.0, threshold=0.70, confirmations=3, offered=False):
        self.clock = Clock()
        self.backend = ScriptedBackend(self.clock, delay)
        self.engine = HandEngine(self.backend, threshold, confirmations)
        self.records = []
        self.delivery = worker.Delivery(hello(threshold, confirmations), self.records.append)
        self.results = []
        self.lines = []  # every line sent: the result lines, and the hand lines when offered
        self.ignored = 0  # samples HandEngine.add did not take (repeated or older seq)
        self.left = Glove(LEFT, 11)
        self.right = Glove(RIGHT, 22)
        if offered:
            self.delivery.offer(1)
            self.delivery.greet(self.send)

    def send(self, line):
        self.lines.append(line)
        if line["type"] == "result":
            self.results.append(line)

    def step(self, *samples, clock=False):
        """Returns the result lines this step sent."""
        self.clock.now += 0.02
        for message in samples:
            self.ignored += not self.engine.add(message, self.clock())
        before = len(self.results)
        worker.run_due(self.engine, self.clock(), self.records.append, self.delivery,
                       self.clock if clock else None, self.send)
        return self.results[before:]

    def play(self, seconds, left=None, right=None, left_confidence=None, right_confidence=None,
             clock=False):
        """Both hands for seconds; a hand given no label sends nothing (it is silent)."""
        produced = []
        for _ in range(int(round(seconds / 0.02))):
            samples = []
            if left:
                samples.append(self.left.sample(left, left_confidence))
            if right:
                samples.append(self.right.sample(right, right_confidence))
            produced += self.step(*samples, clock=clock)
        return produced

    def events(self, device=None, kind=None, delivery=None):
        return [r for r in self.records if r["type"] == "hand_event"
                and (device is None or r["device_id"] == device)
                and (kind is None or r["kind"] == kind)
                and (delivery is None or r["delivery"] == delivery)]

    def hand(self, device):
        return [r for r in self.results if r["device_id"] == device]

    def counts(self, device):
        return self.delivery.counts[device]

    def generation(self, device):
        return self.engine.windows[device].generation


def identities(results):
    return [(r["device_id"], r["boot_id"], r["seq"]) for r in results]


def assert_compat(run):
    """Every result is one forwarded pulse, state_start, state_refresh or idle event, in order;
    no state_end or reset is ever forwarded; no sample is named twice."""
    forwarded = run.events(delivery="forwarded")
    assert [(e["device_id"], e["source"]["boot_id"], e["source"]["seq"]) for e in forwarded] == (
        identities(run.results))
    assert [(e["gesture"], e["confidence"]) for e in forwarded] == [
        (r["gesture"], r["confidence"]) for r in run.results]
    assert {e["kind"] for e in forwarded} <= set(worker.FORWARDED)
    assert all(e["delivery"] == "not_forwarded" for e in run.events()
               if e["kind"] in ("state_end", "reset"))
    assert all(list(r) == RESULT_KEYS for r in run.results)
    assert len(set(identities(run.results))) == len(run.results)
    for device in (LEFT, RIGHT):
        assert run.counts(device)["forwarded"] == len(run.hand(device))
        assert run.counts(device)["state_end_not_forwarded"] == len(run.events(device, "state_end"))


def test_result_lines_have_exactly_the_bridge_result_fields():
    run = Run()
    run.play(3.0, left="PICKUP")
    assert run.results == [dict(type="result", gesture="PICKUP", confidence=0.94, device_id=LEFT,
                                boot_id=11, seq=120)]
    assert json.loads(json.dumps(run.results[0])) == run.results[0]
    assert_compat(run)


def test_both_hands_are_independent_and_each_result_names_its_own_hand():
    run = Run()
    run.play(3.0, left="CHOP", right="PICKUP")
    left, right = run.hand(LEFT), run.hand(RIGHT)
    # A full two-second window first exists at seq 100; three confirmations land at seq 120.
    assert [(r["gesture"], r["seq"]) for r in right] == [("PICKUP", 120)]
    assert [r["gesture"] for r in left] == ["CHOP"] * len(left) and len(left) >= 3
    assert [r["seq"] for r in left] == list(range(120, 120 + 10 * len(left), 10))
    assert {r["boot_id"] for r in left} == {11} and {r["boot_id"] for r in right} == {22}
    kinds = [r["kind"] for r in run.events(LEFT, delivery="forwarded")]
    assert kinds == ["state_start"] + ["state_refresh"] * (len(left) - 1)
    starts = run.events(LEFT, "state_start")
    assert {r["state_id"] for r in run.events(LEFT, "state_refresh")} == {starts[0]["state_id"]}
    assert [r["kind"] for r in run.events(RIGHT)] == ["pulse"]
    # Every result names a sample this hand really sent.
    for device, glove in ((LEFT, run.left), (RIGHT, run.right)):
        sent = {(m["boot_id"], m["seq"]) for m in glove.sent}
        assert all((r["boot_id"], r["seq"]) in sent for r in run.hand(device))
    summary = run.delivery.summary()
    assert summary["1"]["forwarded"] == len(left) and summary["2"]["forwarded"] == 1
    assert all(summary[hand][key] == 0 for hand in ("1", "2")
               for key in ("collisions", "encoder_errors", "event_time_raised", "unsent"))
    assert_compat(run)


def test_repeated_and_out_of_order_samples_are_ignored_and_a_held_pulse_is_one_result():
    run = Run()
    repeats = 0
    run.step(run.left.sample("PICKUP"))
    generation = run.generation(LEFT)
    for tick in range(249):
        samples = [run.left.sample("PICKUP")]
        if tick % 10 == 9:
            samples.append(dict(samples[0]))  # the same sample again
            samples.append(run.left.sample("PICKUP", seq=run.left.seq - 6))  # an older seq
            repeats += 2
        run.step(*samples)
    assert run.ignored == repeats and run.generation(LEFT) == generation  # never a reset
    # A held PICKUP is one pulse, not a pulse per confirmation.
    assert [r["gesture"] for r in run.results] == ["PICKUP"]
    assert len([r for r in run.records if r["type"] == "prediction"]) >= 10
    assert_compat(run)


def test_a_transition_gives_one_result_at_its_sample_and_the_state_end_is_not_sent():
    run = Run()
    run.play(3.0, left="CHOP")
    chop = len(run.results)
    run.play(2.0, left="POURING")
    after = run.results[chop:]
    assert all(r["gesture"] == "CHOP" for r in run.results[:chop])
    assert after and all(r["gesture"] == "POURING" for r in after)
    start = run.events(LEFT, "state_start", "forwarded")[-1]
    assert start["gesture"] == "POURING"
    # Exactly one result at the transition sample: the POURING state_start.
    transition = start["source"]["seq"]
    assert [r for r in run.results if r["seq"] == transition] == [after[0]]
    ends = run.events(LEFT, "state_end")
    assert [(e["gesture"], e["reason"], e["delivery"]) for e in ends] == [
        ("CHOP", "confirmed_transition", "not_forwarded")]
    assert ends[0]["source"]["seq"] <= transition
    assert run.counts(LEFT)["collisions"] == 0 and run.counts(LEFT)["state_end_not_forwarded"] == 1
    assert_compat(run)


def test_a_receive_gap_resets_only_that_hand():
    run = Run()
    run.play(3.0, left="PICKUP", right="CHOP")
    assert [r["gesture"] for r in run.hand(LEFT)] == ["PICKUP"]
    right_start = run.events(RIGHT, "state_start")
    left_generation, right_generation = run.generation(LEFT), run.generation(RIGHT)
    run.play(1.2, right="CHOP")  # the left glove reconnects meanwhile: no samples, seq continues
    during = len(run.hand(RIGHT))
    run.play(3.0, left="PICKUP", right="CHOP")
    # HandWindow reset the left hand alone at its first sample after the gap (over 0.5 s).
    assert run.generation(LEFT) == left_generation + 1 and run.generation(RIGHT) == right_generation
    # After the reset a held PICKUP is a new pulse, once a new two-second window has filled.
    left = run.hand(LEFT)
    assert [r["gesture"] for r in left] == ["PICKUP", "PICKUP"]
    assert left[1]["seq"] >= 150 + 100
    # The right hand's state carried on through the left's reconnect: one state, refreshed.
    assert run.events(RIGHT, "state_start") == right_start
    assert len(run.hand(RIGHT)) > during > 1
    assert not run.events(RIGHT, "reset") and not run.events(RIGHT, "state_end")
    assert_compat(run)


def test_a_reconnect_ends_an_active_state_unsent_and_the_next_state_is_new():
    run = Run()
    run.play(3.0, left="CHOP", right="PICKUP")
    first = run.events(LEFT, "state_start")[0]["state_id"]
    run.play(0.56, right="PICKUP")  # the left is silent: over the 0.5 s gap, under the 0.6 s timeout
    assert run.delivery.encoder.active[LEFT] is not None
    run.play(0.02, left="CHOP", right="PICKUP")
    assert run.delivery.encoder.active[LEFT] is None
    ends = run.events(LEFT, "state_end")
    assert [(e["reason"], e["delivery"]) for e in ends] == [("source_reset", "not_forwarded")]
    before = len(run.results)
    run.play(3.0, left="CHOP", right="PICKUP")
    second = run.events(LEFT, "state_start")[-1]
    assert second["state_id"] != first and run.results[before]["gesture"] == "CHOP"
    assert second["delivery"] == "forwarded" and second["source"]["seq"] == run.results[before]["seq"]
    assert [r["gesture"] for r in run.hand(RIGHT)] == ["PICKUP"]
    assert not run.events(RIGHT, "state_end") and not run.events(RIGHT, "reset")
    assert_compat(run)


def test_a_new_boot_id_resets_only_that_hand():
    run = Run()
    run.play(3.0, left="CHOP", right="PICKUP")
    right_generation = run.generation(RIGHT)
    run.left = Glove(LEFT, 12)  # the left glove rebooted: new boot_id, seq from 0
    run.play(3.0, left="CHOP", right="PICKUP")
    ends = run.events(LEFT, "state_end")
    assert [(e["reason"], e["delivery"]) for e in ends] == [("source_reset", "not_forwarded")]
    assert {r["boot_id"] for r in run.hand(LEFT)} == {11, 12}
    assert len(run.events(LEFT, "state_start", "forwarded")) == 2
    assert [r["gesture"] for r in run.hand(RIGHT)] == ["PICKUP"]
    assert run.generation(RIGHT) == right_generation
    assert_compat(run)


def test_when_samples_stop_no_result_and_no_idle_is_invented():
    run = Run()
    run.play(3.0, left="CHOP")
    last = run.left.seq - 1
    run.play(0.2)  # the newest window still gets its inference when it falls due
    assert run.results[-1]["seq"] == last
    before = len(run.results)
    run.play(3.0)  # both hands silent; the worker keeps polling
    assert len(run.results) == before
    ends = run.events(LEFT, "state_end")
    assert [(e["reason"], e["delivery"]) for e in ends] == [("prediction_timeout", "not_forwarded")]
    assert not run.events(kind="idle")
    assert_compat(run)


def test_an_old_seq_after_a_pause_is_ignored():
    run = Run()
    run.play(3.0, left="CHOP")
    run.play(0.2)  # the newest window's own inference
    before, generation = len(run.results), run.generation(LEFT)
    run.play(0.6)
    run.step(run.left.sample("CHOP", seq=run.left.seq - 10))
    run.play(0.2)
    assert len(run.results) == before and run.ignored == 1 and run.generation(LEFT) == generation


def test_an_inference_slower_than_half_a_second_gives_no_result():
    run = Run(delay=0.6)
    run.play(4.0, left="CHOP", clock=True)
    assert run.results == [] and not run.events()
    types = [r["type"] for r in run.records]
    assert types.count("stale_suppressed") >= 1 and "prediction" not in types


def test_confident_idle_is_one_result_and_low_confidence_never_becomes_idle():
    run = Run()
    run.play(3.0, left="IDLE", right="CHOP", right_confidence=0.5)
    assert run.hand(LEFT) == [dict(type="result", gesture="IDLE", confidence=0.94, device_id=LEFT,
                                   boot_id=11, seq=120)]
    assert run.hand(RIGHT) == []
    right = [r for r in run.records if r["type"] == "prediction" and r["device_id"] == RIGHT]
    assert right and not any(r["above_threshold"] for r in right)
    # A confident state followed by uncertainty ends locally; it never turns into IDLE.
    run.play(1.0, right="CHOP")
    run.play(2.0, right="CHOP", right_confidence=0.5)
    assert run.hand(RIGHT) and all(r["gesture"] == "CHOP" for r in run.hand(RIGHT))
    assert [e["reason"] for e in run.events(RIGHT, "state_end")] == ["prediction_timeout"]
    assert_compat(run)


def test_a_reset_stamped_before_the_last_inference_is_handed_over_at_the_later_time():
    run = Run(delay=0.01)  # each inference takes 10 ms of the clock
    run.play(2.82, left="CHOP", clock=True)  # seq 0..140: the last run_due inferred seq 140
    last = [r for r in run.records if r["type"] == "prediction"][-1]
    assert last["seq"] == 140 and last["elapsed_s"] == run.clock()
    assert run.delivery.encoder.active[LEFT] is not None
    # A sample that arrived while that inference ran, but reaches the engine after it, with a
    # seq gap: the engine resets the hand and stamps the state_end with the sample's arrival.
    late = run.clock() - 0.005
    run.engine.add(run.left.sample("CHOP", seq=run.left.seq + 1), late)
    run.step()
    end = [r for r in run.records if r["type"] == "state_end"]
    assert [(r["reason"], r["elapsed_s"]) for r in end] == [("source_reset", late)]  # recorded as is
    counts = run.counts(LEFT)
    assert counts["encoder_errors"] == 0 and counts["event_time_raised"] == 1
    assert [e["reason"] for e in run.events(LEFT, "state_end")] == ["source_reset"]
    assert run.delivery.encoder.active[LEFT] is None
    assert_compat(run)


def test_an_encoder_error_is_recorded_and_resets_only_that_hands_encoder(monkeypatch):
    run = Run()
    run.play(2.5, left="CHOP", right="CHOP")
    encoder = run.delivery.encoder
    original = encoder.consume
    failures = []

    def consume(record):
        if not failures and record["type"] == "prediction" and record["device_id"] == LEFT:
            failures.append(record)
            raise ProtocolError("scripted encoder failure")
        return original(record)

    monkeypatch.setattr(encoder, "consume", consume)
    before_left, before_right = len(run.hand(LEFT)), len(run.hand(RIGHT))
    run.play(1.0, left="CHOP", right="CHOP")
    assert run.counts(LEFT)["encoder_errors"] == 1 and run.counts(RIGHT)["encoder_errors"] == 0
    [error] = [r for r in run.records if r["type"] == "encoder_error"]
    assert error == dict(type="encoder_error", device_id=LEFT, record_type="prediction",
                         error="scripted encoder failure")
    resets = run.events(kind="reset")
    assert [(e["device_id"], e["reason"], e["delivery"]) for e in resets] == [
        (LEFT, "manual_reset", "not_forwarded")]
    assert run.counts(LEFT)["reset_not_forwarded"] == 1
    # The right hand's results go on. The left engine's CHOP state goes on too, but its
    # encoder no longer knows it, so the left hand has no refresh until its next state.
    assert len(run.hand(RIGHT)) > before_right and len(run.hand(LEFT)) == before_left
    assert_compat(run)


def test_a_second_result_for_one_sample_is_never_forwarded():
    run = Run()
    run.play(3.0, left="PICKUP")
    record = dict(type="idle", device_id=LEFT, gesture="IDLE", confidence=0.94, elapsed_s=run.clock(),
                  boot_id=11, seq=120, uptime_ms=1000 + 120 * 20)
    run.delivery.consume(record, run.results.append)
    assert run.counts(LEFT)["collisions"] == 1
    assert run.events(LEFT, "idle")[-1]["delivery"] == "collision"
    assert len(run.results) == 1


def test_the_sent_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(worker, "MEMORY", 3)
    run = Run(confirmations=1)
    run.play(3.0, left="CHOP")
    assert len(run.results) > 3 and len(run.delivery.sent) == 3
    assert list(run.delivery.sent) == identities(run.results[-3:])


def test_a_bridge_reconnect_resets_both_hands_and_the_next_connection_has_a_new_epoch():
    run = Run()
    run.play(3.0, left="CHOP", right="PICKUP")
    first = run.delivery.encoder.hello["worker_epoch"]
    # The end of a connection, as in observe_socket: the receiver resets both hands, and the
    # last run_due hands the state ends over with nothing more sent.
    run.engine.reset(run.clock())
    run.delivery.closed = True
    assert run.step() == []
    ends = run.events(kind="state_end")
    assert [(e["device_id"], e["reason"], e["delivery"]) for e in ends] == [
        (LEFT, "connection_reset", "not_forwarded")]
    # The next connection, as in serve_socket: a new encoder and no sent memory; the counts go on.
    second = "{:032x}".format(2)
    run.delivery.reopen(make_hello(SESSION, second, worker.LEASE_MS, 0.70, 3))
    assert run.delivery.encoder.active == {LEFT: None, RIGHT: None} and not run.delivery.sent
    assert not run.delivery.closed and run.delivery.failure is None
    before = len(run.results)
    run.play(3.0, left="CHOP", right="PICKUP")
    again = run.results[before:]
    # Both hands start again from a new window: a new pulse and a new state.
    assert [r["gesture"] for r in again if r["device_id"] == RIGHT] == ["PICKUP"]
    assert again[0]["gesture"] == "CHOP" and again[0]["seq"] >= 150 + 100
    starts = run.events(LEFT, "state_start")
    assert [s["event_id"].split(":")[0] for s in starts] == [first, second]
    assert run.counts(RIGHT)["forwarded"] == 2 and run.counts(LEFT)["forwarded"] == len(run.hand(LEFT))
    assert_compat(run)


def test_records_the_encoder_does_not_read_give_nothing():
    run = Run()
    for record in (dict(type="stale_suppressed", device_id=LEFT, elapsed_s=1.0),
                   dict(type="prediction", device_id=3, elapsed_s=1.0), dict(type="error")):
        run.delivery.consume(record, run.results.append)
    assert not run.records and not run.results and not run.counts(LEFT)["event_time_raised"]


# --- observe_socket and main() over a socket pair ------------------------------------------

def bridge_lines(*gloves, ticks=101, hello=HELLO):
    lines = [json.dumps(hello)]
    for _ in range(ticks):
        for glove, gesture in gloves:
            lines.append(json.dumps(glove.sample(gesture)))
    return ("\n".join(lines) + "\n").encode("utf-8")


def read_lines(sock, count, timeout=5.0):
    sock.settimeout(timeout)
    data = b""
    while data.count(b"\n") < count:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    return data


class Peer(threading.Thread):
    """The bridge end: send the lines, read `expect` result lines (or wait until done()),
    end the stream, then read whatever else arrives until the worker closes."""

    def __init__(self, peer, data, expect=0, done=None):
        super().__init__(daemon=True)
        self.peer, self.data, self.expect, self.done = peer, data, expect, done
        self.received, self.rest, self.errors = b"", b"", []

    def run(self):
        try:
            self.peer.sendall(self.data)
            if self.expect:
                self.received = read_lines(self.peer, self.expect)
            deadline = time.monotonic() + 5
            while self.done is not None and not self.done() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.peer.shutdown(socket.SHUT_WR)
            self.peer.settimeout(5)
            while True:
                chunk = self.peer.recv(65536)
                if not chunk:
                    break
                self.rest += chunk
        except Exception as error:  # reported by the test
            self.errors.append(error)


def socket_pair():
    client, peer = socket.socketpair()
    client.settimeout(1)
    return client, peer


def test_observe_socket_sends_one_compact_line_per_result_and_nothing_after_the_stream_ends():
    client, peer = socket_pair()
    engine = HandEngine(ScriptedBackend(), confirmations=1)
    records = []
    delivery = worker.Delivery(hello(confirmations=1), records.append)
    left, right = Glove(LEFT, 11), Glove(RIGHT, 22)
    bridge = Peer(peer, bridge_lines((left, "PICKUP"), (right, "CHOP")), expect=2)
    bridge.start()
    try:
        worker.observe_socket("fixture", engine, records.append, connection_factory=lambda path: client,
                              deliver=delivery)
    finally:
        bridge.join(10)
        peer.close()
    assert not bridge.is_alive() and bridge.errors == []
    assert sorted(bridge.received.splitlines()) == [
        b'{"type":"result","gesture":"CHOP","confidence":0.94,"device_id":2,"boot_id":22,"seq":100}',
        b'{"type":"result","gesture":"PICKUP","confidence":0.94,"device_id":1,"boot_id":11,"seq":100}']
    assert bridge.rest == b""
    # The stream's end reset both hands: the right hand's state ended, recorded and not sent.
    ends = [r for r in records if r["type"] == "hand_event" and r["kind"] == "state_end"]
    assert [(e["device_id"], e["reason"], e["delivery"]) for e in ends] == [
        (RIGHT, "connection_reset", "not_forwarded")]
    assert delivery.closed and delivery.summary()["1"]["forwarded"] == 1


class FailingClient:
    """A bridge connection whose sends fail (after the first `after`), as when the service has gone."""

    def __init__(self, sock, after=0):
        self.sock = sock
        self.after = after
        self.sends = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.sock.close()

    def sendall(self, data):
        self.sends += 1
        if self.sends <= self.after:
            return self.sock.sendall(data)
        raise BrokenPipeError(32, "Broken pipe")

    def __getattr__(self, name):
        return getattr(self.sock, name)


def test_a_failed_send_ends_the_loop_cleanly_with_a_record():
    client, peer = socket_pair()
    failing = FailingClient(client)
    engine = HandEngine(ScriptedBackend(), confirmations=1)
    records = []
    delivery = worker.Delivery(hello(confirmations=1), records.append)
    bridge = Peer(peer, bridge_lines((Glove(LEFT, 11), "PICKUP"), (Glove(RIGHT, 22), "WASH")),
                  done=lambda: failing.sends > 0)
    bridge.start()
    try:
        worker.observe_socket("fixture", engine, records.append, connection_factory=lambda path: failing,
                              deliver=delivery)  # returns: no exception
    finally:
        bridge.join(10)
        peer.close()
    [lost] = [r for r in records if r["type"] == "connection_lost"]
    assert lost["error_type"] == "BrokenPipeError" and lost["results_unsent"] >= 1 and failing.sends == 1
    events = [r for r in records if r["type"] == "hand_event"]
    before = records[:records.index(lost)]
    assert lost["results_unsent"] == sum(r["type"] == "hand_event" and r["delivery"] == "unsent" for r in before)
    # Each event's record says what became of its line, as the summary does: none was sent.
    assert not [e for e in events if e["delivery"] == "forwarded"]
    summary = delivery.summary()
    for hand in (LEFT, RIGHT):
        assert summary[str(hand)]["forwarded"] == 0
        assert summary[str(hand)]["unsent"] == sum(e["device_id"] == hand and e["delivery"] == "unsent"
                                                   for e in events)
    assert sum(summary[h]["unsent"] for h in ("1", "2")) >= 1 and delivery.closed


@pytest.fixture(scope="module")
def synthetic_bundle(tmp_path_factory):
    from tools.hand_hls_prepare import prepare
    folder = tmp_path_factory.mktemp("hand12-worker") / "bundle"
    with contextlib.redirect_stdout(io.StringIO()):
        prepare(folder, synthetic=True)
    return folder


def with_unix(monkeypatch, *connections):
    """main() in socket mode on a Python without AF_UNIX: each connect_bridge gives the next of
    connections (an exception among them is raised instead), then Ctrl+C, as the operator
    stopping the worker. Returns the list of backends main() makes."""
    monkeypatch.setattr(worker, "socket", SimpleNamespace(AF_UNIX=object(), timeout=socket.timeout,
                                                          SHUT_RDWR=socket.SHUT_RDWR))
    monkeypatch.setattr(worker, "RETRY", 0.01)
    pending, backends = list(connections), []

    def connect(path):
        if not pending:
            raise KeyboardInterrupt
        connection = pending.pop(0)
        if isinstance(connection, BaseException):
            raise connection
        return connection

    def backend(bundle):
        backends.append(ScriptedBackend())
        return backends[-1]

    monkeypatch.setattr(worker, "connect_bridge", connect)
    monkeypatch.setattr(worker, "NumpyHandBackend", backend)
    return backends


def read_records(folder):
    [path] = sorted(Path(folder).glob("hand-worker-*.jsonl"))
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def predictions_in(folder, count):
    def done():
        paths = list(Path(folder).glob("hand-worker-*.jsonl"))
        return bool(paths) and paths[0].read_text(encoding="utf-8").count('"type": "prediction"') >= count
    return done


def test_main_delivers_in_socket_mode_and_says_so_in_its_records(monkeypatch, synthetic_bundle, tmp_path,
                                                                 capsys):
    client, peer = socket_pair()
    with_unix(monkeypatch, client)
    bridge = Peer(peer, bridge_lines((Glove(LEFT, 11), "THROW"), (Glove(RIGHT, 22), "IDLE")), expect=2)
    bridge.start()
    try:
        worker.main(["--bundle", str(synthetic_bundle), "--allow-synthetic", "--socket", "/x.sock",
                     "--confirmations", "1", "--out", str(tmp_path)])
    finally:
        bridge.join(10)
        peer.close()
    assert bridge.errors == [] and bridge.rest == b""
    results = sorted(json.loads(line)["gesture"] for line in bridge.received.splitlines())
    assert results == ["IDLE", "THROW"]
    records = read_records(tmp_path)
    scope = records[0]
    assert scope["gameplay_transport"] is True and scope["delivery"] == "compat_v3_gesture_result"
    assert scope["forwarded_kinds"] == ["pulse", "state_start", "state_refresh", "idle"]
    assert scope["logged_only_kinds"] == ["state_end", "reset"] and scope["session_id"] == SESSION
    [opened] = [r for r in records if r["type"] == "connection_open"]
    assert opened["connection"] == 1 and re.fullmatch("[0-9a-f]{32}", opened["worker_epoch"])
    # The stream's end did not end the worker: it went on to connect again, and Ctrl+C stopped it.
    assert records[-2] == dict(type="stopped", signal="SIGINT")
    summary = records[-1]
    assert summary["type"] == "summary" and summary["status"] == "finished"
    assert {hand: counts["forwarded"] for hand, counts in summary["delivery"].items()} == {"1": 1, "2": 1}
    assert set(summary["delivery"]["1"]) == {"forwarded", "collisions", "encoder_errors", "event_time_raised",
                                             "state_end_not_forwarded", "reset_not_forwarded", "unsent"}
    # The operator's console shows each forwarded event.
    console = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert sorted(r["kind"] for r in console if r.get("type") == "hand_event") == ["idle", "pulse"]


def test_main_observe_only_sends_nothing(monkeypatch, synthetic_bundle, tmp_path, capsys):
    client, peer = socket_pair()
    with_unix(monkeypatch, client)
    bridge = Peer(peer, bridge_lines((Glove(LEFT, 11), "THROW"), (Glove(RIGHT, 22), "IDLE")),
                  done=predictions_in(tmp_path, 2))
    bridge.start()
    try:
        worker.main(["--bundle", str(synthetic_bundle), "--allow-synthetic", "--socket", "/x.sock",
                     "--confirmations", "1", "--observe-only", "--out", str(tmp_path)])
    finally:
        bridge.join(10)
        peer.close()
    assert bridge.errors == [] and bridge.received == b"" and bridge.rest == b""
    records = read_records(tmp_path)
    assert records[0]["gameplay_transport"] is False and "delivery" not in records[0]
    assert {r["device_id"] for r in records if r["type"] == "action" or r["type"] == "idle"} == {1, 2}
    assert not any(r["type"] == "hand_event" for r in records) and "delivery" not in records[-1]
    assert [r for r in records if r["type"] == "connection_open"] == [dict(type="connection_open", connection=1)]
    assert records[-2] == dict(type="stopped", signal="SIGINT") and records[-1]["status"] == "finished"


def socket_argv(bundle, out):
    return ["--bundle", str(bundle), "--allow-synthetic", "--socket", "/x.sock", "--confirmations", "1",
            "--out", str(out)]


def test_main_waits_for_the_bridge_and_connects_again_with_a_new_epoch_and_the_same_backend(
        monkeypatch, synthetic_bundle, tmp_path, capsys):
    (first, first_peer), (second, second_peer) = socket_pair(), socket_pair()
    backends = with_unix(monkeypatch, FileNotFoundError(2, "No such file or directory"),
                         ConnectionRefusedError(111, "Connection refused"), first, second)
    # The service restarts between the two connections; the same gloves go on sending.
    left, right = Glove(LEFT, 11), Glove(RIGHT, 22)
    bridges = [Peer(first_peer, bridge_lines((left, "THROW"), (right, "IDLE")), expect=2),
               Peer(second_peer, bridge_lines((left, "THROW"), (right, "IDLE")), expect=2)]
    for bridge in bridges:
        bridge.start()
    try:
        worker.main(socket_argv(synthetic_bundle, tmp_path))
    finally:
        for bridge in bridges:
            bridge.join(10)
        first_peer.close()
        second_peer.close()
    assert all(bridge.errors == [] and bridge.rest == b"" for bridge in bridges)
    sent = [[json.loads(line) for line in bridge.received.splitlines()] for bridge in bridges]
    assert [sorted(r["gesture"] for r in results) for results in sent] == [["IDLE", "THROW"]] * 2
    # The first connection's end reset both hands: the second's results need a new window.
    assert all(r["seq"] == 100 for r in sent[0]) and all(r["seq"] == 201 for r in sent[1])
    records = read_records(tmp_path)
    # One record while the socket is absent, then refused; nothing more until it is there.
    assert [r for r in records if r["type"] == "waiting_for_bridge"] == [
        dict(type="waiting_for_bridge", socket="/x.sock", error_type="FileNotFoundError")]
    opened = [r for r in records if r["type"] == "connection_open"]
    assert [r["connection"] for r in opened] == [1, 2] and opened[0]["worker_epoch"] != opened[1]["worker_epoch"]
    assert [(r["connection"], r["reconnect_in_s"]) for r in records if r["type"] == "connection_end"] == [
        (1, 0.01), (2, 0.01)]
    forwarded = [r for r in records if r["type"] == "hand_event" and r["delivery"] == "forwarded"]
    assert sorted(e["event_id"].split(":")[0] for e in forwarded) == sorted(
        [opened[0]["worker_epoch"]] * 2 + [opened[1]["worker_epoch"]] * 2)
    assert len(backends) == 1  # loaded once (on the board: one overlay download) for both connections
    assert records[-2] == dict(type="stopped", signal="SIGINT")
    summary = records[-1]
    assert summary["status"] == "finished" and summary["counts"]["connection_open"] == 2
    assert {hand: counts["forwarded"] for hand, counts in summary["delivery"].items()} == {"1": 2, "2": 2}


class ResetConnection:
    """A bridge connection the service resets right after its hello."""

    def __init__(self):
        self.chunks = [(json.dumps(HELLO) + "\n").encode("utf-8")]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def recv(self, size):
        if self.chunks:
            return self.chunks.pop(0)
        raise ConnectionResetError(104, "Connection reset by peer")

    def sendall(self, data):
        pass

    def shutdown(self, how):
        pass


def test_a_broken_stream_connects_again_and_an_incompatible_hello_ends_the_worker(
        monkeypatch, synthetic_bundle, tmp_path, capsys):
    (truncated, truncated_peer), (old, old_peer) = socket_pair(), socket_pair()
    with_unix(monkeypatch, ResetConnection(), truncated, old)
    truncated_peer.sendall((json.dumps(HELLO) + "\n" + '{"type":"sam').encode("utf-8"))
    truncated_peer.shutdown(socket.SHUT_WR)
    old_peer.sendall((json.dumps(dict(HELLO, channels=list(CHANNELS[:-1]))) + "\n").encode("utf-8"))
    old_peer.shutdown(socket.SHUT_WR)
    try:
        with pytest.raises(ValueError, match="eleven-channel"):
            worker.main(socket_argv(synthetic_bundle, tmp_path))
    finally:
        truncated_peer.close()
        old_peer.close()
    records = read_records(tmp_path)
    assert [(r["error_type"], r["message"]) for r in records if r["type"] == "receive_error"] == [
        ("ConnectionResetError", "[Errno 104] Connection reset by peer"),
        ("BridgeClosed", "Truncated bridge line")]
    assert [r["connection"] for r in records if r["type"] == "connection_open"] == [1, 2, 3]
    assert [r["connection"] for r in records if r["type"] == "connection_end"] == [1, 2]
    assert records[-2]["type"] == "error" and records[-2]["error_type"] == "ValueError"
    assert records[-1]["status"] == "failed"


class TermBackend(ScriptedBackend):
    """Its first inference gets SIGTERM, as from the launcher stopping or replacing the worker."""

    def probabilities(self, device, window):
        if not self.calls:
            self.calls.append(None)
            signal.raise_signal(signal.SIGTERM)
        return super().probabilities(device, window)


def test_sigterm_ends_socket_mode_with_its_summary_and_restores_the_handler(
        monkeypatch, synthetic_bundle, tmp_path, capsys):
    client, peer = socket_pair()
    with_unix(monkeypatch, client)
    backend = TermBackend()
    monkeypatch.setattr(worker, "NumpyHandBackend", lambda bundle: backend)
    before = signal.getsignal(signal.SIGTERM)
    bridge = Peer(peer, bridge_lines((Glove(LEFT, 11), "THROW"), (Glove(RIGHT, 22), "IDLE")),
                  done=lambda: backend.calls)
    bridge.start()
    try:
        worker.main(socket_argv(synthetic_bundle, tmp_path))  # returns: SIGTERM is the normal end of socket mode
    finally:
        bridge.join(10)
        peer.close()
    assert signal.getsignal(signal.SIGTERM) == before
    assert bridge.errors == [] and bridge.rest == b"" and backend.calls[0] is None
    records = read_records(tmp_path)
    assert [r["type"] for r in records].count("connection_open") == 1
    assert not any(r["type"] in ("connection_end", "error") for r in records)
    assert records[-2] == dict(type="stopped", signal="SIGTERM")
    assert records[-1]["status"] == "finished" and set(records[-1]["delivery"]) == {"1", "2"}


def test_an_out_folder_gets_a_new_file_per_start_and_replay_is_unchanged(monkeypatch, synthetic_bundle,
                                                                         tmp_path, capsys):
    from tools.hand_worker_demo import generate
    source = tmp_path / "replay.jsonl"
    generate(source)
    out = tmp_path / "logs"
    out.mkdir()
    for pid in (4242, 4243):
        monkeypatch.setattr(worker, "os", SimpleNamespace(getpid=lambda pid=pid: pid))
        worker.main(["--bundle", str(synthetic_bundle), "--allow-synthetic", "--replay", str(source),
                     "--out", str(out)])
    names = sorted(path.name for path in out.iterdir())
    assert len(names) == 2 and all(re.fullmatch(r"hand-worker-\d{8}T\d{6}Z-424[23]\.jsonl", n) for n in names)
    records = [json.loads(line) for line in (out / names[0]).read_text(encoding="utf-8").splitlines()]
    # Replay never sends, and its records are as before: no delivery fields, no hand events.
    assert set(records[0]) == {"type", "backend", "synthetic", "gameplay_transport",
                               "final_gesture_accuracy_validated", "timing_scope"}
    assert records[0]["gameplay_transport"] is False
    assert set(records[-1]) == {"type", "status", "counts", "hands"}
    assert records[-1]["counts"]["prediction"] == 42 and "hand_event" not in records[-1]["counts"]
    # A file path keeps its earlier behaviour: an existing file is never overwritten.
    with pytest.raises(FileExistsError):
        worker.main(["--bundle", str(synthetic_bundle), "--allow-synthetic", "--replay", str(source),
                     "--out", str(out / names[0])])


def cli_error(capsys, *argv):
    with pytest.raises(SystemExit) as stopped:
        worker.main(list(argv))
    assert stopped.value.code == 2
    return capsys.readouterr().err


def test_the_cli_refuses_delivery_settings_and_observe_only_misuse_before_the_bundle(monkeypatch, capsys):
    client, peer = socket_pair()
    with_unix(monkeypatch, client)
    monkeypatch.setattr(worker, "ModelBundle", lambda *a: pytest.fail("bundle read"))
    # HandEngine accepts 11 confirmations; the hand-event hello allows at most 10.
    assert "Compat delivery refuses" in cli_error(capsys, "--bundle", "b", "--socket", "/x.sock",
                                                  "--confirmations", "11", "--out", "o")
    assert "Compat delivery refuses" in cli_error(capsys, "--bundle", "b", "--socket", "/x.sock",
                                                  "--session-id", "", "--out", "o")
    assert "--observe-only is for --socket" in cli_error(capsys, "--bundle", "b", "--replay", "r",
                                                         "--observe-only", "--out", "o")
    client.close()
    peer.close()


# --- hand events: the bridge offers them ("hand_events":1) ---------------------------------

NO_HAND = dict(sent=0, unsent=0, not_offered=0)


def compact(line):
    return json.dumps(line, separators=(",", ":")).encode("utf-8")


def sample_lines(*samples):
    return "".join(json.dumps(message) + "\n" for message in samples).encode("utf-8")


def check_hand_lines(lines):
    """The worker's lines on one offered connection: the hand_hello first and only once; every
    hand_event valid against that hello (Wen Ree's validate_event), each hand's event_seq 1, 2,
    3, ...; every result line straight after the hand_event of its own event. Returns the hello."""
    assert lines[0]["type"] == "hand_hello" and set(lines[0]) == {"type", "hello"}
    hello = validate_hello(lines[0]["hello"])
    expected = {LEFT: 1, RIGHT: 1}
    for index, line in enumerate(lines[1:], 1):
        if line["type"] == "hand_event":
            assert set(line) == {"type", "event"}
            event = validate_event(line["event"], hello)
            assert event["event_seq"] == expected[event["device_id"]]
            expected[event["device_id"]] += 1
            continue
        assert line["type"] == "result" and list(line) == RESULT_KEYS
        before = lines[index - 1]
        assert before["type"] == "hand_event" and before["event"]["kind"] in worker.FORWARDED
        event = before["event"]
        assert (line["gesture"], line["confidence"], line["device_id"], line["boot_id"], line["seq"]) == (
            event["gesture"], event["confidence"], event["device_id"], event["source"]["boot_id"],
            event["source"]["seq"])
    return hello


def hand_events_sent(lines):
    return [line["event"] for line in lines if line["type"] == "hand_event"]


def test_offered_hand_events_carry_every_kind_and_leave_the_results_as_they_were():
    plain, offered = Run(), Run(offered=True)
    for run in (plain, offered):
        run.play(3.0, left="CHOP", right="PICKUP")
        run.play(2.0, left="POURING", right="IDLE")
        run.play(3.0)  # both silent: the POURING state times out
    # The compat stream and the records are exactly as without the offer.
    assert offered.results == plain.results and offered.results
    without = [{k: v for k, v in r.items() if k != "hand_delivery"} for r in offered.events()]
    assert without == [{k: v for k, v in r.items() if k != "hand_delivery"} for r in plain.events()]
    assert plain.lines == plain.results and {r["hand_delivery"] for r in plain.events()} == {"not_offered"}
    assert_compat(offered)
    # Every event is a hand line, in encoder order, and each result follows its own event's line.
    assert check_hand_lines(offered.lines) == offered.delivery.encoder.hello
    events = hand_events_sent(offered.lines)
    assert [e["event_id"] for e in events] == [r["event_id"] for r in offered.events()]
    assert {r["hand_delivery"] for r in offered.events()} == {"sent"}
    assert {e["kind"] for e in events} == {"pulse", "state_start", "state_refresh", "state_end", "idle"}
    left = [e for e in events if e["device_id"] == LEFT]
    right = [e for e in events if e["device_id"] == RIGHT]
    assert [(e["kind"], e["gesture"]) for e in right] == [("pulse", "PICKUP"), ("idle", "IDLE")]
    # Both hands in one stream: the right hand's events come between the left hand's.
    devices = [e["device_id"] for e in events]
    assert devices.index(RIGHT) < len(devices) - 1 - devices[::-1].index(LEFT)
    # CHOP to POURING on one sample: state_end CHOP, then state_start POURING, one result (the start's).
    [end] = [e for e in left if e["kind"] == "state_end" and e["gesture"] == "CHOP"]
    start = left[left.index(end) + 1]
    assert end["reason"] == "confirmed_transition" and end["state_id"] == left[0]["event_id"]
    assert end["confidence"] is None and end["lease_ms"] == 0
    assert (start["kind"], start["gesture"], start["source"]) == ("state_start", "POURING", end["source"])
    assert start["state_id"] == start["event_id"] != end["event_id"]
    assert [(r["gesture"], r["seq"]) for r in offered.hand(LEFT) if r["seq"] == start["source"]["seq"]] == [
        ("POURING", start["source"]["seq"])]
    # The silence ends the POURING state: a hand line, and no result.
    assert (left[-1]["kind"], left[-1]["gesture"], left[-1]["reason"]) == ("state_end", "POURING",
                                                                           "prediction_timeout")
    assert offered.lines[-1] == dict(type="hand_event", event=left[-1])
    assert offered.delivery.hand_summary() == dict(
        hello=dict(NO_HAND, sent=1), hands={"1": dict(NO_HAND, sent=len(left)), "2": dict(NO_HAND, sent=2)})


def test_offered_an_encoder_error_sends_its_reset_as_a_hand_event(monkeypatch):
    run = Run(offered=True)
    run.play(2.5, left="CHOP", right="CHOP")
    encoder = run.delivery.encoder
    original = encoder.consume
    failures = []

    def consume(record):
        if not failures and record["type"] == "prediction" and record["device_id"] == LEFT:
            failures.append(record)
            raise ProtocolError("scripted encoder failure")
        return original(record)

    monkeypatch.setattr(encoder, "consume", consume)
    run.play(1.0, left="CHOP", right="CHOP")
    check_hand_lines(run.lines)  # also: no result line follows the reset's line
    [reset] = [e for e in hand_events_sent(run.lines) if e["kind"] == "reset"]
    assert (reset["device_id"], reset["reason"], reset["lease_ms"]) == (LEFT, "manual_reset", 0)
    assert all(reset[k] is None for k in ("gesture", "confidence", "state_id"))
    # Wen Ree's encoder gives a reset the hand's last source sample (null only before any sample).
    assert reset["source"]["boot_id"] == 11 and set(reset["source"]) == {"boot_id", "seq", "uptime_ms"}
    [record] = run.events(kind="reset")
    assert (record["delivery"], record["hand_delivery"]) == ("not_forwarded", "sent")
    assert run.delivery.hand_summary()["hands"]["1"] == dict(NO_HAND, sent=len(run.events(LEFT)))
    assert_compat(run)


def test_offered_a_second_result_for_one_sample_still_sends_its_hand_event():
    run = Run(offered=True)
    run.play(3.0, left="PICKUP")
    record = dict(type="idle", device_id=LEFT, gesture="IDLE", confidence=0.94, elapsed_s=run.clock(),
                  boot_id=11, seq=120, uptime_ms=1000 + 120 * 20)
    run.delivery.consume(record, run.send)
    idle = run.events(LEFT, "idle")[-1]
    assert (idle["delivery"], idle["hand_delivery"]) == ("collision", "sent")
    assert run.lines[-1]["type"] == "hand_event" and run.lines[-1]["event"]["event_id"] == idle["event_id"]
    assert len(run.results) == 1 and run.counts(LEFT)["collisions"] == 1
    check_hand_lines(run.lines)


def test_offered_a_glove_reboot_sends_the_old_state_end_before_any_event_of_the_new_boot():
    run = Run(offered=True)
    run.play(3.0, left="CHOP", right="PICKUP")
    run.left = Glove(LEFT, 12)  # the left glove rebooted: new boot_id, seq from 0
    run.play(3.0, left="CHOP", right="PICKUP")
    check_hand_lines(run.lines)
    events = hand_events_sent(run.lines)
    left = [e for e in events if e["device_id"] == LEFT]
    starts = [e for e in left if e["kind"] == "state_start"]
    [end] = [e for e in left if e["kind"] == "state_end"]
    # The old state ends (source_reset) naming its own state and a sample of the old boot,
    # after every event of the old boot and before every event of the new one.
    cut = left.index(end)
    assert (end["reason"], end["state_id"], end["source"]["boot_id"]) == ("source_reset", starts[0]["state_id"], 11)
    assert {e["source"]["boot_id"] for e in left[:cut + 1]} == {11}
    assert {e["source"]["boot_id"] for e in left[cut + 1:]} == {12}
    assert [s["source"]["boot_id"] for s in starts] == [11, 12]
    assert left[cut + 1] == starts[1] and starts[1]["state_id"] == starts[1]["event_id"] != starts[0]["state_id"]
    # The other hand is untouched: its one PICKUP pulse, nothing more.
    assert [(e["kind"], e["gesture"]) for e in events if e["device_id"] == RIGHT] == [("pulse", "PICKUP")]
    assert {r["hand_delivery"] for r in run.events()} == {"sent"}
    assert_compat(run)


class ScriptedBridge(threading.Thread):
    """The bridge end, step by step: send a step's data, then read until that many lines in all
    have come; after the last step end the stream and read whatever else arrives."""

    def __init__(self, peer, *steps):
        super().__init__(daemon=True)
        self.peer, self.steps = peer, steps
        self.received, self.rest, self.errors = b"", b"", []

    def run(self):
        try:
            self.peer.settimeout(5)
            for data, count in self.steps:
                self.peer.sendall(data)
                while self.received.count(b"\n") < count:
                    chunk = self.peer.recv(65536)
                    if not chunk:
                        raise EOFError("the worker closed the connection early")
                    self.received += chunk
            self.peer.shutdown(socket.SHUT_WR)
            while True:
                chunk = self.peer.recv(65536)
                if not chunk:
                    break
                self.rest += chunk
        except Exception as error:  # reported by the test
            self.errors.append(error)

    def lines(self):
        return [json.loads(line) for line in self.received.splitlines()]


def observe(*steps, captured=None):
    """observe_socket with a Delivery (one confirmation) against a ScriptedBridge. With captured,
    a copy of every event the encoder makes is kept there, as it was made."""
    client, peer = socket_pair()
    engine = HandEngine(ScriptedBackend(), confirmations=1)
    records = []
    delivery = worker.Delivery(hello(confirmations=1), records.append)
    if captured is not None:
        original = delivery.encoder.consume

        def consume(record):
            events = original(record)
            captured.extend(copy.deepcopy(events))
            return events

        delivery.encoder.consume = consume
    bridge = ScriptedBridge(peer, *steps)
    bridge.start()
    try:
        worker.observe_socket("fixture", engine, records.append, connection_factory=lambda path: client,
                              deliver=delivery)
    finally:
        bridge.join(10)
        peer.close()
    assert not bridge.is_alive() and bridge.errors == [] and bridge.rest == b""
    return bridge, delivery, records


def hand_records(records):
    return [r for r in records if r["type"] == "hand_event"]


def test_offered_the_hand_hello_comes_first_and_each_event_line_before_its_result():
    left = Glove(LEFT, 11)
    chop = bridge_lines((left, "CHOP"), hello=HAND_HELLO)  # seq 0..100: the first window is at seq 100
    pouring = sample_lines(left.sample("POURING"))  # seq 101, well within the 0.5 s receive gap
    captured = []
    bridge, delivery, records = observe((chop, 3), (pouring, 6), captured=captured)
    raw = bridge.received.splitlines()
    lines = bridge.lines()
    assert [line["type"] for line in lines] == ["hand_hello", "hand_event", "result",
                                                "hand_event", "hand_event", "result"]
    assert check_hand_lines(lines) == delivery.encoder.hello == hello(confirmations=1)
    events = hand_events_sent(lines)
    assert [(e["kind"], e["gesture"], e["event_seq"], e["source"]["seq"]) for e in events] == [
        ("state_start", "CHOP", 1, 100), ("state_end", "CHOP", 2, 101), ("state_start", "POURING", 3, 101)]
    assert events[1]["state_id"] == events[0]["event_id"] and events[1]["reason"] == "confirmed_transition"
    assert [(r["gesture"], r["seq"]) for r in lines if r["type"] == "result"] == [("CHOP", 100), ("POURING", 101)]
    # Each line is the compact JSON of the encoder's own dict, unchanged, and well under 4096 bytes.
    assert raw[0] == compact(dict(type="hand_hello", hello=hello(confirmations=1)))
    assert [line for line in raw if line.startswith(b'{"type":"hand_event"')] == [
        compact(dict(type="hand_event", event=event)) for event in captured[:3]]
    assert all(len(line) < 1024 for line in raw)
    # The stream's end ends the POURING state: recorded, nothing more sent (the bridge saw no rest).
    assert [(r["kind"], r["delivery"], r["hand_delivery"]) for r in hand_records(records)] == [
        ("state_start", "forwarded", "sent"), ("state_end", "not_forwarded", "sent"),
        ("state_start", "forwarded", "sent"), ("state_end", "not_forwarded", "unsent")]
    assert len(captured) == 4 and captured[3]["reason"] == "connection_reset"
    assert [r["event_id"] for r in hand_records(records)] == [e["event_id"] for e in captured]
    assert [r for r in records if r["type"] == "hand_hello"] == [
        dict(type="hand_hello", worker_epoch=EPOCH, hand_delivery="sent")]
    assert delivery.hand_summary() == dict(hello=dict(NO_HAND, sent=1),
                                           hands={"1": dict(NO_HAND, sent=3, unsent=1), "2": NO_HAND})


@pytest.mark.parametrize("offer", ["absent", True, 2, "1", 0], ids=lambda offer: "hand_events={}".format(offer))
def test_a_bridge_that_does_not_offer_hand_events_gets_the_result_lines_alone(offer):
    left = Glove(LEFT, 11)
    bridge_hello = HELLO if offer == "absent" else dict(HELLO, hand_events=offer)
    chop = bridge_lines((left, "CHOP"), hello=bridge_hello)
    pouring = sample_lines(left.sample("POURING"))
    bridge, delivery, records = observe((chop, 1), (pouring, 2))
    assert bridge.received.splitlines() == [
        b'{"type":"result","gesture":"CHOP","confidence":0.94,"device_id":1,"boot_id":11,"seq":100}',
        b'{"type":"result","gesture":"POURING","confidence":0.94,"device_id":1,"boot_id":11,"seq":101}']
    assert [(r["kind"], r["delivery"], r["hand_delivery"]) for r in hand_records(records)] == [
        ("state_start", "forwarded", "not_offered"), ("state_end", "not_forwarded", "not_offered"),
        ("state_start", "forwarded", "not_offered"), ("state_end", "not_forwarded", "not_offered")]
    assert [r for r in records if r["type"] == "hand_hello"] == [
        dict(type="hand_hello", worker_epoch=EPOCH, hand_delivery="not_offered")]
    assert delivery.hand_summary() == dict(hello=dict(NO_HAND, not_offered=1),
                                           hands={"1": dict(NO_HAND, not_offered=4), "2": NO_HAND})


def test_offered_both_hands_interleave_on_one_connection_each_with_its_own_event_seq():
    left, right = Glove(LEFT, 11), Glove(RIGHT, 22)
    steps = [bridge_lines((left, "CHOP"), (right, "PICKUP"), hello=HAND_HELLO),
             sample_lines(left.sample("POURING"), right.sample("IDLE")),
             sample_lines(left.sample("WASH"), right.sample("THROW"))]
    bridge, delivery, records = observe((steps[0], 5), (steps[1], 10), (steps[2], 15))
    lines = bridge.lines()
    assert len(lines) == 15
    hello_sent = check_hand_lines(lines)
    events = hand_events_sent(lines)
    assert [(e["event_seq"], e["kind"], e["gesture"]) for e in events if e["device_id"] == LEFT] == [
        (1, "state_start", "CHOP"), (2, "state_end", "CHOP"), (3, "state_start", "POURING"),
        (4, "state_end", "POURING"), (5, "state_start", "WASH")]
    assert [(e["event_seq"], e["kind"], e["gesture"]) for e in events if e["device_id"] == RIGHT] == [
        (1, "pulse", "PICKUP"), (2, "idle", "IDLE"), (3, "pulse", "THROW")]
    assert len({e["event_id"] for e in events}) == len(events)
    assert {e["event_id"].split(":")[0] for e in events} == {hello_sent["worker_epoch"]}
    # One ordered stream: every step's lines carry both hands.
    devices = [line["device_id"] if line["type"] == "result" else line["event"]["device_id"] for line in lines[1:]]
    assert [set(devices[0:4]), set(devices[4:9]), set(devices[9:14])] == [{LEFT, RIGHT}] * 3
    assert sorted((r["device_id"], r["seq"], r["gesture"]) for r in lines if r["type"] == "result") == [
        (1, 100, "CHOP"), (1, 101, "POURING"), (1, 102, "WASH"),
        (2, 100, "PICKUP"), (2, 101, "IDLE"), (2, 102, "THROW")]
    # The stream's end ends the WASH state (event_seq 6), recorded and not sent.
    assert [(r["kind"], r["hand_delivery"]) for r in hand_records(records) if r["device_id"] == LEFT][-1] == (
        "state_end", "unsent")
    assert delivery.hand_summary()["hands"] == {"1": dict(NO_HAND, sent=5, unsent=1), "2": dict(NO_HAND, sent=3)}


def test_offered_a_failed_hand_hello_ends_the_connection_and_nothing_is_sent():
    client, peer = socket_pair()
    failing = FailingClient(client)
    engine = HandEngine(ScriptedBackend(), confirmations=1)
    records = []
    delivery = worker.Delivery(hello(confirmations=1), records.append)
    bridge = Peer(peer, bridge_lines((Glove(LEFT, 11), "PICKUP"), (Glove(RIGHT, 22), "WASH"), hello=HAND_HELLO),
                  done=lambda: failing.sends > 0)
    bridge.start()
    try:
        worker.observe_socket("fixture", engine, records.append, connection_factory=lambda path: failing,
                              deliver=delivery)  # returns: no exception
    finally:
        bridge.join(10)
        peer.close()
    assert bridge.errors == [] and bridge.rest == b"" and failing.sends == 1
    [lost] = [r for r in records if r["type"] == "connection_lost"]
    assert lost["error_type"] == "BrokenPipeError"
    assert [r for r in records if r["type"] == "hand_hello"] == [
        dict(type="hand_hello", worker_epoch=EPOCH, hand_delivery="unsent")]
    before = records[:records.index(lost)]
    assert lost["hand_events_unsent"] == sum(r["type"] == "hand_event" and r["hand_delivery"] == "unsent"
                                             for r in before)
    assert lost["results_unsent"] == sum(r["type"] == "hand_event" and r["delivery"] == "unsent" for r in before)
    events = hand_records(records)
    assert all(e["hand_delivery"] == "unsent" and e["delivery"] != "forwarded" for e in events)
    summary = delivery.hand_summary()
    assert summary["hello"] == dict(NO_HAND, unsent=1) and delivery.closed
    assert sum(summary["hands"][h]["unsent"] for h in ("1", "2")) == len(events)
    assert sum(summary["hands"][h]["sent"] for h in ("1", "2")) == 0


def test_offered_a_failed_hand_event_send_tries_no_result_and_ends_the_connection():
    client, peer = socket_pair()
    failing = FailingClient(client, after=1)  # the hand_hello goes; the first hand_event line fails
    engine = HandEngine(ScriptedBackend(), confirmations=1)
    records = []
    delivery = worker.Delivery(hello(confirmations=1), records.append)
    bridge = Peer(peer, bridge_lines((Glove(LEFT, 11), "PICKUP"), hello=HAND_HELLO), done=lambda: failing.sends > 1)
    bridge.start()
    try:
        worker.observe_socket("fixture", engine, records.append, connection_factory=lambda path: failing,
                              deliver=delivery)
    finally:
        bridge.join(10)
        peer.close()
    assert bridge.errors == [] and failing.sends == 2  # the pulse's result line was never tried
    assert [json.loads(line)["type"] for line in bridge.rest.splitlines()] == ["hand_hello"]
    [lost] = [r for r in records if r["type"] == "connection_lost"]
    assert (lost["error_type"], lost["hand_events_unsent"], lost["results_unsent"]) == ("BrokenPipeError", 1, 1)
    [pulse] = hand_records(records)
    assert (pulse["kind"], pulse["delivery"], pulse["hand_delivery"]) == ("pulse", "unsent", "unsent")
    assert delivery.summary()["1"]["unsent"] == 1 and delivery.summary()["1"]["forwarded"] == 0
    assert delivery.hand_summary() == dict(hello=dict(NO_HAND, sent=1),
                                           hands={"1": dict(NO_HAND, unsent=1), "2": NO_HAND})


def test_main_offered_each_connection_has_its_own_hand_hello_epoch_and_event_seq_from_1(
        monkeypatch, synthetic_bundle, tmp_path, capsys):
    (first, first_peer), (second, second_peer) = socket_pair(), socket_pair()
    with_unix(monkeypatch, first, second)
    left, right = Glove(LEFT, 11), Glove(RIGHT, 22)
    bridges = [Peer(first_peer, bridge_lines((left, "THROW"), (right, "IDLE"), hello=HAND_HELLO), expect=5),
               Peer(second_peer, bridge_lines((left, "THROW"), (right, "IDLE"), hello=HAND_HELLO), expect=5)]
    for bridge in bridges:
        bridge.start()
    try:
        worker.main(socket_argv(synthetic_bundle, tmp_path))
    finally:
        for bridge in bridges:
            bridge.join(10)
        first_peer.close()
        second_peer.close()
    assert all(bridge.errors == [] and bridge.rest == b"" for bridge in bridges)
    sent = [[json.loads(line) for line in bridge.received.splitlines()] for bridge in bridges]
    hellos = [check_hand_lines(lines) for lines in sent]
    records = read_records(tmp_path)
    opened = [r for r in records if r["type"] == "connection_open"]
    assert [h["worker_epoch"] for h in hellos] == [r["worker_epoch"] for r in opened]
    assert hellos[0]["worker_epoch"] != hellos[1]["worker_epoch"]
    assert all(h == make_hello(SESSION, h["worker_epoch"], worker.LEASE_MS, 0.70, 1) for h in hellos)
    for lines, results_at in zip(sent, (100, 201)):
        events = hand_events_sent(lines)
        assert sorted((e["device_id"], e["event_seq"], e["kind"], e["source"]["seq"]) for e in events) == [
            (LEFT, 1, "pulse", results_at), (RIGHT, 1, "idle", results_at)]
    # An event of the first connection is obsolete against the second connection's hello.
    with pytest.raises(ProtocolError, match="obsolete"):
        validate_event(hand_events_sent(sent[0])[0], hellos[1])
    assert [(r["worker_epoch"], r["hand_delivery"]) for r in records if r["type"] == "hand_hello"] == [
        (h["worker_epoch"], "sent") for h in hellos]
    assert records[0]["hand_events"] == "when_offered"
    summary = records[-1]
    assert summary["status"] == "finished"
    assert {hand: counts["forwarded"] for hand, counts in summary["delivery"].items()} == {"1": 2, "2": 2}
    assert summary["hand_delivery"] == dict(hello=dict(NO_HAND, sent=2),
                                            hands={"1": dict(NO_HAND, sent=2), "2": dict(NO_HAND, sent=2)})
    # The operator's console shows each connection's hand_hello.
    console = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["hand_delivery"] for r in console if r.get("type") == "hand_hello"] == ["sent", "sent"]


# --- loopback: TLS service (v4) -> model bridge -> worker -> subscriber ---------------------

TICK = 0.02  # 50 Hz
TICKS = 380  # 7.6 s
DUPLICATE_TICK = 150  # the left sample re-sent unchanged
RESULT_FIELDS = {"v", "type", "session_id", "device_id", "boot_id", "seq", "request_id",
                 "result_id", "gesture", "confidence"}


def left_label(tick):
    return "CHOP" if tick < 200 else "POURING"


def right_label(tick):
    # PICKUP, then 1.3 s of silence (a glove reconnect), then PICKUP again.
    return None if 150 <= tick < 215 else "PICKUP"


async def glove(server, context, device, boot, label_at, acks, start):
    """One hand's laptop relay: a v4 SENSOR_BATCH every 20 ms, each waiting for its ACK.

    seq and uptime follow the tick, so the silent ticks are samples lost in the reconnect.
    """
    from common.sensor_v4 import SensorPacket
    from common.tls import TLS_SERVER_NAME
    from common.wire import read_frame, write_frame
    reader, writer = await asyncio.open_connection("127.0.0.1", server.ingest_port, ssl=context,
                                                   server_hostname=TLS_SERVER_NAME)
    loop = asyncio.get_running_loop()
    try:
        for tick in range(TICKS):
            await asyncio.sleep(max(0.0, start + tick * TICK - loop.time()))
            label = label_at(tick)
            if label is None:
                continue
            raw = [0] * 12
            raw[11] = LABELS.index(label) * 1000  # flex3 x10000: physical label index / 10
            message = SensorPacket(device, boot, tick, 1000 + tick * 20, tuple(raw)).to_message(SESSION)
            for _ in range(2 if device == LEFT and tick == DUPLICATE_TICK else 1):
                await write_frame(writer, message)
                ack = await read_frame(reader)
                acks.append((device, tick, ack["status"]))
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 1)
        except (OSError, ConnectionError, asyncio.TimeoutError):
            writer.transport.abort()


async def scenario(pki, hand_events=False):
    """hand_events: the bridge offers hand events as in the service's v4 mode, and the phone
    subscribes with version 2; otherwise the bridge's channels are set by hand (no offer) and
    the phone subscribes with version 1, as before."""
    from common.sensor_v4 import CHANNELS as V4_CHANNELS
    from common.tls import TLS_SERVER_NAME, client_context, server_context
    from common.wire import read_frame, write_frame
    from ultra96.model_bridge import Bridge
    from ultra96.server import CommsServer

    class BridgeModel:
        """The service's model: a model_bridge.Bridge served over a socket pair, not a Unix listener."""
        name = "hand-worker-loopback"

        def __init__(self):
            self.bridge = Bridge()

        def configure_sensor_version(self, version):
            assert version == 4
            if hand_events:
                self.bridge.configure_sensor_version(version)
            else:
                self.bridge.channels = tuple(V4_CHANNELS)

        def start(self, emit):
            self.bridge.start(emit)

        def on_sample(self, device_id, boot_id, seq, uptime_ms, values):
            self.bridge.on_sample(device_id, boot_id, seq, uptime_ms, values)

    if hand_events:
        BridgeModel.start_hand_events = lambda self, emit_hand: self.bridge.start_hand_events(emit_hand)
    model = BridgeModel()
    server = CommsServer(server_context(pki / "server-cert.pem", pki / "server-key.pem"),
                         ingest_port=0, gateway_port=0, model=model, sensor_version=4)
    await server.start()
    service_end, worker_end = socket.socketpair()
    worker_end.settimeout(1)
    model.bridge.attach(service_end)  # after start(): the bridge has its emit
    records = []
    engine = HandEngine(ScriptedBackend())
    delivery = worker.Delivery(make_hello(SESSION, "{:032x}".format(7), worker.LEASE_MS, 0.70, 3),
                               records.append)
    failures = []

    def run_worker():
        try:
            worker.observe_socket("loopback", engine, records.append,
                                  connection_factory=lambda path: worker_end, deliver=delivery)
        except Exception as error:  # reported by the test
            failures.append(error)

    thread = threading.Thread(target=run_worker, name="hand-worker", daemon=True)
    thread.start()
    phone_writer = None
    consumer = None
    results, acks = [], []
    try:
        context = client_context(pki / "ca-cert.pem")
        phone, phone_writer = await asyncio.open_connection(
            "127.0.0.1", server.gateway_port, ssl=context, server_hostname=TLS_SERVER_NAME)
        if hand_events:
            await write_frame(phone_writer, dict(v=2, type="SUBSCRIBE", session_id=SESSION, event_format=1))
            assert await read_frame(phone) == dict(v=2, type="SUBSCRIBED", session_id=SESSION, event_format=1)
        else:
            await write_frame(phone_writer, dict(v=1, type="SUBSCRIBE", session_id=SESSION))
            assert await read_frame(phone) == dict(v=1, type="SUBSCRIBED", session_id=SESSION)

        async def receive():
            while True:
                results.append(await read_frame(phone, timeout=30))

        consumer = asyncio.create_task(receive())
        start = asyncio.get_running_loop().time() + 0.1
        await asyncio.gather(glove(server, context, LEFT, 11, left_label, acks, start),
                             glove(server, context, RIGHT, 22, right_label, acks, start))
        # Let the last inferences finish, then wait until the subscriber has every result
        # (and, with hand events, every published hand event).
        settled, deadline = time.monotonic() + 0.5, time.monotonic() + 3
        while time.monotonic() < settled or len(results) < (
                server.metrics.get("model_results", 0) + server.metrics.get("hand_events_published", 0)):
            assert time.monotonic() < deadline, "results did not reach the subscriber"
            await asyncio.sleep(0.02)
    finally:
        model.bridge.close()  # the worker sees the stream end and returns
        thread.join(5)
        if consumer is not None:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        if phone_writer is not None:
            phone_writer.close()
            try:
                await asyncio.wait_for(phone_writer.wait_closed(), 1)
            except (OSError, ConnectionError, asyncio.TimeoutError):
                phone_writer.transport.abort()
        await server.close()
    return dict(server=server, bridge=model.bridge, delivery=delivery, records=records, results=results,
                acks=acks, failures=failures, worker_alive=thread.is_alive())


@pytest.fixture(scope="module")
def loopback(tmp_path_factory):
    pytest.importorskip("cryptography")
    from tools.generate_comms_pki import generate_pki
    pki = generate_pki(tmp_path_factory.mktemp("hand-worker-loopback") / "pki")
    return asyncio.run(scenario(pki))


@pytest.fixture(scope="module")
def hand_loopback(tmp_path_factory):
    """The same run with hand events offered and a version-2 subscriber."""
    pytest.importorskip("cryptography")
    from tools.generate_comms_pki import generate_pki
    pki = generate_pki(tmp_path_factory.mktemp("hand-worker-hand-loopback") / "pki")
    run = asyncio.run(scenario(pki, hand_events=True))
    frames = run["results"]
    run["results"] = [frame for frame in frames if frame["type"] == "GESTURE_RESULT"]
    run["hand_frames"] = [frame for frame in frames if frame["type"] == "HAND_EVENT"]
    run["other_frames"] = [frame for frame in frames if frame["type"] not in ("GESTURE_RESULT", "HAND_EVENT")]
    return run


def loop_events(run, device, kind=None, delivery=None):
    return [r for r in run["records"] if r["type"] == "hand_event" and r["device_id"] == device
            and (kind is None or r["kind"] == kind) and (delivery is None or r["delivery"] == delivery)]


def loop_hand(run, device):
    return [r for r in run["results"] if r["device_id"] == device]


def test_loopback_the_worker_and_the_service_ran_cleanly(loopback):
    assert not loopback["failures"] and not loopback["worker_alive"]
    metrics = loopback["server"].metrics
    assert metrics["model_results_duplicate"] == 0 and metrics["model_results_rejected"] == 0
    assert metrics["model_errors"] == 0 and metrics["rejected"] == 0
    assert loopback["bridge"].counts["results_refused"] == 0 and loopback["bridge"].counts["bad_lines"] == 0
    for counts in loopback["delivery"].summary().values():
        assert counts["collisions"] == 0 and counts["encoder_errors"] == 0 and counts["unsent"] == 0
    assert not any(r["type"] in ("connection_lost", "receive_error") for r in loopback["records"])


def test_loopback_every_forwarded_event_is_one_v3_gesture_result_naming_its_own_sample(loopback):
    results = loopback["results"]
    forwarded = sum(counts["forwarded"] for counts in loopback["delivery"].summary().values())
    assert results and len(results) == forwarded == loopback["server"].metrics["model_results"]
    assert loopback["bridge"].counts["results"] == forwarded
    for result in results:
        assert set(result) == RESULT_FIELDS and result["type"] == "GESTURE_RESULT" and result["v"] == 3
        assert result["session_id"] == SESSION and result["request_id"] is None
        assert result["result_id"] == "{}:{}:{}".format(result["device_id"], result["boot_id"], result["seq"])
    assert len({r["result_id"] for r in results}) == len(results)
    sent = [(e["device_id"], e["source"]["boot_id"], e["source"]["seq"])
            for e in loopback["records"] if e["type"] == "hand_event" and e["delivery"] == "forwarded"]
    assert sorted(sent) == sorted((r["device_id"], r["boot_id"], r["seq"]) for r in results)


def test_loopback_each_hand_gets_its_own_gestures(loopback):
    left, right = loop_hand(loopback, LEFT), loop_hand(loopback, RIGHT)
    assert {r["boot_id"] for r in left} == {11} and {r["boot_id"] for r in right} == {22}
    gestures = [r["gesture"] for r in left]
    assert gestures[0] == "CHOP" and gestures[-1] == "POURING"
    assert gestures == sorted(gestures, key=["CHOP", "POURING"].index)  # CHOP..., then POURING...
    # The right glove's reconnect reset that hand: its second PICKUP needed a new window.
    assert [r["gesture"] for r in right] == ["PICKUP", "PICKUP"]
    assert right[0]["seq"] < 150 and right[1]["seq"] >= 215 + 100


def test_loopback_a_transition_gives_one_result_at_the_transition_sample(loopback):
    start = [e for e in loop_events(loopback, LEFT, "state_start") if e["gesture"] == "POURING"]
    assert len(start) == 1 and start[0]["delivery"] == "forwarded"
    transition = start[0]["source"]["seq"]
    assert [r["gesture"] for r in loop_hand(loopback, LEFT) if r["seq"] == transition] == ["POURING"]
    first_pouring = next(r for r in loop_hand(loopback, LEFT) if r["gesture"] == "POURING")
    assert first_pouring["seq"] == transition >= 200
    ends = loop_events(loopback, LEFT, "state_end")
    # The 0.6 s state timeout equals three 0.2 s confirmations, so in real time the CHOP state
    # ends by timeout or by the confirmed transition, a few ms either way. Neither is sent.
    assert ends and ends[0]["gesture"] == "CHOP"
    assert ends[0]["reason"] in ("confirmed_transition", "prediction_timeout")
    assert ends[0]["source"]["seq"] <= transition
    assert all(e["delivery"] == "not_forwarded" for e in ends)


def test_loopback_a_resent_batch_is_a_duplicate_ack_and_no_extra_result(loopback):
    resent = [status for device, tick, status in loopback["acks"] if (device, tick) == (LEFT, DUPLICATE_TICK)]
    assert resent == ["accepted", "duplicate"]
    assert loopback["server"].metrics["duplicates"] == 1
    assert all(status == "accepted" for device, tick, status in loopback["acks"]
               if (device, tick) != (LEFT, DUPLICATE_TICK))


def test_loopback_a_glove_reconnect_leaves_the_other_hand_alone(loopback):
    # The left hand's CHOP state ran on through the right glove's reconnect.
    chop = [e for e in loop_events(loopback, LEFT, "state_start") if e["gesture"] == "CHOP"]
    assert len(chop) == 1
    refreshes = [e for e in loop_events(loopback, LEFT, "state_refresh") if e["state_id"] == chop[0]["state_id"]]
    assert any(150 <= e["source"]["seq"] < 215 for e in refreshes)
    assert not loop_events(loopback, RIGHT, "state_end") and not loop_events(loopback, RIGHT, "reset")


# --- loopback with hand events: the same run, the bridge offers them, a version-2 phone -------

def test_hand_loopback_ran_cleanly_with_no_refused_hand_event(hand_loopback):
    run = hand_loopback
    assert not run["failures"] and not run["worker_alive"] and not run["other_frames"]
    metrics, counts = run["server"].metrics, run["bridge"].counts
    assert metrics["hand_hellos"] == 1 and metrics["hand_events_refused"] == 0
    assert metrics["hand_gate_errors"] == 0 and metrics["hand_events_dropped"] == 0
    assert counts["bad_lines"] == 0 and counts["hand_refused"] == 0 and counts["results_refused"] == 0
    assert not any(r["type"] in ("connection_lost", "receive_error", "encoder_error") for r in run["records"])


def test_hand_loopback_keeps_the_compat_results_exactly(hand_loopback):
    run = hand_loopback
    results = run["results"]
    forwarded = sum(counts["forwarded"] for counts in run["delivery"].summary().values())
    assert results and len(results) == forwarded == run["server"].metrics["model_results"]
    for result in results:
        assert set(result) == RESULT_FIELDS and result["v"] == 3
    sent = [(e["device_id"], e["source"]["boot_id"], e["source"]["seq"])
            for e in run["records"] if e["type"] == "hand_event" and e["delivery"] == "forwarded"]
    assert sorted(sent) == sorted((r["device_id"], r["boot_id"], r["seq"]) for r in results)


def test_hand_loopback_every_hand_event_sent_arrives_once_unchanged_and_in_order(hand_loopback):
    from common.hand_contract import validate_event
    from phone.receiver import validate_hand_event
    run = hand_loopback
    hello = run["delivery"].encoder.hello
    sent = [r for r in run["records"] if r["type"] == "hand_event" and r["hand_delivery"] == "sent"]
    received = run["hand_frames"]
    assert received and len(received) == len(sent) == run["server"].metrics["hand_events_accepted"]
    assert len(received) == run["server"].metrics["hand_events_published"]
    assert [e["event_id"] for e in received] == [r["event_id"] for r in sent]
    for event, record in zip(received, sent):
        validate_event(event, hello)  # Wen Ree's contract, against the worker's own hello
        validate_hand_event(event, SESSION)  # the phone-side rules
        for key in ("device_id", "kind", "gesture", "state_id", "reason", "source"):
            assert event[key] == record[key]
    for device in (LEFT, RIGHT):
        sequence = [e["event_seq"] for e in received if e["device_id"] == device]
        assert sequence == list(range(1, len(sequence) + 1))  # per hand, no gap, no repeat


def test_hand_loopback_a_phone_tracker_sees_both_hands_and_the_transition(hand_loopback):
    from phone.receiver import HandStateTracker
    received = hand_loopback["hand_frames"]
    tracker = HandStateTracker(SESSION, clock=lambda: 0.0)  # no lease runs out in this replay
    outcomes = [tracker.apply(event) for event in received]
    assert not {"duplicate", "out_of_order", "orphan_refresh", "orphan_end", "stale_epoch"} & set(outcomes)
    left = [e for e in received if e["device_id"] == LEFT]
    right = [e for e in received if e["device_id"] == RIGHT]
    assert {e["kind"] for e in right} == {"pulse"} and {e["gesture"] for e in right} == {"PICKUP"}
    starts = [e for e in left if e["kind"] == "state_start"]
    assert [e["gesture"] for e in starts] == ["CHOP", "POURING"]
    chop_end = next(e for e in left if e["kind"] == "state_end" and e["gesture"] == "CHOP")
    assert chop_end["state_id"] == starts[0]["state_id"]
    assert left.index(chop_end) < left.index(starts[1])  # the old state ends before the new one starts
    if chop_end["reason"] == "confirmed_transition":  # both events name the transition sample
        assert chop_end["source"] == starts[1]["source"]
    refreshes = [e for e in left if e["kind"] == "state_refresh"]
    assert refreshes and all(e["state_id"] in {s["state_id"] for s in starts} for e in refreshes)

