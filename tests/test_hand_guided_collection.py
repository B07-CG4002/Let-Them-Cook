import json
from pathlib import Path
import tempfile
import unittest
from tools.hand_guided_collection import plan, counts_for
from tools.hand_collection import session_name, completed
from src.hand_ai_flex3.record_gui import prepare_session
from tools.hand_capture_review import inventory, quarantine, sha, LABELS

class GuidedTests(unittest.TestCase):
    def test_combined_guided_session_name_and_marker_accept_split_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'raw'/'left'/'P01_campaign_20261103'
            base=dict(person='P01',session='P01_campaign_20261103',split='train',device_id=1,
                      gesture='CHOP',count=1,root=root,session_scope='combined')
            self.assertEqual(session_name('P01','20261103','campaign'),'P01_campaign_20261103')
            self.assertEqual(prepare_session(base),[1])
            changed=dict(base,split='validation')
            self.assertEqual(prepare_session(changed),[1])

    def test_budgets_and_corrected_throw(self):
        for train,validation,test in ((30,4,6),(20,3,4)):
            rows=plan(train)
            for g in LABELS:
                for split,n in zip(('train','validation','test'),(train,validation,test)):
                    self.assertEqual(sum(r['target'] for r in rows if r['gesture']==g and r['split']==split),n)
            self.assertTrue(all(r['target']>0 for r in rows))
        throw=[r for r in plan(30) if r['split']=='train' and r['gesture']=='THROW']
        self.assertEqual([r['target'] for r in throw],[8,7,7,8])
        self.assertIn('thumb straight; finish palm vertical',throw[0]['note'])
    def test_plan_interleaves_each_variant_train_validation_test(self):
        rows=plan(30)
        self.assertEqual([(r['split'],r['note']) for r in rows[:3]], [
            ('train','PICKUP; underhand supporting lift; thumb straight'),
            ('validation','PICKUP; underhand supporting lift; thumb straight'),
            ('test','PICKUP; underhand supporting lift; thumb straight'),
        ])
        self.assertEqual([(r['split'],r['note']) for r in rows[3:6]], [
            ('train','PICKUP; underhand supporting lift; thumb bent'),
            ('validation','PICKUP; underhand supporting lift; thumb bent'),
            ('test','PICKUP; underhand supporting lift; thumb bent'),
        ])
    def test_counts_ignore_other_sessions_and_trim_notes(self):
        row=plan(30)[0]
        records=[dict(meta=dict(person='P01',session='s',split=row['split'],device_id=1,gesture=row['gesture'],recording_variant=' '+row['note']+' '))]
        self.assertEqual(counts_for(records,row,'P01','s'),{1:1,2:0})
        self.assertEqual(counts_for(records,row,'P01','other'),{1:0,2:0})
    def fixture(self,root):
        for d in (1,2):
            folder=root/str(d);folder.mkdir()
            identity=dict(person='P01',session='s',split='test',device_id=d,recording_contract='v5')
            (folder/'session.json').write_text(json.dumps(identity))
            csv=folder/'trial.csv';csv.write_text('raw preserved\n')
            (folder/'trial.json').write_text(json.dumps(dict(**identity,gesture='DROP',capture_id='abc',captured_devices=[1,2],other_gesture='DROP',csv_sha256=sha(csv))))
    def test_quarantine_pair_and_dry_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'raw';root.mkdir();self.fixture(root)
            self.assertEqual(len(inventory(root)),2)
            self.assertEqual(len(quarantine(root,'abc','wrong motion',Path(temp)/'backup')),2)
            self.assertEqual(len(inventory(root)),2)
            backup=Path(quarantine(root,'abc','wrong motion',Path(temp)/'backup',True))
            self.assertEqual(inventory(root),[])
            self.assertEqual(len(list(backup.rglob('*.csv'))),2)
            for item in json.loads((backup/'manifest.json').read_text())['files']:
                self.assertEqual(sha(backup/item['backup']),item['sha256'])
    def test_tamper_refused_before_quarantine(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'raw';root.mkdir();self.fixture(root)
            (root/'1/trial.csv').write_text('changed')
            with self.assertRaises(ValueError):quarantine(root,'abc','bad',Path(temp)/'backup',True)
            self.assertEqual(len(list(root.rglob('*.csv'))),2)
            self.assertFalse((Path(temp)/'backup').exists())
    def test_per_wearer_rounding_and_incomplete_person(self):
        from src.hand_ai_flex3.wide_training import validate_ratios
        rows=[dict(person=p,session=p+'_'+s,gesture=g,split=s) for p in ('P01','P02','P03') for g in LABELS for s,n in [('train',20),('validation',3),('test',4)] for _ in range(n)]
        counts=validate_ratios(rows)
        self.assertEqual(counts['DROP'],dict(train=60,validation=9,test=12))
        with self.assertRaises(ValueError):validate_ratios([r for r in rows if not (r['person']=='P01' and r['gesture']=='PICKUP' and r['split']=='validation')])
if __name__=='__main__':unittest.main()
