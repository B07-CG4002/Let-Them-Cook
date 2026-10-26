import argparse
import contextlib
import io
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from tools import precision_sweep as q
from tools import precision_hls_prepare as h


class HLSPrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.weights=self.root/'weights.npz';self.data=self.root/'inputs.npz';self.sweep=self.root/'sweep.json'
        columns=[f'{hand}_{c}' for hand in ('left','right') for c in ('ax_g','ay_g','az_g','gx_deg_s','gy_deg_s','gz_deg_s','yaw_deg','pitch_deg','roll_deg','flex_1','flex_2')]
        cfg=dict(class_names=q.OLD_CLASSES,normalization='none',window_seconds=2.,time_positions=50,feature_columns=columns)
        shapes=((32,22,3),(64,32,3),(64,704),(8,64),(32,),(64,),(64,),(8,))
        p={n:np.zeros(s,dtype=np.float32) for n,s in zip(q.NAMES,shapes)};p['fc2_b'][7]=1.
        np.savez(self.weights,**p,config_json=np.array(json.dumps(cfg)))
        x=np.random.default_rng(8).normal(size=(3,50,22)).astype(np.float32);y=np.full(3,7,dtype=np.int64)
        np.savez(self.data,inputs=x,labels=y)
        source=q.load_source(weights=self.weights)
        logits,stats=q.run_quantized(x,p,16,6,12,13)
        floating=np.array([q.float_layers(v,p)['logits'] for v in x])
        row=dict(kind='candidate',activation_bits=16,activation_fraction_bits=6,weight_bits=12,weight_fraction_bits=13,passes_regression_screen=True)
        row.update(q.compare(floating,logits,y));row.update(stats)
        self.report=dict(status='completed',scope='legacy_paired_regression',candidates=[row],source_sha256=source['provenance'],
                         data=dict(data_sha256={str(self.data):q.digest(self.data)}),threshold=.7,threshold_rule='ge',max_probability_error_limit=.01)
        q.write_json(self.sweep,self.report)
        self.args=argparse.Namespace(out=self.root/'experiment',weights=self.weights,data=self.data,sweep=self.sweep,activation_bits=16,weight_bits=12)
    def tearDown(self):self.tmp.cleanup()
    def prepare(self):
        with contextlib.redirect_stdout(io.StringIO()):return h.prepare(self.args)
    def test_outputs_bind_precision_while_retaining_32_bit_axi(self):
        manifest=self.prepare();out=Path(self.args.out)
        header=(out/'precision_cnn.h').read_text();cpp=(out/'precision_cnn.cpp').read_text()
        self.assertIn('typedef ap_int<32> word_t;',header)
        self.assertIn('typedef ap_int<16> act_t;',header);self.assertIn('typedef ap_int<12> weight_word_t;',header)
        self.assertIn('const word_t input_words',cpp)
        self.assertEqual(cpp.count('#pragma HLS PIPELINE II=1'),h.reviewed_sources()['full_cnn_20261020.cpp'].count('#pragma HLS PIPELINE II=1'))
        original_pragmas=[s.strip() for s in h.reviewed_sources()['full_cnn_20261020.cpp'].splitlines() if s.strip().startswith('#pragma HLS')]
        generated_pragmas=[s.strip() for s in cpp.splitlines() if s.strip().startswith('#pragma HLS')]
        self.assertEqual(generated_pragmas,original_pragmas)
        self.assertEqual(manifest['format']['accumulator_bits'],38)
        data=(out/'vectors/precision_test.bin').read_bytes()
        self.assertEqual(struct.unpack('<8I',data[:32]),(h.MAGIC,3,53992,16,6,12,13,38))
        self.assertEqual(manifest['generated_sha256']['vectors/precision_test.bin'],q.digest(out/'vectors/precision_test.bin'))
    def test_original_sources_are_not_written(self):
        before=h.reviewed_sources();self.prepare();self.assertEqual(h.reviewed_sources(),before)
    def test_changed_model_or_data_hash_is_refused_before_output_creation(self):
        self.report['source_sha256']={'wrong':'0'*64};q.write_json(self.sweep,self.report)
        with self.assertRaisesRegex(ValueError,'hashes differ'):self.prepare()
        self.assertFalse(Path(self.args.out).exists())
    def test_interrupted_or_failed_candidate_cannot_be_selected(self):
        for change in ('interrupted','failed_candidate'):
            report=json.loads(json.dumps(self.report))
            if change=='interrupted':report['status']='interrupted'
            else:report['candidates'][0]['passes_regression_screen']=False
            with self.assertRaises(ValueError):h.choose_candidate(report,16,12)
    def test_existing_experiment_cannot_be_overwritten(self):
        Path(self.args.out).mkdir()
        with self.assertRaises(FileExistsError):self.prepare()
    def test_fake_passing_flag_cannot_bypass_recomputation(self):
        row=self.report['candidates'][0];row['activation_bits']=2;row['activation_fraction_bits']=0
        q.write_json(self.sweep,self.report);self.args.activation_bits=2
        # Large inputs force clipping even though the stored passing flag says true.
        np.savez(self.data,inputs=np.full((3,50,22),100,dtype=np.float32),labels=np.full(3,7,dtype=np.int64))
        self.report['data']['data_sha256']={str(self.data):q.digest(self.data)};q.write_json(self.sweep,self.report)
        with self.assertRaisesRegex(ValueError,'Fresh recomputation failed'):self.prepare()
    def test_crlf_checkout_is_accepted_but_source_changes_are_not(self):
        sources=h.reviewed_sources();clone=self.root/'clone/fpga/hls/full_cnn';clone.mkdir(parents=True)
        for name,text in sources.items():(clone/name).write_bytes(text.replace('\n','\r\n').encode())
        self.assertEqual(h.reviewed_sources(self.root/'clone'),sources)
        file=clone/'full_cnn_20261020.cpp';file.write_bytes(file.read_bytes()+b'// edited\r\n')
        with self.assertRaisesRegex(ValueError,'differs'):h.reviewed_sources(self.root/'clone')
    def test_zero_weight_fraction_does_not_generate_negative_shift(self):
        files=h.make_sources(h.reviewed_sources(),16,6,12,0,38)
        self.assertIn('sum_t rounded=value;',files['precision_cnn.cpp'])
        self.assertNotIn('<<-1',files['precision_cnn.cpp'])
    def sweep_args(self):
        return argparse.Namespace(out=self.root/'fresh_sweep',weights=self.weights,run=None,data=self.data,split='validation',
            activation_int_bits=10,weight_int_bits=None,guard_bits=1,threshold=.7,threshold_rule='ge',max_probability_error=.01,
            activation_widths=[16],weight_widths=[12])
    def test_completed_sweep_updates_range_profile_status(self):
        args=self.sweep_args()
        with contextlib.redirect_stdout(io.StringIO()):q.run(args)
        self.assertEqual(q.read_json(args.out/'range_profile.json')['status'],'completed')
        self.assertEqual(q.read_json(args.out/'sweep.json')['status'],'completed')
    def test_interrupted_sweep_retains_interrupted_status(self):
        args=self.sweep_args()
        with patch.object(q,'run_quantized',side_effect=KeyboardInterrupt()),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):q.run(args)
        self.assertEqual(q.read_json(args.out/'range_profile.json')['status'],'interrupted')
        self.assertEqual(q.read_json(args.out/'sweep.json')['status'],'interrupted')


if __name__=='__main__':unittest.main()
