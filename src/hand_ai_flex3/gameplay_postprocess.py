"""Gameplay-side temporal and object-capability filtering for hand events.

The CNN remains unchanged.  Callers should display/log every raw prediction,
then dispatch only decisions whose ``accepted`` field is true.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from typing import Deque, Dict, Optional


LABELS = {"IDLE", "PICKUP", "DROP", "CHOP", "WASH", "THROW", "POURING"}
PULSES = {"PICKUP", "DROP", "THROW"}
ONGOING = {"CHOP", "WASH", "POURING"}


OBJECT_RULES = {
    "POT": frozenset({"PICKUP", "DROP", "POURING"}),
    "INGREDIENT_WHOLE": frozenset({"PICKUP", "DROP", "THROW"}),
    "INGREDIENT_CHOPPED": frozenset({"PICKUP", "DROP", "THROW"}),
    "PLATE_CLEAN": frozenset({"PICKUP", "DROP", "POURING"}),
    "PLATE_WITH_FOOD": frozenset({"PICKUP", "DROP", "POURING"}),
    "DIRTY_PLATE_STACK": frozenset({"PICKUP", "DROP"}),
    "KNIFE": frozenset({"PICKUP", "DROP"}),
}


def _object_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip().upper().replace(" ", "_")
    aliases = {
        "WHOLE_INGREDIENT": "INGREDIENT_WHOLE",
        "CHOPPED_INGREDIENT": "INGREDIENT_CHOPPED",
        "CLEAN_PLATE": "PLATE_CLEAN",
        "PLATE_WITH_FOOD": "PLATE_WITH_FOOD",
        "DIRTY_PLATE": "DIRTY_PLATE_STACK",
    }
    return aliases.get(value, value)


@dataclass(frozen=True)
class GateDecision:
    accepted: bool
    device_id: int
    gesture: str
    reason: str
    object_name: Optional[str] = None
    confidence: Optional[float] = None


class TemporalPostProcessor:
    """Convert raw predictions into stable action/state events.

    Pulse gestures use a 2-of-3 vote and a cooldown.  A new PICKUP, DROP or
    THROW is not accepted until the hand has first produced two stable IDLE
    windows.  Ongoing gestures require two matching windows to start and end
    after two stable IDLE windows.  This is deliberately separate from object
    capability checks so raw predictions remain observable.
    """

    def __init__(self, threshold=.70, vote_window=3, pulse_votes=2,
                 ongoing_votes=2, idle_votes=2, pulse_cooldown=.50):
        if not 0 <= threshold <= 1:
            raise ValueError("threshold must be in [0, 1]")
        if vote_window < 1 or pulse_votes < 1 or ongoing_votes < 1 or idle_votes < 1:
            raise ValueError("vote counts must be positive")
        if pulse_votes > vote_window or ongoing_votes > vote_window:
            raise ValueError("vote count cannot exceed vote window")
        if pulse_cooldown < 0:
            raise ValueError("pulse_cooldown must be non-negative")
        self.threshold = float(threshold)
        self.vote_window = int(vote_window)
        self.pulse_votes = int(pulse_votes)
        self.ongoing_votes = int(ongoing_votes)
        self.idle_votes = int(idle_votes)
        self.pulse_cooldown = float(pulse_cooldown)
        self._history: Dict[int, Deque[tuple[str, float, float]]] = {
            1: deque(maxlen=self.vote_window), 2: deque(maxlen=self.vote_window)
        }
        self._active = {1: None, 2: None}
        self._last_pulse = {1: -math.inf, 2: -math.inf}
        self._idle_streak = {1: 0, 2: 0}

    def reset(self, device_id: Optional[int] = None):
        devices = (device_id,) if device_id is not None else (1, 2)
        for device in devices:
            if device not in (1, 2):
                raise ValueError("device_id must be 1 or 2")
            self._history[device].clear()
            self._active[device] = None
            self._last_pulse[device] = -math.inf
            self._idle_streak[device] = 0

    def update(self, device_id: int, gesture: str, confidence: float, now: float):
        if device_id not in (1, 2):
            raise ValueError("device_id must be 1 or 2")
        gesture = str(gesture).upper()
        if gesture not in LABELS or not math.isfinite(float(confidence)):
            raise ValueError("invalid prediction")
        confidence = float(confidence)
        if not math.isfinite(now) or now < 0:
            raise ValueError("invalid timestamp")
        if confidence <= self.threshold:
            return []
        history = self._history[device_id]
        history.append((gesture, confidence, float(now)))
        if gesture == "IDLE":
            self._idle_streak[device_id] += 1
        else:
            self._idle_streak[device_id] = 0

        count = sum(1 for g, _, _ in history if g == gesture)
        needed = self.pulse_votes if gesture in PULSES else self.ongoing_votes
        average = sum(c for g, c, _ in history if g == gesture) / max(count, 1)
        events = []

        if gesture == "IDLE":
            if self._idle_streak[device_id] >= self.idle_votes:
                self._active[device_id] = None
                events.append(dict(type="idle", device_id=device_id,
                                   gesture="IDLE", confidence=average,
                                   reason="stable_idle"))
            return events

        if count < needed:
            return events

        if gesture in PULSES:
            if self._idle_streak[device_id] < self.idle_votes and self._last_pulse[device_id] > -math.inf:
                return events
            if now - self._last_pulse[device_id] < self.pulse_cooldown:
                return events
            self._last_pulse[device_id] = float(now)
            self._active[device_id] = None
            events.append(dict(type="action", device_id=device_id,
                               gesture=gesture, confidence=average,
                               reason="stable_pulse"))
            return events

        if gesture in ONGOING and self._active[device_id] != gesture:
            previous = self._active[device_id]
            self._active[device_id] = gesture
            events.append(dict(type="state_start", device_id=device_id,
                               gesture=gesture, confidence=average,
                               previous=previous, reason="stable_ongoing"))
        return events


class ObjectActionGate:
    """Validate confirmed gestures against AR/game object state.

    ``set_target`` should be called by AR before a pickup.  ``set_held`` is
    called by the game after a pickup or when restoring an existing inventory.
    The gate never hides raw predictions; it only decides whether a confirmed
    event may be dispatched to gameplay.
    """

    def __init__(self, two_hand_window=.50):
        if two_hand_window <= 0:
            raise ValueError("two_hand_window must be positive")
        self.two_hand_window = float(two_hand_window)
        self.held = {1: None, 2: None}
        self.targets = {1: None, 2: None}
        self._wash_seen = {1: -math.inf, 2: -math.inf}

    def set_target(self, device_id: int, object_name: Optional[str]):
        self._check_device(device_id)
        value = _object_name(object_name)
        if value is not None and value not in OBJECT_RULES:
            raise ValueError(f"unknown object: {object_name}")
        self.targets[device_id] = value

    def set_held(self, device_id: int, object_name: Optional[str]):
        self._check_device(device_id)
        value = _object_name(object_name)
        if value is not None and value not in OBJECT_RULES:
            raise ValueError(f"unknown object: {object_name}")
        self.held[device_id] = value

    def clear_held(self, device_id: int):
        self._check_device(device_id)
        self.held[device_id] = None

    def accept(self, device_id: int, gesture: str, now: float,
               confidence: Optional[float] = None,
               object_name: Optional[str] = None) -> GateDecision:
        self._check_device(device_id)
        gesture = str(gesture).upper()
        if gesture not in LABELS:
            raise ValueError("invalid gesture")
        if not math.isfinite(now) or now < 0:
            raise ValueError("invalid timestamp")
        if confidence is not None and not math.isfinite(float(confidence)):
            raise ValueError("invalid confidence")
        supplied = _object_name(object_name)

        if gesture == "IDLE":
            return self._ok(device_id, gesture, confidence, "context_free")

        if gesture == "CHOP":
            # Chopping is only valid while the hand is holding the knife.
            # The game/AR layer must additionally verify that the player is at
            # the chopping-board station before dispatching the event.
            obj = self.held[device_id]
            if obj != "KNIFE":
                return self._reject(device_id, gesture, confidence,
                                    "chop_requires_knife", obj)
            return self._ok(device_id, gesture, confidence,
                            "knife_chop_allowed", obj)

        if gesture == "PICKUP":
            if self.held[device_id] is not None:
                return self._reject(device_id, gesture, confidence, "hand_already_holding")
            obj = supplied or self.targets[device_id]
            if obj is None:
                return self._reject(device_id, gesture, confidence, "no_target_object")
            self.held[device_id] = obj
            self.targets[device_id] = None
            return self._ok(device_id, gesture, confidence, "pickup_allowed", obj)

        obj = self.held[device_id]
        if obj is None:
            return self._reject(device_id, gesture, confidence, "hand_empty")
        allowed = OBJECT_RULES[obj]
        if gesture == "WASH":
            if obj != "DIRTY_PLATE_STACK":
                return self._reject(device_id, gesture, confidence, "object_not_washable", obj)
            self._wash_seen[device_id] = float(now)
            other = 3 - device_id
            if now - self._wash_seen[other] > self.two_hand_window:
                return self._reject(device_id, gesture, confidence, "waiting_for_second_hand", obj)
            return self._ok(device_id, gesture, confidence, "two_hand_wash_allowed", obj)

        if gesture not in allowed:
            return self._reject(device_id, gesture, confidence,
                               f"{gesture.lower()}_not_allowed_for_{obj.lower()}", obj)
        if gesture in ("DROP", "THROW"):
            self.held[device_id] = None
        return self._ok(device_id, gesture, confidence, "object_action_allowed", obj)

    @staticmethod
    def _check_device(device_id):
        if device_id not in (1, 2):
            raise ValueError("device_id must be 1 or 2")

    @staticmethod
    def _ok(device_id, gesture, confidence, reason, obj=None):
        return GateDecision(True, device_id, gesture, reason, obj, confidence)

    @staticmethod
    def _reject(device_id, gesture, confidence, reason, obj=None):
        return GateDecision(False, device_id, gesture, reason, obj, confidence)
