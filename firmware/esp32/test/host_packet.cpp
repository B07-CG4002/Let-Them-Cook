// Compile/run on a host; expected byte strings live in the independent fixtures.
#include <cassert>
#include <cstdio>
#include <cstring>
#include "comms_packet.h"
#include "comms_security.h"

static void printPacket(const uint8_t* bytes) {
  for (size_t i = 0; i < comms::kPacketSize; ++i) std::printf("%02x", bytes[i]);
  std::printf("\n");
}

int main() {
  static_assert(comms::kPacketSize == 38 && comms::kChannelCount == 11 &&
                comms::kPacketVersion == 3, "version 3 layout");
  uint8_t output[38] = {};
  // A literal copy of fixture row 1, so editing the fixture JSON never moves
  // the golden vector; the fixture path is checked against it separately below.
  const int16_t row[11] = {1200, -300, -8500, 4000, -2000, 1500, 1250, -3000, 600, 6000, 5500};
  assert(comms::serializePacket(output, sizeof(output), 2, 0x78563412, 42, 0x01020304, row));
  printPacket(output);
  const int16_t extremes[11] = {-32768, 32767, -1, 0, 1, 255, 256, -256, 32766, -32767, 0x1234};
  assert(comms::serializePacket(output, sizeof(output), 1, 0xffffffff, 0xffffffff, 0, extremes));
  printPacket(output);
  assert(!comms::serializePacket(output, 37, 1, 0, 0, 0, extremes));
  assert(!comms::serializePacket(output, 38, 0, 0, 0, 0, extremes));
  assert(!comms::serializePacket(output, 38, 3, 0, 0, 0, extremes));
  assert(!comms::serializePacket(nullptr, 38, 1, 0, 0, 0, extremes));

  // The board's send path: the random word selects a whole fixture row.
  uint8_t direct[38] = {};
  for (uint32_t word = 0; word < 2 * comms::kDummyFixtureCount; ++word) {
    const size_t index = word % comms::kDummyFixtureCount;
    assert(comms::serializeFixturePacket(output, sizeof(output), 2, 7, word, 100, word));
    assert(comms::serializePacket(direct, sizeof(direct), 2, 7, word, 100, comms::kDummyFixtures[index]));
    assert(std::memcmp(output, direct, sizeof(output)) == 0);
    assert(output[2] == 3);
  }
  assert(!comms::serializeFixturePacket(output, 37, 2, 7, 0, 100, 0));

  // A successful callback lacking either SC, MITM or bonding must stay closed.
  assert(comms::isAuthenticated(true, 0x0d));
  assert(!comms::isAuthenticated(false, 0x0d));
  assert(!comms::isAuthenticated(true, 0x09));
  assert(!comms::isAuthenticated(true, 0x05));
  assert(!comms::isAuthenticated(true, 0x0c));
  assert(!comms::canNotify(true, true, false, false));
  assert(comms::canNotify(true, true, true, false));
  assert(!comms::canNotify(false, true, true, false));
  assert(!comms::canNotify(true, false, true, false));
  assert(comms::canNotify(true, true, false, true));
  assert(!comms::canNotify(false, true, false, true));
  // 38-byte packet + 3-byte ATT notification header.
  assert(!comms::sensorFitsMtu(23));
  assert(!comms::sensorFitsMtu(35));
  assert(!comms::sensorFitsMtu(40));
  assert(comms::sensorFitsMtu(41));
  assert(comms::sensorFitsMtu(517));
}
