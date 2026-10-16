#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>

#include "comms_source_stats.h"

int main() {
  static_assert(comms::kSourceStatsSize == 36, "<4sBB2xIIIIIII> is 36 bytes");
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

  // idle, unread and refused come from the sample counters; clamped does not
  // travel. Distinct byte patterns catch a swapped field.
  const comms::SampleCounters samples = {0x0d0c0b0au, 0x04030201u, 0x11223344u, 0xffffffffu};
  uint8_t output[comms::kSourceStatsSize] = {};
  assert(comms::serializeSourceStats(output, sizeof(output), 2u, false, stats, samples));
  const uint8_t expected[comms::kSourceStatsSize] = {
      0x4c, 0x43, 0x53, 0x33, 0x02, 0x00, 0x00, 0x00,
      0x12, 0x34, 0x56, 0x78, 0x02, 0x00, 0x00, 0x00,
      0x01, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00,
      0x01, 0x02, 0x03, 0x04, 0x0a, 0x0b, 0x0c, 0x0d,
      0x44, 0x33, 0x22, 0x11,
  };
  assert(std::memcmp(output, expected, sizeof(expected)) == 0);

  // A gated source sets flags bit 0 only; the reserved bytes stay zero even
  // when the buffer held other bytes before.
  std::memset(output, 0xa5, sizeof(output));
  assert(comms::serializeSourceStats(output, sizeof(output), 1u, true, stats, samples));
  assert(output[4] == 0x01 && output[5] == comms::kSourceFlagGated && output[5] == 0x01);
  assert(output[6] == 0x00 && output[7] == 0x00);
  assert(std::memcmp(output + 8, expected + 8, sizeof(expected) - 8) == 0);
  // pytest unpacks this line with the laptop's struct format.
  for (size_t index = 0; index < sizeof(output); ++index) std::printf("%02x", output[index]);
  std::printf("\n");

  const comms::SampleCounters zero = {0u, 0u, 0u, 0u};
  assert(comms::serializeSourceStats(output, sizeof(output), 1u, false, stats, zero));
  assert(output[5] == 0x00 && output[6] == 0x00 && output[7] == 0x00);
  for (size_t index = 24; index < sizeof(output); ++index) assert(output[index] == 0x00);

  comms::SourceStats wrapped = {1u, UINT32_MAX, UINT32_MAX, UINT32_MAX};
  assert(comms::allocateSampleSequence(wrapped) == UINT32_MAX);
  comms::recordSensorSubmission(wrapped, true);
  comms::recordSensorSubmission(wrapped, false);
  assert(wrapped.nextSequence == 0u);
  assert(wrapped.submitted == 0u);
  assert(wrapped.failures == 0u);

  std::memset(output, 0xa5, sizeof(output));
  assert(!comms::serializeSourceStats(nullptr, sizeof(output), 1u, false, stats, samples));
  assert(!comms::serializeSourceStats(output, sizeof(output) - 1u, 1u, false, stats, samples));
  assert(!comms::serializeSourceStats(output, sizeof(output), 0u, true, stats, samples));
  assert(!comms::serializeSourceStats(output, sizeof(output), 3u, true, stats, samples));
  for (size_t index = 0; index < sizeof(output); ++index) {
    assert(output[index] == 0xa5);
  }

  std::puts("PASS source statistics accounting, serialization, wrap, and validation");
}
