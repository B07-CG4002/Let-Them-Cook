// Full eight-class GestureCNN. All neural-network layers execute in this IP.
// Input/activation/bias/logit: signed32 F18; weights: signed22 F21.
// Exact signed64 F39 accumulation; AP_RND-equivalent rounding then saturation
// ONCE per output. This is a NEW numeric contract, not the old Conv1-only IP.
#include "full_cnn_20261020.h"

static product_t multiply(word_t a, weight_word_t b) {
#pragma HLS INLINE
#ifdef CNN_PORTABLE_TEST
    return (int64_t)a*(int64_t)b; // widen BEFORE native C++ multiplication
#else
    return a*b; // ap_int<32> * ap_int<22> produces exact signed54
#endif
}

static word_t round_activation(sum_t value) {
#pragma HLS INLINE
    sum_t rounded=(value+((sum_t)1<<20))>>21; // nearest; ties toward +infinity
    if (rounded>(sum_t)2147483647) return (word_t)2147483647;
    if (rounded<(sum_t)(-2147483647-1)) return (word_t)(-2147483647-1);
    return (word_t)rounded;
}

void gesture_cnn_full(const word_t input_words[CNN_INPUTS],
                      const word_t parameter_words[CNN_PARAMETERS],
                      word_t logit_words[CNN_OUTPUTS]) {
#pragma HLS INTERFACE mode=m_axi port=input_words offset=slave bundle=gmem0 depth=1100
#pragma HLS INTERFACE mode=m_axi port=parameter_words offset=slave bundle=gmem1 depth=53992
#pragma HLS INTERFACE mode=m_axi port=logit_words offset=slave bundle=gmem0 depth=8
#pragma HLS INTERFACE mode=s_axilite port=input_words bundle=control
#pragma HLS INTERFACE mode=s_axilite port=parameter_words bundle=control
#pragma HLS INTERFACE mode=s_axilite port=logit_words bundle=control
#pragma HLS INTERFACE mode=s_axilite port=return bundle=control

    // No complete channel partitioning: reuse a small arithmetic datapath.
    // Reload parameters on every invocation for a simple, explicit contract.
    static weight_word_t weights[CNN_WEIGHTS];
    static word_t biases[168];
    static word_t input[1100],conv1[1536],pool1[768];
    static word_t conv2[1408],pool2[704],hidden[64];
#pragma HLS BIND_STORAGE variable=weights type=ram_2p impl=bram
#pragma HLS BIND_STORAGE variable=input type=ram_2p impl=bram
#pragma HLS BIND_STORAGE variable=conv1 type=ram_2p impl=bram
#pragma HLS BIND_STORAGE variable=pool1 type=ram_2p impl=bram
#pragma HLS BIND_STORAGE variable=conv2 type=ram_2p impl=bram
#pragma HLS BIND_STORAGE variable=pool2 type=ram_2p impl=bram

LOAD_INPUT: for(int i=0;i<1100;++i) {
#pragma HLS PIPELINE II=1
        input[i]=input_words[i];
    }
LOAD_WEIGHTS: for(int i=0;i<CNN_WEIGHTS;++i) {
#pragma HLS PIPELINE II=1
        weights[i]=(weight_word_t)parameter_words[i];
    }
LOAD_BIASES: for(int i=0;i<168;++i) {
#pragma HLS PIPELINE II=1
        biases[i]=parameter_words[CNN_WEIGHTS+i];
    }
C1_OC: for(int oc=0;oc<32;++oc) {
#pragma HLS PIPELINE off
C1_TIME: for(int t=0;t<48;++t) {
#pragma HLS PIPELINE off
            sum_t acc=(sum_t)biases[oc]*(sum_t)2097152;
C1_DOT: for(int d=0;d<66;++d) {
#pragma HLS PIPELINE II=1
                product_t product=multiply(input[(t+d%3)*22+d/3],weights[oc*66+d]);
                acc+=(sum_t)product;
            }
            conv1[oc*48+t]=round_activation(acc);
        }
    }
POOL1: for(int i=0;i<768;++i) {
#pragma HLS PIPELINE II=1
        word_t a=conv1[2*i],b=conv1[2*i+1];
        word_t maximum=a>b?a:b;
        pool1[i]=maximum>0?maximum:(word_t)0;
    }
C2_OC: for(int oc=0;oc<64;++oc) {
#pragma HLS PIPELINE off
C2_TIME: for(int t=0;t<22;++t) {
#pragma HLS PIPELINE off
            sum_t acc=(sum_t)biases[32+oc]*(sum_t)2097152;
C2_DOT: for(int d=0;d<96;++d) {
#pragma HLS PIPELINE II=1
                product_t product=multiply(pool1[(d/3)*24+t+d%3],weights[2112+oc*96+d]);
                acc+=(sum_t)product;
            }
            conv2[oc*22+t]=round_activation(acc);
        }
    }
POOL2: for(int i=0;i<704;++i) {
#pragma HLS PIPELINE II=1
        word_t a=conv2[2*i],b=conv2[2*i+1];
        word_t maximum=a>b?a:b;
        pool2[i]=maximum>0?maximum:(word_t)0;
    }
    // pool2 is already contiguous channel-major, matching PyTorch Flatten.
FC1_OUT: for(int o=0;o<64;++o) {
#pragma HLS PIPELINE off
        sum_t acc=(sum_t)biases[96+o]*(sum_t)2097152;
FC1_DOT: for(int d=0;d<704;++d) {
#pragma HLS PIPELINE II=1
            product_t product=multiply(pool2[d],weights[8256+o*704+d]);
            acc+=(sum_t)product;
        }
        word_t value=round_activation(acc);
        hidden[o]=value>0?value:(word_t)0;
    }
FC2_OUT: for(int o=0;o<8;++o) {
#pragma HLS PIPELINE off
        sum_t acc=(sum_t)biases[160+o]*(sum_t)2097152;
FC2_DOT: for(int d=0;d<64;++d) {
#pragma HLS PIPELINE II=1
            product_t product=multiply(hidden[d],weights[53312+o*64+d]);
            acc+=(sum_t)product;
        }
        logit_words[o]=round_activation(acc);
    }
}
