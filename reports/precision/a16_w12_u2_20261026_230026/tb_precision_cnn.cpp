#include "precision_cnn.h"
#include <cstdint>
#include <cstdio>
#include <cmath>
#include <iostream>
#include <algorithm>
static bool read_words(FILE*f,word_t*out,int n) {
    for(int i=0;i<n;++i){int32_t v;if(std::fread(&v,4,1,f)!=1)return false;out[i]=v;}return true;
}
int main() {
    FILE*f=std::fopen("precision_test.bin","rb");
    if(!f)f=std::fopen("vectors/precision_test.bin","rb");
    if(!f){std::cerr<<"Missing precision_test.bin\n";return 2;}
    uint32_t header[8];
    if(std::fread(header,4,8,f)!=8||header[0]!=0x50524331||header[1]==0||header[1]>10000||header[2]!=CNN_PARAMETERS||header[3]!=ACT_W||header[4]!=ACT_F||header[5]!=WEIGHT_W||header[6]!=WEIGHT_F||header[7]!=ACC_W){std::fclose(f);return 2;}
    static word_t params[CNN_PARAMETERS],input[1100],output[8],expected[8];
    if(!read_words(f,params,CNN_PARAMETERS)){std::fclose(f);return 2;}
    for(int i=0;i<CNN_PARAMETERS;++i) {
        long long value=(long long)params[i];
        long long lo=i<CNN_WEIGHTS?WEIGHT_MIN:ACT_MIN, hi=i<CNN_WEIGHTS?WEIGHT_MAX:ACT_MAX;
        if(value<lo||value>hi){std::cerr<<"Parameter out of range\n";std::fclose(f);return 2;}
    }
    int word_errors=0,agreement=0,fixed_correct=0,float_correct=0;double worst=0;
    for(uint32_t n=0;n<header[1];++n){
        float reference[8];int32_t metadata[2];
        if(!read_words(f,input,1100)||!read_words(f,expected,8)||std::fread(reference,4,8,f)!=8||std::fread(metadata,4,2,f)!=2){std::fclose(f);return 2;}
        for(int i=0;i<CNN_INPUTS;++i) {
            long long value=(long long)input[i];
            if(value<ACT_MIN||value>ACT_MAX){std::cerr<<"Input out of range\n";std::fclose(f);return 2;}
        }
        gesture_cnn_precision_u2(input,params,output);
        int actual_class=0;
        for(int i=0;i<8;++i){
            if(output[i]!=expected[i]){++word_errors;if(word_errors<10)std::cerr<<"Raw mismatch sample="<<n<<" class="<<i<<" actual="<<output[i]<<" expected="<<expected[i]<<"\n";}
            if(output[i]>output[actual_class])actual_class=i;
            worst=std::max(worst,std::fabs((double)output[i]/ACT_SCALE-reference[i]));
        }
        agreement+=actual_class==metadata[1];fixed_correct+=actual_class==metadata[0];float_correct+=metadata[1]==metadata[0];
        std::cout<<"sample "<<n<<": class="<<actual_class<<" software="<<metadata[1]<<" true="<<metadata[0]<<"\n";
    }
    bool trailing=std::fgetc(f)!=EOF;std::fclose(f);
    std::cout<<"Raw-word mismatches: "<<word_errors<<"\nPrediction agreement: "<<agreement<<"/"<<header[1]
             <<"\nFixed accuracy: "<<fixed_correct<<"/"<<header[1]<<"\nFloat accuracy: "<<float_correct<<"/"<<header[1]<<"\nMax logit error: "<<worst<<"\n";
    if(word_errors||agreement!=(int)header[1]||trailing){std::cerr<<"FAIL\n";return 1;}
    std::cout<<"PASS: exact integer reference and all software class predictions match\n";return 0;
}
