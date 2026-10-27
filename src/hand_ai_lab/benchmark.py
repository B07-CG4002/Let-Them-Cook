"""Measured CPU/bundle stages, strict numeric comparison and HLS report summaries."""
import json
from pathlib import Path
import time
import xml.etree.ElementTree as ET
import numpy as np
from src.hand_ai_lab.data import sha,write

def stats(values):
    return dict(calls=len(values),mean_ms=float(np.mean(values)),median_ms=float(np.median(values)),p95_ms=float(np.percentile(values,95)),max_ms=float(np.max(values)))

def cpu(run,data,out,repeats=50):
    from src.hand_ai_lab.replay import RunBackend
    from src.hand_ai_lab.training import load_dataset
    if repeats<1 or Path(out).exists():raise ValueError('Need positive repeats and a new output path')
    x,y,meta,source=load_dataset(data);d=meta[0]['device_id'];backend=RunBackend({d:run})
    if backend.seconds!=source['window_seconds']:raise ValueError('Model/data window duration mismatch')
    # Performance samples are training inputs; held-out test data is not consumed.
    indices=[i for i,m in enumerate(meta) if m['split']=='train']
    for _ in range(5):backend.probabilities(d,x[indices[0]])
    times=[]
    for n in range(repeats):
        started=time.perf_counter();backend.probabilities(d,x[indices[n%len(indices)]]);times.append((time.perf_counter()-started)*1000)
    result=dict(scope='CPU normalization/masking/tensor conversion/forward/softmax; model load excluded',
                timing=stats(times),model=backend.provenance,dataset=source,test_data_used=False,fpga_tested=False)
    write(out,result);return result

def bundle(folder,out,backend_name='numpy',bit=None,allow_program=False,synthetic=False,repeats=20):
    from src.hand_runtime import ModelBundle
    from src.hand_backend import NumpyHandBackend,PynqHandBackend,integer_logits
    if repeats<1 or Path(out).exists():raise ValueError('Need positive repeats and a new output path')
    if backend_name not in ('numpy','fpga'):raise ValueError('Invalid backend')
    if backend_name=='fpga' and (not allow_program or not bit):raise ValueError('FPGA mode needs --bit and --allow-program-fpga on an idle isolated board')
    if backend_name=='numpy' and (bit or allow_program):raise ValueError('FPGA options require FPGA backend')
    b=ModelBundle(folder,synthetic);engine=None;calls=[]
    try:
        engine=NumpyHandBackend(b) if backend_name=='numpy' else PynqHandBackend(b,bit)
        # Real raw-unit reference windows are reconstructed from normalization so
        # zero normalized input is valid without consuming final test recordings.
        for mode in ('cold','warm_alternating'):
            if mode=='warm_alternating':
                for d in (1,2):engine.probabilities(d,np.tile(b.models[d]['mean'],(50,1)))
            for n in range(repeats):
                d=n%2+1;window=np.tile(b.models[d]['mean'],(50,1)).astype(np.float32)
                window[-1,11]+=np.float32(.1*b.models[d]['std'][11])
                if backend_name=='fpga' and mode=='cold':engine.loaded[d]=False
                expected=integer_logits(b.encode(d,window),b.models[d]['parameters'])
                started=time.perf_counter();p=engine.probabilities(d,window);elapsed=(time.perf_counter()-started)*1000
                row=dict(mode=mode,device_id=d,total_ms=elapsed,predicted_class=int(p.argmax()))
                if backend_name=='fpga':
                    actual=np.asarray(engine.out).astype(np.int32).copy()
                    row.update(exact_words=bool(np.array_equal(actual,expected)),cache_status=engine.last_status.get(d),stages_ms=engine.last_timings.get(d))
                    if not row['exact_words']:raise ValueError('FPGA words disagree with exact reference; benchmark stopped')
                calls.append(row)
        result=dict(scope='artificial normalized-input numeric/cache performance probe; not gesture accuracy',backend=backend_name,
            synthetic_bundle=b.synthetic,manifest_sha256=sha(Path(folder)/'manifest.json'),
            modes={mode:stats([r['total_ms'] for r in calls if r['mode']==mode]) for mode in ('cold','warm_alternating')},calls=calls,
            fpga_programmed=backend_name=='fpga',artifacts=getattr(engine,'artifacts',None),
            cold_note='Cold mode forces parameter reload in FPGA. NumPy has no hardware cache; both modes are reference timings.')
        write(out,result);return result
    finally:
        if engine:engine.close()

def hls_summary(folder,out):
    """Use machine-readable HLS synthesis XML; no fragile console-time speed claims."""
    folder=Path(folder);entries=[]
    for path in sorted(folder.rglob('*csynth.xml')):
        tree=ET.parse(path).getroot()
        def fields(tag):
            node=tree.find('.//'+tag)
            return {c.tag:c.text for c in node} if node is not None else {}
        entries.append(dict(path=str(path.resolve()),sha256=sha(path),
            timing=fields('SummaryOfTimingAnalysis'),latency=fields('SummaryOfOverallLatency'),resources=fields('Resources'),available=fields('AvailableResources')))
    if not entries:raise ValueError('No *csynth.xml found; synthesis may not have completed')
    result=dict(scope='HLS synthesis estimates, not board latency or implemented timing closure',reports=entries,
                guidance='Compare top-level latency/II, target clock and BRAM/DSP/LUT/FF together. Control-only min latency is not CNN inference time.')
    write(out,result);return result
