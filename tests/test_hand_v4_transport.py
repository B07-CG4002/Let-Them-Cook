import asyncio
import importlib.util
import json
from pathlib import Path
import socket
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from common import sensor,sensor_v4
from common.wire import ProtocolError
from laptop.bridge import Bridge,BridgeConfig
from laptop.source_audit import parse_source_stats
from laptop.controls import ControlChannel
from common.control import ControlFrame,COMMAND,RESPONSE_FLAG,encode_control,decode_control,transformed_values
from ultra96.protocol import validate_message
from ultra96 import model_bridge
from ultra96.model import ModelPort


def stats(version):return struct.pack('<4sBB2xIIIIIII',b'LCS3' if version==3 else b'LCS4',1,2,7,0,0,0,0,0,0)


class CodecProtocolTests(unittest.TestCase):
    def test_v4_matches_recorder_and_contract(self):
        from src.hand_ai_flex3 import sensor as recorder
        packet=sensor_v4.SensorPacket(1,7,1,20,tuple(range(12)))
        self.assertEqual(sensor_v4.encode_packet(packet),recorder.encode_packet(recorder.SensorPacket(1,7,1,20,tuple(range(12)))))
        self.assertEqual(sensor_v4.PACKET_SIZE,40)
        self.assertEqual(sensor_v4.SENSOR_MTU_FLOOR,43)

    def test_versions_are_explicit_and_never_mixed(self):
        old=sensor.SensorPacket(1,7,1,20,sensor.DUMMY_FIXTURES[0]).to_message('ltc-comms')
        new=sensor_v4.SensorPacket(1,7,1,20,sensor_v4.DUMMY_FIXTURES[0]).to_message('ltc-comms')
        self.assertIs(validate_message(old),old)
        self.assertIs(validate_message(new,sensor_version=4),new)
        with self.assertRaises(ProtocolError):validate_message(new)
        with self.assertRaises(ProtocolError):validate_message(old,sensor_version=4)

    def test_v4_still_requires_twelve_raw_int16_values(self):
        for values in ([0]*11,[0]*13,[0]*11+[True],[0]*11+[40000]):
            with self.assertRaises(ValueError):sensor_v4.SensorPacket(1,7,1,20,values)

    def test_source_stats_version_default_preserved(self):
        self.assertEqual(parse_source_stats(stats(3)).device_id,1)
        self.assertEqual(parse_source_stats(stats(4),sensor_version=4).device_id,1)
        with self.assertRaises(ValueError):parse_source_stats(stats(4))
        with self.assertRaises(ValueError):parse_source_stats(stats(3),sensor_version=4)

    def test_control_echo_packet_codec_is_version_selected(self):
        packet=sensor_v4.SensorPacket(1,7,1,0,sensor_v4.DUMMY_FIXTURES[0])
        frame=ControlFrame(COMMAND,1,1,payload=sensor_v4.encode_packet(packet))
        encoded=encode_control(frame,sensor_version=4)
        self.assertEqual(decode_control(encoded,sensor_version=4),frame)
        with self.assertRaises(ValueError):decode_control(encoded)
        self.assertEqual(transformed_values(packet.values,sensor_version=4)[-1],5001)

    def test_model_bridge_twelve_channel_hello_reaches_existing_observer(self):
        from src.socket_worker_hand import validate_hello
        bridge=model_bridge.Bridge();client,peer=socket.socketpair();peer.settimeout(2)
        try:
            with patch.object(model_bridge,'_BRIDGE',bridge):model_bridge.configure_sensor_version(4)
            bridge.attach(client)
            data=b''
            while b'\n' not in data:data+=peer.recv(4096)
            hello=json.loads(data.split(b'\n')[0]);validate_hello(hello)
            self.assertEqual(hello['channels'][-1],'flex3')
        finally:
            bridge.close();peer.close();client.close()


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_bridge_remains_v3(self):
        bridge=Bridge(BridgeConfig(ca_file='unused'))
        self.assertEqual(bridge.packet_size,38);self.assertEqual(bridge.sensor_mtu_floor,41)
        self.assertEqual(bridge._codec('SensorPacket',1,7,1,20,sensor.DUMMY_FIXTURES[0]).version,3)

    async def test_v4_batched_notification_and_source_audit_parser(self):
        bridge=Bridge(BridgeConfig(ca_file='unused',sensor_version=4,expected_device_id=1),clock=lambda:1.)
        bridge.inbox.activate(1)
        bridge.begin_observation(.5)
        packet=sensor_v4.SensorPacket(1,7,1,20,sensor_v4.DUMMY_FIXTURES[0])
        second=sensor_v4.SensorPacket(1,7,2,40,sensor_v4.DUMMY_FIXTURES[0])
        self.assertTrue(bridge.enqueue(1,sensor_v4.encode_packet(packet)+sensor_v4.encode_packet(second)))
        item=await bridge.inbox.get();decoded=bridge._prepare_item(item)
        self.assertEqual(decoded.values[-1],5000)
        self.assertEqual(bridge._prepare_item(await bridge.inbox.get()).seq,2)
        self.assertEqual(bridge.goodput.report(1.5)['packet_bytes'],80)
        self.assertEqual(bridge.packet_size,40);self.assertEqual(bridge.sensor_mtu_floor,43)
        self.assertEqual(bridge._parse_stats(stats(4),expected_device_id=1).boot_id,7)

    async def test_v4_command_channel_checks_transformed_thumb_and_identity(self):
        control=ControlChannel(1,sensor_version=4);control.attach(SimpleNamespace(mtu_size=517),7)
        packet=sensor_v4.SensorPacket(1,7,10,0,sensor_v4.DUMMY_FIXTURES[0])
        async def exchange(request):
            decoded=sensor_v4.decode_packet(request.payload)
            values=transformed_values(decoded.values,sensor_version=4)
            modified=sensor_v4.SensorPacket(1,7,10,0,values)
            return ControlFrame(COMMAND|RESPONSE_FLAG,1,10,payload=sensor_v4.encode_packet(modified))
        control._exchange=exchange
        result=await control.command(packet)
        self.assertEqual(result.version,4);self.assertEqual(result.values[-1],5001)
        control.detach()

    async def test_model_port_converts_v4_values_once(self):
        seen=[]
        class Model:
            def start(self,emit):pass
            def on_sample(self,*args):seen.append(args)
        port=ModelPort(Model(),lambda *args:None,{},physical_converter=sensor_v4.physical_values)
        port.start(asyncio.get_running_loop())
        port.on_sample(1,7,1,20,(10000,0,0,100,0,0,0,0,0,5000,6000,7000))
        self.assertEqual(seen[0][-1],(1.,0.,0.,1.,0.,0.,0.,0.,0.,.5,.6,.7))
        port.close()


@unittest.skipUnless(importlib.util.find_spec('cryptography'),'Temporary TLS test certificates require cryptography')
class TlsTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_laptop_and_server_tls_ingestion_for_v3_and_v4(self):
        from tools.hand_v4_transport_check import run_check
        result=await run_check()
        self.assertEqual(result['status'],'passed')
        self.assertEqual(result['v3_legacy']['features_per_hand'],11)
        self.assertEqual(result['v4_opt_in']['features_per_hand'],12)
        self.assertEqual(result['v4_opt_in']['acked'],2)
        self.assertFalse(result['production_deployment_changed'])


if __name__=='__main__':unittest.main()
