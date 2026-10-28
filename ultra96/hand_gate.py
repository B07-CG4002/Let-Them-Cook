"""The service's gate for structured hand events (sensor protocol version 4 only).

The model runner reports each hand's events in Wen Ree's HAND_EVENT envelope
(common.hand_contract) through ultra96.model's emit_hand. Before one is published
to a phone that asked for hand events, this gate checks it on the event-loop thread:

- Admission, by Wen Ree's ultra96.hand_admission.HandAdmission (used unchanged):
  the event matches the worker's hello, names an exact sample this service
  accepted, and a prediction names the hand's current boot and a sample accepted
  at most 0.5 s earlier on the server's clock.
- Order: within the worker's epoch each hand's event_seq must rise. The same number
  again is a duplicate (the event_id is the same); a lower one is out of order.
- State: each hand has at most one ongoing state (CHOP, WASH or POURING) with a
  lease on the server's clock. A refresh must name the active state and gesture.
  An end must name the active state, or the state this gate itself last ended for
  that hand (lease expiry or a glove reconnect), so the worker's own end of it
  still reaches the phone. A state_start, pulse, idle or reset ends any active state.

Every newly accepted stream sample is fed in with its acceptance time. The last
REPLAY_SAMPLES samples of each hand are kept with those times, so a hello that
arrives after samples rebuilds the admission ledger as if it had come first.
A new boot_id on one hand (the glove reconnected) ends that hand's state only.

Each hello starts a new epoch on its runner connection: both hands' states and
event_seq high-water marks start afresh. The gate never mints or changes an event;
an accepted event is published exactly as the runner sent it.
"""
from collections import deque

from common.hand_contract import ProtocolError, validate_hello
from ultra96.hand_admission import HandAdmission

REPLAY_SAMPLES = 512  # per hand; the size of HandAdmission's own ledger
MAX_PREDICTION_AGE = 0.5  # seconds from a source sample's acceptance to its prediction
COUNTERS = ("hand_hellos", "hand_hellos_refused", "hand_events_accepted", "hand_events_refused",
            "hand_events_duplicate", "hand_states_expired", "hand_resets")
# HandAdmission.event's message for a prediction whose source was accepted too long ago.
_STALE_SOURCE = "Prediction source is stale at server"


class HandGate:
    """Loop thread only. Its counters live in the service's metrics dict.

    observe(event, **fields), when given, is told when the gate itself ends a
    state ("hand_state_ended" with device_id and reason).
    """

    def __init__(self, session_id, metrics, *, observe=None, replay=REPLAY_SAMPLES,
                 max_namespaces=128, max_prediction_age=MAX_PREDICTION_AGE):
        if type(replay) is not int or replay < 1:
            raise ValueError("replay must be a positive integer")
        self.session_id = session_id
        self.metrics = metrics
        for key in COUNTERS:
            metrics.setdefault(key, 0)
        self._observe = observe
        self._replay_size = replay
        # HandAdmission needs at least two (one per hand); the service has its own limit.
        self._max_namespaces = max(2, max_namespaces)
        self._max_prediction_age = max_prediction_age
        self._replay = {1: deque(maxlen=replay), 2: deque(maxlen=replay)}
        self._boots = {1: None, 2: None}  # each hand's current boot_id
        self._retired = {1: set(), 2: set()}  # boots a hand has left (bounded by the service's limit)
        self._admission = None  # HandAdmission of the current connection's hello
        self._connection = None  # the newest runner connection that sent a hello
        self._epoch = None  # worker_epoch of the last accepted hello
        self._high_water = {1: 0, 2: 0}
        self._active = {1: None, 2: None}  # dict(state_id, gesture, deadline) or None
        self._ended = {1: None, 2: None}  # state_id the gate itself last ended

    def active_state(self, device_id):
        """A copy of the hand's active state (state_id, gesture, deadline), or None."""
        active = self._active[device_id]
        return None if active is None else dict(active)

    def sample(self, message, now):
        """A newly accepted v4 stream sample (not a command, not a duplicate) at server time now."""
        device, boot = message["device_id"], message["boot_id"]
        current = self._boots[device]
        if boot != current:
            if boot in self._retired[device]:
                return  # a late sample of a boot this hand has left: HandAdmission refuses it too
            if current is not None:
                # The glove reconnected: only this hand starts again.
                self._retired[device].add(current)
                self.metrics["hand_resets"] += 1
                self._end(device, "source_reset", remember=True)
            self._boots[device] = boot
        self._expire(device, now)
        self._replay[device].append((message, now))
        if self._admission is not None:
            self._admission.accepted_sample(message, now)

    def hello(self, connection, hello, now):
        """The worker's hello on runner connection number connection: (True, None) or (False, reason)."""
        if self._connection is not None and connection < self._connection:
            return self._refuse_hello("old_connection")
        try:
            validate_hello(hello)
        except ProtocolError:
            return self._refuse_hello("invalid")
        if hello["session_id"] != self.session_id:
            return self._refuse_hello("wrong_session")
        if hello["worker_epoch"] == self._epoch:
            return self._refuse_hello("reused_epoch")  # its event_ids would not be new
        try:
            admission = HandAdmission(hello, capacity=self._replay_size,
                                      max_namespaces=self._max_namespaces,
                                      max_prediction_age=self._max_prediction_age)
            for device in (1, 2):
                for message, accepted_at in self._replay[device]:
                    admission.accepted_sample(message, accepted_at)
        except ValueError:  # ProtocolError is a ValueError
            return self._refuse_hello("replay_failed")
        for device in (1, 2):
            self._end(device, "connection_reset")
        self._admission = admission
        self._connection = connection
        self._epoch = hello["worker_epoch"]
        self._high_water = {1: 0, 2: 0}
        self._ended = {1: None, 2: None}
        self.metrics["hand_hellos"] += 1
        return True, None

    def closed(self, connection, now):
        """Runner connection number connection ended. True if it was the current one."""
        if connection != self._connection or self._admission is None:
            return False
        for device in (1, 2):
            self._end(device, "worker_disconnect")
        self._admission = None  # no events until the next hello
        return True

    def event(self, connection, event, now):
        """One HAND_EVENT from runner connection connection: (True, None) to publish, or (False, reason)."""
        if self._admission is None:
            return self._refuse("no_hello")
        if connection != self._connection:
            return self._refuse("not_current")
        try:
            self._admission.event(event, now)
        except ValueError as error:  # ProtocolError is a ValueError
            return self._refuse("stale_source" if str(error) == _STALE_SOURCE else "invalid")
        device, sequence = event["device_id"], event["event_seq"]
        if sequence == self._high_water[device]:
            self.metrics["hand_events_duplicate"] += 1
            return False, "duplicate"
        if sequence < self._high_water[device]:
            return self._refuse("out_of_order")
        self._expire(device, now)
        kind, active = event["kind"], self._active[device]
        if kind == "state_refresh":
            if (active is None or active["state_id"] != event["state_id"]
                    or active["gesture"] != event["gesture"]):
                return self._refuse("orphan_refresh")
            active["deadline"] = now + event["lease_ms"] / 1000.0
        elif kind == "state_end":
            if active is not None and active["state_id"] == event["state_id"]:
                self._active[device] = None
            elif self._ended[device] == event["state_id"]:
                self._ended[device] = None  # the worker's end of a state the gate ended first
            else:
                return self._refuse("orphan_end")
        elif kind == "state_start":
            self._active[device] = dict(state_id=event["state_id"], gesture=event["gesture"],
                                        deadline=now + event["lease_ms"] / 1000.0)
        else:  # pulse, idle and reset end any active state
            self._active[device] = None
        self._high_water[device] = sequence
        self.metrics["hand_events_accepted"] += 1
        return True, None

    def _expire(self, device, now):
        active = self._active[device]
        if active is not None and now >= active["deadline"]:
            self.metrics["hand_states_expired"] += 1
            self._end(device, "lease_expired", remember=True)

    def _end(self, device, reason, remember=False):
        """End the hand's active state; remember it when the gate, not the worker, ended it."""
        active = self._active[device]
        if active is None:
            return
        self._active[device] = None
        if remember:
            self._ended[device] = active["state_id"]
        if self._observe is not None:
            self._observe("hand_state_ended", session_id=self.session_id, device_id=device,
                          reason=reason)

    def _refuse(self, reason):
        self.metrics["hand_events_refused"] += 1
        return False, reason

    def _refuse_hello(self, reason):
        self.metrics["hand_hellos_refused"] += 1
        return False, reason
