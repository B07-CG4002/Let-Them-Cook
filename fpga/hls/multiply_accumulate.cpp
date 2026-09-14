#include <ap_int.h>

void multiply_accumulate(
    const ap_int<16>* a,
    const ap_int<16>* b,
    ap_int<32>& result
) {
#pragma HLS INTERFACE mode=m_axi port=a offset=slave bundle=gmem
#pragma HLS INTERFACE mode=m_axi port=b offset=slave bundle=gmem
#pragma HLS INTERFACE mode=s_axilite port=a bundle=control
#pragma HLS INTERFACE mode=s_axilite port=b bundle=control
#pragma HLS INTERFACE mode=s_axilite port=result bundle=control
#pragma HLS INTERFACE mode=s_axilite port=return bundle=control

    ap_int<32> sum = 0;

    for (int i = 0; i < 16; i++) {
#pragma HLS PIPELINE
        sum += a[i] * b[i];
    }

    result = sum;
}