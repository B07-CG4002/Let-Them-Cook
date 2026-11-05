#ifndef PRECISION_CNN_H
#define PRECISION_CNN_H
// AXI remains signed32. Only internal values/arithmetic use narrower widths.
const int ACT_W=32, ACT_F=18, WEIGHT_W=22, WEIGHT_F=21, ACC_W=64;
const double ACT_SCALE=262144.0;
const long long ACT_MIN=-2147483648LL, ACT_MAX=2147483647LL;
const long long WEIGHT_MIN=-2097152LL, WEIGHT_MAX=2097151LL;
#ifdef CNN_PORTABLE_TEST
#include <cstdint>
#include <cassert>
typedef int32_t word_t;
typedef int32_t act_t;
typedef int32_t weight_word_t;
typedef int64_t sum_t;
typedef int64_t product_t;
inline void cnn_check_sum(int64_t x) { assert(x >= (-9223372036854775807LL-1) && x <= 9223372036854775807LL); }
#define CNN_CHECK_SUM(x) cnn_check_sum(x)
#else
#include <ap_int.h>
typedef ap_int<32> word_t;
typedef ap_int<32> act_t;
typedef ap_int<22> weight_word_t;
typedef ap_int<64> sum_t;
typedef ap_int<54> product_t;
#define CNN_CHECK_SUM(x) ((void)0)
#endif
const int CNN_INPUTS=600, CNN_WEIGHTS=52800, CNN_PARAMETERS=52967, CNN_OUTPUTS=7;
const unsigned int CACHE_AUTO=0, CACHE_FORCE_RELOAD=1, CACHE_INVALIDATE_ALL=2;
const unsigned int CACHE_HIT=0, CACHE_LOADED=1, CACHE_INVALID_CONTROL=2, CACHE_INVALIDATED=3;
unsigned int gesture_cnn_hand_u2_cache(const word_t input_words[CNN_INPUTS],
                           const word_t parameter_words[CNN_PARAMETERS],
                           word_t logit_words[CNN_OUTPUTS], unsigned int cache_slot,
                      unsigned int model_id, unsigned int cache_command);
#endif
