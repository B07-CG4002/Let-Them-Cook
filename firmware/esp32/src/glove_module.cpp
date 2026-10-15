// The send decision for the glove owner's module (Siqiang). The module itself,
// lib/glove_sensor, is a byte-identical copy of hardware/gloves/integration_files
// made by python -m tools.sync_glove_module; its gloveBegin, gloveUpdate and
// gloveRead have the signatures comms_glove.h declares, so only the gate maps.
#include "comms_glove.h"

#if COMMS_GLOVE_MODULE == 1
#include "glove_sensor.h"

bool gloveShouldSend() {
#if COMMS_GLOVE_SEND_ALL
  return true;  // recording sessions: every sample, IDLE included
#else
  // False until the classifier has filled its first 1 s window, then the
  // smoothed MOVING state (MOVING after 2 windows, IDLE after 3, every 200 ms).
  return gloveIsMoving();
#endif
}
#endif
