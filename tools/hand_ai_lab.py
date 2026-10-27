"""AI-only offline toolkit: audit, replay, sweep, train experiments and benchmark."""
import argparse
import json
from pathlib import Path
from src.hand_ai_lab.data import write,import_triage,audit_recordings

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    d=sub.add_parser('demo');d.add_argument('--out',required=True)
    a=sub.add_parser('audit');a.add_argument('--input',required=True);a.add_argument('--out',required=True)
    t=sub.add_parser('import-triage');t.add_argument('--input',required=True);t.add_argument('--out',required=True)
    t.add_argument('--person',required=True);t.add_argument('--session',required=True);t.add_argument('--split',choices=('train','validation','test','diagnostic'),default='diagnostic')
    r=sub.add_parser('replay');r.add_argument('--capture',required=True);r.add_argument('--annotations');r.add_argument('--out',required=True)
    r.add_argument('--left-run');r.add_argument('--right-run');r.add_argument('--legacy11',action='store_true')
    r.add_argument('--threshold',type=float,default=.7);r.add_argument('--confirmations',type=int,default=3)
    r.add_argument('--interval',type=float,default=.2);r.add_argument('--timeout',type=float,default=.6)
    s=sub.add_parser('sweep');s.add_argument('--replay',required=True);s.add_argument('--out',required=True)
    c=sub.add_parser('prepare-continuous');c.add_argument('--manifest',required=True);c.add_argument('--device',type=int,choices=(1,2),required=True)
    c.add_argument('--seconds',type=float,choices=(.5,1.,1.5,2.),default=2.);c.add_argument('--stride',type=float,default=.2);c.add_argument('--out',required=True)
    tr=sub.add_parser('train');tr.add_argument('--data',required=True);tr.add_argument('--out',required=True)
    tr.add_argument('--features',choices=('all12','imu_flex2','imu9'),default='all12');tr.add_argument('--seeds',type=int,nargs='+',default=[42,73,101])
    tr.add_argument('--epochs',type=int,default=30);tr.add_argument('--noise-std',type=float,default=0.)
    e=sub.add_parser('evaluate');e.add_argument('--run',required=True);e.add_argument('--data',required=True);e.add_argument('--split',choices=('validation','test'),required=True)
    e.add_argument('--allow-test-use',action='store_true');e.add_argument('--out',required=True)
    e=sub.add_parser('export-native');e.add_argument('--run',required=True);e.add_argument('--out',required=True)
    b=sub.add_parser('benchmark-cpu');b.add_argument('--run',required=True);b.add_argument('--data',required=True);b.add_argument('--out',required=True);b.add_argument('--repeats',type=int,default=50)
    b=sub.add_parser('benchmark-bundle');b.add_argument('--bundle',required=True);b.add_argument('--out',required=True);b.add_argument('--repeats',type=int,default=20)
    b.add_argument('--backend',choices=('numpy','fpga'),default='numpy');b.add_argument('--bit');b.add_argument('--allow-program-fpga',action='store_true');b.add_argument('--allow-synthetic',action='store_true')
    h=sub.add_parser('hls-summary');h.add_argument('--input',required=True);h.add_argument('--out',required=True)
    q=sub.add_parser('precision');q.add_argument('--run',required=True);q.add_argument('--data',required=True);q.add_argument('--out',required=True)
    q.add_argument('--activation-widths',type=int,nargs='+',default=[24,20,16]);q.add_argument('--weight-widths',type=int,nargs='+',default=[16,12,10])
    a=p.parse_args(argv);result=None
    if a.command=='demo':
        from src.hand_ai_lab.demo import run
        full=run(a.out);result={k:full[k] for k in ('status','synthetic','learned_model_accuracy_tested','counts','timeline')}
    elif a.command=='audit':
        result=audit_recordings(a.input);write(a.out,result)
        print(json.dumps(dict(status=result['status'],files=len(result['files']),errors=result['errors'],warnings=result['warnings']),indent=2))
        if result['errors']:raise SystemExit(2)
        return
    elif a.command=='import-triage':result=dict(capture=str(import_triage(a.input,a.out,a.person,a.session,a.split)))
    elif a.command=='replay':
        from src.hand_ai_lab.replay import RunBackend,replay,save_replay
        backend=RunBackend({d:v for d,v in ((1,a.left_run),(2,a.right_run)) if v},a.legacy11)
        report,records=replay(a.capture,backend,set(backend.predictors),a.annotations,a.threshold,a.confirmations,a.interval,a.timeout,seconds=backend.seconds)
        report['models']=backend.provenance;save_replay(a.out,report,records)
        result=dict(counts=report['counts'],metrics_status=report['metrics']['status'],timeline=str((Path(a.out)/'timeline.html').resolve()))
    elif a.command=='sweep':
        from src.hand_ai_lab.replay import sweep
        result=sweep(a.replay,a.out)
    elif a.command=='prepare-continuous':
        from src.hand_ai_lab.training import prepare_continuous
        result=prepare_continuous(a.manifest,a.device,a.seconds,a.stride,a.out)
    elif a.command=='train':
        from src.hand_ai_lab.training import train
        result=train(a.data,a.out,a.features,a.seeds,a.epochs,a.noise_std)
    elif a.command=='evaluate':
        from src.hand_ai_lab.training import evaluate
        report=evaluate(a.run,a.data,a.split,a.out,a.allow_test_use);result=report['metrics']
    elif a.command=='benchmark-cpu':
        from src.hand_ai_lab.benchmark import cpu
        result=cpu(a.run,a.data,a.out,a.repeats)
    elif a.command=='export-native':
        from src.hand_ai_lab.training import export_native
        result=export_native(a.run,a.out)
    elif a.command=='benchmark-bundle':
        from src.hand_ai_lab.benchmark import bundle
        result=bundle(a.bundle,a.out,a.backend,a.bit,a.allow_program_fpga,a.allow_synthetic,a.repeats)
    elif a.command=='hls-summary':
        from src.hand_ai_lab.benchmark import hls_summary
        result=hls_summary(a.input,a.out)
    elif a.command=='precision':
        from src.hand_ai_lab.precision import screen
        full=screen(a.run,a.data,a.out,a.activation_widths,a.weight_widths);result=dict(status=full['status'],candidates=len(full['candidates']),test_used=False)
    print(json.dumps(result,indent=2,allow_nan=False))

if __name__=='__main__':main()
