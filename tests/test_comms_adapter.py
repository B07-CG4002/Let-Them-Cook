"""Run from repository root: python -m unittest discover -s tests -p test_comms_adapter.py -v"""
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from src.comms_adapter import Adapter,CHANNELS

class AdapterTests(unittest.TestCase):
    def test_disconnected_and_bounded_queue(self):
        a=Adapter(capacity=2)
        a.on_sample(1,7,1,20,[0]*11)
        self.assertEqual(a.metrics['disconnected_dropped'],1)
        a.client=object()
        for seq in range(3): a.on_sample(1,7,seq,seq*20,[0]*11)
        self.assertEqual(len(a.pending),2); self.assertEqual(a.metrics['queue_dropped'],1)
        self.assertEqual(a._next_sample()['seq'],1)

    def test_results_require_known_unique_identity(self):
        a=Adapter(); delivered=[]; a.emit=lambda *v:delivered.append(v)
        message=dict(type='result',gesture='CHOP',confidence=.9,device_id=1,boot_id=7,seq=42)
        a._handle_result(message); self.assertFalse(delivered)
        a.sent[(1,7,42)]=False
        a._handle_result(message); a._handle_result(message)
        self.assertEqual(len(delivered),1)
        self.assertEqual(delivered[0],('CHOP',.9,1,7,42))

    def test_reject_stale_queue(self):
        a=Adapter();a.pending.append((time.monotonic()-1,dict(type='sample')))
        self.assertIsNone(a._next_sample());self.assertEqual(a.metrics['expired'],1)

    def test_socket_roundtrip(self):
        try: probe=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);probe.close()
        except (AttributeError,OSError): self.skipTest('Unix sockets unavailable in this environment')
        with tempfile.TemporaryDirectory() as temp:
            received=[];event=threading.Event()
            def emit(*values): received.append(values);event.set();return True
            a=Adapter(str(Path(temp)/'ai.sock'));a.start(emit)
            try:
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as worker:
                    worker.settimeout(2);worker.connect(str(a.path))
                    f=worker.makefile('rb')
                    self.assertEqual(json.loads(f.readline())['channels'],CHANNELS)
                    # Hello is sent before the adapter publishes the active client.
                    deadline=time.monotonic()+2
                    while a.client is None and time.monotonic()<deadline: time.sleep(.001)
                    a.on_sample(1,7,42,840,[0]*11)
                    self.assertEqual(json.loads(f.readline())['seq'],42)
                    worker.sendall((json.dumps(dict(type='result',gesture='CHOP',confidence=.9,device_id=1,boot_id=7,seq=42))+'\n').encode())
                    self.assertTrue(event.wait(2));self.assertEqual(received[0],('CHOP',.9,1,7,42));f.close()
            finally: a.close()
            self.assertFalse(a.path.exists())

if __name__=='__main__': unittest.main()
