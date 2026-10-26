import tempfile
import asyncio
from contextlib import asynccontextmanager
import struct
import unittest
from pathlib import Path
import csv
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
import numpy as np
from src.hand_ai_flex3.flex_check import summarize, save_rows, ADDRESSES
from src.hand_ai_flex3 import flex_check

class RangeTests(unittest.TestCase):
    def trial(self,flex):
        values=np.zeros((101,12));values[:,9:]=flex
        return values
    def test_small_but_stable_span_is_measured_without_applying_scaling(self):
        opened=[self.trial([.45,.4,.45]) for _ in range(3)]
        bent=[self.trial([.65,.6,.65]) for _ in range(3)]
        original=opened[0].copy();r=summarize(opened,bent)
        self.assertAlmostEqual(r['flex3']['span_adc_counts'],819)
        self.assertFalse(r['flex3']['candidate_only']['applied'])
        self.assertEqual(r['flex3']['warnings'],[])
        np.testing.assert_array_equal(opened[0],original)
    def test_reverse_direction_is_preserved(self):
        r=summarize([self.trial([.8,.8,.8])],[self.trial([.4,.4,.4])])
        self.assertLess(r['flex1']['signed_span'],0)
        self.assertEqual(r['flex3']['finger'],'thumb')
        self.assertEqual(r['flex3']['gpio'],39)
    def test_stuck_rail_is_flagged_and_no_division_formula_given(self):
        r=summarize([self.trial([1,1,1])],[self.trial([1,1,1])])
        self.assertIsNone(r['flex1']['candidate_only']['formula'])
        self.assertGreaterEqual(len(r['flex1']['warnings']),2)
    def test_noise_and_overlap_are_reported(self):
        a=self.trial([.5,.5,.5]);a[:,9:]+=np.linspace(-.1,.1,101)[:,None]
        r=summarize([a],[a+.01])
        self.assertTrue(r['flex3']['endpoint_bands_overlap'])
        self.assertGreater(r['flex3']['straight']['within_hold_std'],0)
    def test_old_shape_and_nonfinite_rejected(self):
        with self.assertRaises(ValueError):summarize([np.zeros((101,11))],[np.zeros((101,12))])
        a=self.trial([0,0,0]);a[0,0]=np.nan
        with self.assertRaises(ValueError):summarize([a],[a])
    def test_saved_check_preserves_three_flex_values_and_source_identity(self):
        packet=SimpleNamespace(device_id=1,boot_id=7,seq=10,uptime_ms=1234)
        values=list(range(12))
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'check.csv';save_rows(p,[(0,packet,values)])
            with p.open() as stream:rows=list(csv.reader(stream))
            self.assertEqual(rows[0][-3:],['flex1','flex2','flex3'])
            self.assertEqual(rows[1][-1],'11')
            self.assertEqual(rows[1][1:5],['1','7','10','1234'])
            with self.assertRaises(FileExistsError):save_rows(p,[])
        self.assertEqual(ADDRESSES,{1:'38:18:2B:17:E2:E6',2:'38:18:2B:14:EA:FE'})

class GuidedFlowTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, fail=False):
        import json
        closed=[];opened=[]
        class Client:
            def __init__(self,device):self.device=device
            async def read_gatt_char(self,*args,**kwargs):
                return struct.pack('<4sBB2xIIIIIII',b'LCS4',self.device,2,7,0,0,0,0,0,0)
        @asynccontextmanager
        async def connect(device,address,recording):
            opened.append((device,address,recording))
            try:yield Client(device),SimpleNamespace(device=device),dict(dropped=0,malformed=0)
            finally:closed.append(device)
        async def collect(q,health):
            if fail and q.device==2:raise ValueError('sequence gap')
            return [(i*.02,SimpleNamespace(device_id=q.device,boot_id=7,seq=i,uptime_ms=i*20),[.5]*12) for i in range(101)]
        with tempfile.TemporaryDirectory() as d:
            a=SimpleNamespace(out=d,hand='both',left_address=ADDRESSES[1],right_address=ADDRESSES[2],repeats=2)
            with patch.object(flex_check,'connection',connect),patch.object(flex_check,'collect',collect),\
                 patch.object(flex_check.asyncio,'to_thread',AsyncMock(return_value='')),\
                 patch.object(flex_check.asyncio,'sleep',AsyncMock()),patch('builtins.print'):
                if fail:
                    with self.assertRaisesRegex(RuntimeError,'RIGHT straight failed'):await flex_check.run(a)
                else:await flex_check.run(a)
            paths=list(Path(d).rglob('range_report.json'));self.assertEqual(len(paths),1)
            report=json.loads(paths[0].read_text())
            self.assertEqual(report['status'],'failed' if fail else 'measured')
            self.assertFalse(report['scaling_changed'])
            self.assertEqual(report['purpose'],'hardware_quality_check_not_training')
            if not fail:
                self.assertEqual(len(report['hands']['LEFT']['trials']),4)
                self.assertEqual(len(report['hands']['RIGHT']['trials']),4)
                self.assertEqual(len(list(Path(d).rglob('*.csv'))),8)
            self.assertEqual(sorted(closed),[1,2])
            self.assertEqual(opened,[(1,ADDRESSES[1],True),(2,ADDRESSES[2],True)])
    async def test_both_gloves_full_flow_saves_separate_outputs_and_disconnects(self):await self.exercise()
    async def test_failed_trial_saves_failure_and_disconnects_both(self):await self.exercise(True)

if __name__=='__main__':unittest.main()
