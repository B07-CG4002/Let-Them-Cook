#pragma once

#include <stddef.h>
#include <stdint.h>
#include "comms_packet.h"
#include "comms_sample.h"

// The portable parts of the built-in dummy glove module (src/dummy_glove.cpp),
// so the host tests check exactly what the board runs.

namespace comms {

// The dummy movement detector: a repeating cycle from boot, MOVING for
// movingMs and then IDLE for idleMs. idleMs == 0 is always MOVING. The cycle
// length is computed in 64 bits, so no time overflows it; when millis() wraps
// (every 49.7 days) the cycle starts again with MOVING at 0.
inline bool dummyMoving(uint32_t nowMs, uint32_t movingMs, uint32_t idleMs) {
  if (idleMs == 0) return true;
  const uint64_t cycleMs = static_cast<uint64_t>(movingMs) + idleMs;
  return nowMs % cycleMs < movingMs;
}

// One dummy sample: the fixture row randomWord selects (as the version-3
// packets always did), in physical units, physical = int16 / scale. Scaling
// it back with scaleSample gives the same int16 row, so the dummy packets keep
// their bytes after passing through the sensor path.
inline void dummyFixtureSample(uint32_t randomWord, float* out) {
  static_assert(kDummyFixtureCount > 1, "random source needs multiple fixtures");
  const int16_t (&row)[11] = kDummyFixtures[randomWord % kDummyFixtureCount];
  for (size_t channel = 0; channel < kChannelCount; ++channel) {
    out[channel] = channel < 11 ? row[channel] / kChannelScale[channel] : 0.0f;
  }
}

}  // namespace comms
