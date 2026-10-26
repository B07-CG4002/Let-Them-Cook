import contextlib
import hashlib
import io
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from tools import hand_hls_prepare as m


def prepare(out, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return m.prepare(out, **kwargs)


class PreparationTests(unittest.TestCase):
    def test_12_features_7_classes_layout_and_high_precision(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'new'
            manifest=prepare(out,synthetic=True)
            contract=json.loads((out/'hand_contract.json').read_text())
            self.assertEqual(contract['input_shape'],[50,12])
            self.assertEqual(contract['parameter_words'],52967)
            self.assertEqual(contract['output_words'],7)
            self.assertEqual(contract['features'][-1],'flex3')
            self.assertEqual(contract['classes'],m.q.HAND_CLASSES)
            self.assertEqual(manifest['format'],m.FORMAT)
            self.assertEqual(manifest['scope'],'synthetic_contract_test_only')
            self.assertFalse(manifest['final_gesture_accuracy_validated'])
            self.assertEqual([p['offset'] for p in contract['parameters']],list(m.OFFSETS))
            self.assertEqual(sum(np.prod(s) for s in m.SHAPES),52967)
            for model in manifest['models']:
                self.assertNotIn('float_correct',model['numeric_screen'])
                self.assertEqual(model['clipping_events'],0)
            header=(out/'hand_cnn.h').read_text()
            cpp=(out/'hand_cnn.cpp').read_text()
            self.assertIn('CNN_INPUTS=600, CNN_WEIGHTS=52800, CNN_PARAMETERS=52967, CNN_OUTPUTS=7',header)
            self.assertIn('C1_DOT: for(int base=0;base<36;base+=2)',cpp)
            self.assertIn('(t+d%3)*12+d/3',cpp)
            self.assertIn('C2_TIME: for(int t=0;t<22;++t)',cpp)
            self.assertIn('for(int o=0;o<7;++o)',cpp)
            self.assertIn('depth=7\n',cpp)

    def test_distinct_models_and_thumb_fixture(self):
        models=m.synthetic_models()
        self.assertFalse(np.array_equal(models[0]['parameters']['conv1_w'],models[1]['parameters']['conv1_w']))
        self.assertEqual(models[0]['inputs'][1,25,11],1.5)
        self.assertEqual(np.count_nonzero(models[0]['inputs'][1]),1)

    def test_binary_lengths_parameter_blocks_and_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'new';manifest=prepare(out,synthetic=True)
            binary=(out/'vectors/hand_test.bin').read_bytes()
            header=struct.unpack('<12I',binary[:48])
            self.assertEqual(header,(m.MAGIC,1,8,600,52967,7,32,18,22,21,64,2))
            self.assertEqual(len(binary),48+2*52967*4+8*(4+600*4+7*4+7*4))
            for slot,hand in enumerate(('left','right')):
                params=(out/hand/'parameter_words.bin').read_bytes()
                self.assertEqual(params,binary[48+slot*52967*4:48+(slot+1)*52967*4])
            for name,digest in manifest['generated_sha256'].items():
                self.assertEqual(hashlib.sha256((out/name).read_bytes()).hexdigest(),digest)

    def test_normalization_stays_separate_for_each_hand(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'new';prepare(out,synthetic=True)
            for hand in ('left','right'):
                with np.load(out/hand/'normalization.npz',allow_pickle=False) as z:
                    np.testing.assert_array_equal(z['mean'],np.zeros(12))
                    np.testing.assert_array_equal(z['std'],np.ones(12))

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            marker=Path(temp)/'important';marker.write_text('keep')
            with self.assertRaises(FileExistsError):prepare(temp,synthetic=True)
            self.assertEqual(marker.read_text(),'keep')

    def test_synthetic_and_trained_sources_cannot_be_mixed(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'new'
            with self.assertRaises(ValueError):prepare(out,synthetic=True,left_run='fake')
            self.assertFalse(out.exists())

    def test_both_trained_hands_are_required(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'new'
            with self.assertRaises(ValueError):prepare(out,left_run='fake')
            self.assertFalse(out.exists())

    def test_wrong_hand_is_rejected(self):
        fake={'training':{'device_id':2}}
        with patch.object(m.q,'load_source',return_value=fake):
            with self.assertRaisesRegex(ValueError,'identity'):
                m.trained_models('left','right')

    def test_quantization_clipping_is_refused_before_output(self):
        models=m.synthetic_models()
        models[0]['parameters']['conv1_w'][0,0,0]=2.
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'new'
            with patch.object(m,'synthetic_models',return_value=models):
                with self.assertRaisesRegex(ValueError,'numerical screen'):
                    prepare(out,synthetic=True)
            self.assertFalse(out.exists())

    def test_full_range_accumulator_and_partial_sums_fit(self):
        bound=704*(1<<31)*(1<<21)+(1<<31)*(1<<21)+(1<<20)
        self.assertLess(bound,(1<<63)-1)
        for terms in (36,96,704,64):
            partial=(terms//2)*(1<<31)*(1<<21)+(1<<31)*(1<<21)
            self.assertLess(partial,(1<<63)-1)

    def test_generation_preserves_reviewed_legacy_sources(self):
        paths=[Path(m.h.__file__),Path(m.u.__file__),Path(m.c.__file__)]
        paths+=list((Path(m.h.__file__).resolve().parents[1]/'fpga/hls/full_cnn').glob('*'))
        before={p:p.read_bytes() for p in paths if p.is_file()}
        with tempfile.TemporaryDirectory() as temp:
            prepare(Path(temp)/'new',synthetic=True)
        self.assertEqual(before,{p:p.read_bytes() for p in before})

    def test_warm_cold_configs_use_same_design_with_distinct_modes(self):
        source=m.sources()
        for mode in ('warm','cold'):
            cfg=source['hand_perf_'+mode+'.cfg'].decode()
            self.assertIn('syn.top='+m.TOP,cfg)
            self.assertIn('csim.argv=--perf-'+mode,cfg)
            self.assertIn('cosim.argv=--perf-'+mode,cfg)
            self.assertIn('clock=10ns',cfg)


@unittest.skipUnless(shutil.which('g++'),'Portable C++ checks need g++; use Vitis C simulation on Windows')
class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.out=Path(cls.temp.name)/'experiment'
        prepare(cls.out,synthetic=True)
        cls.binary=(cls.out/'vectors/hand_test.bin').read_bytes()
        compiler=subprocess.run([shutil.which('g++'),'-std=c++17','-O2','-DCNN_PORTABLE_TEST',
            'hand_cnn.cpp','tb_hand_cnn.cpp','-o','native_check'],cwd=cls.out,
            capture_output=True,text=True,timeout=60)
        if compiler.returncode:raise RuntimeError(compiler.stderr)

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def run_mode(self,*args):
        return subprocess.run([str(self.out/'native_check'),*args],cwd=self.out,
            capture_output=True,text=True,timeout=30)

    def test_exact_outputs_alternating_models_and_17_cache_controls(self):
        result=self.run_mode()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('Raw-word mismatches: 0',result.stdout)
        self.assertIn('Prediction agreement: 8/8',result.stdout)
        self.assertIn('Cache-control cases: 17/17',result.stdout)
        self.assertIn('slot=0 class=6',result.stdout)
        self.assertIn('slot=1 class=0',result.stdout)

    def test_warm_mode_preloads_both_models(self):
        result=self.run_mode('--perf-warm')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('Preload calls: 2',result.stdout)
        self.assertEqual(result.stdout.count('status=0'),8)

    def test_cold_mode_forces_reload(self):
        result=self.run_mode('--perf-cold')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('Preload calls: 0',result.stdout)
        self.assertEqual(result.stdout.count('status=1'),8)

    def test_wrong_slot_or_truncated_or_trailing_vectors_are_refused(self):
        path=self.out/'vectors/hand_test.bin'
        variants=[self.binary[:-1],self.binary+b'\x00']
        corrupt=bytearray(self.binary)
        struct.pack_into('<I',corrupt,48+2*52967*4,2)
        variants.append(bytes(corrupt))
        try:
            for value in variants:
                with self.subTest(length=len(value)):
                    path.write_bytes(value)
                    self.assertEqual(self.run_mode().returncode,2)
        finally:path.write_bytes(self.binary)

    def test_changed_expected_word_is_detected(self):
        corrupt=bytearray(self.binary)
        offset=48+2*52967*4+4+600*4
        old=struct.unpack_from('<i',corrupt,offset)[0]
        struct.pack_into('<i',corrupt,offset,old+1)
        path=self.out/'vectors/hand_test.bin'
        try:
            path.write_bytes(corrupt)
            self.assertEqual(self.run_mode().returncode,1)
        finally:path.write_bytes(self.binary)


@unittest.skipUnless(importlib.util.find_spec('torch'),'PyTorch is required to exercise trained-checkpoint export')
class TrainedExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import csv
        import torch
        cls.temp=tempfile.TemporaryDirectory()
        cls.root=Path(cls.temp.name)
        cls.old_threads=torch.get_num_threads();torch.set_num_threads(1)
        cls.runs=[];cls.data=[]
        config=dict(version='independent_hand_flex3_v1',feature_columns=m.q.HAND_CHANNELS,
            class_names=m.q.HAND_CLASSES,normalization='training-only per-feature mean/std after resampling',
            time_positions=50,window_seconds=2.0,sensor_packet_version=4,sensor_packet_bytes=40,
            flex_gpio={'flex1':34,'flex2':35,'flex3':39})
        for device,model in enumerate(m.synthetic_models(),1):
            run=cls.root/('run'+str(device));run.mkdir()
            data=cls.root/('data'+str(device));data.mkdir()
            rows=[];labels=[]
            for label,gesture in enumerate(m.q.HAND_CLASSES):
                for split,count in (('train',20),('validation',3),('test',4)):
                    for trial in range(1,count+1):
                        labels.append(label)
                        rows.append(dict(person='fixture',session=split,gesture=gesture,
                                         split=split,trial=trial,device_id=device))
            rng=np.random.default_rng(device)
            x=rng.normal(device,.5,(len(rows),50,12)).astype(np.float32)
            train=np.array([i for i,row in enumerate(rows) if row['split']=='train'])
            mean=x[train].mean(axis=(0,1),dtype=np.float64).astype(np.float32)
            std=x[train].std(axis=(0,1),dtype=np.float64).astype(np.float32)
            np.save(data/'X.npy',x);np.save(data/'y.npy',np.array(labels,dtype=np.int64))
            with (data/'metadata.csv').open('w',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
            (data/'config.json').write_bytes(m.json_bytes(config))
            (run/'config.json').write_bytes(m.json_bytes(config))
            (run/'class_names.json').write_bytes(m.json_bytes(m.q.HAND_CLASSES))
            (run/'normalization.npz').write_bytes(m.normalizer_bytes(mean,std))
            state={key:torch.from_numpy(model['parameters'][name].copy())
                   for name,key in zip(m.q.NAMES,m.q.STATE_KEYS)}
            torch.save(state,run/'model.pth')
            training=dict(device_id=device,data_dir=str(data),dataset_sha256={
                name:m.q.digest(data/name) for name in ('X.npy','y.npy','metadata.csv','config.json')})
            (run/'training.json').write_bytes(m.json_bytes(training))
            cls.runs.append(run);cls.data.append(data)

    @classmethod
    def tearDownClass(cls):
        import torch
        torch.set_num_threads(cls.old_threads);cls.temp.cleanup()

    def test_two_checkpoint_export_uses_validation_and_separate_normalizers(self):
        out=self.root/'exported'
        manifest=prepare(out,left_run=self.runs[0],right_run=self.runs[1])
        self.assertEqual(manifest['scope'],'two_trained_hands_validation_export')
        self.assertEqual(manifest['regression_samples'],42)
        for device,hand in enumerate(('left','right'),1):
            model=manifest['models'][device-1]
            self.assertEqual(model['device_id'],device)
            self.assertFalse(model['provenance']['data']['untouched_test_consumed'])
            self.assertEqual(model['provenance']['calibration_samples'],140)
            with np.load(out/hand/'normalization.npz') as saved, np.load(self.runs[device-1]/'normalization.npz') as original:
                np.testing.assert_array_equal(saved['mean'],original['mean'])
                np.testing.assert_array_equal(saved['std'],original['std'])

    def test_changed_dataset_is_refused_before_output_creation(self):
        path=self.data[1]/'metadata.csv';original=path.read_bytes()
        out=self.root/'refused'
        try:
            path.write_bytes(original+b'\n')
            with self.assertRaisesRegex(ValueError,'provenance'):
                prepare(out,left_run=self.runs[0],right_run=self.runs[1])
            self.assertFalse(out.exists())
        finally:path.write_bytes(original)


if __name__=='__main__':unittest.main()
