"""Starved BLE link detection for one glove's connection.

With both gloves connected the laptop's controller sometimes serves one link
only about 7 times a second (seen on 2026-10-20). The glove then sends every
notification full (6 samples, comms_send_queue.h), so 42 of its 50 samples a
second leave, and its 32-sample queue drops its oldest sample: the laptop sees
seq gaps until the link is rebuilt. A healthy link never shows this for long:
one glove alone gets about 50 notifications a second of 1 sample, two gloves
about 30 each of about 1.7, and a short controller pause (up to about 120 ms)
gives a few full notifications back to back with no seq gap.

The watchdog is pure and clock-free. The bridge passes each notification's
arrival time, its packet count and the (boot_id, seq) of its packets, and
drops the link when the answer says so. Arrival times come from the bridge
clock, which ticks every 15.6 ms on Windows, so several notifications can share
one time; only counts and the span of a streak are used, never their order.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

WINDOW_SECONDS = 2.0
# A starved glove gives the model nothing: Wen Ree's HandWindow resets on every
# seq gap, so another reconnect costs nothing the model would have used. The
# cooldown only keeps the bridge from hammering the Windows Bluetooth stack.
COOLDOWN_SECONDS = 10.0
MIN_NOTIFICATIONS = 8
_HALF = 0x80000000


def full_notification_packets(mtu, packet_size, limit=6):
    """Packets in a full notification at this ATT MTU, as the firmware counts them."""
    return max(0, min(limit, (mtu - 3) // packet_size))


@dataclass(frozen=True)
class Starvation:
    """One detection: "reconnect", or "held" inside the cooldown, and its streak."""
    action: str
    streak_seconds: float
    notifications: int
    missing: int
    full_packets: int


def _positive_seconds(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive finite seconds")
    return float(value)


class StarvedLinkWatchdog:
    """Fire when a link's notifications have all been full, with seq gaps, for a window.

    A streak is a run of consecutive full notifications; a notification with
    fewer packets ends it. The watchdog fires once the streak spans window
    seconds from its first notification, holds min_notifications and has lost
    at least one sample, at most once per connection. The cooldown spans
    connections: inside it the condition is reported "held" once per streak
    and the watchdog keeps watching, firing as soon as the cooldown has expired
    while the streak still holds.
    """

    def __init__(self, window=WINDOW_SECONDS, cooldown=COOLDOWN_SECONDS,
                 min_notifications=MIN_NOTIFICATIONS):
        self.window = _positive_seconds(window, "starvation window")
        self.cooldown = _positive_seconds(cooldown, "starvation cooldown")
        if type(min_notifications) is not int or min_notifications < 1:
            raise ValueError("minimum notifications must be a positive integer")
        self.min_notifications = min_notifications
        # Disarmed until the first connection starts.
        self.full_packets = 0
        self._fired = False
        self._last_fire = None
        self._previous = None
        self._reset_streak()

    @property
    def armed(self):
        # One packet per notification cannot show the signature.
        return self.full_packets >= 2 and not self._fired

    def _reset_streak(self):
        self._streak_start = None
        self._streak_notifications = 0
        self._streak_missing = 0
        self._held = False

    def connection_started(self, full_packets):
        """A new connection: full_packets makes a notification full at its MTU."""
        if type(full_packets) is not int or full_packets < 0:
            raise ValueError("full packets must be a nonnegative integer")
        self.full_packets = full_packets
        self._fired = False
        self._previous = None
        self._reset_streak()

    def observe(self, received_at, batch_size, packets):
        """One notification; returns a Starvation when the bridge should act or know, else None.

        packets are the (boot_id, seq) of its decoded packets of the expected
        glove, in order. Missing samples are seq steps over 1 within one boot;
        a boot change, a duplicate or an older seq is not a gap.
        """
        if not self.armed:
            return None
        missing = 0
        for boot_id, seq in packets:
            if self._previous is not None and self._previous[0] == boot_id:
                delta = (seq - self._previous[1]) & 0xffffffff
                if delta == 0 or delta >= _HALF:
                    continue
                missing += delta - 1
            self._previous = boot_id, seq
        if batch_size < self.full_packets:
            self._reset_streak()
            return None
        if self._streak_start is None:
            self._streak_start = received_at
        self._streak_notifications += 1
        self._streak_missing += missing
        streak = max(0.0, received_at - self._streak_start)
        if (streak < self.window or self._streak_notifications < self.min_notifications
                or not self._streak_missing):
            return None
        if self._last_fire is None or received_at - self._last_fire >= self.cooldown:
            self._fired = True
            self._last_fire = received_at
            action = "reconnect"
        elif self._held:
            return None
        else:
            self._held = True
            action = "held"
        return Starvation(action, streak, self._streak_notifications, self._streak_missing,
                          self.full_packets)
