// The built-in dummy glove module: fixture rows as physical floats, and an
// optional IDLE/MOVING cycle (COMMS_DUMMY_IDLE_MS, COMMS_DUMMY_MOVING_MS).
// Linked only when COMMS_GLOVE_MODULE is 0; an external module replaces it.
#include "comms_glove.h"

#if COMMS_GLOVE_MODULE == 0

#include <Arduino.h>
#include <esp_system.h>
#include "comms_dummy_gate.h"

namespace {
uint32_t fixtureRandomWord() {
#ifdef COMMS_FIXTURE_SEED
  // A reproducible build mode, independent of boot identity and sample sequence.
  static uint32_t state = static_cast<uint32_t>(COMMS_FIXTURE_SEED);
  state = state * 1664525u + 1013904223u;
  return state >> 8;
#else
  return esp_random();
#endif
}
}  // namespace

bool gloveBegin() { return true; }

void gloveUpdate() {}

bool gloveRead(float out[]) {  // comms::kChannelCount values: 11, or 12 with COMMS_FLEX3_GLOVE=1
  if (out == nullptr) return false;
  comms::dummyFixtureSample(fixtureRandomWord(), out);
  return true;
}

bool gloveShouldSend() {
  return comms::dummyMoving(millis(), comms::kDummyMovingMs, comms::kDummyIdleMs);
}

#endif  // COMMS_GLOVE_MODULE == 0
