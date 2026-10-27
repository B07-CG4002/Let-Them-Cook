import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from src.hand_ai_flex3.core import CHANNELS, CONFIG, LABELS, validate_ratios
from tools.hand_collection_plan import check_plan, make_plan, progress, split_counts, write_new


class PlanTests(unittest.TestCase):
    def test_one_wearer_and_cohort_match_native_rounding(self):
        for n in range(1, 21):
            p = make_plan([f'P{i:02d}' for i in range(n)], 'fit1', 'raw')
            rows = check_plan(p)
            self.assertEqual(p['total_trials'], n*378)
            for values in rows.values():
                validate_ratios(values)
        self.assertEqual(split_counts(27),dict(train=20,validation=3,test=4))
        self.assertEqual(split_counts(54),dict(train=41,validation=5,test=8))

    def test_naively_doubling_old_counts_would_fail_native_prepare(self):
        rows = [dict(person='P01',session=s,split=s,gesture=g)
                for g in LABELS for s,n in [('train',40),('validation',6),('test',8)]
                for _ in range(n)]
        with self.assertRaises(ValueError):
            validate_ratios(rows)

    def test_bad_people_and_sessions_refused(self):
        for people in ([],['P01','P01'],['../P01'],['P 01']):
            with self.assertRaises(ValueError):make_plan(people,'fit1','raw')
        with self.assertRaises(ValueError):make_plan(['P01'],'fit 1','raw')
        with self.assertRaises(ValueError):make_plan(['P01'],'fit1','raw',9)

    def test_changed_contract_or_counts_refused(self):
        for mutation in ('contract','hand','total','count'):
            p = make_plan(['P01'],'fit1','raw')
            if mutation=='contract':p['contract']={}
            if mutation=='hand':p['sessions'][0]['device_id']=2
            if mutation=='total':p['total_trials']+=1
            if mutation=='count':p['sessions'][0]['per_gesture_target']=True
            with self.assertRaises(ValueError):check_plan(p)

    def test_new_reports_never_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'report.json'
            write_new(path,{'first':1})
            with self.assertRaises(FileExistsError):write_new(path,{'second':2})
            self.assertEqual(json.loads(path.read_text()),{'first':1})


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.repo=Path(self.temp.name)
        self.plan=make_plan(['P01'],'fit1','raw')
    def tearDown(self):self.temp.cleanup()

    def trial(self,index=0,trial=1,gesture='PICKUP',offset=0):
        s=self.plan['sessions'][index]
        folder=self.repo/'raw'/s['hand'].lower()/s['session']
        folder.mkdir(parents=True,exist_ok=True)
        identity={k:s[k] for k in ('person','session','split','device_id')}
        identity['recording_contract']=CONFIG['version']
        (folder/'session.json').write_text(json.dumps(identity))
        path=folder/f'{s["person"]}_{s["session"]}_{gesture}_{trial:03d}.csv'
        with path.open('w',newline='') as f:
            w=csv.writer(f)
            w.writerow(['elapsed_s','device_id','boot_id','seq','uptime_ms',*CHANNELS])
            for i in range(101):
                t=i*.02
                v=np.sin(t+np.arange(12))*.1+offset+trial*.0001
                w.writerow([t,s['device_id'],11,i,i*20,*v])
        meta=dict(identity,gesture=gesture,trial=trial,
                  csv_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        path.with_suffix('.json').write_text(json.dumps(meta))
        return path

    def test_empty_root_incomplete_without_creating_raw_directory(self):
        r=progress(self.plan,self.repo)
        self.assertEqual((r['status'],r['remaining_trials']),('incomplete',378))
        self.assertFalse((self.repo/'raw').exists())

    def test_counts_from_valid_files_not_highest_trial_number(self):
        self.trial(trial=18)
        r=progress(self.plan,self.repo)
        self.assertEqual(r['remaining_trials'],377)
        self.assertEqual(r['tasks'][0]['valid_trials'],1)
        self.assertEqual(r['tasks'][0]['remaining'],19)

    def test_hash_tamper_blocks_and_is_not_counted(self):
        path=self.trial()
        path.write_bytes(path.read_bytes()+b'\n')
        r=progress(self.plan,self.repo)
        self.assertEqual(r['status'],'blocked')
        self.assertEqual(r['remaining_trials'],378)

    def test_session_marker_tamper_blocks(self):
        path=self.trial()
        (path.parent/'session.json').write_text('{}')
        r=progress(self.plan,self.repo)
        self.assertEqual(r['status'],'blocked')
        self.assertEqual(r['remaining_trials'],378)

    def test_wrong_folder_and_unplanned_session_block(self):
        path=self.trial()
        target=path.parent.parent/'misplaced'
        path.parent.rename(target)
        self.assertEqual(progress(self.plan,self.repo)['status'],'blocked')

    def test_orphan_sidecar_blocks(self):
        path=self.trial();path.unlink()
        r=progress(self.plan,self.repo)
        self.assertEqual(r['status'],'blocked')

    def test_extra_trials_are_reviewed_never_removed(self):
        paths=[self.trial(trial=i) for i in range(1,22)]
        r=progress(self.plan,self.repo)
        self.assertEqual(r['status'],'blocked')
        self.assertEqual(r['tasks'][0]['excess'],1)
        self.assertTrue(all(p.is_file() for p in paths))

    def test_complete_fixture_only_establishes_file_counts(self):
        count=0
        for index,s in enumerate(self.plan['sessions']):
            for g in LABELS:
                for trial in range(1,s['per_gesture_target']+1):
                    count+=1
                    self.trial(index,trial,g,offset=count*.01)
        r=progress(self.plan,self.repo)
        self.assertEqual((r['status'],r['remaining_trials']),('collection_complete',0))
        self.assertEqual(r['errors'],[])


if __name__=='__main__':unittest.main()
