#pragma once

#include <stdint.h>

namespace comms {

// ESP-IDF SC (0x08), MITM (0x04), bond (0x01); main.cpp checks SDK equivalence.
constexpr uint8_t kRequiredAuthMode = 0x0d;

inline bool isAuthenticated(bool success, uint8_t authMode) {
  return success && (authMode & kRequiredAuthMode) == kRequiredAuthMode;
}

inline bool canNotify(bool connected, bool subscribed, bool authenticated,
                      bool diagnostic) {
  return connected && subscribed && (authenticated || diagnostic);
}

inline bool sensorFitsMtu(uint16_t mtu) { return mtu >= 35; }

inline bool canAcceptControl(bool connected, bool subscribed, bool authenticated,
                             bool diagnostic, uint16_t writeConnection,
                             uint16_t currentConnection) {
  return writeConnection == currentConnection &&
      canNotify(connected, subscribed, authenticated, diagnostic);
}

}  // namespace comms
