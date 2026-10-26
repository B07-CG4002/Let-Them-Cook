import contextlib
import copy
import io
import json
from pathlib import Path
import random
import struct
import tempfile
import unittest

from tools import precision_hls_prepare as h
from tools import precision_hls_unroll2 as u


class Unroll2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.parent = self.root / 'parent'
        self.parent.mkdir()
        self.out = self.root / 'parallel'
        source = h.make_sources(h.reviewed_sources(), 16, 6, 12, 13, 38)
        for name, value in source.items():
            (self.parent / name).write_bytes(value.encode())
        parameters = struct.pack('<53992i', *([0] * 53992))
        (self.parent / 'vectors').mkdir()
        vector = struct.pack('<8I', h.MAGIC, 1, 53992, 16, 6, 12, 13, 38)
        vector += parameters + bytes(1100 * 4 + 8 * 4 + 8 * 4 + 8)
        (self.parent / 'vectors/precision_test.bin').write_bytes(vector)
        (self.parent / 'parameter_words.bin').write_bytes(parameters)
        # This file is preserved opaquely; executable test vectors are checked separately.
        (self.parent / 'reference.npz').write_bytes(b'fixture-reference')
        self.manifest = dict(version=1, top='gesture_cnn_precision',
                             part='xazu3eg-sbva484-1-i', clock_target_ns=10,
                             format=copy.deepcopy(u.FORMAT),
                             generated_sha256={name: u.digest((self.parent / name).read_bytes())
                                               for name in u.FILES})
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
            return u.prepare(self.parent, self.out)

    def test_vectors_parameters_and_reference_are_reused_exactly(self):
        result = self.prepare()
        for name in u.BINARY_FILES:
            self.assertEqual((self.out / name).read_bytes(), (self.parent / name).read_bytes())
        for name, wanted in result['generated_sha256'].items():
            self.assertEqual(u.digest((self.out / name).read_bytes()), wanted)
        self.assertEqual(result['format'], self.manifest['format'])
        self.assertEqual(result['regression_samples'], 1)

    def test_all_layers_have_two_lanes_and_only_small_complete_partitions(self):
        self.prepare()
        cpp = (self.out / 'precision_cnn.cpp').read_text()
        for label, count in (('C1', 66), ('C2', 96), ('FC1', 704), ('FC2', 64)):
            self.assertIn(label + '_DOT: for(int base=0;base<' + str(count) + ';base+=2)', cpp)
            self.assertIn(label + '_LANES: for(int lane=0;lane<2;++lane)', cpp)
        self.assertEqual(cpp.count('#pragma HLS UNROLL'), 4)
        self.assertEqual(cpp.count('sum_t acc=partial[0]+partial[1];'), 4)
        self.assertEqual(cpp.count('variable=partial complete dim=1'), 4)
        self.assertEqual(cpp.count('cyclic factor=2 dim=1'), 5)
        self.assertEqual(cpp.count(' complete '), 4)
        parent = (self.parent / 'precision_cnn.cpp').read_text()
        self.assertEqual(cpp.count('#pragma HLS PIPELINE'), parent.count('#pragma HLS PIPELINE'))
        for name in u.TEXT_FILES:
            self.assertIn(u.TOP, (self.out / name).read_text())

    def test_parent_files_are_not_modified(self):
        before = {name: (self.parent / name).read_bytes() for name in u.FILES + ('manifest.json',)}
        self.prepare()
        self.assertEqual(before, {name: (self.parent / name).read_bytes() for name in before})

    def test_changed_parent_file_hash_is_refused_before_output(self):
        path = self.parent / 'precision_cnn.cpp'
        path.write_bytes(path.read_bytes() + b'// edited\n')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_rehashed_manual_source_edit_is_still_refused(self):
        path = self.parent / 'precision_cnn.cpp'
        path.write_bytes(path.read_bytes() + b'// edited\n')
        self.rehash('precision_cnn.cpp')
        with self.assertRaisesRegex(ValueError, 'differs from reviewed generator'):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_unsupported_parent_format_and_top_are_refused(self):
        original = copy.deepcopy(self.manifest)
        for field in ('format', 'top'):
            self.manifest = copy.deepcopy(original)
            if field == 'format':
                self.manifest[field]['accumulator_bits'] = 32
            else:
                self.manifest[field] = u.TOP
            self.save_manifest()
            with self.assertRaisesRegex(ValueError, 'reviewed A16'):
                self.prepare()
            self.assertFalse(self.out.exists())

    def test_wrong_or_truncated_vectors_are_refused_even_when_rehashed(self):
        path = self.parent / 'vectors/precision_test.bin'
        original = path.read_bytes()
        for value in (struct.pack('<I', 0) + original[4:], original[:-1]):
            path.write_bytes(value)
            self.rehash('vectors/precision_test.bin')
            with self.assertRaisesRegex(ValueError, 'vector (format|length)'):
                self.prepare()
            self.assertFalse(self.out.exists())

    def test_parameter_file_must_match_vector_parameters(self):
        path = self.parent / 'parameter_words.bin'
        data = path.read_bytes()
        path.write_bytes(struct.pack('<i', 1) + data[4:])
        self.rehash('parameter_words.bin')
        with self.assertRaisesRegex(ValueError, 'parameter words differ'):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_out_of_range_weights_are_refused(self):
        path = self.parent / 'parameter_words.bin'
        data = struct.pack('<i', 3000) + path.read_bytes()[4:]
        path.write_bytes(data)
        vector_path = self.parent / 'vectors/precision_test.bin'
        vector = vector_path.read_bytes()
        vector_path.write_bytes(vector[:32] + data + vector[32 + len(data):])
        self.rehash('parameter_words.bin')
        self.rehash('vectors/precision_test.bin')
        with self.assertRaisesRegex(ValueError, 'exceeds signed12'):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_existing_output_is_never_overwritten(self):
        self.out.mkdir()
        marker = self.out / 'keep.txt'
        marker.write_text('keep')
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual(marker.read_text(), 'keep')

    def test_crlf_checkout_does_not_change_generated_source(self):
        for name in u.TEXT_FILES:
            path = self.parent / name
            path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
        self.prepare()
        self.assertNotIn(b'\r', (self.out / 'precision_cnn.cpp').read_bytes())

    def test_partial_and_reduced_sums_fit_for_full_operand_ranges(self):
        rng = random.Random(42)
        for count in (66, 96, 704, 64):
            for sign in (-1, 1):
                products = [sign * (1 << 26)] * count
                for bias in (-(1 << 28), (1 << 28)):
                    lanes = [bias + sum(products[::2]), sum(products[1::2])]
                    self.assertEqual(sum(lanes), bias + sum(products))
                    self.assertTrue(all(-(1 << 37) <= v < (1 << 37)
                                        for v in (*lanes, sum(lanes) + (1 << 12))))
            for _ in range(20):
                products = [rng.randrange(-32768, 32768) * rng.randrange(-2048, 2048)
                            for _ in range(count)]
                bias = rng.randrange(-32768, 32768) * 8192
                self.assertEqual(bias + sum(products[::2]) + sum(products[1::2]),
                                 bias + sum(products))


if __name__ == '__main__':
    unittest.main()
