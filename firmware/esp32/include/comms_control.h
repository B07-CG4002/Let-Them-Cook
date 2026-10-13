#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <new>
#include "comms_packet.h"

namespace comms {

constexpr size_t kControlHeaderSize = 14;
constexpr size_t kControlMaxFrame = 194;
constexpr size_t kControlMaxChunk = 180;
constexpr uint32_t kControlMaxFile = 65536;
constexpr uint32_t kFileIdleTimeoutMs = 30000;
enum ControlStatus : uint8_t {
  ControlOk = 0, ControlInvalid = 1, ControlBusy = 2, ControlOrder = 3,
  ControlIntegrity = 4, ControlConflict = 5, ControlUnsupported = 6
};

inline uint32_t readUint32Le(const uint8_t* bytes) {
  return static_cast<uint32_t>(bytes[0]) | (static_cast<uint32_t>(bytes[1]) << 8) |
      (static_cast<uint32_t>(bytes[2]) << 16) | (static_cast<uint32_t>(bytes[3]) << 24);
}
inline bool controlFitsMtu(uint16_t mtu) { return mtu >= 64; }
inline size_t controlChunkCapacity(uint16_t mtu) {
  if (!controlFitsMtu(mtu)) return 0;
  const size_t capacity = mtu - 3 - kControlHeaderSize;
  return capacity < kControlMaxChunk ? capacity : kControlMaxChunk;
}

// Portable state machine shared by host regression tests and the real GATT
// handler. Callers authenticate and bound their input queue before invoking it.
// No command modifies the independently sequenced sensor stream.
class ControlEngine {
 public:
  typedef bool (*DigestFunction)(const uint8_t*, size_t, uint8_t*);
  ControlEngine(uint8_t device, uint32_t boot, DigestFunction digest,
                uint16_t initialRateHz = 10)
      : device_(device), boot_(boot), digest_(digest), rateHz_(initialRateHz) {}
  ~ControlEngine() { delete[] file_; }
  ControlEngine(const ControlEngine&) = delete;
  ControlEngine& operator=(const ControlEngine&) = delete;

  uint16_t rateHz() const { return rateHz_; }
  void disconnect() {
    // Command identities increase within one BLE connection. The board owns
    // prevention of repeated game effects across network retries/reconnects.
    commandHighWater_ = 0;
    nextCache_ = 0;
    for (auto& cached : commands_) cached = CommandCache{};
    clearFile();
  }
  void expire(uint32_t nowMs) {
    if (file_ != nullptr && !fileComplete_ &&
        static_cast<uint32_t>(nowMs - fileActivityMs_) > kFileIdleTimeoutMs) clearFile();
  }

  size_t process(const uint8_t* input, size_t length, uint32_t nowMs,
                 uint8_t* output, size_t capacity) {
    if (input == nullptr || output == nullptr || length < kControlHeaderSize ||
        capacity < 50 || input[0] != 'B' || input[1] != '7') return 0;
    const uint8_t opcode = input[3];
    const uint32_t id = readUint32Le(input + 6);
    const uint32_t offset = readUint32Le(input + 10);
    output[0] = 'B'; output[1] = '7'; output[2] = 1;
    output[3] = opcode | 0x80; output[4] = device_; output[5] = ControlOk;
    writeUint32Le(output + 6, id);
    writeUint32Le(output + 10, offset);
    if (input[2] != 1) return status(output, ControlUnsupported);
    if (length > kControlMaxFrame || input[4] != device_ || input[5] != 0 || id == 0)
      return status(output, ControlInvalid);
    expire(nowMs);
    const uint8_t* payload = input + kControlHeaderSize;
    const size_t payloadLength = length - kControlHeaderSize;
    switch (opcode) {
      case 1: return command(id, offset, payload, payloadLength, nowMs, output);
      case 2: {
        if (offset != 0 || payloadLength != 2) return status(output, ControlInvalid);
        const uint16_t requested = static_cast<uint16_t>(payload[0]) |
            (static_cast<uint16_t>(payload[1]) << 8);
        if (requested < 1 || requested > 200) return status(output, ControlInvalid);
        rateHz_ = requested;
        output[14] = payload[0]; output[15] = payload[1];
        return 16;
      }
      case 0x10: return beginFile(id, offset, payload, payloadLength, nowMs, output);
      case 0x11: return fileChunk(id, offset, payload, payloadLength, nowMs, output);
      case 0x12: return endFile(id, offset, payloadLength, nowMs, output);
      case 0x13:
        if (offset != 0 || payloadLength != 0) return status(output, ControlInvalid);
        if (fileId_ != 0 && fileId_ != id) return status(output, ControlOrder);
        clearFile();
        return kControlHeaderSize;
      default: return status(output, ControlUnsupported);
    }
  }

 private:
  struct CommandCache {
    uint32_t id = 0;
    uint8_t input[kPacketSize] = {};
    uint8_t output[kPacketSize] = {};
  };
  uint8_t device_;
  uint32_t boot_;
  DigestFunction digest_;
  uint16_t rateHz_;
  uint32_t commandHighWater_ = 0;
  CommandCache commands_[8];
  size_t nextCache_ = 0;
  uint8_t* file_ = nullptr;
  uint32_t fileId_ = 0, fileLength_ = 0, fileReceived_ = 0, fileActivityMs_ = 0;
  uint8_t expectedDigest_[32] = {}, actualDigest_[32] = {};
  bool fileComplete_ = false;

  static size_t status(uint8_t* output, ControlStatus value) {
    output[5] = value;
    return kControlHeaderSize;
  }
  size_t command(uint32_t id, uint32_t offset, const uint8_t* payload,
                 size_t length, uint32_t nowMs, uint8_t* output) {
    if (offset != 0 || length != kPacketSize) return status(output, ControlInvalid);
    const bool validPacketMagic =
        (payload[0] == 'L' && payload[1] == 'C') ||
        (payload[0] == 'W' && payload[1] == '7');
    if (!validPacketMagic ||
        payload[2] != 2 || payload[3] != device_ || readUint32Le(payload + 4) != boot_ ||
        readUint32Le(payload + 8) != id) return status(output, ControlInvalid);
    for (const auto& cached : commands_) {
      if (cached.id != id) continue;
      if (memcmp(cached.input, payload, kPacketSize) != 0) return status(output, ControlConflict);
      memcpy(output + kControlHeaderSize, cached.output, kPacketSize);
      return kControlHeaderSize + kPacketSize;
    }
    if (id <= commandHighWater_) return status(output, ControlConflict);
    auto& cached = commands_[nextCache_];
    cached.id = id;
    memcpy(cached.input, payload, kPacketSize);
    memcpy(cached.output, payload, kPacketSize);
    writeUint32Le(cached.output + 12, nowMs);
    for (size_t channel = 0; channel < 8; ++channel) {
      const size_t position = 16 + channel * 2;
      // Increment the unsigned representation; int16 wrap is explicit and has
      // no signed-overflow or out-of-range signed-conversion dependency.
      const uint16_t previous = static_cast<uint16_t>(payload[position]) |
          (static_cast<uint16_t>(payload[position + 1]) << 8);
      const uint16_t changed = static_cast<uint16_t>(previous + 1u);
      cached.output[position] = static_cast<uint8_t>(changed);
      cached.output[position + 1] = static_cast<uint8_t>(changed >> 8);
    }
    commandHighWater_ = id;
    nextCache_ = (nextCache_ + 1) % 8;
    memcpy(output + kControlHeaderSize, cached.output, kPacketSize);
    return kControlHeaderSize + kPacketSize;
  }

  void clearFile() {
    delete[] file_;
    file_ = nullptr;
    fileId_ = fileLength_ = fileReceived_ = fileActivityMs_ = 0;
    fileComplete_ = false;
    memset(expectedDigest_, 0, sizeof(expectedDigest_));
    memset(actualDigest_, 0, sizeof(actualDigest_));
  }
  size_t beginFile(uint32_t id, uint32_t offset, const uint8_t* payload,
                   size_t length, uint32_t nowMs, uint8_t* output) {
    if (offset != 0 || length != 36) return status(output, ControlInvalid);
    const uint32_t total = readUint32Le(payload);
    if (total == 0 || total > kControlMaxFile) return status(output, ControlInvalid);
    if (fileId_ == id) {
      if (fileLength_ != total || memcmp(expectedDigest_, payload + 4, 32) != 0)
        return status(output, ControlConflict);
      fileActivityMs_ = nowMs;
      writeUint32Le(output + 10, fileReceived_);
      return kControlHeaderSize;
    }
    if (file_ != nullptr && !fileComplete_) return status(output, ControlBusy);
    clearFile();
    file_ = new (std::nothrow) uint8_t[total];
    if (file_ == nullptr) return status(output, ControlBusy);
    fileId_ = id; fileLength_ = total; fileActivityMs_ = nowMs;
    memcpy(expectedDigest_, payload + 4, 32);
    return kControlHeaderSize;
  }
  size_t fileChunk(uint32_t id, uint32_t offset, const uint8_t* payload,
                   size_t length, uint32_t nowMs, uint8_t* output) {
    if (length == 0 || length > kControlMaxChunk) return status(output, ControlInvalid);
    if (id != fileId_ || file_ == nullptr || offset > fileReceived_)
      return status(output, ControlOrder);
    if (offset > fileLength_ || length > fileLength_ - offset) return status(output, ControlInvalid);
    if (offset < fileReceived_) {
      if (length > fileReceived_ - offset) return status(output, ControlOrder);
      if (memcmp(file_ + offset, payload, length) != 0) return status(output, ControlConflict);
    } else {
      if (fileComplete_) return status(output, ControlOrder);
      memcpy(file_ + offset, payload, length);
      fileReceived_ += static_cast<uint32_t>(length);
    }
    fileActivityMs_ = nowMs;
    writeUint32Le(output + 10, fileReceived_);
    return kControlHeaderSize;
  }
  size_t endFile(uint32_t id, uint32_t offset, size_t length,
                 uint32_t nowMs, uint8_t* output) {
    if (length != 0) return status(output, ControlInvalid);
    if (id != fileId_ || file_ == nullptr || offset != fileLength_ || fileReceived_ != fileLength_)
      return status(output, ControlOrder);
    if (!fileComplete_) {
      if (digest_ == nullptr || !digest_(file_, fileLength_, actualDigest_) ||
          memcmp(expectedDigest_, actualDigest_, 32) != 0) return status(output, ControlIntegrity);
      fileComplete_ = true;
    }
    fileActivityMs_ = nowMs;
    writeUint32Le(output + kControlHeaderSize, fileLength_);
    memcpy(output + kControlHeaderSize + 4, actualDigest_, 32);
    return kControlHeaderSize + 36;
  }
};

}  // namespace comms
