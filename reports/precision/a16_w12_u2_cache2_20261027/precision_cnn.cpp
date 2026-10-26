// GENERATED two-slot cached experiment; two-lane arithmetic matches parent.
// Full eight-class GestureCNN. All neural-network layers execute in this IP.
// Internal activation/bias/logit: signed16 F6; weights: signed12 F13.
// Signed38 F19 accumulation; AP_RND-equivalent rounding then saturation
// ONCE per output. This is a NEW numeric contract, not the old Conv1-only IP.
#include "precision_cnn.h"

static product_t multiply(act_t a, weight_word_t b) {
#pragma HLS INLINE
#ifdef CNN_PORTABLE_TEST
    return (int64_t)a*(int64_t)b; // widen BEFORE native C++ multiplication
#else
    return a*b; // ap_int<16> * ap_int<12> produces exact signed28
#endif
}

static act_t round_activation(sum_t value) {
#pragma HLS INLINE
    CNN_CHECK_SUM(value);
    sum_t rounded=(value+((sum_t)1<<12))>>13;
    if (rounded>(sum_t)ACT_MAX) return (act_t)ACT_MAX;
    if (rounded<(sum_t)ACT_MIN) return (act_t)ACT_MIN;
    return (act_t)rounded;
}

unsigned int gesture_cnn_precision_u2_cache(const word_t input_words[CNN_INPUTS],
                      const word_t parameter_words[CNN_PARAMETERS],
                      word_t logit_words[CNN_OUTPUTS], unsigned int cache_slot,
                      unsigned int model_id, unsigned int cache_command) {
#pragma HLS INTERFACE mode=m_axi port=input_words offset=slave bundle=gmem0 depth=1100
#pragma HLS INTERFACE mode=m_axi port=parameter_words offset=slave bundle=gmem1 depth=53992
#pragma HLS INTERFACE mode=m_axi port=logit_words offset=slave bundle=gmem0 depth=8
#pragma HLS INTERFACE mode=s_axilite port=input_words bundle=control
#pragma HLS INTERFACE mode=s_axilite port=parameter_words bundle=control
#pragma HLS INTERFACE mode=s_axilite port=logit_words bundle=control
#pragma HLS INTERFACE mode=s_axilite port=cache_slot bundle=control
#pragma HLS INTERFACE mode=s_axilite port=model_id bundle=control
#pragma HLS INTERFACE mode=s_axilite port=cache_command bundle=control
#pragma HLS INTERFACE mode=s_axilite port=return bundle=control

    // Two dot-product lanes; cyclic banked memories and a two-register reduction.
    // Two tagged parameter slots; reload only on miss, ID change or force.
    static weight_word_t weights[2][CNN_WEIGHTS];
    static act_t biases[2][168];
    static bool cache_valid[2]={false,false};
    static unsigned int cached_model_id[2]={0,0};
#pragma HLS ARRAY_PARTITION variable=cache_valid complete dim=1
#pragma HLS ARRAY_PARTITION variable=cached_model_id complete dim=1
#pragma HLS RESET variable=cache_valid
    static act_t input[1100],conv1[1536],pool1[768];
    static act_t conv2[1408],pool2[704],hidden[64];
#pragma HLS ARRAY_PARTITION variable=weights complete dim=1
#pragma HLS ARRAY_PARTITION variable=weights cyclic factor=2 dim=2
#pragma HLS ARRAY_PARTITION variable=biases complete dim=1
#pragma HLS ARRAY_PARTITION variable=input cyclic factor=2 dim=1
#pragma HLS ARRAY_PARTITION variable=pool1 cyclic factor=2 dim=1
#pragma HLS ARRAY_PARTITION variable=pool2 cyclic factor=2 dim=1
#pragma HLS ARRAY_PARTITION variable=hidden cyclic factor=2 dim=1
#pragma HLS BIND_STORAGE variable=weights type=ram_2p impl=bram
#pragma HLS BIND_STORAGE variable=input type=ram_2p impl=bram
#pragma HLS BIND_STORAGE variable=conv1 type=ram_2p impl=bram
#pragma HLS BIND_STORAGE variable=pool1 type=ram_2p impl=bram
#pragma HLS BIND_STORAGE variable=conv2 type=ram_2p impl=bram
#pragma HLS BIND_STORAGE variable=pool2 type=ram_2p impl=bram

    // Reject bad controls before any parameter/input access; preserve cache contents.
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

LOAD_INPUT: for(int i=0;i<1100;++i) {
#pragma HLS PIPELINE II=1
        input[i]=input_words[i];
    }
    if(reload) {
LOAD_WEIGHTS: for(int i=0;i<CNN_WEIGHTS;++i) {
#pragma HLS PIPELINE II=1
        weights[cache_slot][i]=(weight_word_t)parameter_words[i];
    }
LOAD_BIASES: for(int i=0;i<168;++i) {
#pragma HLS PIPELINE II=1
        biases[cache_slot][i]=parameter_words[CNN_WEIGHTS+i];
    }
        cached_model_id[cache_slot]=model_id;
        cache_valid[cache_slot]=true;
    }
C1_OC: for(int oc=0;oc<32;++oc) {
#pragma HLS PIPELINE off
C1_TIME: for(int t=0;t<48;++t) {
#pragma HLS PIPELINE off
            sum_t partial[2];
#pragma HLS ARRAY_PARTITION variable=partial complete dim=1
            partial[0]=(sum_t)biases[cache_slot][oc]*(sum_t)8192ULL;
            partial[1]=0;
            CNN_CHECK_SUM(partial[0]);
C1_DOT: for(int base=0;base<66;base+=2) {
#pragma HLS PIPELINE II=1
C1_LANES: for(int lane=0;lane<2;++lane) {
#pragma HLS UNROLL
                    const int d=base+lane;
                    product_t product=multiply(input[(t+d%3)*22+d/3],weights[cache_slot][oc*66+d]);
                    partial[lane]+=(sum_t)product;
                    CNN_CHECK_SUM(partial[lane]);
                }
            }
            sum_t acc=partial[0]+partial[1];
            CNN_CHECK_SUM(acc);
            conv1[oc*48+t]=round_activation(acc);
        }
    }
POOL1: for(int i=0;i<768;++i) {
#pragma HLS PIPELINE II=1
        act_t a=conv1[2*i],b=conv1[2*i+1];
        act_t maximum=a>b?a:b;
        pool1[i]=maximum>0?maximum:(act_t)0;
    }
C2_OC: for(int oc=0;oc<64;++oc) {
#pragma HLS PIPELINE off
C2_TIME: for(int t=0;t<22;++t) {
#pragma HLS PIPELINE off
            sum_t partial[2];
#pragma HLS ARRAY_PARTITION variable=partial complete dim=1
            partial[0]=(sum_t)biases[cache_slot][32+oc]*(sum_t)8192ULL;
            partial[1]=0;
            CNN_CHECK_SUM(partial[0]);
C2_DOT: for(int base=0;base<96;base+=2) {
#pragma HLS PIPELINE II=1
C2_LANES: for(int lane=0;lane<2;++lane) {
#pragma HLS UNROLL
                    const int d=base+lane;
                    product_t product=multiply(pool1[(d/3)*24+t+d%3],weights[cache_slot][2112+oc*96+d]);
                    partial[lane]+=(sum_t)product;
                    CNN_CHECK_SUM(partial[lane]);
                }
            }
            sum_t acc=partial[0]+partial[1];
            CNN_CHECK_SUM(acc);
            conv2[oc*22+t]=round_activation(acc);
        }
    }
POOL2: for(int i=0;i<704;++i) {
#pragma HLS PIPELINE II=1
        act_t a=conv2[2*i],b=conv2[2*i+1];
        act_t maximum=a>b?a:b;
        pool2[i]=maximum>0?maximum:(act_t)0;
    }
    // pool2 is already contiguous channel-major, matching PyTorch Flatten.
FC1_OUT: for(int o=0;o<64;++o) {
#pragma HLS PIPELINE off
        sum_t partial[2];
#pragma HLS ARRAY_PARTITION variable=partial complete dim=1
        partial[0]=(sum_t)biases[cache_slot][96+o]*(sum_t)8192ULL;
        partial[1]=0;
        CNN_CHECK_SUM(partial[0]);
FC1_DOT: for(int base=0;base<704;base+=2) {
#pragma HLS PIPELINE II=1
FC1_LANES: for(int lane=0;lane<2;++lane) {
#pragma HLS UNROLL
                const int d=base+lane;
                product_t product=multiply(pool2[d],weights[cache_slot][8256+o*704+d]);
                partial[lane]+=(sum_t)product;
                CNN_CHECK_SUM(partial[lane]);
            }
        }
        sum_t acc=partial[0]+partial[1];
        CNN_CHECK_SUM(acc);
        act_t value=round_activation(acc);
        hidden[o]=value>0?value:(act_t)0;
    }
FC2_OUT: for(int o=0;o<8;++o) {
#pragma HLS PIPELINE off
        sum_t partial[2];
#pragma HLS ARRAY_PARTITION variable=partial complete dim=1
        partial[0]=(sum_t)biases[cache_slot][160+o]*(sum_t)8192ULL;
        partial[1]=0;
        CNN_CHECK_SUM(partial[0]);
FC2_DOT: for(int base=0;base<64;base+=2) {
#pragma HLS PIPELINE II=1
FC2_LANES: for(int lane=0;lane<2;++lane) {
#pragma HLS UNROLL
                const int d=base+lane;
                product_t product=multiply(hidden[d],weights[cache_slot][53312+o*64+d]);
                partial[lane]+=(sum_t)product;
                CNN_CHECK_SUM(partial[lane]);
            }
        }
        sum_t acc=partial[0]+partial[1];
        CNN_CHECK_SUM(acc);
        logit_words[o]=round_activation(acc);
    }
    return reload?CACHE_LOADED:CACHE_HIT;
}
