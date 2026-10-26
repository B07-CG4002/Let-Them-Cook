"""Prepare isolated HLS precision benchmarks from a completed paired-model sweep.

No synthesis, deployment or FPGA programming is performed. Existing HLS and
model files are read only. Generated experiments retain 32-bit AXI carrier words.
This first hardware benchmark supports the verified 22-feature/eight-class CNN.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import numpy as np
from tools import precision_sweep as q

ROOT=Path(__file__).resolve().parents[1]
REVIEWED={
    'full_cnn_20261020.h':'2d520974039bbd994f483f578d4f00dbedb85634',
    'full_cnn_20261020.cpp':'3d2b237c0350e670bd49c84e52b5692cec927df2',
    'tb_full_cnn_20261020.cpp':'f9dfa324daa90efd21ea5d42f6a174a0b5384e7d',
    'full_cnn_20261020.cfg':'d8dc0d8d1f346e8538f41218b1ba65ffe7050692',
}
MAGIC=0x50524331

def reviewed_sources(root=ROOT):
    result={}
    for name,expected in REVIEWED.items():
        path=Path(root)/'fpga/hls/full_cnn'/name
        data=path.read_bytes().replace(b'\r\n',b'\n')
        blob=b'blob '+str(len(data)).encode()+b'\0'+data
        if hashlib.sha1(blob).hexdigest()!=expected:
            raise ValueError('Baseline HLS source differs from reviewed version: '+str(path))
        result[name]=data.decode('utf-8')
    return result

def make_sources(original,aw,af,ww,wf,acc):
    if not 2<=aw<=32 or not 2<=ww<=32 or not 0<=af<=31 or not 0<=wf<=52 or not 2<=acc<=64:
        raise ValueError('Unsupported HLS precision')
    amin,amax=-(1<<(aw-1)),(1<<(aw-1))-1
    wmin,wmax=-(1<<(ww-1)),(1<<(ww-1))-1
    accmin='(-9223372036854775807LL-1)' if acc==64 else str(-(1<<(acc-1)))+'LL'
    accmax='9223372036854775807LL' if acc==64 else str((1<<(acc-1))-1)+'LL'
    header=f'''#ifndef PRECISION_CNN_H
#define PRECISION_CNN_H
// AXI remains signed32. Only internal values/arithmetic use narrower widths.
const int ACT_W={aw}, ACT_F={af}, WEIGHT_W={ww}, WEIGHT_F={wf}, ACC_W={acc};
const double ACT_SCALE={2**af}.0;
const long long ACT_MIN={amin}LL, ACT_MAX={amax}LL;
const long long WEIGHT_MIN={wmin}LL, WEIGHT_MAX={wmax}LL;
#ifdef CNN_PORTABLE_TEST
#include <cstdint>
#include <cassert>
typedef int32_t word_t;
typedef int32_t act_t;
typedef int32_t weight_word_t;
typedef int64_t sum_t;
typedef int64_t product_t;
inline void cnn_check_sum(int64_t x) {{ assert(x >= {accmin} && x <= {accmax}); }}
#define CNN_CHECK_SUM(x) cnn_check_sum(x)
#else
#include <ap_int.h>
typedef ap_int<32> word_t;
typedef ap_int<{aw}> act_t;
typedef ap_int<{ww}> weight_word_t;
typedef ap_int<{acc}> sum_t;
typedef ap_int<{aw+ww}> product_t;
#define CNN_CHECK_SUM(x) ((void)0)
#endif
const int CNN_INPUTS=1100, CNN_WEIGHTS=53824, CNN_PARAMETERS=53992, CNN_OUTPUTS=8;
void gesture_cnn_precision(const word_t input_words[CNN_INPUTS],
                           const word_t parameter_words[CNN_PARAMETERS],
                           word_t logit_words[CNN_OUTPUTS]);
#endif
'''
    cpp=original['full_cnn_20261020.cpp']
    cpp=cpp.replace('"full_cnn_20261020.h"','"precision_cnn.h"').replace('gesture_cnn_full','gesture_cnn_precision')
    cpp=re.sub(r'\bword_t\b','act_t',cpp)
    cpp=cpp.replace('const act_t input_words','const word_t input_words')
    cpp=cpp.replace('const act_t parameter_words','const word_t parameter_words')
    cpp=cpp.replace('act_t logit_words','word_t logit_words')
    expression=f'(value+((sum_t)1<<{wf-1}))>>{wf}' if wf else 'value'
    rounded=f'''static act_t round_activation(sum_t value) {{
#pragma HLS INLINE
    CNN_CHECK_SUM(value);
    sum_t rounded={expression};
    if (rounded>(sum_t)ACT_MAX) return (act_t)ACT_MAX;
    if (rounded<(sum_t)ACT_MIN) return (act_t)ACT_MIN;
    return (act_t)rounded;
}}'''
    cpp,count=re.subn(r'static act_t round_activation\(sum_t value\) \{.*?\n\}',lambda m:rounded,cpp,flags=re.S)
    if count!=1 or cpp.count('(sum_t)2097152')!=4:raise ValueError('Unexpected kernel structure')
    cpp=cpp.replace('(sum_t)2097152',f'(sum_t){2**wf}ULL')
    cpp,count=re.subn(r'(^\s*sum_t acc=.*;\n)',r'\1            CNN_CHECK_SUM(acc);\n',cpp,flags=re.M)
    if count!=4 or cpp.count('acc+=(sum_t)product;')!=4:raise ValueError('Unexpected accumulator structure')
    cpp=cpp.replace('acc+=(sum_t)product;','acc+=(sum_t)product;\n                CNN_CHECK_SUM(acc);')
    cpp='// GENERATED precision benchmark; same loop structure as reviewed baseline.\n'+cpp
    cpp=cpp.replace('// Input/activation/bias/logit: signed32 F18; weights: signed22 F21.',f'// Internal activation/bias/logit: signed{aw} F{af}; weights: signed{ww} F{wf}.')
    cpp=cpp.replace('// Exact signed64 F39 accumulation;',f'// Signed{acc} F{af+wf} accumulation;')
    cpp=cpp.replace('ap_int<32> * ap_int<22> produces exact signed54',f'ap_int<{aw}> * ap_int<{ww}> produces exact signed{aw+ww}')
    tb=original['tb_full_cnn_20261020.cpp'].replace('"full_cnn_20261020.h"','"precision_cnn.h"')
    tb=tb.replace('full_cnn_test.bin','precision_test.bin').replace('gesture_cnn_full','gesture_cnn_precision').replace('262144.0','ACT_SCALE')
    tb=tb.replace('FILE*f=std::fopen("precision_test.bin","rb");',
                  'FILE*f=std::fopen("precision_test.bin","rb");\n    if(!f)f=std::fopen("vectors/precision_test.bin","rb");')
    tb=tb.replace('uint32_t header[3]','uint32_t header[8]')
    old='std::fread(header,4,3,f)!=3||header[0]!=0x434E4E38||header[1]==0||header[1]>10000||header[2]!=CNN_PARAMETERS'
    new='std::fread(header,4,8,f)!=8||header[0]!=0x50524331||header[1]==0||header[1]>10000||header[2]!=CNN_PARAMETERS||header[3]!=ACT_W||header[4]!=ACT_F||header[5]!=WEIGHT_W||header[6]!=WEIGHT_F||header[7]!=ACC_W'
    if old not in tb:raise ValueError('Unexpected testbench header')
    tb=tb.replace(old,new)
    needle='    int word_errors=0,agreement=0'
    if needle not in tb:raise ValueError('Unexpected parameter read')
    tb=tb.replace(needle,'''    for(int i=0;i<CNN_PARAMETERS;++i) {
        long long value=(long long)params[i];
        long long lo=i<CNN_WEIGHTS?WEIGHT_MIN:ACT_MIN, hi=i<CNN_WEIGHTS?WEIGHT_MAX:ACT_MAX;
        if(value<lo||value>hi){std::cerr<<"Parameter out of range\\n";std::fclose(f);return 2;}
    }
    int word_errors=0,agreement=0''')
    needle='        gesture_cnn_precision(input,params,output);'
    if needle not in tb:raise ValueError('Unexpected kernel call')
    tb=tb.replace(needle,'''        for(int i=0;i<CNN_INPUTS;++i) {
            long long value=(long long)input[i];
            if(value<ACT_MIN||value>ACT_MAX){std::cerr<<"Input out of range\\n";std::fclose(f);return 2;}
        }
        gesture_cnn_precision(input,params,output);''')
    cfg=original['full_cnn_20261020.cfg'].replace('gesture_cnn_full','gesture_cnn_precision')
    cfg=cfg.replace('tb_full_cnn_20261020.cpp','tb_precision_cnn.cpp')
    cfg=cfg.replace('full_cnn_20261020.cpp','precision_cnn.cpp').replace('full_cnn_20261020.h','precision_cnn.h')
    cfg=cfg.replace('full_cnn_test.bin','precision_test.bin')
    return {'precision_cnn.h':header,'precision_cnn.cpp':cpp,'tb_precision_cnn.cpp':tb,'precision.cfg':cfg}

def choose_candidate(report,aw,ww):
    if report.get('status')!='completed':raise ValueError('Use a completed sweep report')
    matches=[r for r in report['candidates'] if r['activation_bits']==aw and r['weight_bits']==ww]
    if len(matches)!=1:raise ValueError('Expected exactly one matching candidate; use a report without duplicate formats')
    row=matches[0]
    if not row.get('passes_regression_screen'):raise ValueError('Selected candidate failed the software screen')
    return row

def prepare(args):
    out=Path(args.out)
    if out.exists():raise FileExistsError('Choose a NEW experiment folder: '+str(out))
    original=reviewed_sources()
    report=q.read_json(args.sweep);row=choose_candidate(report,args.activation_bits,args.weight_bits)
    source=q.load_source(weights=args.weights)
    if source['mode']!='legacy_paired_regression' or report.get('scope')!=source['mode']:
        raise ValueError('This HLS benchmark is for the verified paired 22/8 model, not the final 12/7 deployment')
    x,y,cal,data=q.load_data(args.data,source,'validation')
    if sorted(source['provenance'].values())!=sorted(report.get('source_sha256',{}).values()):raise ValueError('Sweep/model hashes differ')
    if sorted(data['data_sha256'].values())!=sorted(report.get('data',{}).get('data_sha256',{}).values()):raise ValueError('Sweep/input hashes differ')
    p=source['parameters'];aw=row['activation_bits'];af=row['activation_fraction_bits'];ww=row['weight_bits'];wf=row['weight_fraction_bits']
    logits,stats=q.run_quantized(x,p,aw,af,ww,wf)
    floating=np.array([q.float_layers(v,p)['logits'] for v in x])
    comparison=q.compare(floating,logits,y,report['threshold'],report['threshold_rule'])
    if stats['clipping_events'] or comparison['class_changes'] or comparison['emitted_label_changes'] or comparison['max_probability_error']>report['max_probability_error_limit']:
        raise ValueError('Fresh recomputation failed the software screen')
    for name in ('class_agreement','emitted_label_changes'):
        if comparison[name]!=row[name]:raise ValueError('Sweep metrics differ from fresh recomputation')
    if abs(comparison['max_probability_error']-row['max_probability_error'])>1e-6:raise ValueError('Probability metrics differ')
    required=max(stats['full_range_accumulator_bits'].values())
    acc=min(64,required+1)
    files=make_sources(original,aw,af,ww,wf,acc)
    params={n:q.quantize(v,ww if n.endswith('_w') else aw,wf if n.endswith('_w') else af)[0] for n,v in p.items()}
    parameter_words=np.concatenate([params[n].ravel() for n in q.NAMES]).astype('<i4')
    input_words=q.quantize(x,aw,af)[0].astype('<i4');expected=np.rint(logits*(2.**af)).astype('<i4')
    floats=floating.astype('<f4')
    if not np.array_equal(floats.argmax(1),floating.argmax(1)):raise ValueError('Float32 golden class is ambiguous; use different reference serialization')
    out.mkdir(parents=True);(out/'vectors').mkdir()
    for name,text in files.items():(out/name).write_text(text,encoding='utf-8',newline='\n')
    vectors=out/'vectors/precision_test.bin'
    with vectors.open('wb') as stream:
        stream.write(struct.pack('<8I',MAGIC,len(x),len(parameter_words),aw,af,ww,wf,acc));parameter_words.tofile(stream)
        for words,wanted,fl,label in zip(input_words,expected,floats,y):
            words.tofile(stream);wanted.tofile(stream);fl.tofile(stream);stream.write(struct.pack('<ii',int(label),int(fl.argmax())))
    parameter_words.tofile(out/'parameter_words.bin')
    np.savez_compressed(out/'reference.npz',input_words=input_words,parameter_words=parameter_words,
                        expected_words=expected,float_logits=floats,labels=y,class_names=np.array(source['classes']))
    manifest=dict(version=1,status='prepared_not_synthesized',scope='Legacy paired 22-feature/eight-class precision benchmark only',
                  format=dict(activation_bits=aw,activation_fraction=af,weight_bits=ww,weight_fraction=wf,
                              accumulator_bits=acc,minimum_full_range_accumulator_bits=required,axi_carrier_bits=32),
                  source_sha256=source['provenance'],data=data,sweep_sha256=q.digest(args.sweep),
                  recomputed_comparison=comparison,numeric_ranges=stats,
                  part='xazu3eg-sbva484-1-i',clock_target_ns=10,top='gesture_cnn_precision',
                  changes='Internal widths/scales only; same loop structure, II requests, parameter reload policy and AXI word width',
                  caution='Requires a candidate-specific driver and later controlled board validation. Do not load with the production worker.')
    manifest['generated_sha256']={n:q.digest(out/n) for n in (*files,'vectors/precision_test.bin','parameter_words.bin','reference.npz')}
    q.write_json(out/'manifest.json',manifest)
    print(json.dumps(manifest['format'],indent=2));print('Prepared:',out.resolve())
    print('No Vitis command executed and no FPGA programmed.')
    return manifest

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--weights',type=Path,required=True);p.add_argument('--data',type=Path,required=True)
    p.add_argument('--sweep',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--activation-bits',type=int,required=True);p.add_argument('--weight-bits',type=int,required=True)
    prepare(p.parse_args(argv))

if __name__=='__main__':main()
