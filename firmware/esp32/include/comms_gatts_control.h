#pragma once

#include <stdint.h>

namespace comms {

enum class ProbeWriteStatus { Ignored, Malformed, Valid };
struct ProbeWrite {
  ProbeWriteStatus status;
  uint16_t connectionId;
  uint16_t requestedLength;
  uint16_t inputLength;
};

struct ApplicationControlWrite {
  ProbeWriteStatus status;
  uint16_t connectionId;
  uint16_t inputLength;
  const uint8_t* value;
};

template <typename EventParameters>
ApplicationControlWrite parseApplicationControlWrite(bool isWriteEvent,
    const EventParameters* param, uint16_t controlHandle) {
  if (!isWriteEvent || param == nullptr) return {ProbeWriteStatus::Ignored, 0, 0, nullptr};
  const auto& write = param->write;
  if (write.handle != controlHandle) return {ProbeWriteStatus::Ignored, 0, 0, nullptr};
  if (write.len < 14 || write.len > 194 || write.offset != 0 || write.is_prep || write.value == nullptr)
    return {ProbeWriteStatus::Malformed, write.conn_id, write.len, nullptr};
  return {ProbeWriteStatus::Valid, write.conn_id, write.len, write.value};
}

// ESP GATTS callbacks use a union: only an actual WRITE event may inspect write.
// Template parameters permit the same guard/parser to run against a host union.
template <typename EventParameters>
ProbeWrite parseMtuControlWrite(bool isWriteEvent, const EventParameters* param,
                               uint16_t controlHandle) {
  if (!isWriteEvent || param == nullptr) {
    return {ProbeWriteStatus::Ignored, 0, 0, 0};
  }
  const auto& write = param->write;
  if (write.handle != controlHandle) {
    return {ProbeWriteStatus::Ignored, 0, 0, 0};
  }
  if (write.len != 2 || write.offset != 0 || write.is_prep || write.value == nullptr) {
    return {ProbeWriteStatus::Malformed, write.conn_id, 0, write.len};
  }
  const uint16_t requested = static_cast<uint16_t>(write.value[0]) |
      (static_cast<uint16_t>(write.value[1]) << 8);
  return {ProbeWriteStatus::Valid, write.conn_id, requested, write.len};
}

}  // namespace comms
