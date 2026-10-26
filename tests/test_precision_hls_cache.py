import contextlib
import copy
import io
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import unittest

from tools import precision_hls_prepare as h
from tools import precision_hls_unroll2 as u
from tools import precision_hls_cache as c


class CachePrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.parent, self.out = self.root / 'parent', self.root / 'cached'
        self.parent.mkdir()
        source = h.make_sources(h.reviewed_sources(), 16, 6, 12, 13, 38)
        for name, value in source.items():
            text = u.transform(value) if name == 'precision_cnn.cpp' else value.replace('gesture_cnn_precision', u.TOP)
            (self.parent / name).write_bytes(text.encode())
        (self.parent / 'vectors').mkdir()
        params = bytes(53992 * 4)
        vector = struct.pack('<8I', h.MAGIC, 1, 53992, 16, 6, 12, 13, 38)
        vector += params + bytes(1100 * 4 + 8 * 4 + 8 * 4 + 8)
        (self.parent / 'vectors/precision_test.bin').write_bytes(vector)
        (self.parent / 'parameter_words.bin').write_bytes(params)
        (self.parent / 'reference.npz').write_bytes(b'opaque-reference-fixture')
        self.manifest = dict(version=1, top=u.TOP, parallel_lanes=2,
                             format=copy.deepcopy(u.FORMAT), part='xazu3eg-sbva484-1-i',
                             clock_target_ns=10,
                             generated_sha256={n: u.digest((self.parent / n).read_bytes()) for n in u.FILES})
        self.save_manifest()

    def tearDown(self):
        self.tmp.cleanup()

    def save_manifest(self):
        (self.parent / 'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')

    def rehash(self, name):
        self.manifest['generated_sha256'][name] = u.digest((self.parent / name).read_bytes())
        self.save_manifest()

    def prepare(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return c.prepare(self.parent, self.out)

    def test_parent_and_binary_vectors_are_preserved(self):
        before = {n: (self.parent / n).read_bytes() for n in u.FILES + ('manifest.json',)}
        result = self.prepare()
        self.assertEqual(before, {n: (self.parent / n).read_bytes() for n in before})
        for name in u.BINARY_FILES:
            self.assertEqual((self.out / name).read_bytes(), before[name])
        self.assertEqual(result['format'], u.FORMAT)
        self.assertEqual(result['cache_slots'], 2)
        self.assertEqual(result['cache_control_cases'], 17)
        self.assertEqual(result['parent_manifest_sha256'], u.digest(before['manifest.json']))
        for name, digest in result['generated_sha256'].items():
            self.assertEqual(u.digest((self.out / name).read_bytes()), digest)

    def test_new_control_abi_and_status_contract_match_testbench(self):
        self.prepare()
        header = (self.out / 'precision_cnn.h').read_text()
        cpp = (self.out / 'precision_cnn.cpp').read_text()
        tb = (self.out / 'tb_precision_cnn.cpp').read_text()
        self.assertIn('unsigned int ' + c.TOP + '(', header)
        self.assertIn(c.SIGNATURE, header)
        for name in ('cache_slot', 'model_id', 'cache_command'):
            self.assertIn('mode=s_axilite port=' + name + ' bundle=control', cpp)
        self.assertIn('return reload?CACHE_LOADED:CACHE_HIT;', cpp)
        self.assertEqual(json.loads((self.out / 'cache_contract.json').read_text()), c.CONTRACT)
        self.assertEqual(tb.count('    CASE('), 17)

    def test_two_parameter_slots_valid_flag_reset_and_original_compute_lanes(self):
        self.prepare()
        cpp = (self.out / 'precision_cnn.cpp').read_text()
        self.assertIn('weights[2][CNN_WEIGHTS]', cpp)
        self.assertIn('biases[2][168]', cpp)
        self.assertIn('variable=weights complete dim=1', cpp)
        self.assertIn('variable=weights cyclic factor=2 dim=2', cpp)
        self.assertIn('#pragma HLS RESET variable=cache_valid', cpp)
        self.assertNotIn('#pragma HLS RESET variable=weights', cpp)
        self.assertEqual(cpp.count('#pragma HLS UNROLL'), 4)
        self.assertEqual(cpp.count('sum_t acc=partial[0]+partial[1];'), 4)
        self.assertIn('if(reload) {\nLOAD_WEIGHTS:', cpp)
        self.assertIn('cached_model_id[cache_slot]!=model_id', cpp)

    def test_unrecorded_source_mutation_is_refused(self):
        path = self.parent / 'precision_cnn.cpp'
        path.write_bytes(path.read_bytes() + b'// change\n')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_rehashed_source_mutation_is_still_refused(self):
        path = self.parent / 'precision_cnn.cpp'
        path.write_bytes(path.read_bytes() + b'// change\n')
        self.rehash('precision_cnn.cpp')
        with self.assertRaisesRegex(ValueError, 'differs from reviewed generator'):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_wrong_parent_top_format_or_lane_count_is_refused(self):
        original = copy.deepcopy(self.manifest)
        for key, value in (('top', c.TOP), ('parallel_lanes', 4), ('format', {})):
            self.manifest = copy.deepcopy(original)
            self.manifest[key] = value
            self.save_manifest()
            with self.assertRaisesRegex(ValueError, 'reviewed A16/W12 two-lane'):
                self.prepare()
            self.assertFalse(self.out.exists())

    def test_missing_or_extra_manifest_file_is_refused(self):
        self.manifest['generated_sha256']['../unexpected'] = 'x'
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'seven reviewed'):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_bad_or_truncated_vector_is_refused_when_rehashed(self):
        path = self.parent / 'vectors/precision_test.bin'
        original = path.read_bytes()
        for data in (original[:12], struct.pack('<I', 0) + original[4:], original[:-1]):
            path.write_bytes(data)
            self.rehash('vectors/precision_test.bin')
            with self.assertRaisesRegex(ValueError, '(Truncated|vector format|vector length)'):
                self.prepare()
            self.assertFalse(self.out.exists())

    def test_vector_parameter_mismatch_is_refused(self):
        path = self.parent / 'parameter_words.bin'
        path.write_bytes(struct.pack('<i', 1) + path.read_bytes()[4:])
        self.rehash('parameter_words.bin')
        with self.assertRaisesRegex(ValueError, 'parameter words differ'):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_out_of_range_parameters_are_refused(self):
        path = self.parent / 'parameter_words.bin'
        data = struct.pack('<i', 2048) + path.read_bytes()[4:]
        path.write_bytes(data)
        vector_path = self.parent / 'vectors/precision_test.bin'
        vector = vector_path.read_bytes()
        vector_path.write_bytes(vector[:32] + data + vector[32 + len(data):])
        self.rehash('parameter_words.bin')
        self.rehash('vectors/precision_test.bin')
        with self.assertRaisesRegex(ValueError, 'exceed the numerical contract'):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_existing_output_is_preserved(self):
        self.out.mkdir()
        marker = self.out / 'keep.txt'
        marker.write_text('keep')
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual(marker.read_text(), 'keep')

    def test_crlf_parent_is_accepted(self):
        for name in u.TEXT_FILES:
            path = self.parent / name
            path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
        self.prepare()
        self.assertNotIn(b'\r', (self.out / 'precision_cnn.cpp').read_bytes())

    def run_portable(self, mutation=None, argv=()):
        self.prepare()
        path = self.out / 'precision_cnn.cpp'
        if mutation:
            path.write_text(mutation(path.read_text()), encoding='utf-8')
        exe = self.out / ('precision_sim.exe' if __import__('os').name == 'nt' else 'precision_sim')
        built = subprocess.run([shutil.which('g++'), '-std=c++14', '-O2', '-DCNN_PORTABLE_TEST',
                                'precision_cnn.cpp', 'tb_precision_cnn.cpp', '-o', str(exe)],
                               cwd=self.out, capture_output=True, text=True, timeout=60)
        self.assertEqual(built.returncode, 0, built.stderr)
        return subprocess.run([str(exe), *argv], cwd=self.out, capture_output=True, text=True, timeout=60)

    @unittest.skipUnless(shutil.which('g++'), 'optional native C++ check requires g++')
    def test_native_cache_control_cases_pass(self):
        result = self.run_portable()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Cache-control cases: 17/17', result.stdout)
        self.assertIn('Raw-word mismatches: 0', result.stdout)

    @unittest.skipUnless(shutil.which('g++'), 'optional native C++ check requires g++')
    def test_native_warm_mode_preloads_two_slots_and_checks_hit_status(self):
        result = self.run_portable(argv=('--perf-warm',))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Mode: perf-warm', result.stdout)
        self.assertIn('Preload calls: 2', result.stdout)
        self.assertIn('Regression cache-status errors: 0', result.stdout)
        self.assertNotIn('CACHE invalid', result.stdout)

    @unittest.skipUnless(shutil.which('g++'), 'optional native C++ check requires g++')
    def test_native_cold_mode_checks_forced_load_status(self):
        result = self.run_portable(argv=('--perf-cold',))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Mode: perf-cold', result.stdout)
        self.assertIn('Preload calls: 0', result.stdout)
        self.assertIn('Regression cache-status errors: 0', result.stdout)
        self.assertNotIn('CACHE invalid', result.stdout)

    @unittest.skipUnless(shutil.which('g++'), 'optional native C++ check requires g++')
    def test_testbench_detects_always_reloading(self):
        result = self.run_portable(lambda text: re.sub(r'const bool reload=.*?;', 'const bool reload=true;', text, flags=re.S))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('CACHE same ID ignores changed bytes: FAIL', result.stdout)

    @unittest.skipUnless(shutil.which('g++'), 'optional native C++ check requires g++')
    def test_testbench_detects_missing_model_id_check(self):
        result = self.run_portable(lambda text: text.replace('|| cached_model_id[cache_slot]!=model_id', ''))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('CACHE ID change reloads B: FAIL', result.stdout)

    @unittest.skipUnless(shutil.which('g++'), 'optional native C++ check requires g++')
    def test_testbench_detects_ignored_force_reload(self):
        result = self.run_portable(lambda text: text.replace('cache_command==CACHE_FORCE_RELOAD || ', ''))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('CACHE force reload changed weights: FAIL', result.stdout)


if __name__ == '__main__':
    unittest.main()
