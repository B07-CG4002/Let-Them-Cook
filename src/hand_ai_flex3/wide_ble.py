"""Local v5 diagnostic BLE adapter. Production v3/v4 paths are untouched."""
import asyncio
from contextlib import asynccontextmanager
import struct
import sys
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


def transport_for(gyro_dps):
    if type(gyro_dps) is not int or gyro_dps not in (1000,2000):raise ValueError('Expected gyro range 1000 or 2000')
    return dict(TRANSPORT,imu_range_dps=gyro_dps,imu_sensitivity_lsb_per_dps=32.8 if gyro_dps==1000 else 16.4)

def contract_for(gyro_dps):
    transport_for(gyro_dps)
    return RECORDING_CONTRACT if gyro_dps==1000 else 'independent_hand_flex3_gyro_v5_2000_v1'


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


def make_windows_request(native):
    from winrt.windows.devices.bluetooth import BluetoothLEPreferredConnectionParameters
    return native.request_preferred_connection_parameters(
        BluetoothLEPreferredConnectionParameters.throughput_optimized)


async def windows_throughput(client,device,*,_platform=None):
    """Hold the demonstrated Windows preference; verify it before subscribing.

    Bleak 3.0.1 exposes the native Windows device through _requester. Unsupported
    backends/APIs fail explicitly; a slow default link is not accepted silently.
    """
    if (_platform or sys.platform)!='win32':return None
    request=None
    interval=latency=None
    try:
        native=client._backend._requester
        request=make_windows_request(native)
        deadline=time.monotonic()+5
        while True:
            params=native.get_connection_parameters()
            interval=int(params.connection_interval)
            latency=int(params.connection_latency)
            if 6<=interval<=12 and latency==0:
                print(f'Windows BLE device={device}: interval={interval*1.25:g} ms latency=0; throughput preference held',flush=True)
                return request
            if time.monotonic()>=deadline:break
            await asyncio.sleep(.1)
        raise RuntimeError(f'Windows link remains interval={interval*1.25:g} ms latency={latency}; expected <=15 ms and latency=0')
    except BaseException as error:
        if request is not None:
            try:request.close()
            except Exception:pass
        if isinstance(error,(asyncio.CancelledError,KeyboardInterrupt)):raise
        raise RuntimeError(f'Cannot establish the reviewed Windows BLE link for device {device}: {error}. No sensor subscription started.') from error


@asynccontextmanager
async def connection(device):
    from bleak import BleakClient
    loop=asyncio.get_running_loop();health=dict(dropped=0,malformed=0)
    client=BleakClient(ADDRESSES[device],timeout=20)
    control=None;closing=False;profile_request=None
    try:
        await client.connect()
        stats=parse_stats(await client.read_gatt_char(SOURCE_STATS_UUID,use_cached=False),device)
        profile_request=await windows_throughput(client,device)
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
        try:
            if profile_request is not None:profile_request.close()
        finally:
            await close_client(client)
