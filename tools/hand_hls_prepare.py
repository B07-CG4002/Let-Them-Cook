"""Prepare a 12-feature/seven-class, two-lane, two-slot CNN HLS experiment.

Synthetic preparation requires no gloves, trained model, Vitis or FPGA. Trained
export requires both hand runs and their original prepared datasets. This first
migration uses A32/F18 W22/F21 accumulator64; it does not select final precision.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import struct

import numpy as np

from tools import precision_sweep as q
from tools import precision_hls_prepare as h
from tools import precision_hls_unroll2 as u
from tools import precision_hls_cache as c

TOP = 'gesture_cnn_hand_u2_cache'
MAGIC = 0x48414E44
FEATURES, INPUTS, OUTPUTS, WEIGHTS, PARAMETERS = 12, 600, 7, 52800, 52967
FORMAT = dict(activation_bits=32, activation_fraction=18, weight_bits=22,
              weight_fraction=21, accumulator_bits=64, axi_carrier_bits=32)
SHAPES = ((32,12,3), (64,32,3), (64,704), (7,64), (32,), (64,), (64,), (7,))
OFFSETS = (0, 1152, 7296, 52352, 52800, 52832, 52896, 52960)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, indent=2, allow_nan=False) + '\n').encode()


def normalizer_bytes(mean, std):
    buffer = io.BytesIO()
    np.savez(buffer, mean=mean, std=std)
    return buffer.getvalue()


def synthetic_models(samples=4, seed=42):
    if not 2 <= samples <= 100:
        raise ValueError('Synthetic samples per hand must be between 2 and 100')
    result = []
    for slot in range(2):
        rng = np.random.default_rng(seed + slot)
        parameters = {name:rng.uniform(-.0625,.0625,shape).astype(np.float32)
                      for name,shape in zip(q.NAMES,SHAPES)}
        # Different, separated outputs make wrong-slot reuse observable.
        parameters['fc2_b'] = np.linspace(-.25,.5,7,dtype=np.float32)
        if slot:
            parameters['fc2_b'] = parameters['fc2_b'][::-1].copy()
        inputs = rng.uniform(-1.5,1.5,(samples,50,12)).astype(np.float32)
        inputs[0] = 0
        inputs[1] = 0
        inputs[1,25,11] = 1.5  # explicitly exercise the new thumb feature
        result.append(dict(parameters=parameters, inputs=inputs,
                           labels=None, device_id=slot+1, config=None,
                           normalizer=(np.zeros(12,dtype=np.float32),
                                       np.ones(12,dtype=np.float32)),
                           provenance={'synthetic_seed':seed+slot}))
    return result


def trained_models(left_run, right_run, left_data=None, right_data=None):
    if left_run is None or right_run is None:
        raise ValueError('Trained export requires BOTH --left-run and --right-run')
    result = []
    for device,run,data in ((1,left_run,left_data),(2,right_run,right_data)):
        source = q.load_source(run=run)
        if source['training']['device_id'] != device:
            raise ValueError('Model hand identity does not match LEFT/RIGHT argument')
        data = Path(data or source['training']['data_dir'])
        x,y,calibration,data_info = q.load_data(data,source,'validation')
        p = source['parameters']
        reference = np.array([q.float_layers(v,p)['logits'] for v in x])
        torch_reference = q.torch_reference_check(x,p)
        if (not np.allclose(reference,torch_reference,rtol=2e-4,atol=2e-4)
                or not np.array_equal(reference.argmax(1),torch_reference.argmax(1))):
            raise ValueError('NumPy/PyTorch reference disagreement; export refused')
        result.append(dict(parameters=p, inputs=x, labels=y, device_id=device,
                           config=source['config'], normalizer=source['normalizer'],
                           provenance=dict(source_sha256=source['provenance'],
                                           data=data_info,
                                           calibration_samples=len(calibration))))
    if result[0]['config'] != result[1]['config']:
        raise ValueError('LEFT/RIGHT preprocessing contracts differ')
    return result


def sources():
    """Reconstruct reviewed arithmetic, then change explicit architecture sizes."""
    baseline = h.make_sources(h.reviewed_sources(),32,18,22,21,64)
    parent = {}
    for name,value in baseline.items():
        value = (u.transform(value) if name=='precision_cnn.cpp'
                 else value.replace('gesture_cnn_precision',u.TOP))
        parent[name] = value.encode()
    generated = c.cache_sources(parent)
    cpp = generated['precision_cnn.cpp'].decode()
    header = generated['precision_cnn.h'].decode()
    for old,new in ((1100,600),(53992,52967),(168,167),(66,36),
                    (2112,1152),(8256,7296),(53312,52352)):
        cpp = re.sub(r'\b'+str(old)+r'\b',str(new),cpp)
    stride = '(t+d%3)*22+d/3'
    if cpp.count(stride) != 1 or cpp.count('o<8') != 1:
        raise ValueError('Unexpected reviewed input/output indexing')
    cpp = cpp.replace(stride,'(t+d%3)*12+d/3').replace('o<8','o<7')
    cpp = cpp.replace('depth=8\n','depth=7\n')
    cpp = cpp.replace('Full eight-class GestureCNN.','Independent-hand seven-class GestureCNN.')
    header = header.replace('CNN_INPUTS=1100, CNN_WEIGHTS=53824, CNN_PARAMETERS=53992, CNN_OUTPUTS=8',
                            'CNN_INPUTS=600, CNN_WEIGHTS=52800, CNN_PARAMETERS=52967, CNN_OUTPUTS=7')
    cfg = generated['precision.cfg'].decode()
    output = {}
    for name,value in (('hand_cnn.cpp',cpp),('hand_cnn.h',header),('hand.cfg',cfg)):
        value = value.replace(c.TOP,TOP).replace('precision_cnn.h','hand_cnn.h')
        value = value.replace('precision_cnn.cpp','hand_cnn.cpp')
        value = value.replace('tb_precision_cnn.cpp','tb_hand_cnn.cpp')
        value = value.replace('precision_test.bin','hand_test.bin')
        output[name] = value.encode()
    output['tb_hand_cnn.cpp'] = testbench().encode()
    for mode in ('warm','cold'):
        output['hand_perf_'+mode+'.cfg'] = (output['hand.cfg'] +
            ('\ncsim.argv=--perf-'+mode+'\ncosim.argv=--perf-'+mode+'\n').encode())
    return output


def testbench():
    return r'''#include "hand_cnn.h"
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <algorithm>
#include <cmath>
static bool read_words(FILE*f,word_t*out,int n) {
    for(int i=0;i<n;++i){int32_t v;if(std::fread(&v,4,1,f)!=1)return false;out[i]=v;}return true;
}
static bool check_case(const char*name,const word_t*input,const word_t*params,
                       const word_t*expected,unsigned slot,unsigned id,unsigned command,
                       unsigned wanted) {
    word_t output[CNN_OUTPUTS];
    unsigned status=gesture_cnn_hand_u2_cache(input,params,output,slot,id,command);
    bool pass=status==wanted;
    for(int i=0;i<CNN_OUTPUTS;++i)if(output[i]!=expected[i])pass=false;
    std::cout<<"CACHE "<<name<<": "<<(pass?"PASS":"FAIL")<<"\n";return pass;
}
int main(int argc,char**argv) {
    bool cold=argc==2 && std::strcmp(argv[1],"--perf-cold")==0;
    bool warm=argc==2 && std::strcmp(argv[1],"--perf-warm")==0;
    if(argc>1&&!cold&&!warm)return 2;
    FILE*f=std::fopen("hand_test.bin","rb");
    if(!f)f=std::fopen("vectors/hand_test.bin","rb");
    if(!f){std::cerr<<"Missing hand_test.bin\n";return 2;}
    uint32_t h[12];
    if(std::fread(h,4,12,f)!=12 || h[0]!=0x48414E44 || h[1]!=1 || h[2]<4 || h[2]>10000 ||
       h[3]!=CNN_INPUTS || h[4]!=CNN_PARAMETERS || h[5]!=CNN_OUTPUTS ||
       h[6]!=ACT_W || h[7]!=ACT_F || h[8]!=WEIGHT_W || h[9]!=WEIGHT_F || h[10]!=ACC_W || h[11]!=2){std::fclose(f);return 2;}
    static word_t params[2][CNN_PARAMETERS],input[CNN_INPUTS],expected[CNN_OUTPUTS],output[CNN_OUTPUTS];
    static word_t first_input[2][CNN_INPUTS],first_expected[2][CNN_OUTPUTS];
    for(int s=0;s<2;++s){
        if(!read_words(f,params[s],CNN_PARAMETERS)){std::fclose(f);return 2;}
        for(int i=0;i<CNN_PARAMETERS;++i){
            long long v=(long long)params[s][i];
            if(v<(i<CNN_WEIGHTS?WEIGHT_MIN:ACT_MIN)||v>(i<CNN_WEIGHTS?WEIGHT_MAX:ACT_MAX)){std::fclose(f);return 2;}
        }
    }
    bool seen[2]={false,false};int errors=0,status_errors=0,agreement=0;double worst=0;
    for(uint32_t n=0;n<h[2];++n){
        uint32_t slot;float reference[CNN_OUTPUTS];
        if(std::fread(&slot,4,1,f)!=1 || slot>=2 || !read_words(f,input,CNN_INPUTS) ||
           !read_words(f,expected,CNN_OUTPUTS) || std::fread(reference,4,CNN_OUTPUTS,f)!=CNN_OUTPUTS){std::fclose(f);return 2;}
        if(!seen[slot]){
            for(int i=0;i<CNN_INPUTS;++i)first_input[slot][i]=input[i];
            for(int i=0;i<CNN_OUTPUTS;++i)first_expected[slot][i]=expected[i];
            if(warm){
                status_errors+=gesture_cnn_hand_u2_cache(input,params[slot],output,slot,100+slot,CACHE_AUTO)!=CACHE_LOADED;
                for(int i=0;i<CNN_OUTPUTS;++i)errors+=output[i]!=expected[i];
            }
        }
        unsigned wanted=cold?CACHE_LOADED:(warm||seen[slot]?CACHE_HIT:CACHE_LOADED);
        unsigned status=gesture_cnn_hand_u2_cache(input,params[slot],output,slot,100+slot,cold?CACHE_FORCE_RELOAD:CACHE_AUTO);
        status_errors+=status!=wanted;seen[slot]=true;
        int actual=0,software=0;
        for(int i=0;i<CNN_OUTPUTS;++i){
            errors+=output[i]!=expected[i];
            if(output[i]>output[actual])actual=i;
            if(reference[i]>reference[software])software=i;
            worst=std::max(worst,std::fabs((double)output[i]/ACT_SCALE-reference[i]));
        }
        agreement+=actual==software;
        std::cout<<"sample "<<n<<" slot="<<slot<<" class="<<actual<<" software="<<software<<" status="<<status<<"\n";
    }
    bool trailing=std::fgetc(f)!=EOF;std::fclose(f);
    if(!seen[0]||!seen[1]||trailing)return 2;
    int cases=0,cache_errors=0;
    if(!warm&&!cold){
        static word_t b[CNN_PARAMETERS]={},c[CNN_PARAMETERS]={},d[CNN_PARAMETERS]={},probe[CNN_INPUTS];
        word_t eb[CNN_OUTPUTS]={},ec[CNN_OUTPUTS]={},ed[CNN_OUTPUTS]={},zeros[CNN_OUTPUTS]={};
        const int half_weight=1<<(WEIGHT_F-1),quarter_activation=1<<(ACT_F-2);
        b[CNN_WEIGHTS+162]=quarter_activation;c[CNN_WEIGHTS+162]=quarter_activation;d[CNN_WEIGHTS+162]=2*quarter_activation;
        c[0]=half_weight;c[1152]=half_weight;c[7296]=half_weight;c[52352]=half_weight;
        d[0]=half_weight;d[1152]=half_weight;d[7296]=half_weight;d[52352]=half_weight;
        eb[2]=quarter_activation;ec[2]=quarter_activation;ed[2]=2*quarter_activation;
        ec[0]=1<<(ACT_F-4);ed[0]=1<<(ACT_F-4);
        for(int i=0;i<CNN_INPUTS;++i)probe[i]=1<<ACT_F;
#define CASE(name,inp,par,exp,slot,id,cmd,status) \
    do{++cases;if(!check_case(name,inp,par,exp,slot,id,cmd,status))++cache_errors;}while(0)
        CASE("warm LEFT",first_input[0],params[0],first_expected[0],0,100,CACHE_AUTO,CACHE_HIT);
        CASE("replace RIGHT with probe B",probe,b,eb,1,200,CACHE_AUTO,CACHE_LOADED);
        CASE("LEFT preserved",first_input[0],params[0],first_expected[0],0,100,CACHE_AUTO,CACHE_HIT);
        CASE("warm B",probe,b,eb,1,200,CACHE_AUTO,CACHE_HIT);
        CASE("same ID ignores changed weights",probe,c,eb,1,200,CACHE_AUTO,CACHE_HIT);
        CASE("force changed weights",probe,c,ec,1,200,CACHE_FORCE_RELOAD,CACHE_LOADED);
        CASE("same ID ignores changed bias",probe,d,ec,1,200,CACHE_AUTO,CACHE_HIT);
        CASE("force changed bias",probe,d,ed,1,200,CACHE_FORCE_RELOAD,CACHE_LOADED);
        CASE("ID change reloads",probe,b,eb,1,201,CACHE_AUTO,CACHE_LOADED);
        CASE("other slot preserved",first_input[0],params[0],first_expected[0],0,100,CACHE_AUTO,CACHE_HIT);
        CASE("invalidate both",probe,b,zeros,0,201,CACHE_INVALIDATE_ALL,CACHE_INVALIDATED);
        CASE("LEFT misses after invalidate",first_input[0],params[0],first_expected[0],0,100,CACHE_AUTO,CACHE_LOADED);
        CASE("RIGHT misses after invalidate",first_input[1],params[1],first_expected[1],1,101,CACHE_AUTO,CACHE_LOADED);
        CASE("invalid slot rejected",probe,b,zeros,2,200,CACHE_AUTO,CACHE_INVALID_CONTROL);
        CASE("invalid command rejected",probe,b,zeros,0,100,3,CACHE_INVALID_CONTROL);
        CASE("invalid controls preserve LEFT",first_input[0],params[0],first_expected[0],0,100,CACHE_AUTO,CACHE_HIT);
        CASE("force reload original",first_input[0],params[0],first_expected[0],0,100,CACHE_FORCE_RELOAD,CACHE_LOADED);
#undef CASE
    }
    std::cout<<"Raw-word mismatches: "<<errors<<"\nPrediction agreement: "<<agreement<<"/"<<h[2]
             <<"\nMax logit error: "<<worst<<"\nRegression cache-status errors: "<<status_errors
             <<"\nMode: "<<(warm?"perf-warm":cold?"perf-cold":"validation")
             <<"\nPreload calls: "<<(warm?2:0)<<"\n";
    if(cases)std::cout<<"Cache-control cases: "<<(cases-cache_errors)<<"/"<<cases<<"\n";
    if(errors||status_errors||cache_errors||agreement!=(int)h[2])return 1;
    std::cout<<"PASS: 12-feature/seven-class exact reference and cache controls match\n";
    std::cout<<"No final gesture accuracy or physical-board latency established by this test\n";return 0;
}
'''


def prepare(out, synthetic=False, samples=4, seed=42, left_run=None, right_run=None,
            left_data=None, right_data=None):
    out = Path(out)
    if out.exists():
        raise FileExistsError('Choose a NEW experiment folder: '+str(out))
    if synthetic and any(v is not None for v in (left_run,right_run,left_data,right_data)):
        raise ValueError('Synthetic mode cannot use trained runs or datasets')
    models = (synthetic_models(samples,seed) if synthetic else
              trained_models(left_run,right_run,left_data,right_data))
    output = sources()
    report_models,prepared = [],[]
    for slot,model in enumerate(models):
        p = q.validate_parameters(model['parameters'],12,7)
        x = np.asarray(model['inputs'],dtype=np.float32)
        if x.ndim!=3 or x.shape[1:]!=(50,12) or not len(x) or not np.isfinite(x).all():
            raise ValueError('Invalid model inputs')
        floating = np.array([q.float_layers(v,p)['logits'] for v in x])
        actual,stats = q.run_quantized(x,p,32,18,22,21)
        labels = floating.argmax(1) if synthetic else model['labels']
        comparison = q.compare(floating,actual,labels,.70,'gt')
        if (stats['clipping_events'] or comparison['class_changes'] or
                comparison['emitted_label_changes'] or comparison['max_probability_error']>.01):
            raise ValueError('A32/F18 W22/F21 failed fresh numerical screen; export refused')
        if synthetic:
            comparison.pop('float_correct',None)
            comparison.pop('quantized_correct',None)
        param_words = np.concatenate([q.quantize(p[n],22 if n.endswith('_w') else 32,
                          21 if n.endswith('_w') else 18)[0].ravel() for n in q.NAMES]).astype('<i4')
        if len(param_words)!=PARAMETERS:
            raise ValueError('Parameter layout differs from the hand architecture')
        words = q.quantize(x,32,18)[0].astype('<i4')
        expected = np.rint(actual*2**18).astype('<i4')
        name = ('left','right')[slot]
        output[name+'/parameter_words.bin'] = param_words.tobytes()
        output[name+'/normalization.npz'] = normalizer_bytes(*model['normalizer'])
        output[name+'/config.json'] = json_bytes(model['config'] or dict(
            version='SYNTHETIC_TEST_ONLY',feature_columns=q.HAND_CHANNELS,
            class_names=q.HAND_CLASSES,window_seconds=2.0,time_positions=50))
        prepared.append((words,expected,floating.astype('<f4')))
        report_models.append(dict(hand=name.upper(),device_id=slot+1,cache_slot=slot,
             model_id=100+slot,validation_samples=len(x),provenance=model['provenance'],
             numeric_screen=comparison,clipping_events=stats['clipping_events']))
    # Full representable operands, largest dot, full representable bias, rounding.
    bound = 704*(1<<31)*(1<<21)+(1<<31)*(1<<21)+(1<<20)
    if bound>(1<<63)-1:
        raise ValueError('Unsafe accumulator bound')
    cases = [(slot,i) for i in range(max(len(p[0]) for p in prepared))
             for slot in (0,1) if i<len(prepared[slot][0])]
    if not 4<=len(cases)<=10000:
        raise ValueError('Need between 4 and 10000 total reference cases')
    vectors = bytearray(struct.pack('<12I',MAGIC,1,len(cases),INPUTS,PARAMETERS,OUTPUTS,
                                    32,18,22,21,64,2))
    for hand in ('left','right'):
        vectors.extend(output[hand+'/parameter_words.bin'])
    for slot,i in cases:
        inputs,expected,floating = prepared[slot]
        vectors.extend(struct.pack('<I',slot))
        vectors.extend(inputs[i].tobytes())
        vectors.extend(expected[i].tobytes())
        vectors.extend(floating[i].tobytes())
    output['vectors/hand_test.bin'] = bytes(vectors)
    contract = dict(version=1,top=TOP,features=q.HAND_CHANNELS,classes=q.HAND_CLASSES,
        input_shape=[50,12],input_layout='time-major, already normalized with selected hand mean/std',
        parameters=[dict(name=n,shape=list(s),offset=o) for n,s,o in zip(q.NAMES,SHAPES,OFFSETS)],
        parameter_words=PARAMETERS,output_words=7,format=FORMAT,
        hand_mapping={'LEFT':dict(device_id=1,cache_slot=0),'RIGHT':dict(device_id=2,cache_slot=1)},
        commands=c.CONTRACT['commands'],return_status=c.CONTRACT['return_status'],
        rules=[r for r in c.CONTRACT['rules'] if not r.startswith('This benchmark still requires')]+[
            'Normalize each hand independently on CPU before encoding. The FPGA does not normalize inputs.',
            'Rebuild driver from new HWH/register map; the production paired driver is incompatible.',
            'Synthetic files are test fixtures, never trained model artifacts.',
            'Do not run calls concurrently: two model slots share one compute engine.'])
    output['hand_contract.json'] = json_bytes(contract)
    dependencies = {p.name:sha(p.read_bytes().replace(b'\r\n',b'\n'))
                    for p in (Path(__file__),Path(q.__file__),Path(h.__file__),Path(u.__file__),Path(c.__file__))}
    manifest = dict(version=1,status='prepared_not_synthesized',top=TOP,
        scope='synthetic_contract_test_only' if synthetic else 'two_trained_hands_validation_export',
        final_gesture_accuracy_validated=False,format=FORMAT,part='xazu3eg-sbva484-1-i',
        clock_target_ns=10,parallel_lanes=2,cache_slots=2,regression_samples=len(cases),
        cache_control_cases=17,full_range_accumulator_absolute_bound=bound,
        numeric_screen_settings=dict(threshold=.70,threshold_rule='gt',max_probability_error=.01),
        generator_sha256=dependencies,models=report_models,
        generated_sha256={n:sha(b) for n,b in output.items()},
        pending=['Vitis C simulation','HLS synthesis','C/RTL co-simulation',
                 'Vivado implementation and bit/HWH export','physical-board validation',
                 'v4 communications, independent-hand worker and game integration'])
    # All model/data/numerical checks occur before creating the output directory.
    out.mkdir(parents=True)
    for name,data in output.items():
        target=out/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    (out/'manifest.json').write_bytes(json_bytes(manifest))
    print('Prepared:',out.resolve())
    print('Scope:',manifest['scope'])
    print('12 features, 7 classes, 2 lanes, 2 independent parameter slots; A32/F18 W22/F21 accumulator64')
    print('Reference cases:',len(cases),'| extra cache-control cases: 17')
    print('No Vitis command executed, no FPGA programmed, no final gesture accuracy claimed.')
    return manifest


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--synthetic',action='store_true')
    mode.add_argument('--left-run',type=Path)
    parser.add_argument('--right-run',type=Path)
    parser.add_argument('--left-data',type=Path)
    parser.add_argument('--right-data',type=Path)
    parser.add_argument('--samples',type=int,default=4,help='Synthetic samples per hand')
    parser.add_argument('--seed',type=int,default=42)
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args(argv)
    prepare(**vars(args))


if __name__=='__main__':
    main()
