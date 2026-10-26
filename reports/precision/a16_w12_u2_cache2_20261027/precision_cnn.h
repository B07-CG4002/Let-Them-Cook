#ifndef PRECISION_CNN_H
#define PRECISION_CNN_H
// AXI remains signed32. Only internal values/arithmetic use narrower widths.
const int ACT_W=16, ACT_F=6, WEIGHT_W=12, WEIGHT_F=13, ACC_W=38;
const double ACT_SCALE=64.0;
const long long ACT_MIN=-32768LL, ACT_MAX=32767LL;
const long long WEIGHT_MIN=-2048LL, WEIGHT_MAX=2047LL;
#ifdef CNN_PORTABLE_TEST
#include <cstdint>
#include <cassert>
typedef int32_t word_t;
typedef int32_t act_t;
typedef int32_t weight_word_t;
typedef int64_t sum_t;
typedef int64_t product_t;
inline void cnn_check_sum(int64_t x) { assert(x >= -137438953472LL && x <= 137438953471LL); }
#define CNN_CHECK_SUM(x) cnn_check_sum(x)
#else
#include <ap_int.h>
typedef ap_int<32> word_t;
typedef ap_int<16> act_t;
typedef ap_int<12> weight_word_t;
typedef ap_int<38> sum_t;
typedef ap_int<28> product_t;
#define CNN_CHECK_SUM(x) ((void)0)
#endif
const int CNN_INPUTS=1100, CNN_WEIGHTS=53824, CNN_PARAMETERS=53992, CNN_OUTPUTS=8;
const unsigned int CACHE_AUTO=0, CACHE_FORCE_RELOAD=1, CACHE_INVALIDATE_ALL=2;
const unsigned int CACHE_HIT=0, CACHE_LOADED=1, CACHE_INVALID_CONTROL=2, CACHE_INVALIDATED=3;
unsigned int gesture_cnn_precision_u2_cache(const word_t input_words[CNN_INPUTS],
                           const word_t parameter_words[CNN_PARAMETERS],
                           word_t logit_words[CNN_OUTPUTS], unsigned int cache_slot,
                      unsigned int model_id, unsigned int cache_command);
#endif
