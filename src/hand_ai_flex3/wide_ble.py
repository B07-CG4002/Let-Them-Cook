"""Local v5 diagnostic BLE adapter. Production v3/v4 paths are untouched."""
import asyncio
from contextlib import asynccontextmanager
import struct
import time

from common import sensor_v5 as codec
from common.control import RESPONSE_UUID
from laptop.controls import ControlChannel
from src.hand_ai_flex3.ble import ADDRESSES, NotificationQueue, close_client
from src.hand_ai_flex3.source_stats import SOURCE_STATS_UUID, SourceStats

RECORDING_CONTRACT='independent_hand_flex3_gyro_v5_v1'
TRANSPORT=dict(sensor_packet_version=5,sensor_packet_bytes=40,source_stats_magic='LCS5',
               gyro_scale=10,gyro_units='deg/s',imu_range_dps=1000,
               imu_sensitivity_lsb_per_dps=32.8,
               scope='explicit v5 acquisition; use the reviewed wide_training importer')


def parse_stats(data,device):
    if type(device) is not int or device not in (1,2):raise ValueError('Invalid selected hand')
    if not isinstance(data,(bytes,bytearray,memoryview)) or len(data)!=36:
        raise ValueError('Expected exactly 36 source-statistics bytes')
    magic,source,flags,boot,seq,submitted,failures,idle,unread,refused=struct.unpack('<4sBB2xIIIIIII',data)
    if magic!=b'LCS5':raise ValueError('This diagnostic requires LCS5: flash the matching *-glove-flex3-wide environment')
    if source!=device or flags&~3 or bytes(data[6:8])!=b'\0\0':
        raise ValueError('Wrong source identity, reserved bits or flags')
    if flags&1:raise ValueError('Movement-gated firmware cannot capture IDLE')
    return SourceStats(source,boot,seq,submitted,failures,gated=False,idle=idle,unread=unread,refused=refused)


class WideQueue(NotificationQueue):
    def feed(self,data,received):
        if not self.accepting or (self.cue is not None and received<self.cue):return
        try:
            packets=[codec.decode_packet(p) for p in codec.split_notification(data)]
            if any(p.device_id!=self.device or p.boot_id!=self.boot for p in packets):
                raise ValueError('Device/boot changed')
            for p in packets:self.put_nowait((p,codec.physical_values(p.values),received))
        except asyncio.QueueFull:self.health['dropped']+=1
        except (ValueError,TypeError):self.health['malformed']+=1


@asynccontextmanager
async def connection(device):
    from bleak import BleakClient
    loop=asyncio.get_running_loop();health=dict(dropped=0,malformed=0)
    client=BleakClient(ADDRESSES[device],timeout=20)
    control=None;closing=False
    try:
        await client.connect()
        stats=parse_stats(await client.read_gatt_char(SOURCE_STATS_UUID,use_cached=False),device)
        queue=WideQueue(device,stats.boot_id,health)
        # Control protocol v1 SET_RATE has no sensor payload and is identical
        # across formats. Only SET_RATE is used here; no sensor echo command.
        control=ControlChannel(device,timeout=5,sensor_version=4)
        control.attach(client,stats.boot_id)
        def receive(_sender,data):
            if not closing and not loop.is_closed():
                loop.call_soon_threadsafe(queue.feed,bytes(data),time.monotonic())
        await client.start_notify(RESPONSE_UUID,control.receive)
        await control.set_rate(50)
        await client.start_notify(codec.SENSOR_CHARACTERISTIC_UUID,receive)
        print(f'Connected v5 device={device}, boot={stats.boot_id}; gyro x10, 50 Hz acknowledged',flush=True)
        yield client,queue,health
    finally:
        closing=True
        if control is not None:control.detach()
        await close_client(client)
