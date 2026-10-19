// Host test for the glove module switches (comms_glove.h) and the portable
// parts of the built-in dummy module (comms_dummy_gate.h). pytest builds it
// with the board's build switches and checks the summary line it prints.
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include "comms_dummy_gate.h"
#include "comms_glove.h"
#include "comms_sample.h"
#include "comms_source_stats.h"

static void testDummyCycle() {
  const uint32_t clocks[] = {0u, 1u, 2999u, 3000u, 9999u, 10000u, 0x7fffffffu, UINT32_MAX};
  // Without an idle time the dummy always moves, whatever the moving time.
  for (uint32_t now : clocks) {
    assert(comms::dummyMoving(now, 3000u, 0u));
    assert(comms::dummyMoving(now, 0u, 0u));
    assert(comms::dummyMoving(now, UINT32_MAX, 0u));
  }
  // A moving time of 0 with an idle time stays IDLE.
  for (uint32_t now : clocks) assert(!comms::dummyMoving(now, 0u, 7000u));

  // 3 s moving, then 7 s idle: the exact phase boundaries over several cycles.
  for (uint32_t cycle = 0; cycle < 5; ++cycle) {
    const uint32_t start = cycle * 10000u;
    assert(comms::dummyMoving(start, 3000u, 7000u));
    assert(comms::dummyMoving(start + 2999u, 3000u, 7000u));
    assert(!comms::dummyMoving(start + 3000u, 3000u, 7000u));
    assert(!comms::dummyMoving(start + 9999u, 3000u, 7000u));
  }
  uint32_t movingMs = 0;
  for (uint32_t now = 0; now < 50000u; ++now) movingMs += comms::dummyMoving(now, 3000u, 7000u);
  assert(movingMs == 15000u);
  assert(comms::dummyMoving(0u, 1u, 1u) && !comms::dummyMoving(1u, 1u, 1u) &&
         comms::dummyMoving(2u, 1u, 1u));

  // millis() wraps at 2^32 ms. 4294960000 is a cycle start (a multiple of
  // 10000); the last 7295 ms before the wrap fall in the idle phase, and the
  // cycle starts again with MOVING at 0.
  assert(comms::dummyMoving(4294960000u, 3000u, 7000u));
  assert(comms::dummyMoving(4294962999u, 3000u, 7000u));
  assert(!comms::dummyMoving(4294963000u, 3000u, 7000u));
  assert(!comms::dummyMoving(UINT32_MAX, 3000u, 7000u));
  assert(comms::dummyMoving(0u, 3000u, 7000u));
  // A cycle that divides 2^32 continues across the wrap without a jump.
  assert(!comms::dummyMoving(UINT32_MAX, 1u << 20, 1u << 20));
  assert(comms::dummyMoving(UINT32_MAX - (1u << 20), 1u << 20, 1u << 20));
  // Times whose sum exceeds 32 bits do not overflow the cycle length.
  assert(comms::dummyMoving(UINT32_MAX - 1u, UINT32_MAX, UINT32_MAX));
  assert(!comms::dummyMoving(UINT32_MAX, UINT32_MAX, UINT32_MAX));
  assert(comms::dummyMoving(UINT32_MAX - 1u, UINT32_MAX, 1u));
  assert(!comms::dummyMoving(UINT32_MAX, UINT32_MAX, 1u));
  assert(comms::dummyMoving(0u, UINT32_MAX, UINT32_MAX));
}

static void testFixtureRoundTrip() {
  // Every fixture row survives int16 -> physical float -> scaleSample exactly,
  // so the dummy packets keep the bytes they had before the sensor path.
  for (uint32_t word = 0; word < 3 * comms::kDummyFixtureCount; ++word) {
    const int16_t (&row)[11] = comms::kDummyFixtures[word % comms::kDummyFixtureCount];
    float physical[11];
    comms::dummyFixtureSample(word, physical);
    for (size_t channel = 0; channel < 11; ++channel) {
      assert(physical[channel] == row[channel] / comms::kChannelScale[channel]);
    }
    int16_t back[11];
    comms::SampleCounters counters = {0, 0, 0, 0};
    assert(comms::prepareSample(true, true, physical, back, counters) == comms::SampleSend);
    assert(counters.unread == 0 && counters.idle == 0 && counters.refused == 0 &&
           counters.clamped == 0);
    assert(std::memcmp(back, row, sizeof(back)) == 0);

    uint8_t viaFloats[38], direct[38];
    assert(comms::serializePacket(viaFloats, sizeof(viaFloats), 2, 7, word, 100, back));
    assert(comms::serializeFixturePacket(direct, sizeof(direct), 2, 7, word, 100, word));
    assert(std::memcmp(viaFloats, direct, sizeof(direct)) == 0);
  }

  // Every int16 value of every channel survives the same round trip, with a
  // margin far from the 0.5 rounding limit, so a slightly different float
  // division on the board cannot change a value.
  double worst = 0.0;
  for (size_t channel = 0; channel < 11; ++channel) {
    for (int32_t value = -32768; value <= 32767; ++value) {
      float in[11] = {};
      in[channel] = static_cast<int16_t>(value) / comms::kChannelScale[channel];
      const double error = std::fabs(static_cast<double>(in[channel]) * comms::kChannelScale[channel] - value);
      if (error > worst) worst = error;
      int16_t out[11];
      comms::SampleScaleStats stats = {0, 0};
      assert(comms::scaleSample(in, out, stats));
      assert(out[channel] == value && stats.clamped == 0 && stats.rejected == 0);
    }
  }
  assert(worst < 0.01);
}

static void testDummyTicks() {
  // The board's tick for the dummy at 50 Hz over 20 s with a 3 s / 7 s cycle:
  // 150 samples sent per cycle with a contiguous seq, the rest counted idle.
  comms::SourceStats stats = {7u, 0u, 0u, 0u};
  comms::SampleCounters counters = {0, 0, 0, 0};
  for (uint32_t nowMs = 0; nowMs < 20000u; nowMs += 20u) {
    float values[11];
    comms::dummyFixtureSample(nowMs / 20u, values);
    const bool moving = comms::dummyMoving(nowMs, 3000u, 7000u);
    int16_t scaled[11] = {};
    if (comms::prepareSample(true, moving, values, scaled, counters) == comms::SampleSend) {
      const uint32_t seq = comms::allocateSampleSequence(stats);
      assert(seq == stats.submitted);
      uint8_t packet[38];
      assert(comms::serializePacket(packet, sizeof(packet), 1, stats.bootId, seq, nowMs, scaled));
      comms::recordSensorSubmission(stats, true);
    }
  }
  assert(stats.nextSequence == 300u && stats.submitted == 300u && stats.failures == 0u);
  assert(counters.idle == 700u && counters.unread == 0u && counters.refused == 0u &&
         counters.clamped == 0u);

  uint8_t output[comms::kSourceStatsSize] = {};
  assert(comms::serializeSourceStats(output, sizeof(output), 1, comms::kGloveGated, stats, counters));
  assert(output[5] == (comms::kGloveGated ? 0x03 : 0x02));
}

static void testBeginRetries() {
  // begin() is called until it succeeds or the attempts run out; pause() runs
  // only between two calls and gets the number of the try that failed.
  for (unsigned maxAttempts = 1; maxAttempts <= 10; ++maxAttempts) {
    for (unsigned failures = 0; failures <= maxAttempts + 1; ++failures) {
      unsigned calls = 0, pauses = 0, lastPaused = 0, attempts = 99;
      const bool ok = comms::beginWithRetries(
          [&] { return ++calls > failures; },
          [&](unsigned failed) { ++pauses; assert(failed == calls); lastPaused = failed; },
          maxAttempts, attempts);
      const bool expected = failures < maxAttempts;
      assert(ok == expected);
      assert(attempts == calls);
      assert(attempts == (expected ? failures + 1 : maxAttempts));
      assert(pauses == attempts - 1);
      assert(lastPaused == (attempts > 1 ? attempts - 1 : 0));
    }
  }
}

int main() {
  testDummyCycle();
  testFixtureRoundTrip();
  testDummyTicks();
  testBeginRetries();
  assert(comms::kGloveBeginAttempts == COMMS_GLOVE_BEGIN_ATTEMPTS);
  assert(comms::kGloveExternal == (COMMS_GLOVE_MODULE == 1));
  assert(comms::kGloveGated == (comms::kGloveExternal || comms::kDummyIdleMs > 0));
  std::printf("PASS glove module=%s gated=%u moving_ms=%lu idle_ms=%lu begin_attempts=%u retry_ms=%lu\n",
              comms::kGloveModuleName, comms::kGloveGated ? 1u : 0u,
              static_cast<unsigned long>(comms::kDummyMovingMs),
              static_cast<unsigned long>(comms::kDummyIdleMs), comms::kGloveBeginAttempts,
              static_cast<unsigned long>(comms::kGloveBeginRetryMs));
}
