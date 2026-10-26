#include "precision_cnn.h"
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
