#include "hand_cnn.h"
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
