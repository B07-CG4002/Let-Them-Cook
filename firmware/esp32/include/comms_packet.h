#pragma once

#include <stddef.h>
#include <stdint.h>
#include "comms_fixtures.h"

#ifndef COMMS_FLEX3_GLOVE
#define COMMS_FLEX3_GLOVE 0
#endif
static_assert(COMMS_FLEX3_GLOVE == 0 || COMMS_FLEX3_GLOVE == 1, "COMMS_FLEX3_GLOVE must be 0 or 1");

#ifndef COMMS_WIDE_GYRO
#define COMMS_WIDE_GYRO 0
#endif
static_assert(COMMS_WIDE_GYRO == 0 || COMMS_WIDE_GYRO == 1, "COMMS_WIDE_GYRO must be 0 or 1");
static_assert(!COMMS_WIDE_GYRO || COMMS_FLEX3_GLOVE, "v5 requires twelve channels");

namespace comms {

// Legacy: v3, 38 bytes, 11 channels. Opt-in COMMS_FLEX3_GLOVE=1:
// v4, 40 bytes, appending flex3 (GPIO39) after flex2. Same 16-byte header.
// Explicit v5 keeps the 40-byte layout but encodes gyro at 10 counts/deg/s.
constexpr uint8_t kPacketVersion = COMMS_WIDE_GYRO ? 5 : (COMMS_FLEX3_GLOVE ? 4 : 3);
constexpr size_t kPacketHeaderSize = 16;
constexpr size_t kChannelCount = COMMS_FLEX3_GLOVE ? 12 : 11;
constexpr size_t kPacketSize = kPacketHeaderSize + kChannelCount * 2;
static_assert(kPacketSize == (COMMS_FLEX3_GLOVE ? 40 : 38), "sensor packet layout");
static_assert(sizeof(kDummyFixtures[0]) / sizeof(kDummyFixtures[0][0]) == 11,
              "each fixture row needs 11 channels; run python -m tools.generate_dummy_fixtures");

inline void writeUint32Le(uint8_t* output, uint32_t value) {
  for (size_t byte = 0; byte < 4; ++byte) {
    output[byte] = static_cast<uint8_t>(value >> (byte * 8));
  }
}

// The array reference makes a caller with fewer than 11 values a compile
// error instead of an out-of-bounds read.
inline bool serializePacket(uint8_t* output, size_t capacity, uint8_t deviceId,
                            uint32_t bootId, uint32_t seq, uint32_t uptimeMs,
                            const int16_t (&values)[kChannelCount]) {
  if (output == nullptr || capacity < kPacketSize ||
      (deviceId != 1 && deviceId != 2)) return false;
  output[0] = 'L';
  output[1] = 'C';
  output[2] = kPacketVersion;
  output[3] = deviceId;
  writeUint32Le(output + 4, bootId);
  writeUint32Le(output + 8, seq);
  writeUint32Le(output + 12, uptimeMs);
  for (size_t channel = 0; channel < kChannelCount; ++channel) {
    const uint16_t value = static_cast<uint16_t>(values[channel]);
    output[kPacketHeaderSize + channel * 2] = static_cast<uint8_t>(value);
    output[kPacketHeaderSize + channel * 2 + 1] = static_cast<uint8_t>(value >> 8);
  }
  return true;
}

// Dummy data: an editable fixture row chosen independently of the sequence.
// The version byte says nothing about the data source. The board's dummy
// glove module (src/dummy_glove.cpp) produces the same bytes through the
// sensor path; host tests keep this helper as the reference.
inline bool serializeFixturePacket(uint8_t* output, size_t capacity,
                                   uint8_t deviceId, uint32_t bootId, uint32_t seq,
                                   uint32_t uptimeMs, uint32_t randomWord) {
  static_assert(kDummyFixtureCount > 1, "random source needs multiple fixtures");
  int16_t values[kChannelCount] = {};
  for (size_t i = 0; i < 11; ++i) values[i] = kDummyFixtures[randomWord % kDummyFixtureCount][i];
  return serializePacket(output, capacity, deviceId, bootId, seq, uptimeMs, values);
}

}  // namespace comms
