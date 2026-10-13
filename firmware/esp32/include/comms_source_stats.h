#pragma once

#include <stddef.h>
#include <stdint.h>

#include "comms_packet.h"

namespace comms {

constexpr size_t kSourceStatsSize = 24;

struct SourceStats {
  uint32_t bootId;
  uint32_t nextSequence;
  uint32_t submitted;
  uint32_t failures;
};

inline uint32_t allocateSampleSequence(SourceStats& stats) {
  const uint32_t sequence = stats.nextSequence;
  ++stats.nextSequence;
  return sequence;
}

inline void recordSensorSubmission(SourceStats& stats, bool succeeded) {
  if (succeeded) {
    ++stats.submitted;
  } else {
    ++stats.failures;
  }
}

inline bool serializeSourceStats(uint8_t* output, size_t capacity,
                                 uint8_t deviceId, const SourceStats& stats) {
  if (output == nullptr || capacity < kSourceStatsSize ||
      (deviceId != 1 && deviceId != 2)) {
    return false;
  }

  output[0] = 'L';
  output[1] = 'C';
  output[2] = 'S';
  output[3] = '1';
  output[4] = deviceId;
  output[5] = 0;
  output[6] = 0;
  output[7] = 0;
  writeUint32Le(output + 8, stats.bootId);
  writeUint32Le(output + 12, stats.nextSequence);
  writeUint32Le(output + 16, stats.submitted);
  writeUint32Le(output + 20, stats.failures);
  return true;
}

}  // namespace comms
