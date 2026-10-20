#ifndef FULL_CNN_20261020_H
#define FULL_CNN_20261020_H
#ifdef CNN_PORTABLE_TEST
#include <cstdint>
typedef int32_t word_t;
typedef int32_t weight_word_t;
typedef int64_t sum_t;
typedef int64_t product_t;
#else
#include <ap_int.h>
typedef ap_int<32> word_t;
typedef ap_int<22> weight_word_t;
typedef ap_int<64> sum_t;
typedef ap_int<54> product_t;
#endif
const int CNN_INPUTS=1100;
const int CNN_WEIGHTS=53824;
const int CNN_PARAMETERS=53992;
const int CNN_OUTPUTS=8;
void gesture_cnn_full(const word_t input_words[CNN_INPUTS],
                      const word_t parameter_words[CNN_PARAMETERS],
                      word_t logit_words[CNN_OUTPUTS]);
#endif
