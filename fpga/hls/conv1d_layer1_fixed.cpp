// Fixed-point Conv1D layer 1 candidate, calibrated against one exported
// held-out case. Validate more samples and full-model accuracy before use.
#include "ap_fixed.h"

using input_t  = ap_fixed<28, 10, AP_RND, AP_SAT>; // range [-512, 512), still 18 fractional bits
using weight_t = ap_fixed<22, 1, AP_RND, AP_SAT>;  // 21 fractional bits
using accum_t  = ap_fixed<40, 13, AP_RND, AP_SAT>; // conservative +/-4096

void conv1d_layer1(const input_t input[50][22],
                   const weight_t weights[32][22][3],
                   const accum_t bias[32],
                   accum_t output[32][48]) {
#pragma HLS INTERFACE mode=m_axi port=input offset=slave bundle=gmem0 depth=1100
#pragma HLS INTERFACE mode=m_axi port=weights offset=slave bundle=gmem1 depth=2112
#pragma HLS INTERFACE mode=m_axi port=bias offset=slave bundle=gmem1 depth=32
#pragma HLS INTERFACE mode=m_axi port=output offset=slave bundle=gmem0 depth=1536
#pragma HLS INTERFACE mode=s_axilite port=return bundle=control

    input_t input_buf[50][22];
    weight_t weights_buf[32][22][3];
    accum_t bias_buf[32];

#pragma HLS ARRAY_PARTITION variable=input_buf cyclic factor=3 dim=1
#pragma HLS ARRAY_PARTITION variable=input_buf complete dim=2
#pragma HLS ARRAY_PARTITION variable=weights_buf complete dim=2
#pragma HLS ARRAY_PARTITION variable=weights_buf complete dim=3

    for (int t = 0; t < 50; ++t) {
        for (int ic = 0; ic < 22; ++ic) {
#pragma HLS PIPELINE II=1
            input_buf[t][ic] = input[t][ic];
        }
    }
    for (int oc = 0; oc < 32; ++oc) {
        bias_buf[oc] = bias[oc];
        for (int ic = 0; ic < 22; ++ic) {
            for (int k = 0; k < 3; ++k) {
#pragma HLS PIPELINE II=1
                weights_buf[oc][ic][k] = weights[oc][ic][k];
            }
        }
    }

    for (int oc = 0; oc < 32; ++oc) {
        for (int t = 0; t < 48; ++t) {
#pragma HLS PIPELINE II=1
            accum_t acc = bias_buf[oc];
            for (int ic = 0; ic < 22; ++ic) {
                for (int k = 0; k < 3; ++k) {
                    acc += input_buf[t + k][ic] * weights_buf[oc][ic][k];
                }
            }
            output[oc][t] = acc;
        }
    }
}
