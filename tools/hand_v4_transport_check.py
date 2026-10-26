"""Real loopback TLS check of legacy v3 and opt-in v4 transport; no BLE/FPGA."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile

from common import sensor,sensor_v4
from common.tls import client_context,server_context,TLS_SERVER_NAME
from common.wire import read_frame,write_frame
from laptop.bridge import Bridge,BridgeConfig
from ultra96.protocol import validate_message
from ultra96.server import CommsServer


class ProbeModel:
    name='scripted-v4-transport-probe'
    def __init__(self):self.samples=[]
    def start(self,emit):self.emit=emit
    def on_sample(self,device,boot,seq,uptime,values):
        self.samples.append((device,boot,seq,uptime,tuple(values)))
        self.emit('IDLE',.95,device,boot,seq)  # scripted result, NOT gesture inference


async def probe(version,pki):
    model=ProbeModel()
    service=CommsServer(server_context(pki/'server-cert.pem',pki/'server-key.pem'),
                        ingest_port=0,gateway_port=0,model=model,sensor_version=version)
    bridges=[];phone_writer=None;wrong_writer=None
    await service.start()
    try:
        context=client_context(pki/'ca-cert.pem')
        phone,phone_writer=await asyncio.open_connection('127.0.0.1',service.gateway_port,
            ssl=context,server_hostname=TLS_SERVER_NAME)
        await write_frame(phone_writer,dict(v=1,type='SUBSCRIBE',session_id='ltc-comms'))
        subscribed=await read_frame(phone)
        assert subscribed==dict(v=1,type='SUBSCRIBED',session_id='ltc-comms')
        codec=sensor_v4 if version==4 else sensor
        for device in (1,2):
            bridge=Bridge(BridgeConfig(ca_file=str(pki/'ca-cert.pem'),port=service.ingest_port,
                                       expected_device_id=device,sensor_version=version))
            bridges.append(bridge);bridge.inbox.activate(1)
            values=[10000,-10000,5000,100,-200,300,1000,-2000,3000,4000,5000]
            if version==4:values.append(6000+device*100)
            packet=codec.SensorPacket(device,100+device,1,20,tuple(values))
            assert bridge.enqueue(1,codec.encode_packet(packet))
            await bridge.forward_one()
            assert bridge.metrics.acked==1
            result=await read_frame(phone)
            validate_message(result,'GESTURE_RESULT','ltc-comms',sensor_version=version)
            assert result['device_id']==device and result['gesture']=='IDLE'
        expected=12 if version==4 else 11
        assert all(len(row[-1])==expected for row in model.samples)
        if version==4:
            assert model.samples[0][-1][-1]==.61 and model.samples[1][-1][-1]==.62
        wrong,wrong_writer=await asyncio.open_connection('127.0.0.1',service.ingest_port,
            ssl=context,server_hostname=TLS_SERVER_NAME)
        other=sensor if version==4 else sensor_v4
        await write_frame(wrong_writer,other.SensorPacket(1,999,1,20,other.DUMMY_FIXTURES[0]).to_message('ltc-comms'))
        try:
            await read_frame(wrong,timeout=2.)
        except (asyncio.IncompleteReadError,ConnectionError):pass
        else:raise AssertionError('Wrong packet version was accepted')
        return dict(sensor_version=version,acked=sum(b.metrics.acked for b in bridges),
                    model_samples=len(model.samples),features_per_hand=expected,
                    wrong_version_rejected=True,tls_certificate_and_hostname_verified=True)
    finally:
        for bridge in bridges:await bridge.close_transport()
        for writer in (phone_writer,wrong_writer):
            if writer:
                writer.close()
                try:await writer.wait_closed()
                except (OSError,ConnectionError):pass
        await service.close()


async def run_check():
    try:
        from tools.generate_comms_pki import generate_pki
        import cryptography  # dependency belongs to test PKI generation only
    except ImportError as error:
        raise RuntimeError('This TLS check needs cryptography for temporary test certificates; runtime peers do not.') from error
    with tempfile.TemporaryDirectory(prefix='ltc-hand-v4-tls-') as temp:
        pki=generate_pki(temp)
        legacy=await probe(3,pki);new=await probe(4,pki)
    return dict(status='passed',scope='real localhost TLS ingestion/model-callback/result test with scripted model',
                v3_legacy=legacy,v4_opt_in=new,ble_tested=False,fpga_tested=False,
                phone_game_tested=False,structured_hand_events_forwarded=False,
                production_deployment_changed=False)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.exists():raise FileExistsError('Choose a new output file')
    result=asyncio.run(run_check())
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
