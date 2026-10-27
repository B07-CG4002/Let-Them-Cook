import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from tools import hand_vivado_prepare as h


def packaged_fixture(root):
    ip = root / 'hls_component/hls/impl/ip'
    ip.mkdir(parents=True)
    ns = h.NS['s']
    def node(parent, name, text=None):
        e = ET.SubElement(parent, '{' + ns + '}' + name);e.text = text;return e
    c = ET.Element('{' + ns + '}component')
    for name, text in [('vendor','xilinx.com'),('library','hls'),('name',h.TOP),('version','1.0')]:node(c,name,text)
    buses=node(c,'busInterfaces')
    for name in ('s_axi_control','m_axi_gmem0','m_axi_gmem1','ap_clk','ap_rst_n','interrupt'):
        b=node(buses,'busInterface');node(b,'name',name)
        if name.startswith('m_axi'):node(b,'master')
        elif name=='s_axi_control':node(b,'slave')
    block=node(node(node(c,'memoryMaps'),'memoryMap'),'addressBlock')
    for i,name in enumerate(('CTRL','ap_return','input_words_1','input_words_2','parameter_words_1','parameter_words_2','logit_words_1','logit_words_2','cache_slot','model_id','cache_command')):
        r=node(block,'register');node(r,'name',name);node(r,'addressOffset',str(i*4))
    f=node(node(node(c,'fileSets'),'fileSet'),'file');node(f,'name','hdl/core.v')
    (ip/'hdl').mkdir();(ip/'hdl/core.v').write_text('// Fixture only\n')
    ET.ElementTree(c).write(ip/'component.xml',encoding='utf-8')
    (root/'hand_cnn.h').write_text('fixture\n')
    m=dict(top=h.TOP,part=h.PART,format=h.FORMAT,cache_slots=2,parallel_lanes=2,clock_target_ns=10,generated_sha256={'hand_cnn.h':h.digest(root/'hand_cnn.h')})
    h.write_json(root/'manifest.json',m)
    return ip


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=h.TEMPLATES/'reviewed_full_cnn.bd'
        self.hls=self.root/'hls';self.ip=packaged_fixture(self.hls);self.out=self.root/'experiment'
    def prepare(self):return h.prepare(self.source,self.hls,self.out)

    def test_preserves_entire_ps_ddr_and_original_and_freezes_ip(self):
        before=h.digest(self.source);self.prepare()
        source=h.read_json(self.source)['design'];seed=h.read_json(self.out/'sources/multiply_accumulate/multiply_accumulate.bd')['design']
        self.assertEqual(seed['components']['zynq_ultra_ps_e_0'],source['components']['zynq_ultra_ps_e_0'])
        self.assertEqual(len(seed['components']['zynq_ultra_ps_e_0']['parameters']),930)
        self.assertEqual(h.digest(self.source),before);self.assertNotIn(h.OLD,json.dumps(seed))
        self.assertEqual(h.tree_hashes(self.ip),h.tree_hashes(self.out/'ip'));h.verify(self.out,'Validate')

    def test_changed_ddr_refused_before_output(self):
        source=h.read_json(self.source);source['design']['components']['zynq_ultra_ps_e_0']['parameters']['PSU__DDRC__BUS_WIDTH']['value']='16 Bit'
        p=self.root/'changed.bd';h.write_json(p,source)
        with self.assertRaises(ValueError):h.prepare(p,self.hls,self.out)
        self.assertFalse(self.out.exists())

    def test_crlf_source_accepted(self):
        p=self.root/'windows.bd';p.write_bytes(self.source.read_bytes().replace(b'\n',b'\r\n'));h.prepare(p,self.hls,self.out)

    def test_missing_packaged_rtl_refused(self):
        (self.ip/'hdl/core.v').unlink()
        with self.assertRaisesRegex(ValueError,'missing'):self.prepare()
        self.assertFalse(self.out.exists())

    def test_changed_hls_source_and_incompatible_precision_refused(self):
        (self.hls/'hand_cnn.h').write_text('changed')
        with self.assertRaisesRegex(ValueError,'changed'):self.prepare()
        self.assertFalse(self.out.exists())
        m=h.read_json(self.hls/'manifest.json');m['format']['activation_bits']=16;h.write_json(self.hls/'manifest.json',m)
        with self.assertRaisesRegex(ValueError,'contract'):self.prepare()

    def test_existing_experiment_and_modified_snapshot_refused(self):
        self.prepare()
        with self.assertRaises(FileExistsError):self.prepare()
        (self.out/'ip/hdl/core.v').write_text('changed')
        with self.assertRaisesRegex(ValueError,'IP files changed'):h.verify(self.out,'Validate')

    def test_build_requires_validation_and_unchanged_validated_bd(self):
        self.prepare()
        with self.assertRaises(OSError):h.verify(self.out,'Build')
        bd=self.out/'sources/multiply_accumulate/multiply_accumulate.bd';d=h.read_json(bd)
        d['design']['components'][h.INSTANCE]={'vlnv':'xilinx.com:hls:'+h.TOP+':1.0'}
        h.write_json(bd,d);(self.out/'setup_complete.txt').write_text('fixture')
        h.record_validation(self.out);h.verify(self.out,'Build');bd.write_text(bd.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'changed'):h.verify(self.out,'Build')

    def test_complete_timing_required_negative_hold_or_pulse_refused(self):
        header='WNS(ns) TNS(ns) Failing Total WHS(ns) THS(ns) Failing Total WPWS(ns) TPWS(ns) Failing Total\n------\n'
        good='0.500 0.000 0 123 0.040 0.000 0 123 0.500 0.000 0 123'
        self.assertEqual(h.timing_values(header+good)['wns_ns'],0.5)
        for text in (header+good.replace('0.040','-0.001'),header+good.replace('0.500 0.000 0 123','-0.100 -2.0 1 123'),'All constraints met. No table.'):
            with self.assertRaises(ValueError):h.timing_values(text)

    def make_results(self,wrong=False):
        (self.out/'results').mkdir();(self.out/'results/cnn_hand12_v1.bit').write_bytes(b'x'*100001)
        (self.out/'build_complete.txt').write_text('fixture')
        (self.out/'results/timing_summary.rpt').write_text('WNS(ns) TNS(ns) Failing Total WHS(ns) THS(ns) Failing Total WPWS(ns) TPWS(ns) Failing Total\n---\n0.500 0 0 123 0.040 0 0 123 0.500 0 0 123\n')
        p=self.out/'sources/generated/hw_handoff/multiply_accumulate.hwh';p.parent.mkdir(parents=True)
        name=h.OLD if wrong else h.INSTANCE
        regs=''.join('<REGISTER NAME="'+n+'"/>' for n in ('input_words','parameter_words','logit_words','cache_slot','model_id','cache_command'))
        p.write_text('<SYSTEM><MODULE INSTANCE="'+name+'">'+regs+'</MODULE></SYSTEM>');return p

    def test_legacy_hwh_refused_matching_artifacts_exported_with_scope(self):
        self.prepare();p=self.make_results(wrong=True)
        with self.assertRaisesRegex(ValueError,'HWH'):h.finalize(self.out)
        self.assertFalse((self.out/'artifacts').exists());p.write_text(p.read_text().replace(h.OLD,h.INSTANCE))
        target=h.finalize(self.out);self.assertEqual((target/'cnn_hand12_v1.hwh').read_bytes(),p.read_bytes())
        m=h.read_json(target/'artifact_manifest.json');self.assertFalse(m['physical_board_validated']);self.assertFalse(m['production_deployment_changed'])

    def test_conflicting_hwh_files_refused(self):
        self.prepare();p=self.make_results();other=self.out/'project/generated/multiply_accumulate.hwh';other.parent.mkdir(parents=True)
        other.write_text(p.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'unambiguous'):h.finalize(self.out)

    def test_windows_long_generated_path_refused_before_output(self):
        self.out=self.root.joinpath(*(['deep_folder']*25))
        with patch.object(h.sys,'platform','win32'):
            with self.assertRaisesRegex(ValueError,'260-byte limit'):self.prepare()
        self.assertFalse(self.out.exists())

    def test_known_long_hls_names_fit_short_windows_build_root(self):
        name='hdl/verilog/gesture_cnn_hand_u2_cache_gesture_cnn_hand_u2_cache_ap_int_const_ap_int_const_ap_int_unsigned_int_unsigbkb.dat'
        self.assertEqual(h.check_windows_path_lengths('C:/cg4/h12',[name]),245)
        with self.assertRaisesRegex(ValueError,'260-byte limit'):
            h.check_windows_path_lengths('C:/Users/ooiwe/OneDrive/Documents/CG4002/Let_Them_Cook/.comms-local/hand_vivado/hand12_v1',[name])

    def test_tcl_templates_complete_without_executing_vivado(self):
        import tkinter
        interp=tkinter.Tcl()
        for name in ('setup.tcl','build.tcl'):
            self.assertEqual(int(interp.call('info','complete',(h.TEMPLATES/name).read_text())),1)


if __name__=='__main__':unittest.main()
