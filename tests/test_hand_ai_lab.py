import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np

from src.hand_ai_flex3.core import CHANNELS,LABELS,CONFIG
from src.hand_ai_lab.data import write,sha,load_capture,load_annotations,import_triage,audit_recordings
from src.hand_ai_lab.demo import make_capture,ScriptedBackend,run as demo
from src.hand_ai_lab.replay import replay,filter_predictions,ResearchWindow,save_replay,sweep
from src.hand_ai_lab.metrics import score,training_label
from src.hand_ai_lab.training import validate_arrays,load_dataset,prepare_continuous,fit_normalizer
from src.hand_ai_lab.benchmark import bundle,hls_summary

def dump_capture(path,samples,split='validation'):
    meta=dict(type='capture',format=1,channels=CHANNELS,person='P01',session='s_'+split,split=split)
    with Path(path).open('w',encoding='utf-8') as f:
        for r in [meta]+samples:f.write(json.dumps(r)+'\n')
    return meta
def samples(n=201):
    return [dict(type='sample',device_id=d,boot_id=100+d,seq=i,uptime_ms=i*20,elapsed_s=i/50,values=[0.]*12) for i in range(n) for d in (1,2)]
def annotation(path,capture,meta,segments):
    write(path,dict(format=1,capture_sha256=sha(capture),**{k:meta[k] for k in ('person','session','split')},reviewed=True,segments=segments))
def make_dataset(folder):
    folder=Path(folder);folder.mkdir();rng=np.random.default_rng(9);x=[];y=[];meta=[]
    for split in ('train','validation','test'):
        for i,label in enumerate(LABELS):
            v=rng.normal(i,.1,(50,12)).astype(np.float32);x.append(v);y.append(i)
            meta.append(dict(person='P01',session=split,split=split,device_id=2,gesture=label,input_sha256=hashlib.sha256(v.tobytes()).hexdigest()))
    np.save(folder/'X.npy',np.array(x));np.save(folder/'y.npy',np.array(y));write(folder/'metadata.json',meta)
    write(folder/'lab_data.json',dict(format=1,window_seconds=2.,features=CHANNELS,class_names=LABELS,
        sha256={n:sha(folder/n) for n in ('X.npy','y.npy','metadata.json')}))
    return folder

class ContractTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_capture_rejects_wrong_channel_order(self):
        p=self.root/'c.jsonl';dump_capture(p,samples(2));text=p.read_text().replace('"flex2", "flex3"','"flex3", "flex2"');p.write_text(text)
        with self.assertRaises(ValueError):load_capture(p)
    def test_capture_rejects_backwards_arrival_and_nonfinite(self):
        for mutate in (lambda r:r.update(elapsed_s=-1),lambda r:r['values'].__setitem__(0,float('nan'))):
            rows=samples(2);mutate(rows[-1]);p=self.root/'c.jsonl';dump_capture(p,rows)
            with self.assertRaises(ValueError):load_capture(p)
    def test_annotations_require_review_hash_and_no_overlap(self):
        p=self.root/'c.jsonl';meta=dump_capture(p,samples());a=self.root/'a.json'
        s=[dict(id='a',device_id=2,start_s=0.,end_s=3.,gesture='CHOP'),dict(id='b',device_id=2,start_s=2.,end_s=4.,gesture='DROP')]
        annotation(a,p,meta,s)
        with self.assertRaises(ValueError):load_annotations(a,p,meta,samples())
        value=json.loads(a.read_text());value['segments']=[];value['reviewed']=False;a.write_text(json.dumps(value))
        with self.assertRaises(ValueError):load_annotations(a,p,meta,samples())
        value['reviewed']=True;value['capture_sha256']='0'*64;a.write_text(json.dumps(value))
        with self.assertRaises(ValueError):load_annotations(a,p,meta,samples())
    def test_triage_import_retains_all_rows_identities_and_raw_thumb(self):
        raw=self.root/'raw';raw.mkdir()
        for d,name in ((1,'left'),(2,'right')):
            with (raw/f'{name}_raw.csv').open('w',newline='') as f:
                w=csv.writer(f);w.writerow(['callback_elapsed_s','consume_elapsed_s','device_id','boot_id','seq','uptime_ms',*CHANNELS])
                for i in range(4):w.writerow([i*.02,i*.02+.01,d,101+d,i,i*20,*([.6]*12)])
        c=import_triage(raw,self.root/'import','P01','triage','diagnostic');meta,rows=load_capture(c)
        self.assertEqual(len(rows),8);self.assertEqual(rows[0]['values'][-1],.6)
        self.assertFalse(json.loads((c.parent/'annotations_template.json').read_text())['reviewed'])
    def test_dataset_tampering_and_session_leak_refused(self):
        d=make_dataset(self.root/'dataset');x,y,meta,c=load_dataset(d);meta[-1]['session']='train'
        with self.assertRaises(ValueError):validate_arrays(x,y,meta)
        x[0,0,0]+=1;np.save(d/'X.npy',x)
        with self.assertRaises(ValueError):load_dataset(d)
    def test_normalizer_uses_only_supplied_training_rows(self):
        x=np.zeros((2,50,12),np.float32);x[1]=100
        mean,std=fit_normalizer(x[:1]);self.assertTrue(np.all(mean==0));self.assertTrue(np.all(std==1))

class MetricTests(unittest.TestCase):
    def test_complete_pulse_requires_reviewed_context_and_rejects_partial(self):
        segs=[dict(id='b',device_id=2,start_s=0.,end_s=1.,gesture='BACKGROUND'),dict(id='p',device_id=2,start_s=1.,end_s=1.5,gesture='THROW'),dict(id='i',device_id=2,start_s=1.5,end_s=3.,gesture='IDLE')]
        self.assertEqual(training_label(segs,2,.5,2.5)['gesture'],'THROW')
        self.assertIsNone(training_label(segs,2,1.2,2.5))
        segs[0]['gesture']='UNKNOWN';self.assertIsNone(training_label(segs,2,.5,2.5))
    def test_two_actions_in_window_refused(self):
        segs=[dict(id=str(i),device_id=2,start_s=float(i),end_s=float(i+1),gesture='PICKUP' if i==0 else 'DROP') for i in range(2)]
        self.assertIsNone(training_label(segs,2,0.,2.))
    def test_duplicate_acceptance_is_false_positive_one_match_per_instance(self):
        truth=dict(segments=[dict(id='p',device_id=2,start_s=0.,end_s=2.,gesture='PICKUP')])
        records=[dict(type='action',gesture='PICKUP',device_id=2,elapsed_s=t) for t in (.5,1.)]
        m=score(records,truth)['action_events'];self.assertEqual((m['true_positive'],m['false_positive'],m['duplicate_acceptances']),(1,1,1))
    def test_unknown_and_unannotated_are_not_idle_or_false_positive(self):
        truth=dict(segments=[dict(id='u',device_id=2,start_s=0.,end_s=2.,gesture='UNKNOWN')])
        records=[dict(type='action',gesture='PICKUP',device_id=2,elapsed_s=t) for t in (1.,3.)]
        m=score(records,truth)['action_events'];self.assertEqual(m['false_positive'],0);self.assertEqual(m['unscored_acceptances'],2)
    def test_background_activation_false_positive_and_hand_minutes(self):
        truth=dict(segments=[dict(id=str(d),device_id=d,start_s=0.,end_s=30.,gesture='BACKGROUND') for d in (1,2)])
        m=score([dict(type='state_start',gesture='CHOP',device_id=1,elapsed_s=1.)],truth)['action_events']
        self.assertEqual(m['false_acceptances_per_hand_minute'],1.)
    def test_unmodelled_hand_is_not_scored_as_missing_ai(self):
        # Scoring explicitly supplied annotations still reports misses. Replay
        # must scope annotation scoring to modelled hands (covered below).
        truth=dict(segments=[dict(id='r',device_id=2,start_s=0.,end_s=1.,gesture='DROP')])
        self.assertEqual(score([],truth)['action_events']['false_negative'],1)

class ReplayTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_demo_is_deterministic_and_preserves_independent_actions(self):
        capture,ann=make_capture(self.root/'input')
        a,ar=replay(capture,ScriptedBackend(),{1,2},ann);b,br=replay(capture,ScriptedBackend(),{1,2},ann)
        strip=lambda r:[{k:v for k,v in x.items() if k!='inference_ms'} for x in r]
        self.assertEqual(strip(ar),strip(br));self.assertEqual(a['counts']['action'],6)
        self.assertEqual(a['metrics']['action_events']['false_negative'],0)
        trace=filter_predictions(ar,a['settings'],a['duration_s'],.7,3,.6)
        accepted=lambda r:[(e['type'],e['device_id'],e['gesture'],round(e['elapsed_s'],5)) for e in r if e['type'] in ('action','state_start','state_end','idle')]
        self.assertEqual(accepted(ar),accepted(trace))
    def test_sequence_gap_resets_only_one_hand(self):
        rows=samples(201)
        for r in rows:
            if r['device_id']==2 and r['seq']>=150:r['seq']+=1
        p=self.root/'c.jsonl';dump_capture(p,rows);report,records=replay(p,ScriptedBackend(),{1,2})
        reset=[r for r in records if r['type']=='source_reset'];self.assertEqual([r['device_id'] for r in reset],[2])
    def test_exact_duplicate_does_not_reconfirm_and_changed_bytes_refused(self):
        rows=samples(201);rows.insert(101,dict(rows[100]));p=self.root/'c.jsonl';dump_capture(p,rows)
        report,records=replay(p,ScriptedBackend(),{1,2});self.assertEqual(report['health']['exact_duplicates'],1)
        rows[101]['values']=[1.]*12;dump_capture(p,rows)
        with self.assertRaises(ValueError):replay(p,ScriptedBackend(),{1,2})
    def test_uint32_wrap_continuous_and_same_source_timestamps_preserved(self):
        rows=samples(201)
        for r in rows:r['seq']=(r['seq']+2**32-50)%2**32;r['uptime_ms']=(r['uptime_ms']+2**32-1000)%2**32
        p=self.root/'c.jsonl';dump_capture(p,rows);report,records=replay(p,ScriptedBackend(),{1,2})
        self.assertEqual(report['counts']['source_reset'],0);self.assertGreater(report['counts']['prediction'],0)
    def test_unsupported_research_duration_refused(self):
        with self.assertRaises(ValueError):ResearchWindow(2,3.)
    def test_threshold_exact_point_seven_is_suppressed(self):
        class Boundary:
            def probabilities(self,d,w):return np.array([.7,.05,.05,.05,.05,.05,.05])
        p=self.root/'c.jsonl';dump_capture(p,samples());report,records=replay(p,Boundary(),{2})
        self.assertEqual(report['counts']['action'],0)
    def test_right_only_replay_does_not_score_left_as_missed(self):
        c,a=make_capture(self.root/'input');r,records=replay(c,ScriptedBackend(),{2},a)
        self.assertEqual(r['metrics']['action_events']['false_negative'],0)
        self.assertTrue(all(x['event']['device_id']==2 for x in r['metrics']['action_events']['matches']))
    def test_test_split_cannot_select_confirmation_parameters(self):
        capture,ann=make_capture(self.root/'input',split='test');report,records=replay(capture,ScriptedBackend(),{1,2},ann)
        save_replay(self.root/'replay',report,records)
        with self.assertRaises(ValueError):sweep(self.root/'replay',self.root/'selection')
        self.assertFalse((self.root/'selection').exists())
    def test_timeline_escapes_script_injection_and_refuses_output_overwrite(self):
        c,a=make_capture(self.root/'input');r,records=replay(c,ScriptedBackend(),{1,2},a);r['capture_meta']['person']='</script><script>alert(1)</script>'
        save_replay(self.root/'out',r,records);text=(self.root/'out/timeline.html').read_text()
        self.assertNotIn('</script><script>alert(1)',text)
        with self.assertRaises(FileExistsError):save_replay(self.root/'out',r,records)
    def test_prepare_continuous_splits_before_windowing_and_all_classes_retained(self):
        pairs=[]
        for split,seed in (('train',1),('validation',2),('test',3)):
            c,a=make_capture(self.root/split,split,seed)
            truth=json.loads(a.read_text());short=[]
            for s in truth['segments']:
                if s['gesture'] in ('PICKUP','DROP','THROW'):
                    short.extend([dict(s,id=s['id']+'-before',gesture='BACKGROUND',end_s=s['start_s']+1.),
                                  dict(s,start_s=s['start_s']+1.,end_s=s['start_s']+1.4),
                                  dict(s,id=s['id']+'-after',gesture='BACKGROUND',start_s=s['start_s']+1.4)])
                else:short.append(s)
            truth['segments']=short;a.write_text(json.dumps(truth));pairs.append(dict(capture=str(c),annotations=str(a)))
        manifest=self.root/'collection.json';write(manifest,pairs)
        result=prepare_continuous(manifest,2,1.,.5,self.root/'prepared')
        x,y,meta,source=load_dataset(self.root/'prepared');self.assertEqual(source['window_seconds'],1.)
        self.assertTrue(all(n for row in result['counts'].values() for n in row.values()))
        self.assertEqual(x.shape[1:],(50,12))
    def test_prepare_continuous_rejects_same_session_across_splits(self):
        pairs=[]
        for split,seed in (('train',1),('validation',2),('test',3)):
            c,a=make_capture(self.root/split,split,seed);lines=c.read_text().splitlines();meta=json.loads(lines[0]);meta['session']='same';lines[0]=json.dumps(meta);c.write_text('\n'.join(lines)+'\n')
            truth=json.loads(a.read_text());truth['session']='same';truth['capture_sha256']=sha(c);a.write_text(json.dumps(truth));pairs.append(dict(capture=str(c),annotations=str(a)))
        manifest=self.root/'collection.json';write(manifest,pairs)
        with self.assertRaisesRegex(ValueError,'Session leakage'):prepare_continuous(manifest,2,2.,.5,self.root/'bad')
    def test_bundle_programming_requires_explicit_opt_in_before_loading(self):
        with self.assertRaises(ValueError):bundle('missing',self.root/'result.json',backend_name='fpga')
    def test_hls_xml_summary_does_not_claim_board_time(self):
        (self.root/'top_csynth.xml').write_text('<r><SummaryOfOverallLatency><Best-caseLatency>100</Best-caseLatency></SummaryOfOverallLatency><Resources><DSP>16</DSP></Resources></r>')
        r=hls_summary(self.root,self.root/'hls.json');self.assertEqual(r['reports'][0]['resources']['DSP'],'16')
        self.assertIn('not board',r['scope'])

class AuditTests(unittest.TestCase):
    def test_audit_checks_hashes_sequences_and_never_mutates(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d);p=folder/'PICKUP.csv'
            with p.open('w',newline='') as f:
                w=csv.writer(f);w.writerow(['elapsed_s','device_id','boot_id','seq','uptime_ms',*CHANNELS])
                for n in range(101):w.writerow([n*.02,2,9,n,n*20,*([n*.001]*12)])
            write(p.with_suffix('.json'),dict(person='P01',session='pilot',split='train',recording_contract=CONFIG['version'],device_id=2,gesture='PICKUP',trial=1,csv_sha256=sha(p)))
            before=sha(p);r=audit_recordings(folder);self.assertEqual(r['status'],'passed');self.assertEqual(sha(p),before)
            with p.open('a') as f:f.write('\n')
            r=audit_recordings(folder);self.assertEqual(r['status'],'failed');self.assertIn('hash changed',r['errors'][0]['error'])

class NumericWrapperTests(unittest.TestCase):
    def test_precision_wrapper_uses_training_calibration_and_validation_not_test(self):
        from src.hand_ai_lab.precision import screen
        from tools import precision_sweep as q
        rng=np.random.default_rng(2)
        shapes=((32,12,3),(32,),(64,32,3),(64,),(64,704),(64,),(7,64),(7,))
        # q.NAMES order is weight layers first, then bias layers; use explicit names.
        dimensions=dict(conv1_w=(32,12,3),conv2_w=(64,32,3),fc1_w=(64,704),fc2_w=(7,64),conv1_b=(32,),conv2_b=(64,),fc1_b=(64,),fc2_b=(7,))
        params={n:rng.normal(0,.01,dimensions[n]).astype(np.float32) for n in q.NAMES}
        x=rng.normal(0,.1,(2,50,12)).astype(np.float32);cal=rng.normal(0,.2,(3,50,12)).astype(np.float32)
        reference=lambda v,p:np.array([q.float_layers(row,p)['logits'] for row in v])
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with patch.object(q,'load_source',return_value=dict(parameters=params,provenance={})),patch.object(q,'load_data',return_value=(x,np.array([0,1]),cal,{'split':'validation'})) as loader,patch.object(q,'torch_reference_check',side_effect=reference):
                report=screen(root/'native',root/'data',root/'report',(24,),(16,))
            self.assertEqual(loader.call_args.args[-1],'validation');self.assertFalse(report['test_used'])
            self.assertEqual(report['status'],'completed');self.assertEqual(len(report['candidates']),2)
            self.assertTrue((root/'report/precision.json').is_file())
    def test_fpga_wrapper_forces_cold_reload_and_alternates_warm_slots(self):
        import src.hand_backend as h
        import src.hand_runtime as runtime
        class B:
            synthetic=True
            models={d:dict(mean=np.zeros(12),std=np.ones(12),parameters=np.zeros(52967,dtype=np.int32)) for d in (1,2)}
            def encode(self,d,w):return np.zeros(600,dtype=np.int32)
        class FakeHardware:
            def __init__(self,*args):self.loaded={1:False,2:False};self.last_status={};self.last_timings={};self.out=np.arange(7,dtype=np.int32);self.artifacts={'mock':True}
            def probabilities(self,d,w):
                self.last_status[d]=0 if self.loaded[d] else 1;self.loaded[d]=True;self.last_timings[d]={'mock_stage':1.}
                return np.arange(1,8)/28.
            def close(self):pass
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'manifest.json').write_text('{}')
            with patch.object(runtime,'ModelBundle',return_value=B()),patch.object(h,'PynqHandBackend',FakeHardware),patch.object(h,'integer_logits',return_value=np.arange(7,dtype=np.int32)):
                report=bundle(root,root/'bench.json','fpga','mock.bit',True,True,4)
            self.assertEqual([r['cache_status'] for r in report['calls'][:4]],[1]*4)
            self.assertEqual([r['cache_status'] for r in report['calls'][4:]],[0]*4)
            self.assertEqual([r['device_id'] for r in report['calls'][:4]],[1,2,1,2])

@unittest.skipUnless(importlib.util.find_spec('torch'),'PyTorch not installed in this environment')
class TorchTests(unittest.TestCase):
    def test_training_mask_normalization_reproducibility_and_explicit_test_use(self):
        from src.hand_ai_lab.training import train,LabPredictor,evaluate
        import torch
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);data=make_dataset(root/'data')
            for name in ('a','b'):train(data,root/name,'imu9',(42,),epochs=2)
            a=LabPredictor(root/'a/seed_42');b=LabPredictor(root/'b/seed_42')
            for k,v in a.model.state_dict().items():self.assertTrue(torch.equal(v,b.model.state_dict()[k]))
            x,y,meta,c=load_dataset(data);changed=x[:2].copy();changed[:,:,9:]+=10000
            np.testing.assert_array_equal(a.probabilities(x[:2]),a.probabilities(changed))
            np.testing.assert_array_equal(a.mean,x[:7].mean((0,1),dtype=np.float64).astype(np.float32))
            with self.assertRaises(ValueError):evaluate(root/'a/seed_42',data,'test',root/'test.json')
            r=evaluate(root/'a/seed_42',data,'validation',root/'val.json');self.assertEqual(r['metrics']['samples'],7)
    def test_native_and_lab_precision_reference_checks(self):
        from src.hand_ai_lab.training import train
        from src.hand_ai_lab.precision import screen
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);data=make_dataset(root/'data');train(data,root/'runs','all12',(42,),epochs=1)
            r=screen(root/'runs/seed_42',data,root/'precision',activation_widths=(24,),weight_widths=(16,))
            self.assertEqual(r['status'],'completed');self.assertFalse(r['test_used']);self.assertEqual(len(r['candidates']),2)
    def test_native_export_preserves_predictor_outputs_and_rejects_ablation(self):
        from src.hand_ai_lab.training import train,LabPredictor,export_native
        from src.hand_ai_flex3.pipeline import Predictor
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);data=root/'data';data.mkdir();rng=np.random.default_rng(19);x=[];y=[];meta=[]
            for label in LABELS:
                for split,n in (('train',20),('validation',3),('test',4)):
                    for trial in range(1,n+1):
                        v=rng.normal(LABELS.index(label),.1,(50,12)).astype(np.float32);x.append(v);y.append(LABELS.index(label))
                        meta.append(dict(person='P01',session=split,gesture=label,split=split,trial=trial,device_id=2,
                                         source='fixture.csv',sha256='0'*64,input_sha256=hashlib.sha256(v.tobytes()).hexdigest()))
            np.save(data/'X.npy',np.array(x));np.save(data/'y.npy',np.array(y));write(data/'config.json',CONFIG)
            with (data/'metadata.csv').open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=list(meta[0]));writer.writeheader();writer.writerows(meta)
            train(data,root/'runs','all12',(42,),epochs=1)
            lab=LabPredictor(root/'runs/seed_42');export_native(root/'runs/seed_42',root/'native')
            native=Predictor(root/'native');np.testing.assert_array_equal(lab.probabilities(np.array(x[:2])),native.probabilities(np.array(x[:2])))
            train(data,root/'ablated','imu9',(42,),epochs=1)
            with self.assertRaises(ValueError):export_native(root/'ablated/seed_42',root/'bad_native')

if __name__=='__main__':unittest.main()
