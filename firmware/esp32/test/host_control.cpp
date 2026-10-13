#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>
#include "comms_control.h"
#include "comms_security.h"

static const uint8_t kDigest[32] = {
  0xbe,0xf5,0x7e,0xc7,0xf5,0x3a,0x6d,0x40,0xbe,0xb6,0x40,0xa7,0x80,0xa6,0x39,0xc8,
  0x3b,0xc2,0x9a,0xc8,0xa9,0x81,0x6f,0x1f,0xc6,0xc5,0xc6,0xdc,0xd9,0x3c,0x47,0x21};

// Hashing is a platform library boundary; assert the actual reconstructed bytes
// and return the independently known SHA-256 of "abcdef".
static bool hashSix(const uint8_t* data, size_t size, uint8_t* digest) {
  assert(size == 6 && std::memcmp(data, "abcdef", 6) == 0);
  std::memcpy(digest, kDigest, 32);
  return true;
}

static std::vector<uint8_t> request(uint8_t opcode, uint32_t id,
    uint32_t offset = 0, const std::vector<uint8_t>& payload = {}) {
  std::vector<uint8_t> result = {'B','7',1,opcode,2,0,0,0,0,0,0,0,0,0};
  for (int i = 0; i < 4; ++i) {
    result[6+i] = static_cast<uint8_t>(id >> (i*8));
    result[10+i] = static_cast<uint8_t>(offset >> (i*8));
  }
  result.insert(result.end(), payload.begin(), payload.end());
  return result;
}

static std::vector<uint8_t> execute(comms::ControlEngine& engine,
    const std::vector<uint8_t>& input, uint8_t status = 0, uint32_t now = 500) {
  uint8_t output[comms::kControlMaxFrame] = {};
  const size_t size = engine.process(input.data(), input.size(), now, output, sizeof(output));
  assert(size >= 14 && output[0] == 'B' && output[1] == '7');
  assert(output[2] == 1 && output[3] == (input[3] | 0x80));
  assert(output[4] == 2 && output[5] == status);
  assert(std::memcmp(input.data()+6, output+6, 4) == 0);
  return std::vector<uint8_t>(output, output+size);
}

int main() {
  assert(comms::canAcceptControl(true, true, true, false, 5, 5));
  assert(!comms::canAcceptControl(true, true, false, false, 5, 5));
  assert(!comms::canAcceptControl(true, true, true, false, 4, 5));
  assert(!comms::canAcceptControl(true, false, true, false, 5, 5));
  assert(!comms::canAcceptControl(false, true, true, false, 5, 5));
  assert(comms::canAcceptControl(true, true, false, true, 5, 5));
  uint8_t fixturePacket[32] = {};
  assert(comms::serializeFixturePacket(fixturePacket, 32, 2, 7, 9, 100, 0));
  assert(fixturePacket[0] == 'L' && fixturePacket[1] == 'C');
  assert(fixturePacket[2] == 2 && fixturePacket[3] == 2 && fixturePacket[8] == 9);
  assert(!comms::serializeFixturePacket(fixturePacket, 31, 2, 7, 9, 100, 0));
  uint8_t nextFixturePacket[32] = {};
  assert(comms::serializeFixturePacket(nextFixturePacket, 32, 2, 7, 9, 100, 1));
  assert(std::memcmp(fixturePacket + 16, nextFixturePacket + 16, 16) != 0);
  assert(comms::serializeFixturePacket(nextFixturePacket, 32, 2, 7, 9, 100,
                                      static_cast<uint32_t>(comms::kDummyFixtureCount)));
  assert(std::memcmp(fixturePacket, nextFixturePacket, 32) == 0);
  comms::ControlEngine engine(2, 0x78563412, hashSix);
  // Literal sensor packet exercises int16 wrap and every identity field.
  std::vector<uint8_t> sensor = {
    'W','7',2,2,0x12,0x34,0x56,0x78,10,0,0,0,1,0,0,0,
    0xff,0x7f,0,0x80,0xff,0xff,0,0,1,0,0xff,0,0,1,0,0xff};
  auto currentSensor = sensor;
  currentSensor[0] = 'L'; currentSensor[1] = 'C';
  comms::ControlEngine currentEngine(2, 0x78563412, hashSix);
  auto currentResult = execute(currentEngine, request(1, 10, 0, currentSensor));
  assert(currentResult.size() == 46 && currentResult[14] == 'L' && currentResult[15] == 'C');
  auto mixedMagic = currentSensor; mixedMagic[0] = 'W'; mixedMagic[8] = 11;
  execute(currentEngine, request(1, 11, 0, mixedMagic), 1);
  auto command = request(1, 10, 0, sensor);
  auto result = execute(engine, command);
  const uint8_t expected[] = {
    'W','7',2,2,0x12,0x34,0x56,0x78,10,0,0,0,0xf4,1,0,0,
    0,0x80,1,0x80,0,0,1,0,2,0,0,1,1,1,1,0xff};
  assert(result.size() == 46 && std::memcmp(result.data()+14, expected, 32) == 0);
  assert(std::memcmp(currentResult.data()+16, result.data()+16, 30) == 0);
  assert(execute(engine, command, 0, 900) == result);  // Identical replay, not second transform.
  auto conflict = command; conflict.back() ^= 1;
  execute(engine, conflict, 5);
  auto wrongBoot = command; wrongBoot[18] ^= 1;
  execute(engine, wrongBoot, 1);
  auto wrongDevice = command; wrongDevice[17] = 1;
  execute(engine, wrongDevice, 1);
  auto wrongSequence = command; wrongSequence[22] = 11;
  execute(engine, wrongSequence, 1);
  auto wrongVersion = command; wrongVersion[16] = 1;
  execute(engine, wrongVersion, 1);
  for (uint32_t id = 11; id <= 20; ++id) {
    sensor[8] = static_cast<uint8_t>(id);
    execute(engine, request(1, id, 0, sensor));
  }
  execute(engine, command, 5);  // Evicted IDs never produce new effects.
  engine.disconnect();
  execute(engine, command);  // New BLE session starts a fresh bounded identity scope.

  auto rate = execute(engine, request(2, 30, 0, {200,0}));
  assert(engine.rateHz() == 200 && rate.size() == 16 && rate[14] == 200);
  execute(engine, request(2, 31, 0, {201,0}), 1);
  execute(engine, request(2, 32, 0, {0,0}), 1);
  execute(engine, request(2, 33, 1, {10,0}), 1);
  assert(engine.rateHz() == 200);

  // Multiple chunks, exact retransmission, conflict, order and final integrity.
  std::vector<uint8_t> metadata = {6,0,0,0};
  metadata.insert(metadata.end(), kDigest, kDigest+32);
  auto begin = request(0x10, 100, 0, metadata);
  execute(engine, begin);
  execute(engine, request(0x10, 101, 0, metadata), 2);
  execute(engine, request(0x11, 100, 3, {'d','e','f'}), 3);
  auto chunk = request(0x11, 100, 0, {'a','b','c'});
  auto ack = execute(engine, chunk);
  assert(ack.size() == 14 && ack[10] == 3);
  assert(execute(engine, chunk) == ack);
  execute(engine, request(0x11, 100, 0, {'x','b','c'}), 5);
  execute(engine, request(0x12, 100, 6), 3);
  execute(engine, request(0x11, 100, 3, {'d','e','f'}));
  auto end = request(0x12, 100, 6);
  result = execute(engine, end);
  assert(result.size() == 50 && result[14] == 6);
  assert(std::memcmp(result.data()+18, kDigest, 32) == 0);
  assert(execute(engine, end) == result);
  assert(execute(engine, begin)[10] == 6);
  auto alteredBegin = begin; alteredBegin.back() ^= 1;
  execute(engine, alteredBegin, 5);
  metadata.back() ^= 1;
  execute(engine, request(0x10, 101, 0, metadata));
  execute(engine, request(0x11, 101, 0, {'a','b','c','d','e','f'}));
  execute(engine, request(0x12, 101, 6), 4);
  execute(engine, request(0x13, 101));
  execute(engine, request(0x13, 101));
  execute(engine, request(0x12, 101, 6), 3);

  metadata = {1,0,1,0}; metadata.resize(36, 0);  // 65,537 exceeds RAM cap.
  execute(engine, request(0x10, 102, 0, metadata), 1);
  metadata[0] = 6; metadata[2] = 0;
  execute(engine, request(0x10, 103, 0, metadata));
  engine.disconnect();
  execute(engine, request(0x11, 103, 0, {'a'}), 3);
  execute(engine, request(0x10, 104, 0, metadata), 0, 1000);
  engine.expire(31001);
  execute(engine, request(0x12, 104, 6), 3);

  // Strict frame boundaries, version, opcode, identity and output bounds.
  uint8_t output[194];
  assert(engine.process(nullptr, 14, 0, output, sizeof(output)) == 0);
  assert(engine.process(command.data(), 13, 0, output, sizeof(output)) == 0);
  assert(engine.process(command.data(), command.size(), 0, output, 13) == 0);
  assert(engine.process(command.data(), command.size(), 0, nullptr, 194) == 0);
  auto bad = request(2, 200, 0, {10,0}); bad[0] = 'X';
  assert(engine.process(bad.data(), bad.size(), 0, output, 194) == 0);
  bad = request(2, 200, 0, {10,0}); bad[2] = 2;
  execute(engine, bad, 6);
  execute(engine, request(0x7f, 200), 6);
  execute(engine, request(2, 0, 0, {10,0}), 1);
  bad = request(2, 200, 0, {10,0}); bad[4] = 1;
  execute(engine, bad, 1);
  bad = request(2, 200, 0, {10,0}); bad[5] = 1;
  execute(engine, bad, 1);
  bad = request(0x11, 200, 0, std::vector<uint8_t>(181, 0));
  execute(engine, bad, 1);
  assert(comms::controlFitsMtu(64) && !comms::controlFitsMtu(63));
  assert(comms::controlChunkCapacity(64) == 47);
  assert(comms::controlChunkCapacity(517) == 180);
  std::puts("PASS B07 command, rate, bounded file and validation");
}
