#pragma once

#include <stddef.h>
#include <stdint.h>
#include "comms_fixtures.h"

namespace comms {

// Protocol version 3, the only version produced or accepted: a 16-byte header
// and 11 int16 channels (ax ay az gx gy gz yaw pitch roll flex1 flex2).
constexpr uint8_t kPacketVersion = 3;
constexpr size_t kPacketHeaderSize = 16;
constexpr size_t kChannelCount = 11;
constexpr size_t kPacketSize = kPacketHeaderSize + kChannelCount * 2;
static_assert(kPacketSize == 38, "a version 3 sensor packet is 38 bytes");
static_assert(sizeof(kDummyFixtures[0]) / sizeof(kDummyFixtures[0][0]) == kChannelCount,
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
// The version byte says nothing about the data source.
inline bool serializeFixturePacket(uint8_t* output, size_t capacity,
                                   uint8_t deviceId, uint32_t bootId, uint32_t seq,
                                   uint32_t uptimeMs, uint32_t randomWord) {
  static_assert(kDummyFixtureCount > 1, "random source needs multiple fixtures");
  return serializePacket(output, capacity, deviceId, bootId, seq, uptimeMs,
                         kDummyFixtures[randomWord % kDummyFixtureCount]);
}

}  // namespace comms
