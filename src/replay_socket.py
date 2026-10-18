"""Local Linux/Ultra96 test server: replay one paired CSV to the worker.
NOT Jie Yan's production server. Sends one recording repeatedly for window warmup.
"""
import argparse
import csv
import json
from pathlib import Path
import socket
import threading
import time
from src.ai_common import COLUMNS
from src.socket_worker import CHANNELS
p=argparse.ArgumentParser(description=__doc__); p.add_argument('--csv',required=True)
p.add_argument('--socket',default='/tmp/cg4002-ai-replay-20261018.sock'); a=p.parse_args()
path=Path(a.socket)
if path.exists(): raise FileExistsError('Refusing to remove an existing socket; choose another --socket')
with open(a.csv,newline='',encoding='utf-8-sig') as f: rows=list(csv.DictReader(f))
try:
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as server:
        server.bind(a.socket); server.listen(1); print('Waiting for worker:',a.socket,flush=True)
        client,_=server.accept()
        with client:
            client.sendall((json.dumps(dict(type='hello',format=1,channels=CHANNELS))+'\n').encode())
            def receive():
                with client.makefile('r') as f:
                    for line in f: print('RESULT:',line.strip(),flush=True)
            threading.Thread(target=receive,daemon=True).start()
            boot=int(time.time())&0xffffffff
            for seq in range(500):
                row=rows[seq%len(rows)]
                for device,offset in [(1,0),(2,11)]:
                    message=dict(type='sample',device_id=device,boot_id=boot,seq=seq,uptime_ms=seq*20,
                                 values=[float(row[c]) for c in COLUMNS[offset:offset+11]])
                    client.sendall((json.dumps(message)+'\n').encode())
                time.sleep(.02)
            time.sleep(1)
finally:
    if path.exists(): path.unlink()
