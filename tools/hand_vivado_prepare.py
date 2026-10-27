"""Prepare an isolated hand-CNN Vivado project; never program an FPGA."""
from __future__ import annotations
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import math
import shutil
import sys
import xml.etree.ElementTree as ET

PART = 'xazu3eg-sbva484-1-i'
TOP = 'gesture_cnn_hand_u2_cache'
INSTANCE = TOP + '_0'
OLD = 'gesture_cnn_full_0'
REVIEWED_BD = '2e7434cbaa3812d48bee84e5b0ebcb216fefd8f6cc020f88fd4714ea0210fcbe'
FORMAT = dict(activation_bits=32, activation_fraction=18, weight_bits=22,
              weight_fraction=21, accumulator_bits=64, axi_carrier_bits=32)
NS = {'s': 'http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009'}
TEMPLATES = Path(__file__).with_name('hand_vivado_templates')


def digest(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read_json(p):
    return json.loads(Path(p).read_text(encoding='utf-8-sig'))


def write_json(p, value):
    Path(p).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def inside(root, name):
    rel = PurePosixPath(name)
    if not name or rel.is_absolute() or '..' in rel.parts or '\\' in name or ':' in name:
        raise ValueError(f'Unsafe relative file name: {name}')
    p = root.joinpath(*rel.parts)
    if not p.resolve().is_relative_to(root.resolve()) or p.is_symlink():
        raise ValueError(f'File is outside snapshot: {name}')
    return p


def tree_hashes(root):
    result = {}
    for p in sorted(root.rglob('*')):
        if p.is_symlink():
            raise ValueError(f'Symlinks are not supported: {p}')
        if p.is_file():
            result[p.relative_to(root).as_posix()] = digest(p)
    return result


def validate_ip(ip):
    component = ip / 'component.xml'
    root = ET.parse(component).getroot()
    vlnv = ':'.join(root.findtext('s:' + n, namespaces=NS) or ''
                    for n in ('vendor', 'library', 'name', 'version'))
    if vlnv != 'xilinx.com:hls:' + TOP + ':1.0':
        raise ValueError(f'Wrong packaged IP: {vlnv}')
    buses = {x.findtext('s:name', namespaces=NS): x
             for x in root.findall('s:busInterfaces/s:busInterface', NS)}
    if set(buses) != {'s_axi_control', 'm_axi_gmem0', 'm_axi_gmem1',
                     'ap_clk', 'ap_rst_n', 'interrupt'}:
        raise ValueError('Unexpected IP interfaces')
    for n in ('m_axi_gmem0', 'm_axi_gmem1'):
        if buses[n].find('s:master', NS) is None:
            raise ValueError(f'{n} must be an AXI master')
    if buses['s_axi_control'].find('s:slave', NS) is None:
        raise ValueError('Control interface must be AXI slave')
    registers = {}
    for r in root.findall('s:memoryMaps/s:memoryMap/s:addressBlock/s:register', NS):
        registers[r.findtext('s:name', namespaces=NS)] = int(r.findtext('s:addressOffset', namespaces=NS), 0)
    required = {'CTRL', 'ap_return', 'input_words_1', 'input_words_2',
                'parameter_words_1', 'parameter_words_2', 'logit_words_1',
                'logit_words_2', 'cache_slot', 'model_id', 'cache_command'}
    if not required <= registers.keys():
        raise ValueError('Packaged IP lacks the independent-hand register contract')
    names = {n.text for n in root.findall('s:fileSets/s:fileSet/s:file/s:name', NS)}
    if not names or not any(n.endswith('.v') for n in names):
        raise ValueError('Packaged synthesis RTL is absent')
    for name in names:
        if not inside(ip, name).is_file():
            raise ValueError(f'Packaged IP file missing: {name}')
    return {'vlnv': vlnv, 'registers': registers, 'referenced_files': len(names)}


def seed_design(source):
    if canonical(source) != REVIEWED_BD:
        raise ValueError('Source BD differs from the reviewed 20 October full-CNN design. No output created.')
    result = copy.deepcopy(source)
    d = result['design']
    # Retain the complete PS/DDR configuration, routing fabric and reset wiring.
    del d['components'][OLD]
    del d['design_tree'][OLD]
    d['addressing'].pop('/' + OLD)
    segments = d['addressing']['/zynq_ultra_ps_e_0']['address_spaces']['Data']['segments']
    for key in list(segments):
        if segments[key]['address_block'].startswith('/' + OLD + '/'):
            del segments[key]
    for key in list(d['interface_nets']):
        if any(p.startswith(OLD + '/') for p in d['interface_nets'][key]['interface_ports']):
            del d['interface_nets'][key]
    for net in d['nets'].values():
        net['ports'] = [p for p in net['ports'] if not p.startswith(OLD + '/')]
    d['design_info']['validated'] = 'false'
    d['design_info']['gen_directory'] = 'generated'
    return result



def check_windows_path_lengths(out, names):
    """Reject known generated paths at the Windows 260-byte limit."""
    root = PureWindowsPath(str(out))
    prefixes = (
        root / 'ip',
        root / 'sources/multiply_accumulate/generated/ip/multiply_accumulate_gesture_cnn_hand_u2_cache_0_0',
        root / 'project/cg4002_hand12.ip_user_files/bd/multiply_accumulate/ip/multiply_accumulate_gesture_cnn_hand_u2_cache_0_0',
    )
    paths = [str(base / name) for base in prefixes for name in names]
    longest = max(paths, key=lambda p: len(p.encode('utf-8')))
    length = len(longest.encode('utf-8'))
    if length >= 260:
        raise ValueError(f'Projected Windows path is {length} bytes (260-byte limit): {longest}. '
                         'Use a short output folder such as C:/cg4/h12_v2. No output created.')
    return length


def prepare(source_bd, hls, out):
    source_bd, hls, out = map(lambda p: Path(p).resolve(), (source_bd, hls, out))
    if out.exists():
        raise FileExistsError(f'Output already exists; use a new experiment name: {out}')
    # Validation precedes output directory creation.
    seed = seed_design(read_json(source_bd))
    m = read_json(hls / 'manifest.json')
    if (m.get('top'), m.get('part'), m.get('format'), m.get('cache_slots'), m.get('parallel_lanes')) != (
            TOP, PART, FORMAT, 2, 2):
        raise ValueError('HLS manifest differs from the reviewed hand12 A32/F18 W22/F21 contract')
    if m.get('clock_target_ns') != 10:
        raise ValueError('Expected 10 ns HLS target')
    for name, expected in m['generated_sha256'].items():
        p = inside(hls, name)
        data = p.read_bytes()
        hashes = {hashlib.sha256(data).hexdigest()}
        if p.suffix in {'.cpp', '.h', '.cfg', '.json'}:
            hashes.add(hashlib.sha256(data.replace(b'\r\n', b'\n')).hexdigest())
        if expected not in hashes:
            raise ValueError(f'HLS source/bundle changed since generation: {name}')
    ip = hls / 'hls_component/hls/impl/ip'
    contract = validate_ip(ip)
    hashes = tree_hashes(ip)
    if sys.platform == 'win32':
        check_windows_path_lengths(out, hashes)
    out.mkdir(parents=True)
    try:
        (out / 'sources/multiply_accumulate').mkdir(parents=True)
        shutil.copy2(source_bd, out / 'reviewed_source.bd')
        write_json(out / 'sources/multiply_accumulate/multiply_accumulate.bd', seed)
        shutil.copytree(ip, out / 'ip')
        if tree_hashes(out / 'ip') != hashes:
            raise ValueError('IP changed while snapshotting; retry with a fresh output folder')
        shutil.copy2(hls / 'manifest.json', out / 'hls_manifest.json')
        (out / 'runner').mkdir()
        for name in ('setup.tcl', 'build.tcl', 'run.ps1'):
            shutil.copy2(TEMPLATES / name, out / 'runner' / name)
        shutil.copy2(Path(__file__), out / 'runner/hand_vivado_prepare.py')
        frozen = tree_hashes(out)
        manifest = dict(version=1, status='prepared_not_vivado_validated',
            created_utc=datetime.now(timezone.utc).isoformat(),
            scope='independent_hand_hardware_build_only', physical_board_validated=False,
            gesture_accuracy_validated=False, part=PART, top=TOP, instance=INSTANCE,
            format=FORMAT, source_bd_path=str(source_bd), source_bd_sha256=digest(source_bd),
            ps_configuration_sha256=canonical(seed['design']['components']['zynq_ultra_ps_e_0']),
            ip_contract=contract, ip_tree_sha256=hashes, prepared_sha256=frozen)
        write_json(out / 'build_manifest.json', manifest)
    except BaseException:
        # Keep a failed preparation inspectable; never remove user files.
        (out / 'PREPARATION_FAILED.txt').write_text('Preparation incomplete. Use a new output directory.\n')
        raise
    return out


def verify(experiment, phase):
    e = Path(experiment).resolve()
    m = read_json(e / 'build_manifest.json')
    if tree_hashes(e / 'ip') != m['ip_tree_sha256']:
        raise ValueError('Frozen IP files changed')
    for name, expected in m['prepared_sha256'].items():
        if name.startswith('ip/') or name == 'sources/multiply_accumulate/multiply_accumulate.bd':
            continue
        if digest(inside(e, name)) != expected:
            raise ValueError(f'Frozen input changed: {name}')
    bd = e / 'sources/multiply_accumulate/multiply_accumulate.bd'
    if phase == 'Validate':
        if (e / 'project').exists():
            raise FileExistsError('Validation already attempted. Preserve logs and prepare hand12_v2 for a retry.')
        if digest(bd) != m['prepared_sha256']['sources/multiply_accumulate/multiply_accumulate.bd']:
            raise ValueError('Prepared seed BD changed')
    else:
        record = read_json(e / 'validation_record.json')
        if not (e / 'setup_complete.txt').is_file() or digest(bd) != record['validated_bd_sha256']:
            raise ValueError('Validated BD changed or setup did not finish')
        if (e / 'results').exists():
            raise FileExistsError('Build already attempted; preserve its logs and use a new experiment for retry')
    return e


def record_validation(e):
    e = Path(e).resolve()
    if not (e / 'setup_complete.txt').is_file():
        raise ValueError('Vivado setup completion marker absent')
    bd = e / 'sources/multiply_accumulate/multiply_accumulate.bd'
    d = read_json(bd)['design']
    if INSTANCE not in d['components'] or OLD in d['components']:
        raise ValueError('Vivado did not create the expected independent-hand instance')
    write_json(e / 'validation_record.json', {'validated_bd_sha256': digest(bd),
               'scope': 'Vivado block design validation only; no physical-board test'})


def timing_values(text):
    """Read Vivado's design timing table, not a pass-looking footer."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if 'WNS(ns)' not in line or 'WHS(ns)' not in line or 'WPWS(ns)' not in line:
            continue
        for row in lines[i+1:i+7]:
            words = row.split()
            if len(words) == 12:
                try:
                    values = [float(x) for x in words]
                except ValueError:
                    continue
                if any(not math.isfinite(x) for x in values):
                    raise ValueError('Nonfinite timing summary')
                if min(values[0], values[4], values[8]) < 0 or any(values[j] != 0 for j in (2, 6, 10)):
                    raise ValueError('Routed timing violations; artifacts not exported')
                return dict(wns_ns=values[0], whs_ns=values[4], wpws_ns=values[8])
    raise ValueError('Could not parse complete routed timing table; artifacts not exported')


def finalize(e):
    e = Path(e).resolve()
    if not (e / 'build_complete.txt').is_file():
        raise ValueError('Vivado build completion marker absent')
    timing = timing_values((e / 'results/timing_summary.rpt').read_text(errors='replace'))
    bit = e / 'results/cnn_hand12_v1.bit'
    if not bit.is_file() or bit.stat().st_size < 100000:
        raise ValueError('Complete bitstream absent')
    candidates = list(e.glob('sources/**/multiply_accumulate.hwh')) + list(e.glob('project/**/multiply_accumulate.hwh'))
    valid = []
    required = {'input_words', 'parameter_words', 'logit_words', 'cache_slot', 'model_id', 'cache_command'}
    for p in candidates:
        root = ET.parse(p).getroot()
        modules = [x for x in root.iter('MODULE') if x.get('INSTANCE') == INSTANCE]
        if len(modules) != 1:
            continue
        registers = {x.get('NAME', '') for x in modules[0].iter('REGISTER')}
        # Pointer fields may be grouped or split into low/high register names.
        if all(any(r == n or r.startswith(n + '_') for r in registers) for n in required):
            valid.append(p)
    if not valid or len({digest(p) for p in valid}) != 1:
        raise ValueError('No unambiguous HWH with independent-hand registers; artifacts not exported')
    target = e / 'artifacts'
    target.mkdir(exist_ok=False)
    shutil.copy2(bit, target / bit.name)
    shutil.copy2(valid[0], target / 'cnn_hand12_v1.hwh')
    manifest = read_json(e / 'build_manifest.json')
    result = dict(status='built_pending_board_validation', scope=manifest['scope'],
                  part=PART, instance=INSTANCE, format=FORMAT, timing=timing,
                  hwh_source=str(valid[0].relative_to(e)), sha256=tree_hashes(target),
                  input_manifest_sha256=digest(e / 'build_manifest.json'),
                  synthetic_weights_are_not_gesture_accuracy=True,
                  physical_board_validated=False, production_deployment_changed=False)
    write_json(target / 'artifact_manifest.json', result)
    return target


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    a = sub.add_parser('prepare')
    a.add_argument('--source-bd', required=True)
    a.add_argument('--hls', required=True)
    a.add_argument('--out', required=True)
    a = sub.add_parser('verify'); a.add_argument('--experiment', required=True)
    a.add_argument('--phase', choices=('Validate', 'Build'), required=True)
    for name in ('record-validation', 'finalize'):
        sub.add_parser(name).add_argument('--experiment', required=True)
    a = p.parse_args()
    try:
        if a.command == 'prepare':
            out = prepare(a.source_bd, a.hls, a.out)
            print(f'PREPARED: {out}\nNext: run runner/run.ps1 with -Mode Validate.')
        elif a.command == 'verify':
            verify(a.experiment, a.phase); print('Frozen build inputs verified.')
        elif a.command == 'record-validation':
            record_validation(a.experiment); print('VIVADO BLOCK DESIGN VALIDATED. No FPGA programmed.')
        else:
            print(f'ARTIFACTS READY FOR REVIEW: {finalize(a.experiment)}')
    except (ValueError, OSError, KeyError, ET.ParseError) as error:
        print(f'STOPPED: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
