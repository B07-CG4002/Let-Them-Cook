#include <cassert>
#include <cstdint>
#include <cstdio>
#include "comms_gatts_control.h"

struct FakeWrite {
  uint16_t conn_id, handle, offset, len;
  bool is_prep;
  uint8_t* value;
};
union FakeParams {
  FakeWrite write;
  struct { uint16_t conn_id; uint8_t flag; } exec_write;
};

int main() {
  using comms::ProbeWriteStatus;
  // The parser must not inspect write fields for another union variant or null.
  FakeParams execute = {};
  execute.exec_write.conn_id = 2;
  execute.exec_write.flag = 1;
  assert(comms::parseMtuControlWrite(false, &execute, 123).status == ProbeWriteStatus::Ignored);
  assert(comms::parseMtuControlWrite(false, static_cast<FakeParams*>(nullptr), 123).status == ProbeWriteStatus::Ignored);
  assert(comms::parseMtuControlWrite(true, static_cast<FakeParams*>(nullptr), 123).status == ProbeWriteStatus::Ignored);

  uint8_t payload[] = {2, 2};  // 514 bytes, little endian.
  FakeParams request = {};
  request.write = {7, 123, 0, 2, false, payload};
  // Even bytes that resemble a valid write must not be parsed for another event.
  assert(comms::parseMtuControlWrite(false, &request, 123).status == ProbeWriteStatus::Ignored);
  auto parsed = comms::parseMtuControlWrite(true, &request, 123);
  assert(parsed.status == ProbeWriteStatus::Valid);
  assert(parsed.connectionId == 7 && parsed.requestedLength == 514 && parsed.inputLength == 2);
  assert(comms::parseMtuControlWrite(true, &request, 124).status == ProbeWriteStatus::Ignored);
  request.write.is_prep = true;
  assert(comms::parseMtuControlWrite(true, &request, 123).status == ProbeWriteStatus::Malformed);
  request.write.is_prep = false; request.write.offset = 1;
  assert(comms::parseMtuControlWrite(true, &request, 123).status == ProbeWriteStatus::Malformed);
  request.write.offset = 0; request.write.len = 1;
  assert(comms::parseMtuControlWrite(true, &request, 123).status == ProbeWriteStatus::Malformed);
  request.write.len = 3;
  assert(comms::parseMtuControlWrite(true, &request, 123).status == ProbeWriteStatus::Malformed);
  request.write.len = 2; request.write.value = nullptr;
  assert(comms::parseMtuControlWrite(true, &request, 123).status == ProbeWriteStatus::Malformed);
  uint8_t control[46] = {};
  request.write = {7, 123, 0, 46, false, control};
  auto application = comms::parseApplicationControlWrite(true, &request, 123);
  assert(application.status == ProbeWriteStatus::Valid && application.connectionId == 7);
  assert(application.inputLength == 46 && application.value == control);
  assert(comms::parseApplicationControlWrite(false, &execute, 123).status == ProbeWriteStatus::Ignored);
  assert(comms::parseApplicationControlWrite(true, &request, 124).status == ProbeWriteStatus::Ignored);
  request.write.is_prep = true;
  assert(comms::parseApplicationControlWrite(true, &request, 123).status == ProbeWriteStatus::Malformed);
  request.write.is_prep = false; request.write.offset = 1;
  assert(comms::parseApplicationControlWrite(true, &request, 123).status == ProbeWriteStatus::Malformed);
  request.write.offset = 0; request.write.len = 13;
  assert(comms::parseApplicationControlWrite(true, &request, 123).status == ProbeWriteStatus::Malformed);
  request.write.len = 195;
  assert(comms::parseApplicationControlWrite(true, &request, 123).status == ProbeWriteStatus::Malformed);
  request.write.len = 46; request.write.value = nullptr;
  assert(comms::parseApplicationControlWrite(true, &request, 123).status == ProbeWriteStatus::Malformed);
  std::puts("PASS GATT event/union and malformed-write regression");
}
