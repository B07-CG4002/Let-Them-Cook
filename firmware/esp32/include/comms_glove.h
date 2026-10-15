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

namespace comms {
constexpr bool fitsUint32(long long value) { return value >= 0 && value <= 4294967295LL; }
}  // namespace comms

static_assert(COMMS_GLOVE_MODULE == 0 || COMMS_GLOVE_MODULE == 1,
              "COMMS_GLOVE_MODULE must be 0 (built-in dummy) or 1 (external module)");
static_assert(comms::fitsUint32(COMMS_DUMMY_MOVING_MS) && comms::fitsUint32(COMMS_DUMMY_IDLE_MS),
              "dummy gate times are uint32 milliseconds");

// Once in setup(), before BLE starts. May block (about 5 s of calibration,
// with the glove still). false = no sensor found; the board still advertises.
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
bool gloveRead(float out[11]);
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

}  // namespace comms
