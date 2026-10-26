import contextlib
import io
import json
from pathlib import Path
import struct
import socket
import threading
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from tools.hand_hls_prepare import prepare
from tools.hand_worker_demo import generate
from src.hand_runtime import ModelBundle,HandWindow,ActionFilter,HandEngine,CHANNELS
from src.hand_backend import NumpyHandBackend,PynqHandBackend,integer_logits,pointer_offsets
from src.socket_worker_hand import validate_hello,replay,observe_socket,connect_bridge,main


def message(device,seq,boot=1,values=None,uptime=None):
    return dict(type='sample',device_id=device,boot_id=boot,seq=seq%(2**32),
                uptime_ms=(seq*20 if uptime is None else uptime)%(2**32),
                values=[0.]*12 if values is None else values)


def fill(engine,device,start=0,end=100,boot=1):
    for seq in range(start,end+1):engine.add(message(device,seq,boot),seq*.02)


def probabilities(label):
    from src.hand_runtime import LABELS
    result=np.full(7,.01);result[LABELS.index(label)]=.94
    return result


class ScriptBackend:
    def __init__(self):self.calls=[]
    def probabilities(self,device,window):
        self.calls.append(device)
        return probabilities('CHOP' if device==1 else 'PICKUP')


class WindowTests(unittest.TestCase):
    def test_one_hand_forms_window_without_other_hand(self):
        engine=HandEngine(ScriptBackend());fill(engine,2)
        records=engine.process_due(2.)
        self.assertEqual([r['device_id'] for r in records if r['type']=='prediction'],[2])
        self.assertEqual(engine.windows[2].job(2.).window.shape,(50,12))

    def test_duplicate_and_out_of_order_packets_do_not_reset_or_confirm(self):
        engine=HandEngine(ScriptBackend());fill(engine,1)
        generation=engine.windows[1].generation
        self.assertFalse(engine.add(message(1,100),2.))
        self.assertFalse(engine.add(message(1,99),2.01))
        self.assertEqual(engine.windows[1].generation,generation)
        first=engine.process_due(2.)
        self.assertEqual(len([r for r in first if r['type']=='prediction']),1)
        self.assertEqual(engine.process_due(2.25),[])
        self.assertEqual(engine.filters[1].count,1)

    def test_sequence_gap_resets_only_affected_hand(self):
        engine=HandEngine(ScriptBackend());fill(engine,1);fill(engine,2)
        left=engine.windows[1].generation;right=engine.windows[2].generation
        engine.add(message(1,102),2.04)
        self.assertGreater(engine.windows[1].generation,left)
        self.assertEqual(engine.windows[2].generation,right)
        self.assertIsNone(engine.windows[1].job(2.04))
        self.assertIsNotNone(engine.windows[2].job(2.04))

    def test_boot_change_does_not_reset_other_hand(self):
        engine=HandEngine(ScriptBackend());fill(engine,1);fill(engine,2)
        generation=engine.windows[2].generation
        engine.add(message(1,0,boot=999),2.02)
        self.assertIsNone(engine.windows[1].job(2.02))
        self.assertEqual(engine.windows[2].generation,generation)

    def test_uint32_sequence_and_uptime_wrap_are_continuous(self):
        window=HandWindow(1);generation=window.generation
        for i in range(101):
            window.add(message(1,2**32-50+i,uptime=2**32-1000+i*20),i*.02)
        self.assertEqual(window.generation,generation)
        self.assertIsNotNone(window.job(2.))

    def test_angles_interpolate_through_wrap(self):
        window=HandWindow(1)
        for i in range(101):
            values=[0.]*12;values[6]=179 if i<=59 else -179
            window.add(message(1,i,values=values),i*.02)
        self.assertGreater(abs(window.job(2.).window[29,6]),170)

    def test_stale_or_11_feature_data_is_not_inferred(self):
        engine=HandEngine(ScriptBackend());fill(engine,1)
        self.assertIsNone(engine.windows[1].job(2.51))
        with self.assertRaises(ValueError):engine.add(message(2,0,values=[0.]*11),0.)


class ConfirmationTests(unittest.TestCase):
    def test_strict_threshold_and_three_predictions(self):
        f=ActionFilter()
        self.assertEqual(f.update('PICKUP',.70,0.),[])
        self.assertEqual(f.update('PICKUP',.95,.2),[])
        self.assertEqual(f.update('PICKUP',.95,.4),[])
        events=f.update('PICKUP',.95,.6)
        self.assertEqual([e['type'] for e in events],['action'])

    def test_one_shot_requires_confirmed_rearm_not_confidence_flicker(self):
        f=ActionFilter();events=[]
        for i in range(6):events+=f.update('PICKUP',.95,i*.2)
        events+=f.update('IDLE',.2,1.2)
        for i in range(3):events+=f.update('PICKUP',.95,1.4+i*.2)
        self.assertEqual(sum(e['type']=='action' for e in events),1)
        self.assertFalse(any(e.get('gesture')=='IDLE' for e in events))
        for i in range(3):events+=f.update('IDLE',.95,2.+i*.2)
        for i in range(3):events+=f.update('PICKUP',.95,2.6+i*.2)
        self.assertEqual(sum(e['type']=='action' for e in events),2)

    def test_ongoing_state_expires_without_inventing_idle(self):
        f=ActionFilter()
        for i in range(3):events=f.update('CHOP',.95,i*.2)
        self.assertEqual(events[0]['type'],'state_start')
        self.assertEqual(f.update('CHOP',.95,.6),[])
        f.update('PICKUP',.95,.8);f.update('DROP',.95,1.)
        events=f.tick(1.21)
        self.assertEqual(events,[dict(type='state_end',gesture='CHOP',reason='prediction_timeout')])

    def test_hands_have_independent_confirmation_and_latches(self):
        engine=HandEngine(ScriptBackend());fill(engine,1);fill(engine,2)
        events=[]
        for end in (100,110,120,130):
            if end>100:
                fill(engine,1,end-9,end);fill(engine,2,end-9,end)
            events+=engine.process_due(end*.02)
        accepted=[e for e in events if e['type'] in ('action','state_start')]
        self.assertEqual([(e['device_id'],e['type'],e['gesture']) for e in accepted],
                         [(1,'state_start','CHOP'),(2,'action','PICKUP')])

    def test_source_reset_during_inference_discards_result(self):
        backend=ScriptBackend();engine=HandEngine(backend);fill(engine,1)
        def reboot(device,window):
            engine.add(message(1,0,boot=999),2.01)
            return probabilities('PICKUP')
        backend.probabilities=reboot
        events=engine.process_due(2.)
        self.assertEqual([e['type'] for e in events],['stale_suppressed'])
        self.assertEqual(engine.filters[1].count,0)

    def test_slow_result_is_suppressed_without_advancing_confirmation(self):
        engine=HandEngine(ScriptBackend());fill(engine,1)
        moments=iter((2.,2.6,2.6))
        events=engine.process_due(2.,clock=lambda:next(moments))
        self.assertEqual([e['type'] for e in events],['stale_suppressed'])
        self.assertEqual(engine.filters[1].count,0)


class FakeBuffer(np.ndarray):
    def __new__(cls,shape,dtype,address):
        obj=np.zeros(shape,dtype=dtype).view(cls)
        obj.device_address=address;obj.flushes=0;obj.invalidates=0;obj.freed=False
        return obj
    def flush(self):self.flushes+=1
    def invalidate(self):self.invalidates+=1
    def freebuffer(self):self.freed=True


class FakeIP:
    def __init__(self,buffers):
        self.buffers=buffers;self.registers={0x10:4};self.cache={};self.force_status=None;self.hang=False
    def write(self,offset,value):
        self.registers[offset]=value
        if offset==0x10 and value==1:
            if self.hang:return
            def pointer(offset):return self.buffers[self.registers[offset]+(self.registers[offset+4]<<32)]
            slot,model,command=(self.registers[n] for n in (0x40,0x44,0x48))
            loaded=command==1 or slot not in self.cache or self.cache[slot][0]!=model
            if loaded:self.cache[slot]=(model,pointer(0x28).copy())
            pointer(0x30)[:]=integer_logits(pointer(0x20),self.cache[slot][1])
            self.registers[0x4c]=int(loaded) if self.force_status is None else self.force_status
            self.registers[0x10]=6
    def read(self,offset):return self.registers.get(offset,0)


class BundleBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.folder=Path(cls.temp.name)/'bundle'
        with contextlib.redirect_stdout(io.StringIO()):prepare(cls.folder,synthetic=True)
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def bundle(self):return ModelBundle(self.folder,allow_synthetic=True)

    def fake_backend(self,timeout=.1):
        self.buffers={};self.ip=FakeIP(self.buffers)
        def allocate(shape,dtype):
            address=(1<<33)+0x1000*(len(self.buffers)+1)
            value=FakeBuffer(shape,dtype,address);self.buffers[address]=value;return value
        registers={name:dict(address_offset=offset,size=size) for name,offset,size in (
            ('CTRL',0x10,32),('input_words',0x20,64),('parameter_words',0x28,64),('logit_words',0x30,64),
            ('cache_slot',0x40,32),('model_id',0x44,32),('cache_command',0x48,32),('ap_return',0x4c,32))}
        name='gesture_cnn_hand_u2_cache_0'
        class Overlay:pass
        overlay=Overlay();overlay.ip_dict={name:{'registers':registers}};setattr(overlay,name,self.ip)
        bit=self.folder/'fake.bit';bit.write_bytes(b'FAKE TEST ONLY')
        bit.with_suffix('.hwh').write_bytes(b'FAKE TEST ONLY')
        return PynqHandBackend(self.bundle(),bit,timeout=timeout,
                               overlay_factory=lambda path:overlay,allocator=allocate)

    def test_synthetic_export_requires_explicit_opt_in(self):
        with self.assertRaisesRegex(ValueError,'allow-synthetic'):ModelBundle(self.folder)

    def test_changed_parameter_hash_is_refused(self):
        path=self.folder/'left/parameter_words.bin';original=path.read_bytes()
        try:
            path.write_bytes(original[:-1]+bytes([original[-1]^1]))
            with self.assertRaisesRegex(ValueError,'hash changed'):self.bundle()
        finally:path.write_bytes(original)

    def test_integer_reference_matches_all_hls_golden_words(self):
        binary=(self.folder/'vectors/hand_test.bin').read_bytes();header=struct.unpack('<12I',binary[:48])
        bundle=self.bundle();offset=48+2*52967*4
        for i in range(header[2]):
            slot=struct.unpack_from('<I',binary,offset)[0];offset+=4
            inputs=np.frombuffer(binary,dtype='<i4',count=600,offset=offset);offset+=2400
            expected=np.frombuffer(binary,dtype='<i4',count=7,offset=offset);offset+=56
            np.testing.assert_array_equal(integer_logits(inputs,bundle.models[slot+1]['parameters']),expected)
        self.assertEqual(offset,len(binary))

    def test_normalization_is_selected_by_hand_and_checked_before_encoding(self):
        bundle=self.bundle()
        bundle.models[1]['mean']=np.ones(12,dtype=np.float32)
        bundle.models[1]['std']=np.full(12,2.,dtype=np.float32)
        np.testing.assert_array_equal(bundle.encode(1,np.ones((50,12))),np.zeros(600))
        np.testing.assert_array_equal(bundle.encode(2,np.ones((50,12))),np.full(600,1<<18))
        with self.assertRaisesRegex(ValueError,'no silent saturation'):
            bundle.encode(2,np.full((50,12),1e20))

    def test_dynamic_register_offsets_64_bit_pointers_and_cache_switching(self):
        backend=self.fake_backend();x=np.zeros(600,dtype=np.int32)
        for device,status in ((1,1),(2,1),(1,0),(2,0)):
            expected=integer_logits(x,backend.bundle.models[device]['parameters'])
            np.testing.assert_array_equal(backend.run_words(device,x),expected)
            self.assertEqual(backend.last_status[device],status)
        self.assertEqual(backend.last_status,{1:0,2:0})
        self.assertEqual([backend.params[d].flushes for d in (1,2)],[1,1])
        self.assertEqual(backend.out.invalidates,4)
        self.assertEqual(backend.out.flushes,0)
        backend.close();self.assertTrue(all(b.freed for b in self.buffers.values()))
        with self.assertRaisesRegex(RuntimeError,'closed'):backend.run_words(1,x)

    def test_invalid_hardware_status_poisoned_and_busy_timeout_retains_buffers(self):
        backend=self.fake_backend();self.ip.force_status=2
        with self.assertRaisesRegex(RuntimeError,'cache return'):backend.run_words(1,np.zeros(600,dtype=np.int32))
        with self.assertRaisesRegex(RuntimeError,'poisoned'):backend.run_words(1,np.zeros(600,dtype=np.int32))
        backend.close()
        backend=self.fake_backend(timeout=.001);self.ip.hang=True
        with self.assertRaises(TimeoutError):backend.run_words(1,np.zeros(600,dtype=np.int32))
        with self.assertRaisesRegex(RuntimeError,'buffers retained'):backend.close()
        self.assertFalse(any(b.freed for b in self.buffers.values()))
        self.ip.registers[0x10]=4;backend.close()

    def test_mocked_fpga_probabilities_match_reference_and_report_stage_timings(self):
        backend=self.fake_backend();window=np.zeros((50,12),dtype=np.float32)
        expected=NumpyHandBackend(backend.bundle).probabilities(1,window)
        np.testing.assert_allclose(backend.probabilities(1,window),expected,rtol=0,atol=0)
        self.assertEqual(set(backend.last_timings[1]),
            {'input_setup_ms','start_poll_ms','output_read_ms','input_encode_ms','softmax_ms'})
        backend.close()

    def test_pointer_split_forms_and_missing_registers(self):
        self.assertEqual(pointer_offsets({'x_1':dict(size=32,address_offset=24),
                                         'x_2':dict(size=32,address_offset=28)},'x'),(24,28))
        with self.assertRaises(ValueError):pointer_offsets({},'x')

    def test_replay_routes_both_hands_and_preserves_all_raw_predictions(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'replay.jsonl'
            with contextlib.redirect_stdout(io.StringIO()):generate(source)
            records=[]
            with patch('src.socket_worker_hand.socket',SimpleNamespace()):
                replay(source,HandEngine(NumpyHandBackend(self.bundle())),records.append)
            predictions=[r for r in records if r['type']=='prediction']
            self.assertEqual(len(predictions),42)
            self.assertEqual(sum(r['device_id']==1 for r in predictions),21)
            self.assertEqual(sum(r['device_id']==2 for r in predictions),21)
            self.assertFalse(any(r['type'] in ('action','state_start','idle') for r in records))
            self.assertTrue(all(not r['above_threshold'] for r in predictions))

    def test_11_channel_bridge_hello_fails_explicitly(self):
        validate_hello(dict(type='hello',format=1,channels=CHANNELS))
        with self.assertRaisesRegex(ValueError,'eleven-channel'):
            validate_hello(dict(type='hello',format=1,channels=CHANNELS[:-1]))


class SocketTests(unittest.TestCase):
    def connection(self):
        client,peer=socket.socketpair()
        client.settimeout(1);peer.settimeout(2)
        self.addCleanup(client.close);self.addCleanup(peer.close)
        class Connected:
            def connect(self,path):pass
            def __enter__(self):return self
            def __exit__(self,*args):client.close()
            def __getattr__(self,name):return getattr(client,name)
        return Connected(),peer

    def without_unix(self):
        # Reproduce the Windows Python API: deliberately no AF_UNIX attribute.
        return SimpleNamespace(timeout=socket.timeout,SHUT_RDWR=socket.SHUT_RDWR)

    def test_old_hello_is_refused_before_backend_or_overlay_initialization(self):
        client,peer=self.connection();engine=HandEngine(None);initialized=[]
        peer.sendall((json.dumps(dict(type='hello',format=1,channels=CHANNELS[:-1]))+'\n').encode())
        peer.shutdown(socket.SHUT_WR)
        try:
            with patch('src.socket_worker_hand.socket',self.without_unix()):
                with self.assertRaisesRegex(ValueError,'eleven-channel'):
                    observe_socket('fixture',engine,lambda record:None,lambda:initialized.append(True),
                                   connection_factory=lambda path:client)
            self.assertEqual(initialized,[])
        finally:peer.close()

    def test_receiver_feeds_both_hands_and_sends_no_game_commands(self):
        client,peer=self.connection();backend=ScriptBackend();engine=HandEngine(backend)
        events=[];done=threading.Event();sent=[];sender_errors=[]
        def record(event):
            events.append(event)
            if sum(e['type']=='prediction' for e in events)>=2:done.set()
        lines=[dict(type='hello',format=1,channels=CHANNELS)]
        for seq in range(101):
            for device in (1,2):lines.append(message(device,seq))
        def sender():
            try:
                peer.sendall(('\n'.join(json.dumps(line) for line in lines)+'\n').encode())
                if not done.wait(2):raise TimeoutError('Worker did not process both hands')
                peer.shutdown(socket.SHUT_WR)
                sent.append(peer.recv(4096))
            except Exception as error:sender_errors.append(error)
            finally:peer.close()
        thread=threading.Thread(target=sender);thread.start()
        try:
            with patch('src.socket_worker_hand.socket',self.without_unix()):
                observe_socket('fixture',engine,record,connection_factory=lambda path:client)
        finally:
            done.set()
            try:client.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(sender_errors,[])
        self.assertEqual({e['device_id'] for e in events if e['type']=='prediction'},{1,2})
        self.assertEqual(sent,[b''])

    def test_real_socket_mode_without_af_unix_gives_clear_error(self):
        with patch('src.socket_worker_hand.socket',self.without_unix()):
            with self.assertRaisesRegex(RuntimeError,'Use --replay on Windows'):
                connect_bridge('fixture')

    def test_cli_rejects_unsupported_socket_before_bundle_or_overlay(self):
        with patch('src.socket_worker_hand.socket',self.without_unix()), \
             patch('src.socket_worker_hand.ModelBundle') as bundle, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main(['--bundle','unused','--socket','fixture','--out','unused.jsonl'])
            self.assertEqual(error.exception.code,2)
            bundle.assert_not_called()


if __name__=='__main__':unittest.main()
