// Compile/run on a host; expected byte strings live in the independent fixtures.
#include <cassert>
#include <cstdio>
#include "comms_packet.h"
#include "comms_security.h"

static void printPacket(const uint8_t* bytes) {
  for (size_t i = 0; i < comms::kPacketSize; ++i) std::printf("%02x", bytes[i]);
  std::printf("\n");
}

int main() {
  uint8_t output[32] = {};
  assert(comms::serializeDummyPacket(output, sizeof(output), 2, 0x78563412, 42, 0x01020304));
  printPacket(output);
  const int16_t extremes[8] = {-32768, 32767, -1, 0, 1, 255, 256, -256};
  assert(comms::serializePacket(output, sizeof(output), 1, 0xffffffff, 0xffffffff, 0, extremes));
  printPacket(output);
  assert(!comms::serializePacket(output, 31, 1, 0, 0, 0, extremes));
  assert(!comms::serializePacket(output, 32, 0, 0, 0, 0, extremes));
  assert(!comms::serializePacket(output, 32, 3, 0, 0, 0, extremes));
  assert(!comms::serializePacket(nullptr, 32, 1, 0, 0, 0, extremes));
  assert(!comms::serializePacket(output, 32, 1, 0, 0, 0, nullptr));
  assert(comms::dummyValue(0, 0) == -1000);
  assert(comms::dummyValue(1999, 7) == 1069);
  assert(comms::dummyValue(2000, 0) == -1000);
  assert(comms::dummyValue(0xffffffff, 7) == 365);

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
  assert(!comms::sensorFitsMtu(34));
  assert(comms::sensorFitsMtu(35));
  assert(comms::sensorFitsMtu(517));
}
