#pragma once

#include <stddef.h>
#include <stdint.h>

#include "comms_packet.h"
#include "comms_source_stats.h"

namespace comms {

// One sensor notification carries 1 to kMaxPacketsPerNotification whole
// packets back to back, oldest first. Six packets (228 bytes in v3, 240 in v4) plus
// the ATT (3) and L2CAP (4) headers fit one 251-byte link-layer packet when the
// link uses the LE data length extension.
constexpr size_t kMaxPacketsPerNotification = 6;
constexpr size_t kMaxSensorNotificationSize = kMaxPacketsPerNotification * kPacketSize;
static_assert(kMaxSensorNotificationSize + 3 + 4 <= 251,
              "a full sensor notification must fit one 251-byte link-layer packet");

// Samples waiting on the board for the link: 0.64 s at 50 Hz.
constexpr size_t kSendQueueCapacity = 32;

// Sensor notifications allowed below the application (Bluedroid's task
// queues, its L2CAP queue for the ATT channel and the controller) before the
// next one is built. Two keep one ready for the next connection event while
// the other is on the air; the rest of the backlog waits here, where it is
// coalesced, instead of in Bluedroid's 20-deep L2CAP queue, which delays every
// sample and, once full, refuses notifications and drops ATT replies.
constexpr uint16_t kMaxNotificationsInFlight = 2;

// How many packets one notification may carry at this ATT MTU.
inline size_t packetsPerNotification(uint16_t mtu) {
  if (static_cast<size_t>(mtu) < kPacketSize + 3) return 0;
  const size_t fit = (static_cast<size_t>(mtu) - 3) / kPacketSize;
  return fit < kMaxPacketsPerNotification ? fit : kMaxPacketsPerNotification;
}

// Notifications still below the application, from two Bluedroid counts read
// against their values with nothing pending: the ATT channel's sendable count
// (esp_ble_get_cur_sendable_packets_num, which falls as notifications wait in
// the host) and the controller's free LE buffers
// (esp_ble_get_sendable_packets_num, which fall while packets wait in the
// controller for the air and the peer's acknowledgement).
inline uint16_t notificationsInFlight(uint16_t idleSendable, uint16_t sendable,
                                      uint16_t idleWindow, uint16_t window) {
  const uint16_t host = sendable < idleSendable ? idleSendable - sendable : 0;
  const uint16_t controller = window < idleWindow ? idleWindow - window : 0;
  return static_cast<uint16_t>(host + controller);
}

struct PendingSample {
  uint32_t uptimeMs;
  int16_t values[kChannelCount];
};

// Samples produced but not yet sent, oldest first. A queued sample has no seq:
// it takes one when it leaves the queue, either in a notification the stack
// accepted (submitted) or as the oldest sample dropped from a full queue (a
// failure, which the laptop sees as a seq gap). Samples still queued when the
// laptop turns notifications off are discarded without a seq, like the ticks
// that follow; those queued when the link drops are counted as failures by the
// caller (main.cpp). Either way next_seq = submitted + failures always holds.
class SampleQueue {
 public:
  size_t size() const { return count_; }
  bool full() const { return count_ == kSendQueueCapacity; }
  const PendingSample& at(size_t index) const {
    return items_[(head_ + index) % kSendQueueCapacity];
  }
  bool push(const PendingSample& sample) {
    if (full()) return false;
    items_[(head_ + count_) % kSendQueueCapacity] = sample;
    ++count_;
    return true;
  }
  void dropFront(size_t count) {
    if (count > count_) count = count_;
    head_ = (head_ + count) % kSendQueueCapacity;
    count_ -= count;
  }
  // Empties the queue and returns how many samples it held.
  size_t clear() {
    const size_t discarded = count_;
    head_ = count_ = 0;
    return discarded;
  }

 private:
  PendingSample items_[kSendQueueCapacity];
  size_t head_ = 0;
  size_t count_ = 0;
};

// Queues one sample to send. When the queue is full the oldest sample leaves
// first, takes the next seq and counts as a failure. Returns true when a
// sample was dropped that way.
inline bool enqueueSample(SampleQueue& queue, SourceStats& stats, const PendingSample& sample) {
  bool dropped = false;
  if (queue.full()) {
    allocateSampleSequence(stats);
    recordSensorSubmission(stats, false);
    queue.dropFront(1);
    dropped = true;
  }
  queue.push(sample);
  return dropped;
}

// Serializes the oldest min(queued, limit, kMaxPacketsPerNotification) samples
// into output, numbered from stats.nextSequence. Changes nothing, so a
// notification the stack refuses is simply built again later. Returns the
// packet count; 0 when nothing is queued or the output is too small.
inline size_t buildSensorNotification(uint8_t* output, size_t capacity, const SampleQueue& queue,
                                      size_t limit, uint8_t deviceId, const SourceStats& stats) {
  size_t count = queue.size() < limit ? queue.size() : limit;
  if (count > kMaxPacketsPerNotification) count = kMaxPacketsPerNotification;
  if (output == nullptr || capacity < count * kPacketSize) return 0;
  for (size_t index = 0; index < count; ++index) {
    const PendingSample& sample = queue.at(index);
    if (!serializePacket(output + index * kPacketSize, kPacketSize, deviceId, stats.bootId,
                         static_cast<uint32_t>(stats.nextSequence + index), sample.uptimeMs,
                         sample.values)) {
      return 0;
    }
  }
  return count;
}

// The stack accepted a notification of count packets built by
// buildSensorNotification: their seqs are final and they leave the queue.
inline void commitSensorNotification(SampleQueue& queue, SourceStats& stats, size_t count) {
  if (count > queue.size()) count = queue.size();
  for (size_t index = 0; index < count; ++index) {
    allocateSampleSequence(stats);
    recordSensorSubmission(stats, true);
  }
  queue.dropFront(count);
}

}  // namespace comms
