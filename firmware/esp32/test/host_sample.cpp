// Host test for comms_sample.h. Build it twice: as is (clamp policy) and with
// -DCOMMS_OUT_OF_RANGE_DROP=1 (drop policy). Expected values are literals.
#include <cassert>
#include <cfloat>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include "comms_sample.h"

static const int16_t kUntouched = 0x5a5a;

static void fillUntouched(int16_t* out) {
  for (size_t channel = 0; channel < 11; ++channel) out[channel] = kUntouched;
}

static bool untouched(const int16_t* out) {
  for (size_t channel = 0; channel < 11; ++channel) {
    if (out[channel] != kUntouched) return false;
  }
  return true;
}

static void expectScaled(const float (&in)[11], const int16_t (&expected)[11],
                         comms::SampleScaleStats& stats) {
  int16_t out[11];
  fillUntouched(out);
  assert(comms::scaleSample(in, out, stats));
  assert(std::memcmp(out, expected, sizeof(out)) == 0);
}

static void expectRefused(const float (&in)[11], comms::SampleScaleStats& stats) {
  int16_t out[11];
  fillUntouched(out);
  const uint32_t clamped = stats.clamped, rejected = stats.rejected;
  assert(!comms::scaleSample(in, out, stats));
  assert(untouched(out));
  assert(stats.rejected == rejected + 1 && stats.clamped == clamped);
}

int main() {
  static_assert(comms::kChannelCount == 11, "11 channels");
  const float scales[11] = {10000, 10000, 10000, 100, 100, 100, 100, 100, 100, 10000, 10000};
  for (size_t channel = 0; channel < 11; ++channel) {
    assert(comms::kChannelScale[channel] == scales[channel]);
  }
  comms::SampleScaleStats stats = {0, 0};

  // Ordinary values in every unit: g, deg/s, degrees, flex fraction.
  const float ordinary[11] = {1.0f, -0.5f, 0.0001f, 12.5f, -250.0f, 0.0f,
                              180.0f, -90.0f, 45.5f, 0.6f, 1.0f};
  const int16_t ordinaryOut[11] = {10000, -5000, 1, 1250, -25000, 0,
                                   18000, -9000, 4550, 6000, 10000};
  expectScaled(ordinary, ordinaryOut, stats);
  const float zero[11] = {0.0f, -0.0f, 0, 0, 0, 0, 0, 0, 0, 0, 0};
  const int16_t zeroOut[11] = {};
  expectScaled(zero, zeroOut, stats);

  // Exact halves round away from zero (not to even, not toward zero).
  // 0.005f is slightly below 0.005, so 0.4999999888 rounds to 0.
  const float halves[11] = {0, 0, 0, 0.125f, -0.125f, 0.625f, -0.625f, 0.375f, 0.005f, 0, 0};
  const int16_t halvesOut[11] = {0, 0, 0, 13, -13, 63, -63, 38, 0, 0, 0};
  expectScaled(halves, halvesOut, stats);

  // Exact limits: 3.2767 g, -3.2768 g, 327.67 deg/s, -327.68 deg/s, flex 3.2767.
  // Values that round back inside int16 are not clamped.
  const float limits[11] = {3.2767f, -3.2768f, 3.27674f, 327.67f, -327.68f, 327.67f,
                            -327.68f, 327.67f, -327.68f, 3.2767f, -3.27684f};
  const int16_t limitsOut[11] = {32767, -32768, 32767, 32767, -32768, 32767,
                                 -32768, 32767, -32768, 32767, -32768};
  expectScaled(limits, limitsOut, stats);
  assert(stats.clamped == 0 && stats.rejected == 0);

  // Just over the limits, plus large finite values: three channels out of range.
  const float over[11] = {3.2768f, 0, 0, 0, -327.69f, 0, 0, 0, 0, 0, 1e30f};
  const float overNegative[11] = {-3.2769f, 0, 0, 327.68f, 0, 0, -FLT_MAX, 0, 0, 0, 0};
  if (comms::kOutOfRangeDrop) {
    expectRefused(over, stats);
    expectRefused(overNegative, stats);
    assert(stats.clamped == 0 && stats.rejected == 2);
  } else {
    const int16_t overOut[11] = {32767, 0, 0, 0, -32768, 0, 0, 0, 0, 0, 32767};
    expectScaled(over, overOut, stats);
    const int16_t overNegativeOut[11] = {-32768, 0, 0, 32767, 0, 0, -32768, 0, 0, 0, 0};
    expectScaled(overNegative, overNegativeOut, stats);
    assert(stats.clamped == 6 && stats.rejected == 0);
  }

  // NaN or infinity in any channel refuses the whole sample under either
  // policy, without counting clamps for channels scaled before it.
  const uint32_t clampedBefore = stats.clamped, rejectedBefore = stats.rejected;
  const float specials[3] = {NAN, INFINITY, -INFINITY};
  const size_t positions[3] = {0, 5, 10};
  for (size_t special = 0; special < 3; ++special) {
    for (size_t position = 0; position < 3; ++position) {
      float in[11] = {1e9f, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0};
      in[positions[position]] = specials[special];
      expectRefused(in, stats);
    }
  }
  assert(stats.clamped == clampedBefore && stats.rejected == rejectedBefore + 9);

  // Null pointers are refused and counted.
  int16_t out[11];
  fillUntouched(out);
  assert(!comms::scaleSample(nullptr, out, stats));
  assert(untouched(out));
  assert(!comms::scaleSample(ordinary, nullptr, stats));
  assert(stats.rejected == rejectedBefore + 11);

  // Counters accumulate and a later ordinary sample still succeeds.
  expectScaled(ordinary, ordinaryOut, stats);
  assert(stats.clamped == clampedBefore && stats.rejected == rejectedBefore + 11);

  std::printf("PASS sample scaling, rounding, limits and refusals (%s policy)\n",
              comms::kOutOfRangeDrop ? "drop" : "clamp");
}
