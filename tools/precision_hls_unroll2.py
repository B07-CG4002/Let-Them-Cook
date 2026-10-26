"""Prepare a NEW two-lane HLS experiment from the reviewed A16/W12 benchmark.

Reuses the parent's numeric format and exact vectors. No synthesis or board
operations are launched. Only the four dot-product loops and internal memory
banking change; AXI carriers, parameter reloads and rounding stay the same.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import struct

from tools import precision_hls_prepare as h


FORMAT = dict(activation_bits=16, activation_fraction=6,
              weight_bits=12, weight_fraction=13, accumulator_bits=38,
              minimum_full_range_accumulator_bits=37, axi_carrier_bits=32)
TEXT_FILES = ('precision_cnn.h', 'precision_cnn.cpp',
              'tb_precision_cnn.cpp', 'precision.cfg')
BINARY_FILES = ('vectors/precision_test.bin', 'parameter_words.bin', 'reference.npz')
FILES = TEXT_FILES + BINARY_FILES
TOP = 'gesture_cnn_precision_u2'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def transform(cpp):
    """Two independent, exact partial sums; round only after their reduction."""
    for label, count in (('C1', 66), ('C2', 96), ('FC1', 704), ('FC2', 64)):
        pattern = (
            r'(?P<indent> +)sum_t acc=(?P<bias>[^;\n]+);\n'
            r'\s*CNN_CHECK_SUM\(acc\);\n'
            + label + r'_DOT: for\(int d=0;d<' + str(count) + r';\+\+d\) \{\n'
            r'#pragma HLS PIPELINE II=1\n'
            r'\s*product_t product=(?P<product>[^;\n]+);\n'
            r'\s*acc\+=\(sum_t\)product;\n'
            r'\s*CNN_CHECK_SUM\(acc\);\n\s*\}'
        )

        def replacement(match):
            indent = match['indent']
            return (
                indent + 'sum_t partial[2];\n'
                '#pragma HLS ARRAY_PARTITION variable=partial complete dim=1\n'
                + indent + 'partial[0]=' + match['bias'] + ';\n'
                + indent + 'partial[1]=0;\n'
                + indent + 'CNN_CHECK_SUM(partial[0]);\n'
                + label + '_DOT: for(int base=0;base<' + str(count) + ';base+=2) {\n'
                '#pragma HLS PIPELINE II=1\n'
                + label + '_LANES: for(int lane=0;lane<2;++lane) {\n'
                '#pragma HLS UNROLL\n'
                + indent + '        const int d=base+lane;\n'
                + indent + '        product_t product=' + match['product'] + ';\n'
                + indent + '        partial[lane]+=(sum_t)product;\n'
                + indent + '        CNN_CHECK_SUM(partial[lane]);\n'
                + indent + '    }\n'
                + indent + '}\n'
                + indent + 'sum_t acc=partial[0]+partial[1];\n'
                + indent + 'CNN_CHECK_SUM(acc);'
            )

        cpp, matches = re.subn(pattern, replacement, cpp)
        if matches != 1:
            raise ValueError('Unexpected parent dot-product structure: ' + label)

    anchor = '#pragma HLS BIND_STORAGE variable=weights type=ram_2p impl=bram'
    if cpp.count(anchor) != 1:
        raise ValueError('Unexpected parent memory structure')
    banking = '\n'.join(
        '#pragma HLS ARRAY_PARTITION variable=' + name + ' cyclic factor=2 dim=1'
        for name in ('weights', 'input', 'pool1', 'pool2', 'hidden')
    )
    cpp = cpp.replace(anchor, banking + '\n' + anchor)
    cpp = cpp.replace('// GENERATED precision benchmark; same loop structure as reviewed baseline.',
                      '// GENERATED two-lane experiment; precision and exact vectors match parent.')
    cpp = cpp.replace('// No complete channel partitioning: reuse a small arithmetic datapath.',
                      '// Two dot-product lanes; cyclic banked memories and a two-register reduction.')
    return cpp.replace('gesture_cnn_precision', TOP)


def read_parent(parent):
    parent = Path(parent)
    manifest_path = parent / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if (manifest.get('version') != 1 or manifest.get('format') != FORMAT
            or manifest.get('top') != 'gesture_cnn_precision'
            or manifest.get('part') != 'xazu3eg-sbva484-1-i'
            or manifest.get('clock_target_ns') != 10):
        raise ValueError('Use the reviewed A16/F6, W12/F13, accumulator38 parent')
    hashes = manifest.get('generated_sha256', {})
    if set(hashes) != set(FILES):
        raise ValueError('Parent manifest must list exactly the seven generated files')
    data = {}
    for name in FILES:
        raw = (parent / name).read_bytes()
        normalized = raw.replace(b'\r\n', b'\n') if name in TEXT_FILES else raw
        if hashes[name] not in (digest(raw), digest(normalized)):
            raise ValueError('Parent file hash mismatch: ' + name)
        data[name] = normalized
    # Do not build on manual edits, different pragma sets, or an older generator.
    expected = h.make_sources(h.reviewed_sources(), 16, 6, 12, 13, 38)
    for name in TEXT_FILES:
        if data[name].decode('utf-8') != expected[name]:
            raise ValueError('Parent source differs from reviewed generator: ' + name)

    vector = data['vectors/precision_test.bin']
    if len(vector) < 32:
        raise ValueError('Truncated parent vector header')
    header = struct.unpack('<8I', vector[:32])
    magic, samples, parameters, aw, af, ww, wf, acc = header
    if (magic != h.MAGIC or not 1 <= samples <= 10000
            or (parameters, aw, af, ww, wf, acc) != (53992, 16, 6, 12, 13, 38)):
        raise ValueError('Parent vector format differs')
    if len(vector) != 32 + 53992 * 4 + samples * (1100 * 4 + 8 * 4 + 8 * 4 + 8):
        raise ValueError('Parent vector length differs')
    if data['parameter_words.bin'] != vector[32:32 + 53992 * 4]:
        raise ValueError('Parent parameter words differ from vectors')

    # Full representable operands bound every partial sum and the final reduction.
    words = struct.unpack('<53992i', data['parameter_words.bin'])
    if any(not -2048 <= value <= 2047 for value in words[:53824]):
        raise ValueError('Parent weight exceeds signed12')
    if any(not -32768 <= value <= 32767 for value in words[53824:]):
        raise ValueError('Parent bias exceeds signed16')
    bounds = {}
    for label, n, start, end in (('conv1', 66, 0, 32), ('conv2', 96, 32, 96),
                               ('fc1', 704, 96, 160), ('fc2', 64, 160, 168)):
        bias = max(abs(v) for v in words[53824 + start:53824 + end]) * (1 << 13)
        bound = n * (1 << 15) * (1 << 11) + bias + (1 << 12)
        if bound > (1 << 37) - 1:
            raise ValueError('Unsafe partial-sum/reduction bound: ' + label)
        bounds[label] = dict(absolute_sum_bound_including_rounding=bound,
                             signed_accumulator_bits=38)
    return manifest, data, samples, bounds, digest(manifest_path.read_bytes())


def prepare(parent, out):
    parent, out = Path(parent), Path(out)
    if out.exists():
        raise FileExistsError('Choose a NEW experiment folder: ' + str(out))
    manifest, data, samples, bounds, manifest_hash = read_parent(parent)
    output = dict(data)
    output['precision_cnn.cpp'] = transform(data['precision_cnn.cpp'].decode('utf-8')).encode()
    for name in ('precision_cnn.h', 'tb_precision_cnn.cpp', 'precision.cfg'):
        output[name] = data[name].replace(b'gesture_cnn_precision', TOP.encode())
    result = copy.deepcopy(manifest)
    result.update(status='prepared_not_synthesized', top=TOP,
                  changes='Two-lane dot products in all four layers; cyclic factor2 internal banking; numeric contract and parameter reload policy unchanged',
                  parallel_lanes=2, partial_sum_bounds=bounds,
                  parent_manifest_sha256=manifest_hash,
                  parent_generated_sha256=manifest['generated_sha256'],
                  regression_samples=samples,
                  validation='Requires new C simulation, synthesis and C/RTL co-simulation; no speedup claimed')
    result['generated_sha256'] = {name: digest(value) for name, value in output.items()}
    out.mkdir(parents=True)
    for name, value in output.items():
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    (out / 'manifest.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8', newline='\n')
    print('Prepared:', out.resolve())
    print('Top:', TOP, '| dot-product lanes: 2 | unchanged regression samples:', samples)
    print('Precision: A16/F6, W12/F13, accumulator38; AXI words remain signed32.')
    print('No Vitis command executed and no FPGA programmed.')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    prepare(args.parent, args.out)


if __name__ == '__main__':
    main()
