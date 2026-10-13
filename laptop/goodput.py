"""Reception goodput, independent of queueing, forwarding and presentation.

Only observation time is counted. Startup/drain are excluded; silence is part
of the denominator. A bounded timestamp window supports exact rolling rates.
"""
from collections import deque
import math
import threading

from common.sensor import PACKET_SIZE, SensorPacket, dummy_values


def _time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('time must be finite monotonic seconds')
    return float(value)


class GoodputMeter:
    def __init__(self, window_seconds=5.0):
        self.window_seconds = _time(window_seconds)
        if self.window_seconds <= 0:
            raise ValueError('window must be positive')
        self._lock = threading.Lock()
        self._start = self._end = None
        self._last = {}
        self._ticks = deque()
        self._packets = 0
        self.tracking_overflow = 0

    def start(self, now):
        now = _time(now)
        with self._lock:
            self._start, self._end = now, None
            self._packets = 0
            self._ticks.clear()

    def stop(self, now):
        now = _time(now)
        with self._lock:
            if self._start is None or now < self._start:
                raise ValueError('observation must start before it stops')
            self._end = now

    def record(self, packet, received_at):
        received_at = _time(received_at)
        if not isinstance(packet, SensorPacket):
            return False
        if packet.version == 1 and packet.values != dummy_values(packet.seq):
            return False
        with self._lock:
            key = packet.device_id, packet.boot_id
            previous = self._last.get(key)
            if previous is not None:
                delta = (packet.seq - previous) & 0xffffffff
                if delta == 0 or delta >= 0x80000000:
                    return False
            elif len(self._last) >= 128:
                self.tracking_overflow += 1
                return False
            self._last[key] = packet.seq
            if (self._start is None or received_at < self._start
                    or (self._end is not None and received_at >= self._end)):
                return True
            self._packets += 1
            self._prune(received_at)
            # >4000 packets/s in the default window exceeds the configured ESP
            # rate cap by 20x. Fail visibly if a custom producer exceeds storage.
            if len(self._ticks) >= 20000:
                self._ticks.popleft()
                self.tracking_overflow += 1
            self._ticks.append(received_at)
            return True

    def _prune(self, now):
        cutoff = now - self.window_seconds
        while self._ticks and self._ticks[0] < cutoff:
            self._ticks.popleft()

    def report(self, now):
        now = _time(now)
        with self._lock:
            end = self._end if self._end is not None else now
            if self._start is not None and end < self._start:
                raise ValueError('report precedes observation')
            elapsed = 0 if self._start is None else end - self._start
            self._prune(end)
            rolling_elapsed = min(elapsed, self.window_seconds)
            packet_bytes = self._packets * PACKET_SIZE
            return {
                'boundary': 'unique valid sensor packet bytes at laptop reception',
                'interval': 'steady observation; excludes startup and final draining',
                'packet_bytes': packet_bytes,
                'packets': self._packets,
                'elapsed_seconds': elapsed,
                'average_kbps': packet_bytes * 8 / elapsed / 1000 if elapsed else 0.0,
                'rolling_kbps': len(self._ticks) * PACKET_SIZE * 8 / rolling_elapsed / 1000
                    if rolling_elapsed else 0.0,
                'rolling_elapsed_seconds': rolling_elapsed,
                'tracking_overflow': self.tracking_overflow,
            }
