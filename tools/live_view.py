"""State, live view and run-log lines for python demo.py start.

RunState gathers what the capture child, the board monitor and the SSH tunnel report.
apply() turns each event into run-log lines; render() draws the live view from the same
state. Both are pure: tools/live_session.py owns the threads, the console and the file.

Board counters are cumulative since the Ultra96 service started; the run's numbers are
their increase since the run's first board update. A service restart (uptime or a
counter going down) carries what was counted so far, and every gap in the board view is
remembered, so the summary never claims an exact phone count it cannot back.
"""
from __future__ import annotations

from collections import Counter, deque
from datetime import datetime
import json
import math
import threading

from common.sensor import CHANNELS, physical_values

GLOVES = {1: "LEFT", 2: "RIGHT"}
WHO_WIDTH = 6
BOARD_COUNTERS = ("accepted", "duplicates", "results", "commands", "refused", "sent", "unsent")
SERVICE_COUNTERS = ("rejected", "client_limit", "idle_evicted", "subscribers", "replaced",
                    "model_samples", "model_errors", "model_slow_calls", "model_results_late")
# A rate whose last sample is older than this is shown as "-" (its source stopped).
RATE_STALE_SECONDS = 1.5
_UNITS = (("ax", "ay", "az", "g"), ("gx", "gy", "gz", "deg/s"),
          ("yaw", "pitch", "roll", "deg"), ("flex1", "flex2", None, "0..1"))
_DIGITS = {"ax": 4, "ay": 4, "az": 4, "flex1": 4, "flex2": 4}
_QUIET_RECORDS = frozenset({"sensor", "sensor_ack", "status", "finished", "log"})
_LAPTOP_DROPS = ("queue_dropped", "stale_dropped", "callback_generation_dropped",
                 "generation_dropped", "ambiguous_dropped")


def clock_text(epoch, *, millis=True):
    moment = datetime.fromtimestamp(epoch)
    text = moment.strftime("%H:%M:%S")
    return text + ".{:03d}".format(int(moment.microsecond / 1000)) if millis else text


def record_epoch(record, fallback):
    """The laptop wall time a capture record was made, from its timestamp_utc."""
    try:
        return datetime.fromisoformat(record["timestamp_utc"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return fallback


def duration_text(seconds):
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "-"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return "{:d}:{:02d}:{:02d}".format(hours, minutes, seconds)


def uptime_text(seconds):
    if seconds is None:
        return "-"
    if seconds < 120:
        return "{:.0f} s".format(seconds)
    if seconds < 7200:
        return "{:.0f} min".format(seconds / 60)
    return "{:.1f} h".format(seconds / 3600)


def values_text(values):
    """The 11 values in physical units, grouped by sensor, on one line."""
    return "  ".join(_value_groups(values))


def values_lines(values):
    """The same groups on two lines (acceleration and rate; angles and flex), for the view."""
    groups = _value_groups(values)
    if len(groups) != 4:
        return groups
    return ["  ".join(groups[:2]), "  ".join(groups[2:])]


def _value_groups(values):
    try:
        physical = dict(zip(CHANNELS, physical_values(values)))
    except (TypeError, ValueError):
        return ["values " + json.dumps(values)]
    groups = []
    for *names, unit in _UNITS:
        parts = []
        for name in names:
            if name is None:
                continue
            digits = _DIGITS.get(name, 2)
            form = "{} {:.%df}" % digits if name.startswith("flex") else "{} {:+.%df}" % digits
            parts.append(form.format(name, physical[name]))
        groups.append(" ".join(parts) + " " + unit)
    return groups


class Rate:
    """Events per second over the last few seconds, from (time, total) samples."""

    def __init__(self, window=3.0):
        self.window = window
        self._samples = deque()

    def add(self, now, total):
        if total is None:
            return
        self._samples.append((now, total))
        while len(self._samples) > 2 and now - self._samples[0][0] > self.window:
            self._samples.popleft()

    def per_second(self, now=None):
        """None until there is a window to measure, and once its source has gone quiet."""
        if len(self._samples) < 2:
            return None
        (start, first), (end, last) = self._samples[0], self._samples[-1]
        if end - start < 0.5:
            return None
        if now is not None and now - end > RATE_STALE_SECONDS:
            return None
        return max(0.0, (last - first) / (end - start))


class Glove:
    def __init__(self, device_id, address):
        self.device_id = device_id
        self.name = GLOVES[device_id]
        self.address = address
        self.status = None              # the capture's latest DSTAT for this glove
        self.packet = None              # (epoch, record) of the latest sensor record
        self.packets = 0                # sensor records seen in the stream
        self.boots = {}                 # boot_id -> highest seq among its sensor records
        self.results = deque(maxlen=6)  # (epoch, kind, seq, gesture, confidence)
        self.labels = Counter()
        self.board = None               # latest DEV counters from the board
        self.board_base = None          # DEV counters at the run's first board update
        self.board_carry = dict.fromkeys(BOARD_COUNTERS, 0)  # counted before a restart
        self.connected_generation = None
        self.failed_attempts = 0        # BLE attempts that ended without a connection
        self.rates ={step: Rate() for step in ("sent", "received", "acked", "results", "phone")}
        self.last_counts_log = 0.0

    def board_delta(self, key):
        if self.board is None or self.board_base is None:
            return None
        return self.board_carry[key] + max(0, self.board[key] - self.board_base[key])

    def counts(self):
        """The count at each step for this run, or None where it is not known.

        Sent and received come from the same capture snapshot (a packet record can be
        ahead of it). After a glove reboot its new boot starts again at seq 0, so each
        later boot adds its highest seq + 1.
        """
        status = self.status or {}
        start_boot, first, last = status.get("boot_id"), status.get("first_seq"), status.get("last_seq")
        sent = last - first + 1 if first is not None and last is not None and last >= first else None
        later = sum(seq + 1 for boot, seq in self.boots.items() if boot != start_boot)
        if later:
            sent = (sent or 0) + later
        received = status.get("received")
        if sent is not None and received is not None:
            sent = max(sent, received)  # the newest seq can trail the callback count briefly
        return {
            "sent": sent,
            "received": received,
            "acked": status.get("acked"),
            "results": self.board_delta("results"),
            "commands": self.board_delta("commands"),
            "phone": self.board_delta("sent"),
            "unsent": self.board_delta("unsent"),
            "refused": self.board_delta("refused"),
        }


class RunState:
    """Everything the live view and the run log know; guarded by self.lock."""

    def __init__(self, *, folder, addresses, rate_hz, session_id, duration=None,
                 keyboard=False, tunnel=None):
        self.lock = threading.Lock()
        self.folder = folder
        self.rate_hz = rate_hz
        self.session_id = session_id
        self.duration = duration
        self.keyboard = keyboard
        self.tunnel_text = tunnel
        self.gloves = {device: Glove(device, addresses.get(device)) for device in GLOVES}
        self.phase = "starting"          # starting, tunnel, connecting, running, stopping, finished
        self.capture_phase = None
        self.capture_elapsed = None
        self.observation_started = None
        self.board_state = None
        self.board_detail = ""
        self.board = None                # latest MONITOR_UPDATE
        self.board_first = None          # the run's first MONITOR_UPDATE
        self.board_late = False          # the first update came after samples were acknowledged
        self.board_gaps = 0              # board view losses after the first update
        self.board_restarts = 0          # Ultra96 service restarts seen during the run
        self.board_settled = None        # set by the session after the capture ended
        self.board_events_lost = 0       # events the board dropped, over all connections
        self._board_dropped_connection = 0
        self.service_base = None
        self.service_carry = dict.fromkeys(SERVICE_COUNTERS, 0)
        self.first_subscriber = "unknown"
        self.subscriber_events = 0
        self.messages = deque(maxlen=12)
        self.message_count = 0           # messages noted so far (the deque keeps the last 12)
        self.finished = None             # the capture's "finished" record
        self.stop_reason = None

    # ----- events -> log lines -------------------------------------------------------

    def note(self, now, who, text, *, message=True):
        """A notable line: it goes to the log and to the view's message list."""
        if message:
            self.messages.append((now, who, text))
            self.message_count += 1
        return [(now, who, text)]

    def new_messages(self, seen):
        """Messages noted after the first seen ones (as many as the list still holds)."""
        fresh = min(self.message_count - seen, len(self.messages))
        return list(self.messages)[len(self.messages) - fresh:] if fresh > 0 else []

    def apply(self, now, source, kind, payload):
        """Fold one event into the state and return its log lines [(epoch, who, text)]."""
        if source == "capture" and kind == "record":
            return self._record(now, payload["record"])
        if source == "capture" and kind == "stderr":
            return self.note(now, "laptop", payload["line"])
        if source == "board" and kind == "update":
            return self._board_update(now, payload["update"])
        if source == "board" and kind == "state":
            return self._board_state(now, payload["state"], payload.get("detail", ""))
        if source == "tunnel" and kind == "stderr":
            return self.note(now, "tunnel", payload["line"])
        if kind == "note":
            return self.note(now, source, payload["text"], message=payload.get("message", True))
        return []

    def _record(self, now, record):
        kind = record.get("type")
        device = record.get("device_id")
        glove = self.gloves.get(device)
        when = record_epoch(record, now)
        if kind == "status":
            return self._status(now, record)
        if kind == "log":
            message = record.get("message", "")
            if message.endswith("failed:") or message.endswith("failed: "):
                # An exception without text, in practice a scan or connect that timed out.
                message = message.rstrip() + " no detail (usually a scan or connect timeout)"
            return self.note(when, "laptop", "{} {}".format(record.get("level", ""), message).strip())
        if kind == "finished":
            self.finished = record
            return self.note(when, "run", "capture finished: report {}, clean={}".format(
                "saved" if record.get("report_saved") else "NOT saved", record.get("clean")))
        if glove is None:
            return [(when, "laptop", _generic(record))]
        if kind == "sensor":
            return self._sensor(when, glove, record)
        if kind == "sensor_ack":
            return [(when, glove.name, "ack    #{}  {}".format(record.get("seq"),
                                                              record.get("validation")))]
        if kind == "BLE_connected":
            glove.connected_generation = record.get("generation")
            return self.note(when, glove.name, "BLE connected (MTU {}, {})".format(
                record.get("mtu"),
                "authenticated" if record.get("authenticated") else "NOT authenticated"))
        if kind == "BLE_disconnected":
            if glove.connected_generation is None or \
                    glove.connected_generation != record.get("generation"):
                glove.failed_attempts += 1
                if glove.failed_attempts == 1 or glove.failed_attempts % 5 == 0:
                    # Say where it looked: a wrong address in boards.json looks like a glove that is off.
                    return self.note(when, glove.name, (
                        "not found or not connected at {} (attempt {}): is the glove on, done "
                        "calibrating (held still about 5 s after power-up) and advertising, and is "
                        "this its address? It comes from .comms-local/boards.json; --{}-address "
                        "overrides it").format(glove.address, glove.failed_attempts, glove.name.lower()))
                return [(when, glove.name, "BLE connection attempt ended")]
            glove.connected_generation = None
            return self.note(when, glove.name, "BLE disconnected")
        return self.note(when, glove.name, _record_text(record),
                         message=kind not in _QUIET_RECORDS)

    def _sensor(self, when, glove, record):
        glove.packet = (when, record)
        glove.packets += 1
        lines = []
        boot, seq = record.get("boot_id"), record.get("seq")
        if type(boot) is int and type(seq) is int:
            if boot not in glove.boots and glove.boots:
                lines += self.note(when, glove.name, "the glove restarted (new boot id {}): its "
                                   "new packets are counted, but this capture cannot pass".format(boot))
            glove.boots[boot] = max(seq, glove.boots.get(boot, seq))
        lines.append((when, glove.name, "packet #{}  t {:.3f} s  {}".format(
            seq, (record.get("uptime_ms") or 0) / 1000, values_text(record.get("values")))))
        return lines

    def _status(self, now, record):
        self.capture_phase = record.get("phase")
        self.capture_elapsed = record.get("elapsed_seconds")
        if self.capture_phase == "observation" and self.observation_started is None:
            self.observation_started = now
            if self.phase in ("starting", "tunnel", "connecting"):
                self.phase = "running"
        lines = []
        devices = record.get("devices") or {}
        for device, glove in self.gloves.items():
            status = devices.get(str(device))
            if not isinstance(status, dict):
                continue
            glove.status = status
            counts = glove.counts()
            for step in ("sent", "received", "acked"):
                glove.rates[step].add(now, counts[step])
            if self.observation_started is not None and now - glove.last_counts_log >= 1.0:
                glove.last_counts_log = now
                lines.append((now, glove.name, "counts " + counts_text(glove, now)))
        return lines

    def _board_state(self, now, state, detail):
        if state == "lost" and self.board is not None and self.board_state != "lost":
            self.board_gaps += 1
        self.board_state = state
        self.board_detail = detail
        texts = {
            "connected": "board view connected",
            "unsupported": "the Ultra96 service closed the board view: it predates the "
                           "monitor (redeploy it) or runs another session",
            "no_service": "the tunnel is up but no service answered on the Ultra96 ({})".format(detail),
            "tls_failed": "the Ultra96 certificate was refused: {}".format(detail),
            "lost": "board view lost ({}); reconnecting: model and phone counts pause".format(detail),
        }
        text = texts.get(state)
        return self.note(now, "board", text) if text else []

    def service_delta(self, key):
        if self.board is None or self.service_base is None:
            return None
        return self.service_carry[key] + max(0, self.board["service"][key] - self.service_base[key])

    def _board_update(self, now, update):
        lines = []
        previous, first = self.board, self.board is None
        if not first and _restarted(previous, update):
            self.board_restarts += 1
            for glove in self.gloves.values():
                for key in BOARD_COUNTERS:
                    glove.board_carry[key] = glove.board_delta(key) or 0
                glove.board_base = dict.fromkeys(BOARD_COUNTERS, 0)
            for key in SERVICE_COUNTERS:
                self.service_carry[key] = self.service_delta(key) or 0
            self.service_base = dict.fromkeys(SERVICE_COUNTERS, 0)
            lines += self.note(now, "board", "the Ultra96 service restarted (up {}): counting "
                               "goes on, but results during the restart are not counted".format(
                                   uptime_text(update["uptime_seconds"])))
        self.board = update
        if first:
            self.board_first = update
            self.first_subscriber = update["subscriber"]
            self.service_base = {key: update["service"][key] for key in SERVICE_COUNTERS}
            self.board_late = any((glove.status or {}).get("acked") for glove in self.gloves.values())
            lines += self.note(now, "board", "service up {}, model {}, phone {}, identities {}/{}".format(
                uptime_text(update["uptime_seconds"]), update["model"],
                "subscribed (subscriber {})".format(update["subscriber"])
                if update["subscriber"] is not None else "NOT subscribed",
                update["identities"], update["identity_limit"]))
            if self.board_late:
                lines += self.note(now, "board", "the board view started after the first samples: "
                                   "model and phone counts cover only the rest of the run")
        if update["update"] == 0:
            self._board_dropped_connection = 0  # events_dropped counts per connection
        if update["events_dropped"] > self._board_dropped_connection:
            lost = update["events_dropped"] - self._board_dropped_connection
            self._board_dropped_connection = update["events_dropped"]
            self.board_events_lost += lost
            lines += self.note(now, "board", "{} board events lost (the board view fell behind): "
                               "counts stay right, but some results are not listed".format(lost))
        early = Counter()
        for event in update["events"]:
            if first and event[0] in ("result", "command"):
                early[(event[1], event[0])] += 1
        for device, glove in self.gloves.items():
            counters = update["devices"][str(device)]
            if glove.board_base is None:
                glove.board_base = dict(counters)
                # Results listed in the first update were counted before it: keep the
                # listed labels inside the run's counts.
                for kind, key in (("result", "results"), ("command", "commands")):
                    glove.board_base[key] = max(0, counters[key] - early[(device, kind)])
            glove.board = counters
            counts = glove.counts()
            glove.rates["results"].add(now, counts["results"])
            glove.rates["phone"].add(now, counts["phone"])
        for event in update["events"]:
            lines += self._board_event(now, event)
        lines += self._model_trouble(now, previous, update)
        return lines

    def _model_trouble(self, now, previous, update):
        """One message when the model starts raising, running slow or emitting late."""
        if previous is None:
            return []
        lines = []
        for key, text in (("model_errors", "the model raised in on_sample"),
                          ("model_slow_calls", "the model took longer than 5 ms in on_sample"),
                          ("model_results_late", "the model emitted after the service stopped")):
            before, after = previous["service"][key], update["service"][key]
            if after > before:
                lines += self.note(now, "board", "{} ({} so far this run)".format(
                    text, self.service_delta(key)), message=before == self.service_base.get(key, before))
        return lines

    def _board_event(self, now, event):
        kind = event[0]
        if kind in ("result", "command"):
            _, device, _, seq, gesture, confidence, _ = event
            glove = self.gloves[device]
            glove.results.appendleft((now, kind, seq, gesture, confidence))
            if kind == "result":
                glove.labels[gesture] += 1
                return [(now, glove.name, "result #{}  {} {:.2f}".format(seq, gesture, confidence))]
            return [(now, glove.name, "command echo #{}  {}".format(seq, gesture))]
        if kind == "refused":
            _, device, _, seq, reason, _ = event
            return self.note(now, self.gloves[device].name,
                             "model result refused #{} ({})".format(seq, reason))
        if kind == "subscriber":
            _, change, subscriber, reason, _ = event
            self.subscriber_events += 1
            if change == "claimed":
                return self.note(now, "board", "phone subscribed (subscriber {})".format(subscriber))
            return self.note(now, "board", "phone subscription ended (subscriber {}, {})".format(
                subscriber, reason))
        return []

    # ----- the phone count ---------------------------------------------------------------

    def board_caveats(self):
        """Why the board's counts may not cover the whole run (empty when they do)."""
        caveats = []
        if self.board is None:
            return ["the board view never connected"]
        if self.board_late:
            caveats.append("the board view started after the first samples")
        if self.board_gaps:
            caveats.append("the board view was lost {} time(s)".format(self.board_gaps))
        if self.board_restarts:
            caveats.append("the Ultra96 service restarted {} time(s)".format(self.board_restarts))
        if self.board_settled is not True:
            caveats.append("the last board counts were not confirmed after the capture ended")
        return caveats

    def phone_caveats(self):
        """Why the phone may not have received exactly what the board wrote."""
        if self.board is None:
            return []
        caveats = []
        claims = self.service_delta("subscribers") or 0
        unsent = sum(glove.board_delta("unsent") or 0 for glove in self.gloves.values())
        final = self.board["subscriber"]
        if self.first_subscriber is None and not (claims == 1 and final is not None and not unsent):
            caveats.append("the phone was not subscribed at the start")
        elif self.first_subscriber is not None and (claims or final != self.first_subscriber):
            caveats.append("the phone connection changed ({} new subscription(s))".format(claims))
        if final is None:
            caveats.append("the phone was not subscribed at the end")
        if unsent:
            caveats.append("{} results were not written to the phone".format(unsent))
        return caveats

    # ----- the live view ---------------------------------------------------------------

    def render(self, now, width=100, height=40):
        """The live view as lines, at most height lines of at most width - 1 characters.

        A short window drops the step headers and the blank lines first, so the newest
        messages stay visible in a 30-line window.
        """
        width = max(40, width) - 1
        compact = height < 3 + 2 * 12 + 4
        lines = [self._title(now), self._service_line(now)]
        if not compact:
            lines.append("")
        for glove in self.gloves.values():
            lines += self._glove_lines(glove, now, compact)
            if not compact:
                lines.append("")
        room = height - len(lines)
        if room >= 2 and self.messages:
            lines.append("Messages")
            for when, who, text in list(self.messages)[-(room - 1):]:
                lines.append("  {}  {:<{}} {}".format(clock_text(when, millis=False), who,
                                                      WHO_WIDTH, text))
        return [line[:width] for line in lines[:height]]

    def _title(self, now):
        if self.observation_started is not None:
            elapsed = duration_text(now - self.observation_started)
        else:
            elapsed = "-"
        limit = " of " + duration_text(self.duration) if self.duration else ""
        phase = {"starting": "starting", "tunnel": "waiting for the SSH login",
                 "connecting": "connecting the gloves", "running": "running",
                 "stopping": "stopping (final counters)", "finished": "finished"}[self.phase]
        # The keys first, so a narrow window cuts the session name, not the way to stop.
        keys = ""
        if self.phase in ("starting", "tunnel", "connecting", "running"):
            keys = "[q stop" + ("  1/2 command" if self.keyboard else "") + "]  "
        return "{}Let Them Cook  {}  {}{}  {} Hz  session {}".format(
            keys, phase, elapsed, limit, self.rate_hz, self.session_id)

    def _service_line(self, now):
        board = self.board
        if board is not None and self.board_state in (None, "connected"):
            phone = ("subscribed (subscriber {})".format(board["subscriber"])
                     if board["subscriber"] is not None else "NOT subscribed: results are dropped")
            text = "Ultra96  model {}, up {}, identities {}/{}".format(
                board["model"], uptime_text(board["uptime_seconds"]), board["identities"],
                board["identity_limit"])
            trouble = ["{} {}".format(label, self.service_delta(key))
                       for key, label in (("model_errors", "model errors"),
                                          ("model_slow_calls", "slow calls"),
                                          ("model_results_late", "late results"))
                       if self.service_delta(key)]
            if trouble:
                text += " (" + ", ".join(trouble) + ")"
            return text + "   Phone  " + phone
        state = {None: "not connected yet", "waiting": "not connected yet",
                 "unsupported": "board view off (service predates the monitor)",
                 "no_service": "no service answered", "tls_failed": "certificate refused",
                 "lost": "board view lost, reconnecting", "stopped": "board view closed",
                 "connected": "connected"}.get(self.board_state, self.board_state)
        return "Ultra96  {}   Phone  unknown (read Received on the phone)".format(state)

    def _glove_lines(self, glove, now, compact=False):
        status = glove.status or {}
        link = []
        if status:
            link.append("BLE " + ("connected" if status.get("connected") else "not connected"))
            if status.get("mtu"):
                link.append("MTU {}".format(status["mtu"]))
            link.append("TLS " + ("up" if status.get("transport") else "down"))
            if status.get("rate_hz"):
                link.append("{} Hz".format(status["rate_hz"]))
            gated = status.get("gated")
            link.append({True: "gated (sends only while moving)", False: "not gated"}.get(
                gated, "gating unknown"))
        else:
            link.append("waiting for the capture")
        lines = ["{} glove  ID {}  {}   {}".format(glove.name, glove.device_id,
                                                  glove.address or "", "  ".join(link))]
        counts = glove.counts()
        board_known = glove.board is not None
        board_live = board_known and self.board_state in (None, "connected")
        drops = status.get("dropped") or {}
        dropped = sum(value for value in drops.values() if type(value) is int)
        lost_ble = (counts["sent"] - counts["received"]
                    if counts["sent"] is not None and counts["received"] is not None else None)
        results_note = []
        if counts["refused"]:
            results_note.append("{} refused".format(counts["refused"]))
        if counts["commands"]:
            results_note.append("+{} command echoes".format(counts["commands"]))
        phone_note = "{} not sent".format(counts["unsent"]) if counts["unsent"] is not None else ""
        if board_known and not board_live:
            results_note.append("board view not live")
        rows = (
            ("Glove sent", "sent", ""),
            ("Laptop received", "received",
             "" if lost_ble is None else "{} missing".format(max(0, lost_ble))),
            ("Ultra96 acknowledged", "acked",
             "laptop queue {}, {} dropped by the laptop".format(status.get("queue", 0), dropped)
             if status else ""),
            ("Model results", "results", ", ".join(results_note)),
            ("Sent to phone", "phone", phone_note),
        )
        if not compact:
            lines.append("  {:<22}{:>9}{:>10}   {}".format("Step", "Count", "Rate", ""))
        for label, key, note in rows:
            value = counts[key]
            if value is None:
                text = "n/a" if key in ("results", "phone") and not board_known else "-"
                rate = ""
            else:
                text = str(value)
                per_second = glove.rates[key].per_second(now)
                rate = "-" if per_second is None else "{:.1f}/s".format(per_second)
            lines.append("  {:<22}{:>9}{:>10}   {}".format(label, text, rate, note))
        if glove.packet is not None:
            when, record = glove.packet
            lines.append("  Latest packet #{}  glove time {:.3f} s  ({:.1f} s ago)".format(
                record.get("seq"), (record.get("uptime_ms") or 0) / 1000, max(0.0, now - when)))
            lines += ["    " + line for line in values_lines(record.get("values"))]
        else:
            lines += ["  Latest packet  none yet", "", ""]  # same height as with a packet
        if glove.results:
            recent = "  ".join("{} {:.2f} #{}".format(gesture, confidence, seq)
                               if kind == "result" else "cmd {} #{}".format(gesture, seq)
                               for _, kind, seq, gesture, confidence in glove.results)
            lines.append("  Results  " + recent)
        elif board_known:
            lines.append("  Results  none yet")
        else:
            lines.append("  Results  shown when the board view is connected")
        return lines


def _restarted(previous, update):
    """A new service process: its uptime or one of its counters went down."""
    if update["uptime_seconds"] < previous["uptime_seconds"]:
        return True
    return any(update["devices"][device][key] < previous["devices"][device][key]
               for device in ("1", "2") for key in BOARD_COUNTERS)


def counts_text(glove, now=None):
    counts = glove.counts()
    parts = []
    for key, label in (("sent", "sent"), ("received", "received"), ("acked", "acked"),
                       ("results", "results"), ("phone", "to phone")):
        value = counts[key]
        if value is None:
            continue
        per_second = glove.rates[key].per_second(now)
        parts.append("{} {}{}".format(label, value, "" if per_second is None
                                      else " ({:.1f}/s)".format(per_second)))
    if counts["commands"]:
        parts.append("command echoes {}".format(counts["commands"]))
    status = glove.status or {}
    if status:
        parts.append("laptop queue {}".format(status.get("queue", 0)))
    return "  ".join(parts) if parts else "none yet"


def _generic(record):
    fields = {key: value for key, value in record.items()
              if key not in ("type", "timestamp_utc", "monotonic_seconds", "device_id")}
    return "{} {}".format(record.get("type"), json.dumps(fields, sort_keys=True))


def _record_text(record):
    kind = record.get("type") or ""
    if kind == "source_rate":
        return "glove rate set to {} Hz".format(record.get("rate_hz"))
    if kind == "BLE_connection_parameters":
        return "BLE connection interval {} ms (request {})".format(
            record.get("actual_interval_ms"), record.get("request_status"))
    if kind == "control_connected":
        return "control channel ready"
    if kind == "sensor_invalid":
        return "malformed packet {}".format(record.get("raw_hex"))
    if kind == "source_quiesced":
        snapshot = record.get("snapshot") or {}
        return "glove stopped sending: it counts {} sent, {} idle ticks".format(
            snapshot.get("submitted"), snapshot.get("idle"))
    if kind == "source_tail_drain":
        return "last packets {} ({} of {} received)".format(
            record.get("validation"), record.get("received"), record.get("expected_received"))
    if kind == "source_final_verified":
        return "glove counters read again: unchanged"
    if kind.startswith("command_"):
        return "command {} {}".format(kind[len("command_"):], record.get("request_id"))
    return _generic(record)


def log_line(when, who, text):
    return "{}  {:<{}} {}".format(clock_text(when), who, WHO_WIDTH, text)


def header_lines(*, folder, started, command, revision, session_id, rate_hz, duration,
                 keyboard, addresses, tunnel):
    local = datetime.fromtimestamp(started).astimezone()
    return [
        "Let Them Cook (B07): Communications run log",
        "Run folder  {}".format(folder),
        "Started     {}".format(local.strftime("%Y-%m-%d %H:%M:%S %z")),
        "Command     {}".format(command),
        "Revision    {}".format(revision or "unknown"),
        "Settings    session {}, {} Hz, {}, keyboard commands {}".format(
            session_id, rate_hz, "until stopped" if not duration else
            "{:g} s".format(duration), "on (keys 1/2)" if keyboard else "off"),
        "Gloves      LEFT = ID 1 = {}   RIGHT = ID 2 = {}".format(addresses.get(1), addresses.get(2)),
        "Tunnel      {}".format(tunnel),
        "Also here   report.json (the verdict data), packets.jsonl (every packet, read by "
        "python -m tools.export_capture), exit-code.txt",
        "Lines       laptop time, who (run, tunnel, board, laptop, LEFT, RIGHT), what. Packet "
        "values in g, deg/s, deg and flex 0..1.",
        "            Board lines (model results, the phone) come in batches every 0.25 s, "
        "so they follow the packets they answer.",
        "-" * 79,
    ]


def capture_problems(report, exit_code=None):
    """Plain reasons a capture did not pass, in the terms of demo.py's check."""
    if not isinstance(report, dict) or not report:
        return ["report.json is missing or unreadable (the capture did not finish)"]
    problems = []
    if report.get("status") == "incomplete":
        problems.append("report.json is still the placeholder: the capture was cut off "
                        "before it finished")
        return problems
    if report.get("mock_input") is not False:
        problems.append("synthetic input (--mock) never passes")
    if report.get("report_saved") is not True:
        problems.append("the report was not saved")
    if report.get("progress_error"):
        problems.append("the progress output failed")
    evidence = report.get("evidence") if isinstance(report.get("evidence"), dict) else {}
    for key in ("dropped", "write_errors", "unfinished"):
        if evidence.get(key):
            problems.append("packets.jsonl: {} {}".format(evidence[key], key.replace("_", " ")))
    devices = report.get("devices") if isinstance(report.get("devices"), dict) else {}
    for device, name in GLOVES.items():
        item = devices.get(str(device))
        if not isinstance(item, dict):
            problems.append("{}: no counts in the report".format(name))
            continue
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        found = []
        if item.get("protected_ble") is not True:
            found.append("BLE was not protected")
        if item.get("unfinished"):
            found.append("packets were still queued or in flight at the end")
        found += ["runtime failure {}".format(failure) for failure in item.get("runtime_failures") or []]
        if not source:
            found.append("no glove counters (source statistics unavailable)")
        else:
            if source.get("complete") is not True:
                found.append("the glove's final counters were not read")
            generated = source.get("generated")
            if not generated:
                found.append("the glove sent nothing")
            for key, text in (("missing_received", "missing over BLE"),
                              ("missing_acked", "without an ACK")):
                if source.get(key):
                    found.append("{} {}".format(source[key], text))
        if item.get("sustained_coverage") is False:
            found.append("coverage {:.0%} of the expected samples (needs 90%)".format(
                item.get("coverage_ratio") or 0))
        if item.get("silence_ok") is False:
            found.append("a silence longer than the 2 s freshness limit ({:.1f} s)".format(
                item.get("observation_max_silence") or 0))
        anomalies = ["{} {}".format(item[key], key.replace("_", " ")) for key in (
            "malformed", "stale_dropped", "ack_errors", "ambiguous_dropped", "transport_errors",
            "ble_errors", "disconnects", "queue_dropped", "gaps", "duplicates", "out_of_order",
            "new_boots") if item.get(key)]
        found += anomalies
        if found:
            problems.append("{}: {}".format(name, "; ".join(found)))
    if exit_code not in (None, 0) and not problems:
        problems.append("the capture exited with code {}".format(exit_code))
    return problems


def summary_lines(state, report, ended, *, passed=None, exit_code=None):
    """The end-of-run summary: counts per step and glove, the phone, and why it failed.

    passed is the CAPTURE PASSED verdict (None when unknown); the phone line and the
    reasons depend on it.
    """
    lines = ["", "=" * 79, "Summary", "=" * 79]
    report = report if isinstance(report, dict) else {}
    devices = report.get("devices") if isinstance(report.get("devices"), dict) else {}
    observed = report.get("common_observation_seconds")
    lines.append("Ended       {}; {}; observed {}".format(
        datetime.fromtimestamp(ended).astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
        state.stop_reason or "reason unknown",
        "{:.1f} s".format(observed) if isinstance(observed, (int, float)) else "unknown"))
    board = state.board
    if board is not None:
        lines.append("Ultra96     model {}, identities {}/{}".format(
            board["model"], board["identities"], board["identity_limit"]))
        service = board["service"]
        lines.append("Model       errors {}, calls over 5 ms {}, late results {} (this run); "
                     "slowest call {:.1f} ms since the service started".format(
                         state.service_delta("model_errors"), state.service_delta("model_slow_calls"),
                         state.service_delta("model_results_late"), service["model_max_call_ms"]))
    else:
        lines.append("Ultra96     board view not connected; model results and phone delivery "
                     "were not counted on the laptop")
    caveats = state.board_caveats() if board is not None else []
    if caveats:
        lines.append("Board view  " + "; ".join(caveats) + ": its counts are partial")
    if state.board_events_lost:
        lines.append("            {} board events were lost: the labels below miss some "
                     "results".format(state.board_events_lost))
    lines.append("")
    lines.append("{:<36}{:>14}{:>14}".format("", "LEFT (ID 1)", "RIGHT (ID 2)"))

    def row(label, values):
        lines.append("{:<36}{:>14}{:>14}".format(
            label, *("-" if value is None else str(value) for value in values)))

    def item(device):
        value = devices.get(str(device))
        return value if isinstance(value, dict) else {}

    def source(device, key):
        value = item(device).get("source")
        return value.get(key) if isinstance(value, dict) else None

    def laptop_drops(device):
        values = [item(device).get(key) for key in _LAPTOP_DROPS]
        return sum(values) if all(type(value) is int for value in values) else None

    def missing_ble(device):
        generated, received = source(device, "generated"), item(device).get("callback_received")
        if type(generated) is not int or type(received) is not int:
            return None
        return max(0, generated - received)

    row("Glove generated (glove counters)", [source(d, "generated") for d in GLOVES])
    row("Laptop received (BLE)", [item(d).get("callback_received") for d in GLOVES])
    row("Missing over BLE", [missing_ble(d) for d in GLOVES])
    row("Dropped on the laptop", [laptop_drops(d) for d in GLOVES])
    row("Ultra96 acknowledged", [source(d, "acked") for d in GLOVES])
    row("Missing an ACK", [source(d, "missing_acked") for d in GLOVES])
    row("Glove idle ticks (gated gloves)", [source(d, "source_idle") for d in GLOVES])
    counts = {d: glove.counts() for d, glove in state.gloves.items()}
    row("Model results (Ultra96)", [counts[d]["results"] for d in GLOVES])
    row("Model results refused (Ultra96)", [counts[d]["refused"] for d in GLOVES])
    row("Command echoes (Ultra96)", [counts[d]["commands"] for d in GLOVES])
    row("Written to the phone (Ultra96)", [counts[d]["phone"] for d in GLOVES])
    row("Not written to the phone (Ultra96)", [counts[d]["unsent"] for d in GLOVES])
    commands = []
    for device in GLOVES:
        value = item(device).get("commands")
        commands.append("{}/{}".format(value.get("completed"), value.get("accepted"))
                        if isinstance(value, dict) else None)
    row("Commands answered", commands)
    for glove in state.gloves.values():
        if glove.labels:
            lines.append("{} labels: {}".format(glove.name, ", ".join(
                "{} {}".format(label, count) for label, count in glove.labels.most_common())))
    lines.append("")
    lines += _phone_lines(state, counts, passed)
    if passed is False:
        problems = capture_problems(report, exit_code)
        if problems:
            lines += ["", "Why the capture did not pass:"] + ["  - " + text for text in problems]
    return lines


def _phone_lines(state, counts, passed):
    written = [counts[d]["phone"] for d in GLOVES]
    if passed is None and state.finished is None:
        return ["Phone: nothing was sent (the capture did not start)."]
    if state.board is None or any(value is None for value in written):
        if passed:
            return ["Phone: not counted here (no board view); compare the phone's Received count "
                    "with the 'Phone expected increase' below (dummy model only)."]
        return ["Phone: not counted here (no board view), and no phone total can be inferred "
                "because the capture did not pass."]
    total = sum(written)
    caveats = state.board_caveats() + state.phone_caveats()
    if caveats:
        return ["Phone: the Ultra96 wrote at least {} results to a phone connection during this "
                "run, but {}: compare with the phone's Received count with care.".format(
                    total, "; ".join(caveats))]
    tail = "" if passed else (" (the board's own count, so it holds even though the capture "
                              "did not pass)")
    return ["Phone: the Ultra96 wrote {} results to the phone during this run; its Received "
            "count should have risen by exactly {}{}.".format(total, total, tail)]
