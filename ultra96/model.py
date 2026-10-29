"""The model interface of the board service: start(emit), on_sample(...) and a thread-safe emit.

Communications calls the model; what happens inside it (pairing gloves, windows,
inference, when to produce a result) belongs to the model owner.

A model is any object or module with two callables:
    start(emit)                                   called once, before the first sample
    on_sample(device_id, boot_id, seq, uptime_ms, values)
                                                  called on the service's event-loop thread
                                                  for each newly accepted stream sample;
                                                  must return immediately
and it reports results by calling
    emit(gesture, confidence, device_id, boot_id, seq) -> bool
from any thread. values is a tuple of 11 floats in the agreed physical units
(12 under sensor protocol version 4 or 5; version 5 differs from 4 only in how the
gyro travels, so the model gets the same deg/s either way).

In sensor-version-4 or -5 mode a model may also have
    start_hand_events(emit_hand)                  called once, right after start(emit)
and then report structured hand events by calling
    emit_hand(kind, payload, connection) -> bool
from any thread: kind "hello" (payload: the worker's hello dict), "event" (payload: one
complete HAND_EVENT dict) or "closed" (payload None, the runner connection ended);
connection is the model's own number for the runner connection (an int, new for each
connection). The service checks them (ultra96.hand_gate); the model forwards them unchanged.
"""
import copy
import importlib
import inspect
import logging
import numbers
import random
import sys
import threading
import time
import types

from common.sensor import physical_values
from ultra96.protocol import DUMMY_GESTURES, valid_confidence, valid_gesture

LOG = logging.getLogger(__name__)
SLOW_CALL_SECONDS = 0.005
_WARNING_INTERVAL = 1.0


class ModelStartError(RuntimeError):
    """The model's start(emit) raised; the service does not open its ports."""


class DummyModel:
    """Built-in stand-in: one result per accepted sample, a random dummy label, confidence 1.0."""
    name = "dummy"

    def __init__(self, rng=None):
        self._rng = random.SystemRandom() if rng is None else rng
        self._emit = None

    def start(self, emit):
        self._emit = emit

    def on_sample(self, device_id, boot_id, seq, uptime_ms, values):
        self._emit(self._rng.choice(DUMMY_GESTURES), 1.0, device_id, boot_id, seq)


def load_model(spec=None, path=None, rng=None):
    """Return the built-in dummy model, or import MODULE (optionally from DIR first)."""
    if spec is None:
        return DummyModel(rng)
    if not isinstance(spec, str) or not spec:
        raise ValueError("model must be an importable module name")
    if path is not None:
        sys.path.insert(0, str(path))
    module = importlib.import_module(spec)
    for name in ("start", "on_sample"):
        function = getattr(module, name, None)
        if not callable(function):
            raise TypeError("model module {} has no callable {}()".format(spec, name))
        # The service calls both directly on its event loop; a coroutine would never run.
        if inspect.iscoroutinefunction(function):
            raise TypeError("model {}.{}() must be a plain function, not async def".format(spec, name))
    hand = getattr(module, "start_hand_events", None)
    if hand is not None and (not callable(hand) or inspect.iscoroutinefunction(hand)):
        raise TypeError("model {}.start_hand_events() must be a plain function".format(spec))
    return module


def model_name(model):
    # A module's own globals (for example a stray `name`) must not rename it.
    if isinstance(model, types.ModuleType):
        return model.__name__
    return str(getattr(model, "name", None) or type(model).__name__)


def _uint32(value):
    return (isinstance(value, numbers.Integral) and not isinstance(value, bool)
            and 0 <= int(value) <= 0xFFFFFFFF)


def normalize_result(gesture, confidence, device_id, boot_id, seq):
    """Convert numpy-style scalars to plain Python values; None when any argument is invalid."""
    if not isinstance(gesture, str) or not _uint32(boot_id) or not _uint32(seq):
        return None
    if not isinstance(device_id, numbers.Integral) or isinstance(device_id, bool) \
            or int(device_id) not in (1, 2):
        return None
    if not isinstance(confidence, numbers.Real) or isinstance(confidence, bool):
        return None
    try:
        confidence = float(confidence)
    except (OverflowError, ValueError):
        return None
    gesture = str(gesture)
    if not valid_gesture(gesture) or not valid_confidence(confidence):
        return None
    return gesture, confidence, int(device_id), int(boot_id), int(seq)


HAND_KINDS = ("hello", "event", "closed")


def normalize_hand(kind, payload, connection):
    """A private copy of one emit_hand call; None when its arguments are malformed.

    Only the shape is checked here. Whether a hello or an event is valid, and
    whether it may be published, is decided on the event loop (ultra96.hand_gate).
    """
    if kind not in HAND_KINDS or type(connection) is not int or connection < 0:
        return None
    if kind == "closed":
        if payload is not None:
            return None
    elif not isinstance(payload, dict):
        return None
    try:
        return kind, copy.deepcopy(payload), connection
    except Exception:  # an object deepcopy cannot copy is no JSON event
        return None


class ModelPort:
    """Owns the calls into the model and moves every emitted result onto the event loop.

    deliver(gesture, confidence, device_id, boot_id, seq) runs on the loop thread
    and is supplied by the server. All metric updates happen on the loop thread,
    except model_results_late, which is counted under a lock.

    With deliver_hand(kind, payload, connection) (sensor version 4 or 5) a model that has
    start_hand_events(emit_hand) is given emit_hand as well. Its calls reach
    deliver_hand on the loop thread in the order they were made, interleaved with
    the results in that order too (both go through call_soon_threadsafe).
    """

    def __init__(self, model, deliver, metrics, *, physical_converter=None, deliver_hand=None):
        self.model = model
        self._physical_converter = physical_converter
        self.name = model_name(model)
        self._deliver = deliver
        self._deliver_hand = deliver_hand
        self.metrics = metrics
        for key in ("model_samples", "model_errors", "model_slow_calls", "model_results",
                    "model_results_rejected", "model_results_duplicate", "model_results_late"):
            metrics.setdefault(key, 0)
        if deliver_hand is not None:
            for key in ("model_hand_rejected", "model_hand_late"):
                metrics.setdefault(key, 0)
        metrics.setdefault("model_max_call_ms", 0.0)
        self._loop = None
        self._thread = None
        self._closed = False
        self._late_lock = threading.Lock()
        self._warned = {}

    def start(self, loop):
        """Call the model's start(emit), then its start_hand_events(emit_hand) if both sides have it.

        Returns the duration of both calls in seconds.
        """
        if self._loop is not None:
            raise RuntimeError("model already started")
        self._loop = loop
        self._thread = threading.get_ident()
        started = time.perf_counter()
        calls = [("start(emit)", self.model.start, self.emit)]
        start_hand_events = getattr(self.model, "start_hand_events", None)
        if self._deliver_hand is not None and callable(start_hand_events):
            calls.append(("start_hand_events(emit_hand)", start_hand_events, self.emit_hand))
        for name, function, argument in calls:
            try:
                function(argument)
            except (Exception, SystemExit) as exc:  # a model's sys.exit() must not end the service
                self._closed = True
                raise ModelStartError("model {} raised {}".format(name, type(exc).__name__)) from exc
        return time.perf_counter() - started

    def close(self):
        self._closed = True

    def warn(self, kind, message, *args):
        """At most one warning per kind per second; loop thread only."""
        now = time.monotonic()
        if now - self._warned.get(kind, -_WARNING_INTERVAL) >= _WARNING_INTERVAL:
            self._warned[kind] = now
            LOG.warning(message, *args)

    def on_sample(self, device_id, boot_id, seq, uptime_ms, values):
        """Loop thread. Exceptions and slow calls are counted; they never reach the connection."""
        self.metrics["model_samples"] += 1
        started = time.perf_counter()
        try:
            self.model.on_sample(device_id, boot_id, seq, uptime_ms, (self._physical_converter or physical_values)(values))
        except (Exception, SystemExit) as exc:  # a model's sys.exit() must not end the service
            self.metrics["model_errors"] += 1
            self.warn("error", "model on_sample raised %s", type(exc).__name__)
        finally:
            elapsed = time.perf_counter() - started
            if elapsed > SLOW_CALL_SECONDS:
                self.metrics["model_slow_calls"] += 1
                self.warn("slow", "model on_sample took %.1f ms; it must return immediately",
                          elapsed * 1000)
            self.metrics["model_max_call_ms"] = max(self.metrics["model_max_call_ms"],
                                                    round(elapsed * 1000, 3))

    def emit(self, gesture, confidence, device_id, boot_id, seq):
        """Any thread. True: valid and handed to the service. False: refused or service stopped.

        The identity check (an accepted sample of this service) and the duplicate
        check run on the event loop afterwards and are counted there.
        """
        result = normalize_result(gesture, confidence, device_id, boot_id, seq)
        loop = self._loop
        if loop is None or self._closed:
            self._count_late()
            return False
        if threading.get_ident() == self._thread:
            self._receive(result)
            return result is not None
        try:
            loop.call_soon_threadsafe(self._receive, result)
        except RuntimeError:  # the event loop is closed
            self._count_late()
            return False
        return result is not None

    def emit_hand(self, kind, payload, connection):
        """Any thread. True: handed to the service. False: malformed call or service stopped.

        Like emit, a call on another thread moves to the event loop with
        call_soon_threadsafe, so the loop sees results and hand messages in the
        order the model made the calls. Validation and admission run there.
        """
        message = normalize_hand(kind, payload, connection)
        loop = self._loop
        if loop is None or self._closed or self._deliver_hand is None:
            self._count_late("model_hand_late")
            return False
        if threading.get_ident() == self._thread:
            self._receive_hand(message)
            return message is not None
        try:
            loop.call_soon_threadsafe(self._receive_hand, message)
        except RuntimeError:  # the event loop is closed
            self._count_late("model_hand_late")
            return False
        return message is not None

    def _count_late(self, key="model_results_late"):
        with self._late_lock:
            self.metrics[key] = self.metrics.get(key, 0) + 1

    def _receive(self, result):
        if self._closed:
            self._count_late()
            return
        if result is None:
            self.metrics["model_results_rejected"] += 1
            self.warn("invalid", "model emitted an invalid result; it was not sent")
            return
        self._deliver(*result)

    def _receive_hand(self, message):
        if self._closed:
            self._count_late("model_hand_late")
            return
        if message is None:
            self.metrics["model_hand_rejected"] += 1
            self.warn("hand_invalid", "model emitted a malformed hand message; it was not sent")
            return
        self._deliver_hand(*message)
