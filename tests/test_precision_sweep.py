import argparse
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
from tools import precision_sweep as q


def params(features=12,classes=7,random=False):
    rng=np.random.default_rng(41)
    shapes=((32,features,3),(64,32,3),(64,704),(classes,64),(32,),(64,),(64,),(classes,))
    return {n:(rng.normal(0,.02,s).astype(np.float32) if random else np.zeros(s,dtype=np.float32)) for n,s in zip(q.NAMES,shapes)}


class NumericTests(unittest.TestCase):
    def test_rounding_ties_toward_positive_infinity(self):
        values,count=q.quantize([.25,-.25,.75,-.75],8,1)
        np.testing.assert_array_equal(values,[1,0,2,-1]);self.assertEqual(count,0)
        values,count=q.requantize(np.array([1,-1,3,-3],dtype=np.int64),1,8)
        np.testing.assert_array_equal(values,[1,0,2,-1])
    def test_saturation_is_counted_instead_of_wrapped(self):
        values,count=q.quantize([-100,100],2,0)
        np.testing.assert_array_equal(values,[-2,1]);self.assertEqual(count,2)
    def test_scaled_fraction_count_can_exceed_word_width(self):
        values,count=q.quantize([-.14,.14],12,13)
        self.assertEqual(count,0);self.assertGreater(abs(values[0]),1000)
    def test_range_integer_bits_and_signed_bounds(self):
        self.assertEqual(q.integer_bits(282.15),10)
        self.assertEqual(q.integer_bits(.1423),-1)
        self.assertEqual(q.integer_bits(256),10)
        self.assertEqual(q.signed_width(-128,127),8)
        self.assertEqual(q.signed_width(-128,128),9)
    def test_model_shape_contract_rejected(self):
        with self.assertRaises(ValueError):q.validate_parameters(params(22,8),12,7)
        with self.assertRaises(ValueError):q.validate_inputs(np.zeros((2,50,11)),np.array([0,1]),12,7)
    def test_nonfinite_data_rejected(self):
        x=np.zeros((2,50,12));x[0,0,0]=np.nan
        with self.assertRaises(ValueError):q.validate_inputs(x,np.array([0,1]),12,7)
    def test_partial_sum_peak_is_measured_before_cancellation(self):
        p=params();p['conv1_w'][0,0,:]=1
        x=np.zeros((1,50,12));x[0,:,0]=np.tile([1,1,-1,-1],13)[:50]
        logits,report=q.run_quantized(x,p,8,0,8,0)
        partial=report['partial_sum_ranges']['conv1']
        self.assertEqual((partial['minimum'],partial['maximum']),(-2,2))
        self.assertEqual(partial['measured_partial_sum_bits'],3)
        self.assertEqual(report['clipping_events'],0)
    def test_unsafe_int64_accumulation_is_refused_before_computing(self):
        with self.assertRaisesRegex(ValueError,'exceeds int64'):q.run_quantized(np.zeros((1,50,12)),params(),32,18,32,31)
    def test_zero_fraction_requires_no_negative_rounding_shift(self):
        output,_=q.requantize(np.array([-3,3],dtype=np.int64),0,8)
        np.testing.assert_array_equal(output,[-3,3])
    def test_confidence_gate_change_detected_with_unchanged_class(self):
        r=q.compare(np.array([[0.,1.]]),np.array([[0.,.5]]),np.array([1]))
        self.assertEqual(r['class_changes'],0);self.assertEqual(r['threshold_decision_changes'],1)
        self.assertEqual(r['emitted_label_changes'],1)
    def test_hidden_class_change_is_distinguished_from_emitted_action(self):
        r=q.compare(np.array([[0.,.05]]),np.array([[.05,0.]]),np.array([1]))
        self.assertEqual(r['class_changes'],1);self.assertEqual(r['emitted_label_changes'],0)
    def test_strict_threshold_boundary_is_explicit(self):
        ref=np.array([[0.,0.]]);candidate=np.array([[.1,0.]]);labels=np.array([0])
        self.assertEqual(q.compare(ref,candidate,labels,.5,'ge')['threshold_decision_changes'],0)
        self.assertEqual(q.compare(ref,candidate,labels,.5,'gt')['threshold_decision_changes'],1)
    def test_existing_output_folder_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileExistsError):q.run(argparse.Namespace(out=td))
    @unittest.skipUnless(importlib.util.find_spec('torch'),'PyTorch optional for legacy NPZ mode')
    def test_numpy_architecture_matches_pytorch_for_both_contracts(self):
        rng=np.random.default_rng(7)
        for features,classes in ((12,7),(22,8)):
            p=params(features,classes,True);x=rng.normal(size=(3,50,features)).astype(np.float32)
            actual=np.array([q.float_layers(v,p)['logits'] for v in x])
            expected=q.torch_reference_check(x,p)
            np.testing.assert_allclose(actual,expected,rtol=2e-4,atol=2e-4)


class PreparedDatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.config=dict(version='independent_hand_flex3_v1')
        q.write_json(self.root/'config.json',self.config)
        x=np.stack([np.zeros((50,12)),np.full((50,12),10),np.full((50,12),100)]).astype(np.float32)
        np.save(self.root/'X.npy',x);np.save(self.root/'y.npy',np.array([2,2,2],dtype=np.int64))
        self.rows=[dict(person='P01',session=s,gesture='CHOP',trial='1',split=s,device_id=1) for s in ('train','validation','test')]
        self.save_meta()
        self.source=dict(features=12,classes=q.HAND_CLASSES,config=self.config,mode='three_flex_run',normalizer=(np.full(12,2,dtype=np.float32),np.full(12,2,dtype=np.float32)),
                         training=dict(device_id=1,dataset_sha256=self.hashes()))
    def tearDown(self):self.tmp.cleanup()
    def hashes(self):return {n:q.digest(self.root/n) for n in ('X.npy','y.npy','metadata.csv','config.json')}
    def save_meta(self):
        with (self.root/'metadata.csv').open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(self.rows[0]));w.writeheader();w.writerows(self.rows)
    def test_training_calibration_and_validation_evaluation_are_separate(self):
        x,y,cal,info=q.load_data(self.root,self.source,'validation')
        self.assertEqual(x.shape,(1,50,12));self.assertTrue(np.all(x==4));self.assertTrue(np.all(cal==-1))
        self.assertFalse(info['untouched_test_consumed'])
    def test_test_use_is_recorded_explicitly(self):
        x,y,cal,info=q.load_data(self.root,self.source,'test')
        self.assertTrue(info['untouched_test_consumed']);self.assertTrue(np.all(x==49))
    def test_dataset_tampering_rejected(self):
        (self.root/'X.npy').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'provenance'):q.load_data(self.root,self.source,'validation')
    def test_wrong_hand_is_rejected_after_provenance_check(self):
        self.rows[1]['device_id']=2;self.save_meta();self.source['training']['dataset_sha256']=self.hashes()
        with self.assertRaisesRegex(ValueError,'hand mismatch'):q.load_data(self.root,self.source,'validation')
    def test_session_leak_is_rejected(self):
        self.rows[1]['session']='train';self.save_meta();self.source['training']['dataset_sha256']=self.hashes()
        with self.assertRaisesRegex(ValueError,'crosses'):q.load_data(self.root,self.source,'validation')


if __name__=='__main__':unittest.main()
