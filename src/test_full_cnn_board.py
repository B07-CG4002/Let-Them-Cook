"""Compare new full-network FPGA logits with the exact integer reference."""
import argparse
import json
from pathlib import Path
import numpy as np
from src.full_cnn_backend import FullCNNPredictor

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--weights',required=True);p.add_argument('--vectors',required=True);p.add_argument('--bit',required=True)
    p.add_argument('--ip-name',default='gesture_cnn_full_0');p.add_argument('--report',default='full_cnn_board_report_20261020.json')
    a=p.parse_args();path=Path(a.report)
    if path.exists():raise FileExistsError('Choose a new report path')
    with np.load(a.vectors,allow_pickle=False) as z:
        inputs=np.array(z['input_words']);expected=np.array(z['expected_words']);floats=np.array(z['float_logits']);labels=np.array(z['labels']);names=np.array(z['class_names']).astype(str)
    backend=FullCNNPredictor(a.weights,a.bit,ip_name=a.ip_name)
    actual=[];times=[]
    try:
        for n,words in enumerate(inputs):
            output=backend.run_words(words);actual.append(output);times.append(backend.last_call_seconds)
            print(f'sample {n:02d}: true={names[labels[n]]}, FPGA={names[output.argmax()]}, software={names[floats[n].argmax()]}, exact_words={np.array_equal(output,expected[n])}')
    finally:backend.close()
    actual=np.array(actual);pred=actual.argmax(1)
    report=dict(samples=len(inputs),exact_word_mismatches=int(np.count_nonzero(actual!=expected)),
        class_agreement=int((pred==floats.argmax(1)).sum()),correct=int((pred==labels).sum()),
        max_logit_error=float(np.max(np.abs(actual/2**18-floats))),
        mean_call_ms=float(np.mean(times)*1000),max_call_ms=float(np.max(times)*1000),
        timing_scope='AXI start/poll/output invalidate/copy; excludes input packing/flush and model/overlay loading')
    path.write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report,indent=2))
    if report['exact_word_mismatches'] or report['class_agreement']!=len(inputs):raise SystemExit('FAIL')
    print('PASS: full-network FPGA matches exact reference and software predictions')
if __name__=='__main__':main()
