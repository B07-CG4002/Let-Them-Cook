#pragma once

#include <stddef.h>
#include <stdint.h>

#include "comms_packet.h"
#include "comms_sample.h"

namespace comms {

// <4sBB2xIIIIIII>: LCS3, device ID, flags, 2 reserved zero bytes, then seven
// uint32 totals since boot.
constexpr size_t kSourceStatsSize = 36;
// Flags bit 0: a gated source, which skips ticks while the glove is IDLE.
constexpr uint8_t kSourceFlagGated = 0x01;
static_assert(kPacketVersion == 3, "update the LCS3 source statistics marker");

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

inline bool serializeSourceStats(uint8_t* output, size_t capacity, uint8_t deviceId,
                                 bool gated, const SourceStats& stats,
                                 const SampleCounters& samples) {
  if (output == nullptr || capacity < kSourceStatsSize ||
      (deviceId != 1 && deviceId != 2)) {
    return false;
  }

  // LCS3 follows the packet version, so an audited laptop bridge refuses a
  // board still running version 2 firmware (LCS1) before it subscribes.
  output[0] = 'L';
  output[1] = 'C';
  output[2] = 'S';
  output[3] = '3';
  output[4] = deviceId;
  output[5] = gated ? kSourceFlagGated : 0;
  output[6] = 0;
  output[7] = 0;
  writeUint32Le(output + 8, stats.bootId);
  writeUint32Le(output + 12, stats.nextSequence);
  writeUint32Le(output + 16, stats.submitted);
  writeUint32Le(output + 20, stats.failures);
  writeUint32Le(output + 24, samples.idle);
  writeUint32Le(output + 28, samples.unread);
  writeUint32Le(output + 32, samples.refused);
  return true;
}

}  // namespace comms
