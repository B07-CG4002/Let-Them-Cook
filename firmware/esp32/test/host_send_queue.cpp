#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>

#include "comms_send_queue.h"

namespace {

comms::PendingSample sample(uint32_t uptimeMs) {
  comms::PendingSample result;
  result.uptimeMs = uptimeMs;
  for (size_t channel = 0; channel < comms::kChannelCount; ++channel) {
    result.values[channel] = static_cast<int16_t>(uptimeMs + channel);
  }
  return result;
}

uint32_t readUint32Le(const uint8_t* input) {
  return static_cast<uint32_t>(input[0]) | static_cast<uint32_t>(input[1]) << 8 |
      static_cast<uint32_t>(input[2]) << 16 | static_cast<uint32_t>(input[3]) << 24;
}

// Every operation keeps the wire invariant next_seq = submitted + failures.
void assertInvariant(const comms::SourceStats& stats, uint32_t start) {
  assert(static_cast<uint32_t>(stats.nextSequence - start) ==
         static_cast<uint32_t>(stats.submitted + stats.failures));
}

void testLimits() {
  static_assert(comms::kMaxPacketsPerNotification == 6, "the laptop accepts 1 to 6 packets");
  static_assert(comms::kMaxSensorNotificationSize == 228, "six 38-byte packets");
  assert(comms::packetsPerNotification(23) == 0);
  assert(comms::packetsPerNotification(40) == 0);
  assert(comms::packetsPerNotification(41) == 1);
  assert(comms::packetsPerNotification(78) == 1);
  assert(comms::packetsPerNotification(79) == 2);
  assert(comms::packetsPerNotification(231) == 6);
  assert(comms::packetsPerNotification(517) == 6);

  assert(comms::notificationsInFlight(10, 10, 9, 9) == 0);
  assert(comms::notificationsInFlight(10, 9, 9, 9) == 1);
  assert(comms::notificationsInFlight(10, 10, 9, 7) == 2);
  assert(comms::notificationsInFlight(10, 8, 9, 8) == 3);
  // A count above its recorded idle value is not negative work in flight.
  assert(comms::notificationsInFlight(9, 10, 8, 9) == 0);
  // Counts that never move (an unknown connection reads 0) gate nothing.
  assert(comms::notificationsInFlight(0, 0, 0, 0) == 0);
}

void testOrderAndCommit() {
  comms::SourceStats stats = {0x01020304u, 0u, 0u, 0u};
  comms::SampleQueue queue;
  for (uint32_t index = 0; index < 3; ++index) {
    assert(!comms::enqueueSample(queue, stats, sample(1000u + 20u * index)));
  }
  assert(queue.size() == 3 && stats.nextSequence == 0u);

  uint8_t first[comms::kMaxSensorNotificationSize];
  const size_t count = comms::buildSensorNotification(first, sizeof(first), queue, 6, 2, stats);
  assert(count == 3);
  // Building takes no seq: a notification the stack refuses is built again
  // from the same queue with the same bytes.
  assert(stats.nextSequence == 0u && stats.submitted == 0u && queue.size() == 3);
  uint8_t again[comms::kMaxSensorNotificationSize];
  assert(comms::buildSensorNotification(again, sizeof(again), queue, 6, 2, stats) == 3);
  assert(std::memcmp(first, again, 3 * comms::kPacketSize) == 0);

  for (size_t index = 0; index < count; ++index) {
    const uint8_t* packet = first + index * comms::kPacketSize;
    uint8_t expected[comms::kPacketSize];
    const comms::PendingSample source = sample(1000u + 20u * static_cast<uint32_t>(index));
    assert(comms::serializePacket(expected, sizeof(expected), 2, stats.bootId,
                                  static_cast<uint32_t>(index), source.uptimeMs, source.values));
    assert(std::memcmp(packet, expected, comms::kPacketSize) == 0);
    assert(readUint32Le(packet + 8) == index);
  }

  comms::commitSensorNotification(queue, stats, count);
  assert(queue.size() == 0 && stats.nextSequence == 3u && stats.submitted == 3u &&
         stats.failures == 0u);
  assertInvariant(stats, 0u);
  // Nothing queued: nothing to build.
  assert(comms::buildSensorNotification(first, sizeof(first), queue, 6, 2, stats) == 0);
}

void testBatchLimit() {
  comms::SourceStats stats = {7u, 0u, 0u, 0u};
  comms::SampleQueue queue;
  for (uint32_t index = 0; index < 10; ++index) comms::enqueueSample(queue, stats, sample(index));
  uint8_t payload[comms::kMaxSensorNotificationSize];

  // The MTU limit (one packet below ATT MTU 79) wins over the queue.
  assert(comms::buildSensorNotification(payload, sizeof(payload), queue, 1, 1, stats) == 1);
  // A limit above six is capped at six.
  assert(comms::buildSensorNotification(payload, sizeof(payload), queue, 13, 1, stats) == 6);
  assert(readUint32Le(payload + 8) == 0u && readUint32Le(payload + 5 * comms::kPacketSize + 8) == 5u);
  comms::commitSensorNotification(queue, stats, 6);
  assert(comms::buildSensorNotification(payload, sizeof(payload), queue, 6, 1, stats) == 4);
  assert(readUint32Le(payload + 8) == 6u && readUint32Le(payload + 12) == 6u);
  comms::commitSensorNotification(queue, stats, 4);
  assert(stats.nextSequence == 10u && stats.submitted == 10u && queue.size() == 0);
  assertInvariant(stats, 0u);

  // An output buffer too small for the batch builds nothing.
  for (uint32_t index = 0; index < 2; ++index) comms::enqueueSample(queue, stats, sample(index));
  assert(comms::buildSensorNotification(payload, 2 * comms::kPacketSize - 1, queue, 6, 1, stats) == 0);
  assert(comms::buildSensorNotification(nullptr, sizeof(payload), queue, 6, 1, stats) == 0);
  // An invalid device ID builds nothing.
  assert(comms::buildSensorNotification(payload, sizeof(payload), queue, 6, 3, stats) == 0);
}

void testOverflowDropsOldestAsFailure() {
  comms::SourceStats stats = {9u, 0u, 0u, 0u};
  comms::SampleQueue queue;
  size_t dropped = 0;
  for (uint32_t index = 0; index < comms::kSendQueueCapacity + 3; ++index) {
    if (comms::enqueueSample(queue, stats, sample(100u + index))) ++dropped;
    assertInvariant(stats, 0u);
  }
  // The three oldest left the full queue: each took a seq (0, 1, 2) and
  // counts as a failure, so the laptop sees a gap of three before seq 3.
  assert(dropped == 3 && queue.full() && queue.size() == comms::kSendQueueCapacity);
  assert(stats.nextSequence == 3u && stats.failures == 3u && stats.submitted == 0u);
  assert(queue.at(0).uptimeMs == 103u);
  uint8_t payload[comms::kMaxSensorNotificationSize];
  assert(comms::buildSensorNotification(payload, sizeof(payload), queue, 6, 1, stats) == 6);
  assert(readUint32Le(payload + 8) == 3u && readUint32Le(payload + 12) == 103u);
  assert(!queue.push(sample(0)));  // push alone never overwrites
}

void testDiscardTakesNoSeq() {
  comms::SourceStats stats = {9u, 5u, 4u, 1u};
  comms::SampleQueue queue;
  for (uint32_t index = 0; index < 4; ++index) comms::enqueueSample(queue, stats, sample(index));
  // Notifications turned off or the link lost: the queue is emptied and the
  // statistics stay as they were, so the laptop's two shutdown reads agree.
  assert(queue.clear() == 4 && queue.size() == 0);
  assert(stats.nextSequence == 5u && stats.submitted == 4u && stats.failures == 1u);
  assert(queue.clear() == 0);
  // A commit never removes more than is queued.
  comms::enqueueSample(queue, stats, sample(1));
  comms::commitSensorNotification(queue, stats, 3);
  assert(queue.size() == 0 && stats.nextSequence == 6u && stats.submitted == 5u);
}

void testSequenceWrap() {
  comms::SourceStats stats = {1u, UINT32_MAX - 1u, UINT32_MAX - 1u, 0u};
  comms::SampleQueue queue;
  for (uint32_t index = 0; index < 3; ++index) comms::enqueueSample(queue, stats, sample(index));
  uint8_t payload[comms::kMaxSensorNotificationSize];
  assert(comms::buildSensorNotification(payload, sizeof(payload), queue, 6, 1, stats) == 3);
  assert(readUint32Le(payload + 8) == UINT32_MAX - 1u);
  assert(readUint32Le(payload + comms::kPacketSize + 8) == UINT32_MAX);
  assert(readUint32Le(payload + 2 * comms::kPacketSize + 8) == 0u);
  comms::commitSensorNotification(queue, stats, 3);
  assert(stats.nextSequence == 1u && stats.submitted == 1u);
}

// pytest splits this two-packet notification with the laptop's parser.
void printReferenceNotification() {
  comms::SourceStats stats = {0xa1b2c3d4u, 41u, 41u, 0u};
  comms::SampleQueue queue;
  comms::enqueueSample(queue, stats, sample(5000u));
  comms::enqueueSample(queue, stats, sample(5020u));
  uint8_t payload[comms::kMaxSensorNotificationSize];
  const size_t count = comms::buildSensorNotification(payload, sizeof(payload), queue, 6, 2, stats);
  assert(count == 2);
  for (size_t index = 0; index < count * comms::kPacketSize; ++index) {
    std::printf("%02x", payload[index]);
  }
  std::printf("\n");
}

}  // namespace

int main() {
  testLimits();
  testOrderAndCommit();
  testBatchLimit();
  testOverflowDropsOldestAsFailure();
  testDiscardTakesNoSeq();
  testSequenceWrap();
  printReferenceNotification();
  std::puts("PASS send queue order, batching, overflow, discard and wrap");
}
