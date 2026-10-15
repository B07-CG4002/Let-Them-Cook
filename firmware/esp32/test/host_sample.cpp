// Host test for comms_sample.h. Build it twice: as is (clamp policy) and with
// -DCOMMS_OUT_OF_RANGE_DROP=1 (drop policy). Expected values are literals.
#include <cassert>
#include <cfloat>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include "comms_sample.h"

static const int16_t kUntouched = 0x5a5a;

static void fillUntouched(int16_t* out) {
  for (size_t channel = 0; channel < 11; ++channel) out[channel] = kUntouched;
}

static bool untouched(const int16_t* out) {
  for (size_t channel = 0; channel < 11; ++channel) {
    if (out[channel] != kUntouched) return false;
  }
  return true;
}

static void expectScaled(const float (&in)[11], const int16_t (&expected)[11],
                         comms::SampleScaleStats& stats) {
  int16_t out[11];
  fillUntouched(out);
  assert(comms::scaleSample(in, out, stats));
  assert(std::memcmp(out, expected, sizeof(out)) == 0);
}

static void expectRefused(const float (&in)[11], comms::SampleScaleStats& stats) {
  int16_t out[11];
  fillUntouched(out);
  const uint32_t clamped = stats.clamped, rejected = stats.rejected;
  assert(!comms::scaleSample(in, out, stats));
  assert(untouched(out));
  assert(stats.rejected == rejected + 1 && stats.clamped == clamped);
}

struct TickRun {
  uint32_t ticks;
  uint32_t passes;
  uint32_t shortestGapUs;
};

// The board's main loop in miniature: each pass reads the clock, checks the
// send tick, then costs costUs +/- jitterUs (the glove module's update,
// delay(1), BLE work). The first pass ticks; the clock may wrap.
static TickRun runTicks(uint32_t startUs, uint32_t intervalUs, uint32_t durationUs,
                        uint32_t costUs, uint32_t jitterUs) {
  TickRun run = {0, 0, UINT32_MAX};
  uint32_t lastTickUs = startUs - intervalUs, nowUs = startUs, elapsedUs = 0;
  uint32_t previousTickUs = 0, random = 12345u;
  while (elapsedUs < durationUs) {
    if (comms::sendTickDue(lastTickUs, nowUs, intervalUs)) {
      const uint32_t gapUs = nowUs - previousTickUs;
      if (run.ticks != 0 && gapUs < run.shortestGapUs) run.shortestGapUs = gapUs;
      previousTickUs = nowUs;
      ++run.ticks;
    }
    // A tick is never owed more than one interval: no backlog, no burst.
    assert(static_cast<uint32_t>(nowUs - lastTickUs) < intervalUs);
    random = random * 1664525u + 1013904223u;
    const uint32_t passUs = costUs - jitterUs + (random >> 8) % (2 * jitterUs + 1);
    nowUs += passUs;
    elapsedUs += passUs;
    ++run.passes;
  }
  return run;
}

static void testSendTick() {
  // 50 Hz for 60 s keeps all 3000 ticks whatever a pass costs below the 20 ms
  // interval, each pass jittered by +/-20 %. Restarting the interval from the
  // pass that noticed the deadline lost about half a pass per tick instead:
  // under 90 % of the rate from about 5 ms per pass.
  const uint32_t costsUs[] = {1000u, 2000u, 3000u, 5000u, 10000u, 15000u};
  for (uint32_t costUs : costsUs) {
    const TickRun run = runTicks(1000u, 20000u, 60000000u, costUs, costUs / 5u);
    assert(run.ticks == 3000u);
    // A late tick only shortens the gap to the next one.
    assert(run.shortestGapUs > 20000u - (costUs + costUs / 5u));
  }
  // The other ends of the 1..200 Hz range.
  assert(runTicks(0u, 5000u, 60000000u, 1500u, 300u).ticks == 12000u);
  assert(runTicks(0u, 1000000u, 60000000u, 15000u, 3000u).ticks == 60u);
  // micros() wraps (every 71.6 minutes) inside the run.
  assert(runTicks(UINT32_MAX - 5000000u, 20000u, 60000000u, 3000u, 600u).ticks == 3000u);

  // Passes longer than the interval: one tick per pass, never two.
  const TickRun slow = runTicks(0u, 20000u, 60000000u, 25000u, 0u);
  assert(slow.passes == 2400u && slow.ticks == 2400u && slow.shortestGapUs == 25000u);

  // One 1 s stall at 10.010 s among 1 ms passes: the 49 ticks it covered are
  // dropped, one tick follows the stall and the next comes a full interval
  // later. 999 ticks in 20 s without the stall, 950 with it.
  uint32_t lastTickUs = 0, nowUs = 0, ticks = 0, previousTickUs = 0, shortestGapUs = UINT32_MAX;
  while (nowUs < 20000000u) {
    if (comms::sendTickDue(lastTickUs, nowUs, 20000u)) {
      if (ticks != 0 && nowUs - previousTickUs < shortestGapUs) shortestGapUs = nowUs - previousTickUs;
      previousTickUs = nowUs;
      ++ticks;
    }
    nowUs += nowUs == 10010000u ? 1000000u : 1000u;
  }
  assert(ticks == 950u && shortestGapUs == 20000u);
}

int main() {
  static_assert(comms::kChannelCount == 11, "11 channels");
  const float scales[11] = {10000, 10000, 10000, 100, 100, 100, 100, 100, 100, 10000, 10000};
  for (size_t channel = 0; channel < 11; ++channel) {
    assert(comms::kChannelScale[channel] == scales[channel]);
  }
  comms::SampleScaleStats stats = {0, 0};

  // Ordinary values in every unit: g, deg/s, degrees, flex fraction.
  const float ordinary[11] = {1.0f, -0.5f, 0.0001f, 12.5f, -250.0f, 0.0f,
                              180.0f, -90.0f, 45.5f, 0.6f, 1.0f};
  const int16_t ordinaryOut[11] = {10000, -5000, 1, 1250, -25000, 0,
                                   18000, -9000, 4550, 6000, 10000};
  expectScaled(ordinary, ordinaryOut, stats);
  const float zero[11] = {0.0f, -0.0f, 0, 0, 0, 0, 0, 0, 0, 0, 0};
  const int16_t zeroOut[11] = {};
  expectScaled(zero, zeroOut, stats);

  // Exact halves round away from zero (not to even, not toward zero).
  // 0.005f is slightly below 0.005, so 0.4999999888 rounds to 0.
  const float halves[11] = {0, 0, 0, 0.125f, -0.125f, 0.625f, -0.625f, 0.375f, 0.005f, 0, 0};
  const int16_t halvesOut[11] = {0, 0, 0, 13, -13, 63, -63, 38, 0, 0, 0};
  expectScaled(halves, halvesOut, stats);

  // Exact limits: 3.2767 g, -3.2768 g, 327.67 deg/s, -327.68 deg/s, flex 3.2767.
  // Values that round back inside int16 are not clamped.
  const float limits[11] = {3.2767f, -3.2768f, 3.27674f, 327.67f, -327.68f, 327.67f,
                            -327.68f, 327.67f, -327.68f, 3.2767f, -3.27684f};
  const int16_t limitsOut[11] = {32767, -32768, 32767, 32767, -32768, 32767,
                                 -32768, 32767, -32768, 32767, -32768};
  expectScaled(limits, limitsOut, stats);
  assert(stats.clamped == 0 && stats.rejected == 0);

  // Just over the limits, plus large finite values: three channels out of range.
  const float over[11] = {3.2768f, 0, 0, 0, -327.69f, 0, 0, 0, 0, 0, 1e30f};
  const float overNegative[11] = {-3.2769f, 0, 0, 327.68f, 0, 0, -FLT_MAX, 0, 0, 0, 0};
  if (comms::kOutOfRangeDrop) {
    expectRefused(over, stats);
    expectRefused(overNegative, stats);
    assert(stats.clamped == 0 && stats.rejected == 2);
  } else {
    const int16_t overOut[11] = {32767, 0, 0, 0, -32768, 0, 0, 0, 0, 0, 32767};
    expectScaled(over, overOut, stats);
    const int16_t overNegativeOut[11] = {-32768, 0, 0, 32767, 0, 0, -32768, 0, 0, 0, 0};
    expectScaled(overNegative, overNegativeOut, stats);
    assert(stats.clamped == 6 && stats.rejected == 0);
  }

  // NaN or infinity in any channel refuses the whole sample under either
  // policy, without counting clamps for channels scaled before it.
  const uint32_t clampedBefore = stats.clamped, rejectedBefore = stats.rejected;
  const float specials[3] = {NAN, INFINITY, -INFINITY};
  const size_t positions[3] = {0, 5, 10};
  for (size_t special = 0; special < 3; ++special) {
    for (size_t position = 0; position < 3; ++position) {
      float in[11] = {1e9f, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0};
      in[positions[position]] = specials[special];
      expectRefused(in, stats);
    }
  }
  assert(stats.clamped == clampedBefore && stats.rejected == rejectedBefore + 9);

  // Null pointers are refused and counted.
  int16_t out[11];
  fillUntouched(out);
  assert(!comms::scaleSample(nullptr, out, stats));
  assert(untouched(out));
  assert(!comms::scaleSample(ordinary, nullptr, stats));
  assert(stats.rejected == rejectedBefore + 11);

  // Counters accumulate and a later ordinary sample still succeeds.
  expectScaled(ordinary, ordinaryOut, stats);
  assert(stats.clamped == clampedBefore && stats.rejected == rejectedBefore + 11);

  // prepareSample: the outcome of one send tick and the totals it adds to.
  comms::SampleCounters counters = {0, 0, 0, 0};
  int16_t prepared[11];

  // Unread: moving is ignored and the values are never looked at.
  fillUntouched(prepared);
  assert(comms::prepareSample(false, true, nullptr, prepared, counters) == comms::SampleUnread);
  assert(comms::prepareSample(false, false, ordinary, prepared, counters) == comms::SampleUnread);
  assert(comms::prepareSample(false, true, over, nullptr, counters) == comms::SampleUnread);
  assert(untouched(prepared));
  assert(counters.unread == 3 && counters.idle == 0 && counters.refused == 0 && counters.clamped == 0);

  // Idle never scales: values that would be refused or clamped are not
  // checked, not counted and not written.
  const float notFinite[11] = {0, 0, 0, 0, 0, NAN, 0, 0, 0, 0, INFINITY};
  assert(comms::prepareSample(true, false, notFinite, prepared, counters) == comms::SampleIdle);
  assert(comms::prepareSample(true, false, over, prepared, counters) == comms::SampleIdle);
  assert(comms::prepareSample(true, false, nullptr, nullptr, counters) == comms::SampleIdle);
  assert(comms::prepareSample(true, false, ordinary, prepared, counters) == comms::SampleIdle);
  assert(untouched(prepared));
  assert(counters.unread == 3 && counters.idle == 4 && counters.refused == 0 && counters.clamped == 0);

  // Moving and valid: scaled exactly like scaleSample.
  assert(comms::prepareSample(true, true, ordinary, prepared, counters) == comms::SampleSend);
  assert(std::memcmp(prepared, ordinaryOut, sizeof(prepared)) == 0);
  assert(counters.unread == 3 && counters.idle == 4 && counters.refused == 0 && counters.clamped == 0);

  // Moving but not finite, or null pointers: refused under both policies,
  // output untouched.
  fillUntouched(prepared);
  assert(comms::prepareSample(true, true, notFinite, prepared, counters) == comms::SampleRefused);
  assert(comms::prepareSample(true, true, nullptr, prepared, counters) == comms::SampleRefused);
  assert(comms::prepareSample(true, true, ordinary, nullptr, counters) == comms::SampleRefused);
  assert(untouched(prepared));
  assert(counters.unread == 3 && counters.idle == 4 && counters.refused == 3 && counters.clamped == 0);

  // Moving and out of range: the drop policy refuses it; the clamp policy
  // sends it and adds its three clamped channels.
  if (comms::kOutOfRangeDrop) {
    assert(comms::prepareSample(true, true, over, prepared, counters) == comms::SampleRefused);
    assert(untouched(prepared));
    assert(counters.refused == 4 && counters.clamped == 0);
  } else {
    assert(comms::prepareSample(true, true, over, prepared, counters) == comms::SampleSend);
    const int16_t overOut[11] = {32767, 0, 0, 0, -32768, 0, 0, 0, 0, 0, 32767};
    assert(std::memcmp(prepared, overOut, sizeof(prepared)) == 0);
    assert(counters.refused == 3 && counters.clamped == 3);
  }
  assert(counters.unread == 3 && counters.idle == 4);

  // Only SampleSend takes a sequence number, so a run of mixed ticks sends a
  // contiguous seq and the totals account for every tick.
  struct Tick { bool readOk; bool moving; const float* values; };
  const Tick ticks[] = {
      {true, true, ordinary}, {false, true, ordinary}, {true, false, ordinary},
      {true, true, notFinite}, {true, true, zero}, {true, true, over},
      {true, false, over}, {true, true, limits}, {false, false, nullptr},
  };
  comms::SampleCounters run = {0, 0, 0, 0};
  uint32_t nextSeq = 0, ticksSeen = 0;
  for (const Tick& tick : ticks) {
    fillUntouched(prepared);
    const comms::SampleOutcome outcome =
        comms::prepareSample(tick.readOk, tick.moving, tick.values, prepared, run);
    if (outcome == comms::SampleSend) {
      assert(!untouched(prepared));
      ++nextSeq;
    } else {
      assert(untouched(prepared));
    }
    ++ticksSeen;
  }
  const uint32_t sends = comms::kOutOfRangeDrop ? 3 : 4;
  assert(nextSeq == sends);
  assert(run.unread == 2 && run.idle == 2 && run.refused == (comms::kOutOfRangeDrop ? 2u : 1u));
  assert(nextSeq + run.unread + run.idle + run.refused == ticksSeen);
  assert(run.clamped == (comms::kOutOfRangeDrop ? 0u : 3u));

  testSendTick();
  std::printf("PASS sample scaling, rounding, limits, refusals and tick outcomes (%s policy)\n",
              comms::kOutOfRangeDrop ? "drop" : "clamp");
}
