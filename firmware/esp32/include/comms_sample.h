#pragma once

#include <stddef.h>
#include <stdint.h>
#include <cmath>
#include "comms_packet.h"

// What to do with a channel whose scaled value does not fit int16.
// PROVISIONAL until agreed with the model owner: 0 (default) clamps it to the
// int16 limit and still sends the sample; 1 refuses the whole sample.
#ifndef COMMS_OUT_OF_RANGE_DROP
#define COMMS_OUT_OF_RANGE_DROP 0
#endif

namespace comms {

constexpr bool kOutOfRangeDrop = COMMS_OUT_OF_RANGE_DROP != 0;

// physical = int16 / scale. Order: ax ay az (g), gx gy gz (deg/s),
// yaw pitch roll (degrees), flex1 flex2 (0.0..1.0). Provisional until checked
// against real glove samples.
constexpr float kChannelScale[kChannelCount] = {
    10000.0f, 10000.0f, 10000.0f, 100.0f, 100.0f, 100.0f,
    100.0f, 100.0f, 100.0f, 10000.0f, 10000.0f};

struct SampleScaleStats {
  uint32_t clamped;   // channel values clamped to an int16 limit
  uint32_t rejected;  // samples refused; the caller must not send them
};

// Scales one sample of physical values into the transported int16 values.
// Rounds half away from zero. Returns false, counts one rejection and leaves
// out untouched for null pointers, any NaN or infinity, and (drop policy only)
// any value outside int16. The caller allocates a sequence number only after
// success, so a refused sample leaves no seq gap, only a longer uptime_ms step.
inline bool scaleSample(const float* in, int16_t* out, SampleScaleStats& stats) {
  if (in == nullptr || out == nullptr) {
    ++stats.rejected;
    return false;
  }
  int16_t scaled[kChannelCount];
  uint32_t clamped = 0;
  for (size_t channel = 0; channel < kChannelCount; ++channel) {
    if (!std::isfinite(in[channel])) {
      ++stats.rejected;
      return false;
    }
    // float x integer scale is exact in double, so rounding never sees a
    // product already rounded by float arithmetic or the FPU rounding mode.
    const double value = static_cast<double>(in[channel]) * kChannelScale[channel];
    const double rounded = value >= 0.0 ? std::floor(value + 0.5) : std::ceil(value - 0.5);
    if (rounded >= -32768.0 && rounded <= 32767.0) {
      scaled[channel] = static_cast<int16_t>(rounded);
    } else if (kOutOfRangeDrop) {
      ++stats.rejected;
      return false;
    } else {
      scaled[channel] = rounded > 0.0 ? INT16_MAX : INT16_MIN;
      ++clamped;
    }
  }
  for (size_t channel = 0; channel < kChannelCount; ++channel) out[channel] = scaled[channel];
  stats.clamped += clamped;
  return true;
}

}  // namespace comms
