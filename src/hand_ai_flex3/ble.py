"""BLE transport shared by the recorder and laptop live inference."""
import asyncio
import time
from contextlib import asynccontextmanager
from src.hand_ai_flex3.sensor import split_notification, decode_packet, physical_values, SENSOR_CHARACTERISTIC_UUID
from common.control import RESPONSE_UUID
from laptop.controls import ControlChannel
from src.hand_ai_flex3.source_stats import SOURCE_STATS_UUID, parse_source_stats

# Physical RIGHT confirmed on 2026-10-24 after the hardware rebuild.
# Confirm LEFT's current board before adding its address; do not infer a swap.
ADDRESSES = {1: '38:18:2B:17:E2:E6', 2: '38:18:2B:14:EA:FE'}

class NotificationQueue(asyncio.Queue):
    """Keep trial data only between GO and STOP; live mode remains continuous.

    All methods are called on the asyncio loop, including feed() via
    call_soon_threadsafe. Intentional exclusions outside trials are not loss.
    """
    def __init__(self, device, boot, health, *, trial_mode=False, maxsize=1024):
        super().__init__(maxsize=maxsize)
        self.device, self.boot, self.health = device, boot, health
        self.trial_mode = trial_mode
        self.accepting = not trial_mode
        self.cue = None

    def start_trial(self, cue):
        clear_queue(self)
        self.cue = cue
        self.accepting = True

    def stop_trial(self):
        self.accepting = False
        clear_queue(self)

    def feed(self, data, received):
        if not self.accepting or (self.cue is not None and received < self.cue):
            return
        try:
            packets = [decode_packet(p) for p in split_notification(data)]
            if any(p.device_id != self.device or p.boot_id != self.boot for p in packets):
                raise ValueError('Device/boot changed')
            for p in packets:
                self.put_nowait((p, physical_values(p.values), received))
        except asyncio.QueueFull:
            self.health['dropped'] += 1
        except (ValueError, TypeError):
            self.health['malformed'] += 1

async def close_client(client, timeout=5):
    """Disconnect directly; do not wait for a separate remote CCCD write first."""
    try:
        await asyncio.wait_for(client.disconnect(), timeout=timeout)
    except asyncio.TimeoutError as error:
        raise TimeoutError('BLE disconnect timed out; connection closure is unconfirmed. Close the recorder before reconnecting.') from error
    if client.is_connected:
        raise ConnectionError('BLE backend still reports connected after disconnect')

@asynccontextmanager
async def connection(device, address=None, *, recording=False):
    from bleak import BleakClient
    loop = asyncio.get_running_loop()
    health = dict(dropped=0, malformed=0)
    selected_address = address or ADDRESSES.get(device)
    if not selected_address:
        raise ValueError('LEFT Bluetooth address is not confirmed after the hardware rebuild. '
                         'Confirm that board address before using LEFT; CLI users may pass --address.')
    client = BleakClient(selected_address, timeout=20)
    control = None
    closing = False
    try:
        await client.connect()
        raw = await client.read_gatt_char(SOURCE_STATS_UUID, use_cached=False)
        stats = parse_source_stats(raw, expected_device_id=device)
        if stats.gated:
            raise ValueError('Glove is movement-gated; use SEND_ALL firmware before recording IDLE')
        queue = NotificationQueue(device, stats.boot_id, health, trial_mode=recording)
        control = ControlChannel(device, timeout=5)
        control.attach(client, stats.boot_id)
        def receive(_sender, data):
            if not closing and not loop.is_closed():
                loop.call_soon_threadsafe(queue.feed, bytes(data), time.monotonic())
        await client.start_notify(RESPONSE_UUID, control.receive)
        await control.set_rate(50)
        await client.start_notify(SENSOR_CHARACTERISTIC_UUID, receive)
        print(f'Connected device={device}, boot={stats.boot_id}, MTU={client.mtu_size}; 50 Hz acknowledged', flush=True)
        yield client, queue, health
    finally:
        closing = True
        if control is not None:
            control.detach()
        # Bleak disconnect tears down notification handlers too. Calling
        # stop_notify first adds a remote GATT write that can stall on WinRT.
        await close_client(client)

def clear_queue(queue):
    while not queue.empty():
        queue.get_nowait()

def ensure_healthy(health):
    if any(health.values()):
        raise ValueError(f'BLE reception failed quality checks: {health}; reconnect and retry')

async def collect(queue, health):
    """Collect a full 2-second window by source time, not notification arrival time."""
    cue = time.monotonic()
    trial_mode = isinstance(queue, NotificationQueue) and queue.trial_mode
    if trial_mode:
        queue.start_trial(cue)
    try:
        return await _collect(queue, health, cue)
    finally:
        if trial_mode:
            queue.stop_trial()

async def _collect(queue, health, cue):
    clear_queue(queue)
    rows = []
    elapsed = 0
    previous = None
    while elapsed < 2000:
        ensure_healthy(health)
        packet, values, received = await asyncio.wait_for(queue.get(), timeout=3)
        if received < cue:
            continue
        if time.monotonic() - received > .5:
            raise ValueError('Stale queued samples')
        if previous is not None:
            if packet.boot_id != previous.boot_id or packet.seq != (previous.seq + 1) % 2**32:
                raise ValueError('Boot change or sequence gap during recording')
            dt = (packet.uptime_ms - previous.uptime_ms) % 2**32
            if not 0 < dt <= 100:
                raise ValueError('Invalid source time or >100 ms gap')
            elapsed += dt
        rows.append((elapsed / 1000., packet, values))
        previous = packet
    ensure_healthy(health)
    if elapsed > 2100 or len(rows) < 85:
        raise ValueError('Recording duration/sample count outside limits')
    return rows
