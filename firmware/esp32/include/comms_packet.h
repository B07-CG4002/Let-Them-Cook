#pragma once

#include <stddef.h>
#include <stdint.h>
#include "comms_fixtures.h"

namespace comms {

constexpr size_t kPacketSize = 32;

inline void writeUint32Le(uint8_t* output, uint32_t value) {
  for (size_t byte = 0; byte < 4; ++byte) {
    output[byte] = static_cast<uint8_t>(value >> (byte * 8));
  }
}

inline int16_t dummyValue(uint32_t seq, uint8_t channel) {
  return static_cast<int16_t>(static_cast<int32_t>(seq % 2000) - 1000 +
                              10 * channel);
}

inline bool serializePacket(uint8_t* output, size_t capacity, uint8_t deviceId,
                            uint32_t bootId, uint32_t seq, uint32_t uptimeMs,
                            const int16_t* values) {
  if (output == nullptr || values == nullptr || capacity < kPacketSize ||
      (deviceId != 1 && deviceId != 2)) return false;
  output[0] = 'L';
  output[1] = 'C';
  output[2] = 1;
  output[3] = deviceId;
  writeUint32Le(output + 4, bootId);
  writeUint32Le(output + 8, seq);
  writeUint32Le(output + 12, uptimeMs);
  for (size_t channel = 0; channel < 8; ++channel) {
    const uint16_t value = static_cast<uint16_t>(values[channel]);
    output[16 + channel * 2] = static_cast<uint8_t>(value);
    output[17 + channel * 2] = static_cast<uint8_t>(value >> 8);
  }
  return true;
}

inline bool serializeDummyPacket(uint8_t* output, size_t capacity,
                                 uint8_t deviceId, uint32_t bootId, uint32_t seq,
                                 uint32_t uptimeMs) {
  int16_t values[8];
  for (uint8_t channel = 0; channel < 8; ++channel) {
    values[channel] = dummyValue(seq, channel);
  }
  return serializePacket(output, capacity, deviceId, bootId, seq, uptimeMs, values);
}

// Version 1 remains the deterministic baseline. Version 2 has the same binary
// representation and chooses an editable fixture independently of sequence.
inline bool serializeFixturePacket(uint8_t* output, size_t capacity,
                                   uint8_t deviceId, uint32_t bootId, uint32_t seq,
                                   uint32_t uptimeMs, uint32_t randomWord) {
  static_assert(kDummyFixtureCount > 1, "random source needs multiple fixtures");
  if (!serializePacket(output, capacity, deviceId, bootId, seq, uptimeMs,
                       kDummyFixtures[randomWord % kDummyFixtureCount])) return false;
  output[2] = 2;
  return true;
}

}  // namespace comms
