#pragma once

#include <stdint.h>

// The glove module the Communications firmware calls. The names are the
// proposal to the glove owner (Siqiang); the module owns calibration, filters
// and movement detection, Communications owns scaling, packing and BLE.
//
// COMMS_GLOVE_MODULE selects the implementation linked into the board build:
//   0 (default)  the built-in dummy module, src/dummy_glove.cpp (fixture rows);
//   1            an external module that defines the four functions below.
#ifndef COMMS_GLOVE_MODULE
#define COMMS_GLOVE_MODULE 0
#endif
// Dummy module only: a repeating cycle from boot, MOVING for
// COMMS_DUMMY_MOVING_MS, then IDLE for COMMS_DUMMY_IDLE_MS. An idle time of 0
// (default) never gates; a moving time of 0 with an idle time keeps it IDLE.
#ifndef COMMS_DUMMY_MOVING_MS
#define COMMS_DUMMY_MOVING_MS 3000
#endif
#ifndef COMMS_DUMMY_IDLE_MS
#define COMMS_DUMMY_IDLE_MS 0
#endif
// External module only: 1 sends every sample, IDLE included (for recording
// sessions); 0 (default) sends only while the module reports MOVING.
#ifndef COMMS_GLOVE_SEND_ALL
#define COMMS_GLOVE_SEND_ALL 0
#endif
// Boot only: setup() calls gloveBegin() up to COMMS_GLOVE_BEGIN_ATTEMPTS times
// until it returns true, pausing COMMS_GLOVE_BEGIN_RETRY_MS between tries, so
// one failed sensor read during start-up does not leave the glove without data
// until the next power-up. Each try repeats the module's calibration.
#ifndef COMMS_GLOVE_BEGIN_ATTEMPTS
#define COMMS_GLOVE_BEGIN_ATTEMPTS 3
#endif
#ifndef COMMS_GLOVE_BEGIN_RETRY_MS
#define COMMS_GLOVE_BEGIN_RETRY_MS 500
#endif

namespace comms {
constexpr bool fitsUint32(long long value) { return value >= 0 && value <= 4294967295LL; }
}  // namespace comms

static_assert(COMMS_GLOVE_MODULE == 0 || COMMS_GLOVE_MODULE == 1,
              "COMMS_GLOVE_MODULE must be 0 (built-in dummy) or 1 (external module)");
static_assert(comms::fitsUint32(COMMS_DUMMY_MOVING_MS) && comms::fitsUint32(COMMS_DUMMY_IDLE_MS),
              "dummy gate times are uint32 milliseconds");
static_assert(COMMS_GLOVE_BEGIN_ATTEMPTS >= 1 && COMMS_GLOVE_BEGIN_ATTEMPTS <= 10,
              "COMMS_GLOVE_BEGIN_ATTEMPTS must be 1 to 10");
static_assert(COMMS_GLOVE_BEGIN_RETRY_MS >= 0 && COMMS_GLOVE_BEGIN_RETRY_MS <= 10000,
              "COMMS_GLOVE_BEGIN_RETRY_MS must be 0 to 10000 milliseconds");

// In setup(), before BLE starts, up to COMMS_GLOVE_BEGIN_ATTEMPTS times. May
// block (about 5 s of calibration, with the glove still). false = no sensor
// found or a failed read; after the last try the board still advertises.
// Must be safe to call again after it returned false.
bool gloveBegin();
// Every main-loop pass (about every 1 to 2 ms plus this call), outside any
// lock, whether or not the laptop is connected. A slow call delays the next
// send tick but not the rate, while one pass (this call plus about 1 ms) stays
// shorter than the send interval (20 ms at 50 Hz); a longer pass skips ticks.
// Must not wait.
void gloveUpdate();
// At a send tick, only while the laptop is connected and subscribed, so calls
// can be seconds apart. true = out holds one current sample: ax ay az (g),
// gx gy gz (deg/s), yaw pitch roll (deg), flex1 flex2 (0..1). false = no valid
// sample now. Must not wait.
bool gloveRead(float out[]); // 11 channels normally; 12 with COMMS_FLEX3_GLOVE=1.
// Right after a successful gloveRead: true = MOVING (send this sample),
// false = IDLE (skip it). Must not wait.
bool gloveShouldSend();

namespace comms {

constexpr bool kGloveExternal = COMMS_GLOVE_MODULE == 1;
constexpr const char* kGloveModuleName = kGloveExternal ? "external" : "dummy";
// The source may skip ticks while IDLE; the laptop reads this from the source
// statistics flags and then expects silence while the player is still.
constexpr bool kGloveGated = (COMMS_GLOVE_MODULE == 1 && COMMS_GLOVE_SEND_ALL == 0) ||
                              (COMMS_GLOVE_MODULE == 0 && COMMS_DUMMY_IDLE_MS > 0);
constexpr uint32_t kDummyMovingMs = static_cast<uint32_t>(COMMS_DUMMY_MOVING_MS);
constexpr uint32_t kDummyIdleMs = static_cast<uint32_t>(COMMS_DUMMY_IDLE_MS);
constexpr unsigned kGloveBeginAttempts = COMMS_GLOVE_BEGIN_ATTEMPTS;
constexpr uint32_t kGloveBeginRetryMs = static_cast<uint32_t>(COMMS_GLOVE_BEGIN_RETRY_MS);

// Calls begin() until it returns true or maxAttempts calls have failed, and
// calls pause(failedAttempt) between two calls. attempts receives the number
// of begin() calls made.
template <typename Begin, typename Pause>
bool beginWithRetries(Begin begin, Pause pause, unsigned maxAttempts, unsigned& attempts) {
  attempts = 0;
  while (attempts < maxAttempts) {
    ++attempts;
    if (begin()) return true;
    if (attempts < maxAttempts) pause(attempts);
  }
  return false;
}

}  // namespace comms
