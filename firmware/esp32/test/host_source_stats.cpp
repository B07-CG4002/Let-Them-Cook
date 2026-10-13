#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>

#include "comms_source_stats.h"

int main() {
  comms::SourceStats stats = {0x78563412u, 0u, 0u, 0u};

  const uint32_t failedSequence = comms::allocateSampleSequence(stats);
  comms::recordSensorSubmission(stats, false);
  const uint32_t submittedSequence = comms::allocateSampleSequence(stats);
  comms::recordSensorSubmission(stats, true);

  assert(failedSequence == 0u);
  assert(submittedSequence == 1u);
  assert(stats.nextSequence == 2u);
  assert(stats.submitted == 1u);
  assert(stats.failures == 1u);

  uint8_t output[comms::kSourceStatsSize] = {};
  assert(comms::serializeSourceStats(output, sizeof(output), 2u, stats));
  const uint8_t expected[comms::kSourceStatsSize] = {
      0x4c, 0x43, 0x53, 0x31, 0x02, 0x00, 0x00, 0x00,
      0x12, 0x34, 0x56, 0x78, 0x02, 0x00, 0x00, 0x00,
      0x01, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00,
  };
  assert(std::memcmp(output, expected, sizeof(expected)) == 0);

  comms::SourceStats wrapped = {1u, UINT32_MAX, UINT32_MAX, UINT32_MAX};
  assert(comms::allocateSampleSequence(wrapped) == UINT32_MAX);
  comms::recordSensorSubmission(wrapped, true);
  comms::recordSensorSubmission(wrapped, false);
  assert(wrapped.nextSequence == 0u);
  assert(wrapped.submitted == 0u);
  assert(wrapped.failures == 0u);

  std::memset(output, 0xa5, sizeof(output));
  assert(!comms::serializeSourceStats(nullptr, sizeof(output), 1u, stats));
  assert(!comms::serializeSourceStats(output, sizeof(output) - 1u, 1u, stats));
  assert(!comms::serializeSourceStats(output, sizeof(output), 0u, stats));
  assert(!comms::serializeSourceStats(output, sizeof(output), 3u, stats));
  for (size_t index = 0; index < sizeof(output); ++index) {
    assert(output[index] == 0xa5);
  }

  std::puts("PASS source statistics accounting, serialization, wrap, and validation");
}
