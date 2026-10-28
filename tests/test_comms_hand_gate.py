"""ultra96.hand_gate on its own: Wen Ree's hello, real v4 samples and his encoder's events (no sockets).

The events come from src.hand_events.EventEncoder fed local records, as the worker
makes them; the samples are v4 SENSOR_BATCH messages exactly as the service accepts
them. The server clock is a plain number the test moves forward.
"""
import copy

import pytest

from common.hand_contract import make_hello
from common.sensor_v4 import DUMMY_FIXTURES, SensorPacket
from src.hand_events import EventEncoder
from ultra96.hand_gate import COUNTERS, HandGate
from ultra96.protocol import validate_message

SESSION = "ltc-comms"
LEFT, RIGHT = 1, 2
FIRST, SECOND = "{:032x}".format(0xA1), "{:032x}".format(0xB2)


def packet(device, boot, seq):
    """A v4 SENSOR_BATCH as the service accepts it."""
    message = SensorPacket(device, boot, seq, 1000 + 20 * seq,
                           DUMMY_FIXTURES[seq % len(DUMMY_FIXTURES)]).to_message(SESSION)
    return validate_message(message, "SENSOR_BATCH", SESSION, sensor_version=4)


def renumber(event, sequence):
    """The same event under another event_seq (a test-made event, not the encoder's)."""
    event = dict(event, event_seq=sequence)
    event["event_id"] = "{}:{}:{}".format(event["worker_epoch"], event["device_id"], sequence)
    if event["kind"] == "state_start":
        event["state_id"] = event["event_id"]
    return event


class Run:
    """One gate, the worker's encoder of the current runner connection, and the server clock."""

    def __init__(self, epoch=FIRST, connection=1, hello=True, **options):
        self.metrics = {}
        self.observed = []
        self.gate = HandGate(SESSION, self.metrics, **options,
                             observe=lambda event, **fields: self.observed.append((event, fields)))
        self.now = 100.0
        self.sources = {}
        self.elapsed = {LEFT: 0.0, RIGHT: 0.0}
        self.encoder = EventEncoder(make_hello(SESSION, epoch))
        self.connection = connection
        if hello:
            assert self.connect(epoch, connection) == (True, None)

    def connect(self, epoch, connection):
        """A new runner connection: its hello reaches the gate; a new encoder makes its events."""
        hello = make_hello(SESSION, epoch)
        self.encoder = EventEncoder(hello)
        self.elapsed = {LEFT: 0.0, RIGHT: 0.0}
        self.connection = connection
        return self.gate.hello(connection, copy.deepcopy(hello), self.now)

    def sample(self, device, boot, seq, after=0.02):
        self.now += after
        message = packet(device, boot, seq)
        self.gate.sample(message, self.now)
        self.sources[device] = message
        return message

    def encode(self, kind, device, gesture, confidence=0.93, reason=None, source=None):
        """Wen Ree's EventEncoder fed one local record; returns its one event."""
        self.elapsed[device] += 0.2  # the worker's refresh cadence
        source = self.sources[device] if source is None else source
        record = dict(type=kind, device_id=device, elapsed_s=self.elapsed[device], gesture=gesture,
                      confidence=confidence, **{key: source[key] for key in ("boot_id", "seq", "uptime_ms")})
        if reason is not None:
            record["reason"] = reason
        events = self.encoder.consume(record)
        assert len(events) == 1
        return events[0]

    def start(self, device, gesture):
        return self.encode("state_start", device, gesture)

    def refresh(self, device, gesture, **options):
        return self.encode("prediction", device, gesture, **options)

    def end(self, device, gesture, reason="prediction_timeout", **options):
        return self.encode("state_end", device, gesture, confidence=None, reason=reason, **options)

    def pulse(self, device, gesture, **options):
        return self.encode("action", device, gesture, **options)

    def offer(self, event, connection=None, after=0.0):
        self.now += after
        return self.gate.event(self.connection if connection is None else connection,
                               copy.deepcopy(event), self.now)

    def ended(self):
        return [(fields["device_id"], fields["reason"]) for event, fields in self.observed
                if event == "hand_state_ended"]


def test_the_gate_puts_its_counters_in_the_service_metrics():
    run = Run(hello=False)
    assert set(run.metrics) == set(COUNTERS) and not any(run.metrics.values())


def test_both_hands_at_once_are_independent():
    run = Run()
    run.sample(LEFT, 11, 0)
    run.sample(RIGHT, 22, 0)
    chop, pickup, wash = run.start(LEFT, "CHOP"), run.pulse(RIGHT, "PICKUP"), run.start(RIGHT, "WASH")
    # Each hand counts its own event_seq: the same number on the other hand is no duplicate.
    assert (chop["event_seq"], pickup["event_seq"], wash["event_seq"]) == (1, 1, 2)
    for event in (chop, pickup, wash):
        assert run.offer(event) == (True, None)
    assert run.gate.active_state(LEFT)["state_id"] == chop["state_id"]
    assert run.gate.active_state(RIGHT)["state_id"] == wash["state_id"]
    # Ending the right hand's state leaves the left one running.
    run.sample(RIGHT, 22, 1)
    assert run.offer(run.end(RIGHT, "WASH")) == (True, None)
    assert run.gate.active_state(RIGHT) is None
    assert run.gate.active_state(LEFT)["gesture"] == "CHOP"
    run.sample(LEFT, 11, 1)
    assert run.offer(run.refresh(LEFT, "CHOP")) == (True, None)
    assert (run.metrics["hand_events_accepted"], run.metrics["hand_events_refused"],
            run.metrics["hand_events_duplicate"]) == (5, 0, 0)


def test_a_duplicate_and_an_out_of_order_event_are_refused():
    run = Run()
    run.sample(LEFT, 11, 0)
    first, second = run.pulse(LEFT, "PICKUP"), run.pulse(LEFT, "DROP")
    assert run.offer(first) == (True, None)
    assert run.offer(second) == (True, None)
    assert run.offer(second) == (False, "duplicate")  # the same event_seq, so the same event_id
    assert run.offer(first) == (False, "out_of_order")
    assert (run.metrics["hand_events_accepted"], run.metrics["hand_events_duplicate"],
            run.metrics["hand_events_refused"]) == (2, 1, 1)
    # A gap is allowed: an event the worker made but the gate never saw.
    skipped = run.pulse(LEFT, "THROW")
    assert run.offer(run.pulse(LEFT, "DROP")) == (True, None)
    assert run.offer(skipped) == (False, "out_of_order")


def test_a_chop_to_pouring_transition_on_one_sample_is_two_accepted_events():
    run = Run()
    run.sample(LEFT, 11, 0)
    chop = run.start(LEFT, "CHOP")
    assert run.offer(chop) == (True, None)
    transition = run.sample(LEFT, 11, 1)
    end, pouring = run.end(LEFT, "CHOP", "confirmed_transition"), run.start(LEFT, "POURING")
    source = {key: transition[key] for key in ("boot_id", "seq", "uptime_ms")}
    assert end["source"] == pouring["source"] == source
    assert end["event_id"] != pouring["event_id"] and end["state_id"] == chop["state_id"]
    assert run.offer(end) == (True, None)
    assert run.offer(pouring) == (True, None)
    assert run.gate.active_state(LEFT)["state_id"] == pouring["event_id"]
    assert run.gate.active_state(LEFT)["gesture"] == "POURING"
    # Without the end in between, a start still ends the state before it.
    run.sample(LEFT, 11, 2)
    assert run.offer(run.end(LEFT, "POURING", "confirmed_transition")) == (True, None)
    assert run.offer(run.start(LEFT, "WASH")) == (True, None)


def test_a_refresh_for_an_unknown_ended_or_expired_state_is_refused():
    # Unknown: the gate never saw the start.
    run = Run()
    run.sample(LEFT, 11, 0)
    run.start(LEFT, "CHOP")
    run.sample(LEFT, 11, 1)
    assert run.offer(run.refresh(LEFT, "CHOP")) == (False, "orphan_refresh")
    assert run.offer(run.end(LEFT, "CHOP")) == (False, "orphan_end")
    # Ended: a refresh after the end cannot bring the state back.
    run = Run()
    run.sample(LEFT, 11, 0)
    chop = run.start(LEFT, "CHOP")
    late = run.refresh(LEFT, "CHOP")
    end = run.end(LEFT, "CHOP")
    for event in (chop, end):
        assert run.offer(event) == (True, None)
    assert run.offer(renumber(late, end["event_seq"] + 1)) == (False, "orphan_refresh")
    # A refresh must name the active state's gesture as well as its state_id.
    run = Run()
    run.sample(LEFT, 11, 0)
    chop = run.start(LEFT, "CHOP")
    assert run.offer(chop) == (True, None)
    assert run.offer(dict(renumber(run.refresh(LEFT, "CHOP"), 2), gesture="WASH")) == (False, "orphan_refresh")
    # Expired: past the lease on the server clock the state is gone.
    run = Run()
    run.sample(LEFT, 11, 0)
    assert run.offer(run.start(LEFT, "CHOP")) == (True, None)
    run.sample(LEFT, 11, 1, after=1.1)
    assert run.metrics["hand_states_expired"] == 1 and run.gate.active_state(LEFT) is None
    assert run.offer(run.refresh(LEFT, "CHOP")) == (False, "orphan_refresh")
    assert run.ended() == [(LEFT, "lease_expired")]


def test_a_lease_expires_on_the_server_clock_and_a_refresh_moves_it():
    run = Run()
    run.sample(LEFT, 11, 0)
    assert run.offer(run.start(LEFT, "CHOP")) == (True, None)
    started = run.now
    assert run.gate.active_state(LEFT)["deadline"] == pytest.approx(started + 1.0)
    run.sample(LEFT, 11, 1, after=0.6)
    assert run.offer(run.refresh(LEFT, "CHOP")) == (True, None)
    assert run.gate.active_state(LEFT)["deadline"] == pytest.approx(started + 1.6)
    run.sample(LEFT, 11, 2, after=0.98)  # 1.58 s: still running
    assert run.gate.active_state(LEFT) is not None and run.metrics["hand_states_expired"] == 0
    run.sample(LEFT, 11, 3, after=0.04)  # 1.62 s: expired, counted once
    run.sample(LEFT, 11, 4)
    assert run.gate.active_state(LEFT) is None and run.metrics["hand_states_expired"] == 1
    # The worker's own end of the state the gate expired still goes to the phone, once.
    end = run.end(LEFT, "CHOP")
    assert run.offer(end) == (True, None)
    assert run.offer(renumber(end, end["event_seq"] + 1)) == (False, "orphan_end")
    # Expiry is also checked when the event arrives, without a sample in between.
    run.sample(LEFT, 11, 5)
    assert run.offer(run.start(LEFT, "WASH")) == (True, None)
    run.sample(RIGHT, 22, 0, after=1.0)
    assert run.offer(run.pulse(RIGHT, "DROP")) == (True, None)
    assert run.metrics["hand_states_expired"] == 1  # nothing touched the left hand yet
    run.sample(LEFT, 11, 6, after=0.01)
    assert run.metrics["hand_states_expired"] == 2


def test_a_glove_reconnect_resets_only_that_hand():
    run = Run()
    run.sample(LEFT, 11, 0)
    run.sample(RIGHT, 22, 0)
    chop, wash = run.start(LEFT, "CHOP"), run.start(RIGHT, "WASH")
    for event in (chop, wash):
        assert run.offer(event) == (True, None)
    old = run.sources[LEFT]
    run.sample(LEFT, 12, 0)  # the left glove rebooted: a new boot_id
    assert run.metrics["hand_resets"] == 1 and run.ended() == [(LEFT, "source_reset")]
    assert run.gate.active_state(LEFT) is None
    assert run.gate.active_state(RIGHT)["state_id"] == wash["state_id"]
    # A refresh of the reset state is refused, even with a fresh sample of the new boot ...
    assert run.offer(run.refresh(LEFT, "CHOP")) == (False, "orphan_refresh")
    # ... but the worker's own end of it is accepted.
    assert run.offer(run.end(LEFT, "CHOP", "source_reset")) == (True, None)
    # A prediction from the retired boot is refused by Wen Ree's admission.
    assert run.offer(run.pulse(LEFT, "PICKUP", source=old)) == (False, "invalid")
    assert run.offer(run.pulse(LEFT, "PICKUP")) == (True, None)
    # A late sample of the old boot does not reset the hand again.
    run.sample(LEFT, 11, 9)
    assert run.metrics["hand_resets"] == 1
    # The right hand never noticed.
    run.sample(RIGHT, 22, 1)
    assert run.offer(run.refresh(RIGHT, "WASH")) == (True, None)
    assert run.gate.active_state(RIGHT)["state_id"] == wash["state_id"]


def test_a_stale_source_is_refused_but_an_end_may_name_an_older_sample():
    run = Run()
    run.sample(LEFT, 11, 0)
    assert run.offer(run.pulse(LEFT, "PICKUP"), after=0.51) == (False, "stale_source")
    run.sample(LEFT, 11, 1)
    assert run.offer(run.pulse(LEFT, "DROP"), after=0.45) == (True, None)
    run.sample(LEFT, 11, 2)
    assert run.offer(run.start(LEFT, "CHOP")) == (True, None)
    assert run.offer(run.end(LEFT, "CHOP"), after=0.7) == (True, None)  # a lifecycle end
    assert run.metrics["hand_events_refused"] == 1


def test_events_before_a_hello_or_of_a_closed_or_older_connection_are_refused():
    run = Run(hello=False)
    run.sample(LEFT, 11, 0)
    early = run.pulse(LEFT, "PICKUP")
    assert run.offer(early) == (False, "no_hello")
    assert run.connect(FIRST, 1) == (True, None)
    assert run.offer(run.pulse(LEFT, "DROP")) == (True, None)
    assert run.gate.closed(1, run.now) is True
    assert run.offer(run.pulse(LEFT, "THROW")) == (False, "no_hello")
    assert run.gate.closed(1, run.now) is False
    # The next runner connection says hello; the old connection's events are refused.
    run.sample(LEFT, 11, 1)
    old_epoch = run.pulse(LEFT, "PICKUP")
    assert run.connect(SECOND, 2) == (True, None)
    assert run.offer(old_epoch, connection=1) == (False, "not_current")
    assert run.offer(run.pulse(LEFT, "PICKUP"), connection=3) == (False, "not_current")
    # A hello on the older connection cannot take over again.
    assert run.gate.hello(1, make_hello(SESSION, "{:032x}".format(0xC3)), run.now) == (False, "old_connection")
    assert run.offer(run.pulse(LEFT, "DROP")) == (True, None)
    assert run.metrics["hand_events_refused"] == 4 and run.metrics["hand_hellos_refused"] == 1


def test_a_hello_after_samples_rebuilds_the_ledger_with_their_original_times():
    run = Run(hello=False)
    run.sample(LEFT, 11, 0)
    run.sample(RIGHT, 22, 0)
    assert run.connect(FIRST, 1) == (True, None)
    # These name samples accepted before the hello.
    assert run.offer(run.pulse(LEFT, "PICKUP")) == (True, None)
    assert run.offer(run.start(RIGHT, "POURING")) == (True, None)
    # The replayed samples keep their acceptance times: 0.6 s before the hello is stale.
    run = Run(hello=False)
    run.sample(LEFT, 11, 0)
    run.now += 0.6
    assert run.connect(FIRST, 1) == (True, None)
    assert run.offer(run.pulse(LEFT, "PICKUP")) == (False, "stale_source")
    # Only the last `replay` samples of a hand are kept.
    run = Run(hello=False, replay=4)
    first = run.sample(LEFT, 11, 0)
    for seq in range(1, 6):
        run.sample(LEFT, 11, seq, after=0.001)
    assert run.connect(FIRST, 1) == (True, None)
    assert run.offer(run.pulse(LEFT, "PICKUP", source=first)) == (False, "invalid")
    assert run.offer(run.pulse(LEFT, "DROP")) == (True, None)


def test_a_new_hello_starts_a_new_epoch_with_fresh_high_water_marks():
    run = Run()
    run.sample(LEFT, 11, 0)
    run.sample(RIGHT, 22, 0)
    for event in (run.pulse(LEFT, "PICKUP"), run.pulse(LEFT, "DROP"), run.start(RIGHT, "WASH")):
        assert run.offer(event) == (True, None)
    old = run.pulse(LEFT, "THROW")
    assert run.connect(SECOND, 2) == (True, None)
    assert run.ended() == [(RIGHT, "connection_reset")] and run.gate.active_state(RIGHT) is None
    fresh = run.pulse(LEFT, "PICKUP")
    assert fresh["event_seq"] == 1 and run.offer(fresh) == (True, None)
    assert run.offer(old) == (False, "invalid")  # the old epoch's event, even on the new connection
    # The same epoch again would repeat event_ids; other bad hellos are refused too.
    assert run.gate.hello(3, make_hello(SESSION, SECOND), run.now) == (False, "reused_epoch")
    assert run.gate.hello(3, make_hello("elsewhere", "{:032x}".format(0xD4)), run.now) == (False, "wrong_session")
    assert run.gate.hello(3, dict(type="hello", format=1), run.now) == (False, "invalid")
    assert run.gate.hello(3, None, run.now) == (False, "invalid")
    assert run.metrics["hand_hellos"] == 2 and run.metrics["hand_hellos_refused"] == 4
    assert run.offer(run.pulse(LEFT, "DROP")) == (True, None)  # connection 2 is still current


def test_the_runner_connection_closing_ends_both_hands_states():
    run = Run()
    run.sample(LEFT, 11, 0)
    run.sample(RIGHT, 22, 0)
    for event in (run.start(LEFT, "CHOP"), run.start(RIGHT, "POURING")):
        assert run.offer(event) == (True, None)
    assert run.gate.closed(2, run.now) is False  # not the current connection
    assert run.gate.closed(1, run.now) is True
    assert run.ended() == [(LEFT, "worker_disconnect"), (RIGHT, "worker_disconnect")]
    assert run.gate.active_state(LEFT) is None and run.gate.active_state(RIGHT) is None


def test_pulse_idle_and_reset_end_the_active_state_and_bad_events_are_invalid():
    run = Run()
    run.sample(LEFT, 11, 0)
    for event in (run.start(LEFT, "CHOP"), run.encode("idle", LEFT, "IDLE")):
        assert run.offer(event) == (True, None)
    assert run.gate.active_state(LEFT) is None
    assert run.offer(run.start(LEFT, "WASH")) == (True, None)
    # A reset names the hand's last sample, if it has one; a second one names none.
    reset = run.encoder.reset("manual_reset", device=LEFT)[0]
    assert reset["source"] is not None and run.offer(reset) == (True, None)
    assert run.gate.active_state(LEFT) is None
    again = run.encoder.reset("manual_reset", device=LEFT)[0]
    assert again["source"] is None and run.offer(again) == (True, None)
    assert run.offer(run.start(LEFT, "CHOP")) == (True, None)
    assert run.offer(run.pulse(LEFT, "THROW")) == (True, None)
    assert run.gate.active_state(LEFT) is None
    pulse = run.pulse(LEFT, "PICKUP")
    for bad in (dict(pulse, extra=1), dict(pulse, confidence=0.5), dict(pulse, session_id="elsewhere"),
                dict(pulse, gesture="CHOP"), dict(pulse, source=dict(pulse["source"], uptime_ms=1)),
                [pulse], None):
        assert run.offer(bad) == (False, "invalid")
    assert run.offer(pulse) == (True, None)
