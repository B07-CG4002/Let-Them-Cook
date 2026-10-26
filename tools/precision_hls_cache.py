"""Prepare an isolated two-slot parameter-cache experiment from reviewed U2 HLS.

The old paired 22-feature/eight-class numerical benchmark is retained. Two
parameter slots avoid reloads when alternating between same-architecture models.
No Vitis process, FPGA overlay or running worker is changed by preparation.
"""
import argparse
import copy
import json
from pathlib import Path
import struct

from tools import precision_hls_prepare as h
from tools import precision_hls_unroll2 as u

TOP = 'gesture_cnn_precision_u2_cache'
SIGNATURE = ('word_t logit_words[CNN_OUTPUTS], unsigned int cache_slot,\n'
             '                      unsigned int model_id, unsigned int cache_command)')
CONTRACT = {
    'version': 1, 'top': TOP, 'slots': 2,
    'arguments': ['input_words', 'parameter_words', 'logit_words',
                  'cache_slot', 'model_id', 'cache_command'],
    'commands': {'AUTO_INFER': 0, 'FORCE_RELOAD_INFER': 1, 'INVALIDATE_ALL': 2},
    'return_status': {'HIT': 0, 'LOADED': 1, 'INVALID_CONTROL': 2, 'INVALIDATED': 3},
    'rules': [
        'All scalar controls and return status use unsigned32 via AXI4-Lite.',
        'AUTO loads when the selected slot is invalid or its model_id differs.',
        'An AUTO hit deliberately ignores parameter_words; its buffer must still be a valid allocated pointer.',
        'Use a different model_id or FORCE_RELOAD after changing parameter bytes.',
        'The host must map model IDs to model hashes without reusing an ID for different parameters unless forcing reload.',
        'INVALIDATE_ALL clears both valid flags, writes zero output, returns INVALIDATED and performs no inference.',
        'Invalid slot (>=2) or command (>2) writes zero output and returns INVALID_CONTROL without changing slots.',
        'Hardware reset explicitly clears cache valid flags; parameter memories need not be reset.',
        'One call at a time: two slots share one CNN compute engine.',
        'Slots contain parameter sets, not independent input windows or gameplay state.',
        'This benchmark still requires the legacy 22-feature/eight-class model shape and A16/F6 W12/F13 contract.',
        'Use the generated register map for later drivers; do not reuse production register offsets.',
    ],
}


def read_parent(parent):
    parent = Path(parent)
    manifest_path = parent / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if (manifest.get('version') != 1 or manifest.get('top') != u.TOP
            or manifest.get('parallel_lanes') != 2 or manifest.get('format') != u.FORMAT
            or manifest.get('part') != 'xazu3eg-sbva484-1-i'
            or manifest.get('clock_target_ns') != 10):
        raise ValueError('Use the reviewed A16/W12 two-lane parent')
    if set(manifest.get('generated_sha256', {})) != set(u.FILES):
        raise ValueError('Parent must contain the seven reviewed generated files')
    data = {}
    for name in u.FILES:
        raw = (parent / name).read_bytes()
        normalized = raw.replace(b'\r\n', b'\n') if name in u.TEXT_FILES else raw
        if manifest['generated_sha256'][name] not in (u.digest(raw), u.digest(normalized)):
            raise ValueError('Parent file hash mismatch: ' + name)
        data[name] = normalized
    expected = h.make_sources(h.reviewed_sources(), 16, 6, 12, 13, 38)
    for name in u.TEXT_FILES:
        wanted = (u.transform(expected[name]) if name == 'precision_cnn.cpp'
                  else expected[name].replace('gesture_cnn_precision', u.TOP))
        if data[name].decode() != wanted:
            raise ValueError('Parent source differs from reviewed generator: ' + name)
    vector = data['vectors/precision_test.bin']
    if len(vector) < 32:
        raise ValueError('Truncated parent vectors')
    head = struct.unpack('<8I', vector[:32])
    if (head[0] != h.MAGIC or not 1 <= head[1] <= 10000
            or head[2:] != (53992, 16, 6, 12, 13, 38)):
        raise ValueError('Parent vector format differs')
    if len(vector) != 32 + 53992 * 4 + head[1] * (1100 * 4 + 8 * 4 + 8 * 4 + 8):
        raise ValueError('Parent vector length differs')
    parameters = data['parameter_words.bin']
    if parameters != vector[32:32 + 53992 * 4]:
        raise ValueError('Parent parameter words differ from vectors')
    values = struct.unpack('<53992i', parameters)
    if (any(not -2048 <= v <= 2047 for v in values[:53824])
            or any(not -32768 <= v <= 32767 for v in values[53824:])):
        raise ValueError('Parent parameters exceed the numerical contract')
    # Even the largest legal signed16 bias fits, for every partial and final sum.
    bound = 704 * (1 << 15) * (1 << 11) + (1 << 15) * (1 << 13) + (1 << 12)
    if bound > (1 << 37) - 1:
        raise ValueError('Unsafe full-range accumulator bound')
    return manifest, data, head[1], u.digest(manifest_path.read_bytes())


def cache_sources(data):
    cpp = data['precision_cnn.cpp'].decode()
    cpp = cpp.replace('void ' + u.TOP + '(', 'unsigned int ' + TOP + '(')
    cpp = cpp.replace('word_t logit_words[CNN_OUTPUTS])', SIGNATURE)
    if cpp.count('unsigned int ' + TOP + '(') != 1 or cpp.count(SIGNATURE) != 1:
        raise ValueError('Unexpected parent signature')
    cpp = cpp.replace('#pragma HLS INTERFACE mode=s_axilite port=return bundle=control',
                      '#pragma HLS INTERFACE mode=s_axilite port=cache_slot bundle=control\n'
                      '#pragma HLS INTERFACE mode=s_axilite port=model_id bundle=control\n'
                      '#pragma HLS INTERFACE mode=s_axilite port=cache_command bundle=control\n'
                      '#pragma HLS INTERFACE mode=s_axilite port=return bundle=control')
    weight_decl = 'static weight_word_t weights[CNN_WEIGHTS];'
    bias_decl = 'static act_t biases[168];'
    if cpp.count(weight_decl) != 1 or cpp.count(bias_decl) != 1:
        raise ValueError('Unexpected parent parameter declarations')
    cpp = cpp.replace(weight_decl, 'CACHE_WEIGHT_DECL').replace(bias_decl, 'CACHE_BIAS_DECL')
    cpp = cpp.replace('weights[', 'weights[cache_slot][').replace('biases[', 'biases[cache_slot][')
    cpp = cpp.replace('CACHE_WEIGHT_DECL', 'static weight_word_t weights[2][CNN_WEIGHTS];')
    cpp = cpp.replace('CACHE_BIAS_DECL', 'static act_t biases[2][168];\n'
                      '    static bool cache_valid[2]={false,false};\n'
                      '    static unsigned int cached_model_id[2]={0,0};\n'
                      '#pragma HLS ARRAY_PARTITION variable=cache_valid complete dim=1\n'
                      '#pragma HLS ARRAY_PARTITION variable=cached_model_id complete dim=1\n'
                      '#pragma HLS RESET variable=cache_valid')
    cpp = cpp.replace('#pragma HLS ARRAY_PARTITION variable=weights cyclic factor=2 dim=1',
                      '#pragma HLS ARRAY_PARTITION variable=weights complete dim=1\n'
                      '#pragma HLS ARRAY_PARTITION variable=weights cyclic factor=2 dim=2\n'
                      '#pragma HLS ARRAY_PARTITION variable=biases complete dim=1')
    marker = 'LOAD_INPUT:'
    if cpp.count(marker) != 1:
        raise ValueError('Unexpected parent input loader')
    control = '''    // Reject bad controls before any parameter/input access; preserve cache contents.
    if(cache_slot>=2 || cache_command>CACHE_INVALIDATE_ALL) {
BAD_CONTROL_OUTPUT: for(int i=0;i<CNN_OUTPUTS;++i) {
#pragma HLS PIPELINE II=1
            logit_words[i]=0;
        }
        return CACHE_INVALID_CONTROL;
    }
    if(cache_command==CACHE_INVALIDATE_ALL) {
        cache_valid[0]=false;cache_valid[1]=false;
CLEAR_OUTPUT: for(int i=0;i<CNN_OUTPUTS;++i) {
#pragma HLS PIPELINE II=1
            logit_words[i]=0;
        }
        return CACHE_INVALIDATED;
    }
    const bool reload=(cache_command==CACHE_FORCE_RELOAD || !cache_valid[cache_slot]
                       || cached_model_id[cache_slot]!=model_id);
'''
    cpp = cpp.replace(marker, control + '\n' + marker)
    start, end = cpp.index('LOAD_WEIGHTS:'), cpp.index('C1_OC:')
    loader = cpp[start:end]
    cpp = (cpp[:start] + '    if(reload) {\n' + loader
           + '        cached_model_id[cache_slot]=model_id;\n'
           + '        cache_valid[cache_slot]=true;\n    }\n' + cpp[end:])
    if not cpp.endswith('}\n'):
        raise ValueError('Unexpected parent function ending')
    cpp = cpp[:-2] + '    return reload?CACHE_LOADED:CACHE_HIT;\n}\n'
    cpp = cpp.replace('// Reload parameters on every invocation for a simple, explicit contract.',
                      '// Two tagged parameter slots; reload only on miss, ID change or force.')
    cpp = cpp.replace('// GENERATED two-lane experiment; precision and exact vectors match parent.',
                      '// GENERATED two-slot cached experiment; two-lane arithmetic matches parent.')
    header = data['precision_cnn.h'].decode().replace('void ' + u.TOP + '(',
                                                    'unsigned int ' + TOP + '(')
    header = header.replace('word_t logit_words[CNN_OUTPUTS])', SIGNATURE)
    constants = '''const unsigned int CACHE_AUTO=0, CACHE_FORCE_RELOAD=1, CACHE_INVALIDATE_ALL=2;
const unsigned int CACHE_HIT=0, CACHE_LOADED=1, CACHE_INVALID_CONTROL=2, CACHE_INVALIDATED=3;
'''
    header = header.replace('unsigned int ' + TOP + '(', constants + 'unsigned int ' + TOP + '(')
    cfg = data['precision.cfg'].decode().replace(u.TOP, TOP)
    return {'precision_cnn.cpp': cpp.encode(), 'precision_cnn.h': header.encode(),
            'precision.cfg': cfg.encode(), 'tb_precision_cnn.cpp': testbench().encode()}


def testbench():
    return r'''#include "precision_cnn.h"
#include <cstdint>
#include <cstdio>
#include <cmath>
#include <cstring>
#include <iostream>
#include <algorithm>
static bool read_words(FILE*f,word_t*out,int n) {
    for(int i=0;i<n;++i){int32_t v;if(std::fread(&v,4,1,f)!=1)return false;out[i]=v;}return true;
}
static bool check_case(const char*name,const word_t*input,const word_t*params,
                       const word_t*expected,unsigned slot,unsigned id,unsigned command,
                       unsigned wanted_status) {
    word_t output[8];
    unsigned status=gesture_cnn_precision_u2_cache(input,params,output,slot,id,command);
    bool pass=status==wanted_status;
    for(int i=0;i<8;++i)if(output[i]!=expected[i])pass=false;
    std::cout<<"CACHE "<<name<<": "<<(pass?"PASS":"FAIL")
             <<" status="<<status<<" expected="<<wanted_status<<"\n";
    return pass;
}
int main(int argc,char**argv) {
    const bool perf_cold=argc==2 && std::strcmp(argv[1],"--perf-cold")==0;
    const bool perf_warm=argc==2 && std::strcmp(argv[1],"--perf-warm")==0;
    if(argc>1 && !perf_cold && !perf_warm){std::cerr<<"Unknown mode\n";return 2;}
    FILE*f=std::fopen("precision_test.bin","rb");
    if(!f)f=std::fopen("vectors/precision_test.bin","rb");
    if(!f){std::cerr<<"Missing precision_test.bin\n";return 2;}
    uint32_t header[8];
    if(std::fread(header,4,8,f)!=8||header[0]!=0x50524331||header[1]==0||header[1]>10000||header[2]!=CNN_PARAMETERS||header[3]!=ACT_W||header[4]!=ACT_F||header[5]!=WEIGHT_W||header[6]!=WEIGHT_F||header[7]!=ACC_W){std::fclose(f);return 2;}
    static word_t params[CNN_PARAMETERS],input[1100],output[8],expected[8];
    static word_t first_input[1100],first_expected[8];
    if(!read_words(f,params,CNN_PARAMETERS)){std::fclose(f);return 2;}
    for(int i=0;i<CNN_PARAMETERS;++i) {
        long long value=(long long)params[i];
        long long lo=i<CNN_WEIGHTS?WEIGHT_MIN:ACT_MIN, hi=i<CNN_WEIGHTS?WEIGHT_MAX:ACT_MAX;
        if(value<lo||value>hi){std::fclose(f);return 2;}
    }
    int word_errors=0,agreement=0,fixed_correct=0,float_correct=0,status_errors=0;double worst=0;
    for(uint32_t n=0;n<header[1];++n){
        float reference[8];int32_t metadata[2];
        if(!read_words(f,input,1100)||!read_words(f,expected,8)||std::fread(reference,4,8,f)!=8||std::fread(metadata,4,2,f)!=2){std::fclose(f);return 2;}
        for(int i=0;i<CNN_INPUTS;++i) {
            long long value=(long long)input[i];
            if(value<ACT_MIN||value>ACT_MAX){std::fclose(f);return 2;}
            if(n==0)first_input[i]=input[i];
        }
        if(n==0)for(int i=0;i<8;++i)first_expected[i]=expected[i];
        if(perf_warm && n==0) {
            for(unsigned slot=0;slot<2;++slot) {
                unsigned preload=gesture_cnn_precision_u2_cache(input,params,output,slot,100+slot,CACHE_AUTO);
                status_errors+=preload!=CACHE_LOADED;
                for(int i=0;i<8;++i)if(output[i]!=expected[i])++word_errors;
            }
        }
        const unsigned slot=(perf_cold||perf_warm)?n%2:0;
        const unsigned id=(perf_cold||perf_warm)?100+slot:100;
        const unsigned command=perf_cold?CACHE_FORCE_RELOAD:CACHE_AUTO;
        unsigned status=gesture_cnn_precision_u2_cache(input,params,output,slot,id,command);
        const unsigned wanted=perf_cold?CACHE_LOADED:(perf_warm?CACHE_HIT:(n==0?CACHE_LOADED:CACHE_HIT));
        status_errors+=status!=wanted;
        int actual_class=0;
        for(int i=0;i<8;++i){
            if(output[i]!=expected[i])++word_errors;
            if(output[i]>output[actual_class])actual_class=i;
            worst=std::max(worst,std::fabs((double)output[i]/ACT_SCALE-reference[i]));
        }
        agreement+=actual_class==metadata[1];fixed_correct+=actual_class==metadata[0];float_correct+=metadata[1]==metadata[0];
        std::cout<<"sample "<<n<<": class="<<actual_class<<" software="<<metadata[1]
                 <<" true="<<metadata[0]<<" cache_status="<<status<<"\n";
    }
    bool trailing=std::fgetc(f)!=EOF;std::fclose(f);
    int cases=0,cache_errors=0;
    if(!perf_cold && !perf_warm) {
    // Analytical probe models, not new trained LEFT/RIGHT classifiers.
    // Model B: zero weights, class2 bias=128 -> raw logits [0,0,128,0,...].
    // Model C: same biases plus one 2047-weight path in each layer. With
    // input=2048 the path requantizes 2048 -> 512 -> 128 -> 32 -> 8.
    static word_t model_b[CNN_PARAMETERS]={},model_c[CNN_PARAMETERS]={},model_d[CNN_PARAMETERS]={},probe_input[1100];
    word_t expected_b[8]={},expected_c[8]={},expected_d[8]={},zeros[8]={};
    model_b[CNN_WEIGHTS+162]=128;model_c[CNN_WEIGHTS+162]=128;model_d[CNN_WEIGHTS+162]=256;
    model_c[0]=2047;model_c[2112]=2047;model_c[8256]=2047;model_c[53312]=2047;
    model_d[0]=2047;model_d[2112]=2047;model_d[8256]=2047;model_d[53312]=2047;
    expected_b[2]=128;expected_c[2]=128;expected_c[0]=8;expected_d[2]=256;expected_d[0]=8;
    for(int i=0;i<1100;++i)probe_input[i]=2048;
#define CASE(name,inp,par,exp,slot,id,cmd,status) \
    do{++cases;if(!check_case(name,inp,par,exp,slot,id,cmd,status))++cache_errors;}while(0)
    CASE("warm original",first_input,params,first_expected,0,100,CACHE_AUTO,CACHE_HIT);
    CASE("cold second slot B",probe_input,model_b,expected_b,1,200,CACHE_AUTO,CACHE_LOADED);
    CASE("alternating original",first_input,params,first_expected,0,100,CACHE_AUTO,CACHE_HIT);
    CASE("warm B",probe_input,model_b,expected_b,1,200,CACHE_AUTO,CACHE_HIT);
    CASE("same ID ignores changed bytes",probe_input,model_c,expected_b,1,200,CACHE_AUTO,CACHE_HIT);
    CASE("force reload changed weights",probe_input,model_c,expected_c,1,200,CACHE_FORCE_RELOAD,CACHE_LOADED);
    CASE("same ID ignores changed bias",probe_input,model_d,expected_c,1,200,CACHE_AUTO,CACHE_HIT);
    CASE("force reload changed bias",probe_input,model_d,expected_d,1,200,CACHE_FORCE_RELOAD,CACHE_LOADED);
    CASE("ID change reloads B",probe_input,model_b,expected_b,1,201,CACHE_AUTO,CACHE_LOADED);
    CASE("other slot remains original",first_input,params,first_expected,0,100,CACHE_AUTO,CACHE_HIT);
    CASE("invalidate both",probe_input,model_b,zeros,0,0,CACHE_INVALIDATE_ALL,CACHE_INVALIDATED);
    CASE("original miss after invalidate",first_input,params,first_expected,0,100,CACHE_AUTO,CACHE_LOADED);
    CASE("second slot miss after invalidate",probe_input,model_c,expected_c,1,200,CACHE_AUTO,CACHE_LOADED);
    CASE("invalid slot rejected",probe_input,model_b,zeros,2,200,CACHE_AUTO,CACHE_INVALID_CONTROL);
    CASE("invalid command rejected",probe_input,model_b,zeros,0,100,99,CACHE_INVALID_CONTROL);
    CASE("bad commands preserve original",first_input,params,first_expected,0,100,CACHE_AUTO,CACHE_HIT);
    CASE("force reload same original",first_input,params,first_expected,0,100,CACHE_FORCE_RELOAD,CACHE_LOADED);
#undef CASE
    }
    std::cout<<"Raw-word mismatches: "<<word_errors<<"\nPrediction agreement: "<<agreement<<"/"<<header[1]
             <<"\nFixed accuracy: "<<fixed_correct<<"/"<<header[1]<<"\nFloat accuracy: "<<float_correct<<"/"<<header[1]
             <<"\nMax logit error: "<<worst<<"\nRegression cache-status errors: "<<status_errors
             <<"\nMode: "<<(perf_cold?"perf-cold":perf_warm?"perf-warm":"validation")
             <<"\nPreload calls: "<<(perf_warm?2:0)<<"\n";
    if(cases)std::cout<<"Cache-control cases: "<<(cases-cache_errors)<<"/"<<cases<<"\n";
    if(word_errors||agreement!=(int)header[1]||status_errors||cache_errors||trailing){std::cerr<<"FAIL\n";return 1;}
    std::cout<<"PASS: exact regression outputs and requested cache mode match\n";return 0;
}
'''


def prepare(parent, out):
    parent, out = Path(parent), Path(out)
    if out.exists():
        raise FileExistsError('Choose a NEW experiment folder: ' + str(out))
    manifest, data, samples, parent_hash = read_parent(parent)
    output = dict(data)
    output.update(cache_sources(data))
    for mode in ('cold', 'warm'):
        # Same design and testbench, only argv changes. Use the existing synthesized
        # component when running these timing modes, and save each RTL report.
        output['precision_perf_' + mode + '.cfg'] = (output['precision.cfg'].decode()
            + '\ncsim.argv=--perf-' + mode + '\ncosim.argv=--perf-' + mode + '\n').encode()
    output['cache_contract.json'] = (json.dumps(CONTRACT, indent=2) + '\n').encode()
    result = copy.deepcopy(manifest)
    result.update(top=TOP, status='prepared_not_synthesized', cache_slots=2,
                  changes='Two tagged parameter cache slots; two-lane arithmetic unchanged; new scalar controls and return-status ABI',
                  parent_manifest_sha256=parent_hash,
                  parent_generated_sha256=manifest['generated_sha256'],
                  regression_samples=samples, cache_control_cases=17,
                  validation='New Vitis C simulation, synthesis, warm/cold RTL tests and physical board validation are pending')
    result['generated_sha256'] = {name: u.digest(value) for name, value in output.items()}
    out.mkdir(parents=True)
    for name, value in output.items():
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    (out / 'manifest.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8', newline='\n')
    print('Prepared:', out.resolve())
    print('Top:', TOP, '| parameter-cache slots: 2 | compute lanes: 2')
    print('Regression samples:', samples, '| extra cache-control cases: 17')
    print('Cold/miss calls load parameters; matching warm slots skip parameter loading.')
    print('New control/return ABI: read cache_contract.json; no Vitis or board operation executed.')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    prepare(args.parent, args.out)


if __name__ == '__main__':
    main()
