#pragma once
#include <stdint.h>
#include <string.h>

namespace comms {
// Access only while holding connectionStateMutex. A successful security event
// can precede the application connection callback on a bonded reconnect.
class EarlyAuth {
 public:
  void clear() { valid_ = false; }
  void remember(const uint8_t* peer, uint32_t now, bool approved) {
    valid_ = approved;
    if (approved) { memcpy(peer_, peer, sizeof(peer_)); at_ = now; }
  }
  bool consume(const uint8_t* peer, uint32_t now) {
    const bool accepted = valid_ && static_cast<uint32_t>(now - at_) <= 2000 &&
        memcmp(peer_, peer, sizeof(peer_)) == 0;
    clear();  // One connection attempt only, including mismatches/expiry.
    return accepted;
  }
 private:
  bool valid_ = false;
  uint8_t peer_[6] = {};
  uint32_t at_ = 0;
};
}  // namespace comms
