#include <Arduino.h>
#include <BLE2902.h>
#include <BLEDevice.h>
#include <BLESecurity.h>
#include <BLEServer.h>
#include <esp_gatt_common_api.h>
#include <esp_idf_version.h>
#include <esp_system.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <mbedtls/sha256.h>
#include <vector>
#include "comms_packet.h"
// The glove module (built-in dummy or external, COMMS_GLOVE_MODULE) supplies
// physical samples and the IDLE/MOVING decision; prepareSample scales them and
// a sequence number is allocated only for a sample that is sent.
#include "comms_glove.h"
#include "comms_sample.h"
#include "comms_send_queue.h"
#include "comms_source_stats.h"
#include "comms_security.h"
#include "comms_gatts_control.h"
#include "comms_control.h"

#ifndef COMMS_UNPROTECTED_DIAGNOSTIC
#define COMMS_UNPROTECTED_DIAGNOSTIC 0
#endif
#ifndef COMMS_DEVICE_ID
#define COMMS_DEVICE_ID 1
#endif
#ifndef COMMS_INITIAL_RATE_HZ
#define COMMS_INITIAL_RATE_HZ 10
#endif

namespace {
constexpr bool kDiagnostic = COMMS_UNPROTECTED_DIAGNOSTIC != 0;
constexpr uint8_t kDeviceId = COMMS_DEVICE_ID;
static_assert(kDeviceId == 1 || kDeviceId == 2, "device ID must be 1 or 2");
static_assert(comms::kRequiredAuthMode == ESP_LE_AUTH_REQ_SC_MITM_BOND,
              "Update portable security policy for this SDK");
// The sensor send gate reads Bluedroid's ESP-IDF 4.4 sendable counts (and
// relies on its 20/10 ATT congestion thresholds); check them on another SDK.
static_assert(ESP_IDF_VERSION_MAJOR == 4 && ESP_IDF_VERSION_MINOR == 4,
              "Re-check the sensor send gate (comms_send_queue.h) for this ESP-IDF");
constexpr uint32_t kReportIntervalMs = 1000;
constexpr uint16_t kInitialRateHz = COMMS_INITIAL_RATE_HZ;
static_assert(kInitialRateHz >= 1 && kInitialRateHz <= 200, "rate must be 1..200 Hz");
constexpr char kDeviceName[] = "LTC-COMMS";
constexpr char kServiceUuid[] = "6e1c0001-7a45-4dc4-b678-3f2d5a9c1001";
constexpr char kCounterUuid[] = "6e1c0002-7a45-4dc4-b678-3f2d5a9c1001";
constexpr char kMtuControlUuid[] = "6e1c0003-7a45-4dc4-b678-3f2d5a9c1001";
constexpr char kMtuProbeUuid[] = "6e1c0004-7a45-4dc4-b678-3f2d5a9c1001";
constexpr char kSensorUuid[] = "6e1c0005-7a45-4dc4-b678-3f2d5a9c1001";
constexpr char kSourceStatsUuid[] = "6e1c0006-7a45-4dc4-b678-3f2d5a9c1001";
constexpr char kControlUuid[] = "6e1c0007-7a45-4dc4-b678-3f2d5a9c1001";
constexpr char kResponseUuid[] = "6e1c0008-7a45-4dc4-b678-3f2d5a9c1001";
constexpr size_t kMtuProbePayloadCapacity = ESP_GATT_MAX_MTU_SIZE - 3;

uint32_t bootId = 0, aliveSequence = 0, notificationCounter = 0;
comms::SourceStats sensorStats = {};
// Written only by loop(), under the mutex; the source statistics read them.
comms::SampleCounters sampleCounters = {};
uint32_t sampleRaceDropped = 0;
// Samples waiting for the link, and the send gate's view of Bluedroid (all
// under the mutex). The idle values are the largest counts seen: nothing is
// pending then. The *Peak values restart after each comms_stats line.
comms::SampleQueue sampleQueue;
uint16_t idleSendable = 0, idleControllerWindow = 0;
uint32_t sensorNotifications = 0, sensorSendRefused = 0, queueDiscarded = 0, queueOverflow = 0;
uint32_t queuePeak = 0, batchPeak = 0, inFlightPeak = 0;
// The gate re-learns idleSendable after this long shut with the controller idle.
constexpr uint32_t kGateRebaselineMs = 200;
uint32_t gateStuckSinceMs = 0, gateRebaselines = 0;
bool gateStuck = false;
bool dataLengthRequested = false;
// Counted in the BTU task without the mutex (ESP_GATTS_CONGEST_EVT must not block).
volatile uint32_t congestedEvents = 0;
uint32_t sensorMtuSuppressed = 0, securitySuppressed = 0;
uint32_t submissionErrors = 0, lastReportMs = 0, lastNotificationUs = 0;
bool clientConnected = false, authenticated = false;
bool notificationsEnabled = false, mtuProbeNotificationsEnabled = false;
bool sensorNotificationsEnabled = false, restartAdvertising = false, bleReady = false;
bool responseNotificationsEnabled = false;
bool mtuProbeRequestPending = false;
uint16_t pendingMtuProbeLength = 0, connectionId = 0;
uint16_t negotiatedMtu = ESP_GATT_DEF_BLE_MTU_SIZE;
esp_gatt_if_t serverGattIf = ESP_GATT_IF_NONE;
esp_bd_addr_t connectedAddress = {};
bool erasingBonds = false, bondEraseStarted = false;
uint32_t bondErasePending = 0, bondEraseFailures = 0;
SemaphoreHandle_t connectionStateMutex = nullptr;
BLEServer* server = nullptr;
BLEAdvertising* advertising = nullptr;
BLECharacteristic *counterCharacteristic = nullptr, *mtuProbeControlCharacteristic = nullptr;
BLECharacteristic *mtuProbeCharacteristic = nullptr, *sensorCharacteristic = nullptr;
BLECharacteristic* sourceStatsCharacteristic = nullptr;
BLECharacteristic *controlCharacteristic = nullptr, *responseCharacteristic = nullptr;
BLE2902 *counterCccd = nullptr, *mtuProbeCccd = nullptr, *sensorCccd = nullptr;
BLE2902* responseCccd = nullptr;
comms::ControlEngine* controls = nullptr;
constexpr size_t kControlQueueSize = 4;
struct QueuedControl { uint16_t length; uint8_t bytes[comms::kControlMaxFrame]; };
QueuedControl controlQueue[kControlQueueSize];
size_t controlQueueHead = 0, controlQueueCount = 0;
uint32_t controlQueueRejected = 0;

bool sha256File(const uint8_t* data, size_t length, uint8_t* digest) {
  return mbedtls_sha256_ret(data, length, digest, 0) == 0;
}

// Called with the mutex held. IDF send enqueues work without waiting for receipt.
bool canNotify(bool subscribed) {
  return !erasingBonds && comms::canNotify(clientConnected, subscribed, authenticated, kDiagnostic);
}

// Target our one peer. BLECharacteristic::notify() broadcasts and returns void;
// the IDF API gives us an actual submission result for sequence accounting.
bool submitNotification(BLECharacteristic* characteristic, uint8_t* payload, uint16_t length) {
  const esp_err_t result = esp_ble_gatts_send_indicate(serverGattIf, connectionId,
      characteristic->getHandle(), length, payload, false);
  if (result != ESP_OK) ++submissionErrors;
  return result == ESP_OK;
}

// Called with the mutex held. Samples that never left the board take no seq
// (comms_send_queue.h), so the source statistics do not change here. Used when
// the laptop turns notifications off: its two shutdown reads must agree.
void discardQueuedSamples() {
  queueDiscarded += static_cast<uint32_t>(sampleQueue.clear());
}

// Called with the mutex held, when the link drops. These samples were lost
// while the laptop was listening, so each takes a seq and counts as a failure,
// like an overflow: the laptop sees the gap after it reconnects.
void failQueuedSamples() {
  while (sampleQueue.size() != 0) {
    comms::allocateSampleSequence(sensorStats);
    comms::recordSensorSubmission(sensorStats, false);
    sampleQueue.dropFront(1);
    ++queueOverflow;
  }
}

void resetSubscriptions() {
  notificationsEnabled = mtuProbeNotificationsEnabled = sensorNotificationsEnabled = false;
  responseNotificationsEnabled = false;
  discardQueuedSamples();
  idleSendable = 0;
  gateStuck = false;
  controlQueueHead = controlQueueCount = 0;
  if (controls != nullptr) controls->disconnect();
  mtuProbeRequestPending = false;
  pendingMtuProbeLength = 0;
  counterCccd->setNotifications(false);
  mtuProbeCccd->setNotifications(false);
  sensorCccd->setNotifications(false);
  responseCccd->setNotifications(false);
}

class ServerCallbacks final : public BLEServerCallbacks {
  void onConnect(BLEServer* owner, esp_ble_gatts_cb_param_t* param) override {
    xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
    if (clientConnected || erasingBonds) {
      xSemaphoreGive(connectionStateMutex);
      owner->disconnect(param->connect.conn_id);
      return;
    }
    clientConnected = true;
    authenticated = false;
    dataLengthRequested = false;
    connectionId = param->connect.conn_id;
    memcpy(connectedAddress, param->connect.remote_bda, sizeof(connectedAddress));
    resetSubscriptions();
    negotiatedMtu = ESP_GATT_DEF_BLE_MTU_SIZE;
    restartAdvertising = false;
    xSemaphoreGive(connectionStateMutex);
    advertising->stop();
    Serial.printf("ble_connected boot_id=%lu uptime_ms=%lu security=%s\n",
        static_cast<unsigned long>(bootId), static_cast<unsigned long>(millis()),
        kDiagnostic ? "UNPROTECTED_DIAGNOSTIC" : "pending");
    if (!kDiagnostic) {
      const esp_err_t result = esp_ble_set_encryption(param->connect.remote_bda, ESP_BLE_SEC_ENCRYPT_MITM);
      if (result != ESP_OK) {
        Serial.printf("ble_security_request_failed return_code=%d\n", result);
        owner->disconnect(param->connect.conn_id);
      }
    }
  }

  void onDisconnect(BLEServer*, esp_ble_gatts_cb_param_t* param) override {
    xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
    if (!clientConnected || param->disconnect.conn_id != connectionId) {
      xSemaphoreGive(connectionStateMutex);
      return;
    }
    clientConnected = authenticated = false;
    failQueuedSamples();
    resetSubscriptions();
    negotiatedMtu = ESP_GATT_DEF_BLE_MTU_SIZE;
    restartAdvertising = !erasingBonds;
    xSemaphoreGive(connectionStateMutex);
    Serial.printf("ble_disconnected boot_id=%lu counter=%lu uptime_ms=%lu\n",
        static_cast<unsigned long>(bootId), static_cast<unsigned long>(notificationCounter),
        static_cast<unsigned long>(millis()));
  }

  void onMtuChanged(BLEServer*, esp_ble_gatts_cb_param_t* param) override {
    xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
    if (clientConnected && param->mtu.conn_id == connectionId) negotiatedMtu = param->mtu.mtu;
    xSemaphoreGive(connectionStateMutex);
    Serial.printf("ble_mtu_changed conn_id=%u negotiated_mtu=%u uptime_ms=%lu\n",
        param->mtu.conn_id, param->mtu.mtu, static_cast<unsigned long>(millis()));
  }
};

class SecurityCallbacks final : public BLESecurityCallbacks {
  uint32_t onPassKeyRequest() override {
    // DisplayOnly has no keypad. Invalid input fails an unexpected association.
    return UINT32_MAX;
  }
  void onPassKeyNotify(uint32_t passkey) override {
    // Only passkey output: interactive serial only, never durable log capture.
    Serial.printf("PAIR LOCALLY: enter %06lu on Windows (do not record)\n",
        static_cast<unsigned long>(passkey));
  }
  bool onSecurityRequest() override {
    xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
    const bool accept = clientConnected && !erasingBonds;
    xSemaphoreGive(connectionStateMutex);
    return accept;
  }
  bool onConfirmPIN(uint32_t) override { return false; }
  void onAuthenticationComplete(esp_ble_auth_cmpl_t result) override {
    xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
    const bool currentPeer = clientConnected &&
        memcmp(connectedAddress, result.bd_addr, sizeof(connectedAddress)) == 0;
    const bool approved = !erasingBonds && comms::isAuthenticated(result.success, result.auth_mode);
    if (currentPeer) authenticated = approved;
    const uint16_t rejectedConnection = connectionId;
    xSemaphoreGive(connectionStateMutex);
    // Never print the callback struct: it includes key material.
    Serial.printf("ble_security_complete success=%u auth_mode=%u approved=%u current_peer=%u reason=%u\n",
        result.success, result.auth_mode, currentPeer && approved, currentPeer,
        result.success ? 0 : result.fail_reason);
    if (currentPeer && !approved) server->disconnect(rejectedConnection);
  }
};

class CccdCallbacks final : public BLEDescriptorCallbacks {
  void onWrite(BLEDescriptor*) override {
    xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
    notificationsEnabled = clientConnected && counterCccd->getNotifications();
    mtuProbeNotificationsEnabled = clientConnected && mtuProbeCccd->getNotifications();
    sensorNotificationsEnabled = clientConnected && sensorCccd->getNotifications();
    responseNotificationsEnabled = clientConnected && responseCccd->getNotifications();
    // In the same locked step as the laptop's notification-off write, so its
    // quiesce read and its confirming read see the same statistics.
    if (!sensorNotificationsEnabled) discardQueuedSamples();
    xSemaphoreGive(connectionStateMutex);
  }
};

class SourceStatsCallbacks final : public BLECharacteristicCallbacks {
  void onRead(BLECharacteristic* characteristic) override {
    comms::SourceStats snapshot;
    comms::SampleCounters samples;
    xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
    snapshot = sensorStats;
    samples = sampleCounters;
    xSemaphoreGive(connectionStateMutex);

    uint8_t payload[comms::kSourceStatsSize];
    if (comms::serializeSourceStats(payload, sizeof(payload), kDeviceId, comms::kGloveGated,
                                    snapshot, samples)) {
      characteristic->setValue(payload, sizeof(payload));
    }
  }
};

bool setSecurityParam(esp_ble_sm_param_t parameter, uint8_t value) {
  const esp_err_t result = esp_ble_gap_set_security_param(parameter, &value, sizeof(value));
  if (result != ESP_OK) Serial.printf("ble_security_configuration_failed parameter=%u return_code=%d\n", parameter, result);
  return result == ESP_OK;
}

bool configureSecurity() {
  if (kDiagnostic) {
    Serial.println("ble_security=UNPROTECTED_DIAGNOSTIC not_protected_evidence");
    return true;
  }
  BLEDevice::setSecurityCallbacks(new SecurityCallbacks());
  // Stack-generated fresh passkey per pairing. setStaticPIN is intentionally
  // avoided because that Arduino helper overwrites the authentication mode.
  return setSecurityParam(ESP_BLE_SM_AUTHEN_REQ_MODE, ESP_LE_AUTH_REQ_SC_MITM_BOND) &&
      setSecurityParam(ESP_BLE_SM_IOCAP_MODE, ESP_IO_CAP_OUT) &&
      setSecurityParam(ESP_BLE_SM_MAX_KEY_SIZE, 16) &&
      setSecurityParam(ESP_BLE_SM_SET_INIT_KEY, ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK) &&
      setSecurityParam(ESP_BLE_SM_SET_RSP_KEY, ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK) &&
      setSecurityParam(ESP_BLE_SM_ONLY_ACCEPT_SPECIFIED_SEC_AUTH, ESP_BLE_ONLY_ACCEPT_SPECIFIED_AUTH_ENABLE);
}

void gattsCallback(esp_gatts_cb_event_t event, esp_gatt_if_t interface,
                   esp_ble_gatts_cb_param_t* param) {
  // Runs in the BTU task: count only, never take the mutex. The send gate
  // keeps the ATT queue below the congestion threshold, so this stays 0.
  if (event == ESP_GATTS_CONGEST_EVT) {
    if (param->congest.congested) congestedEvents = congestedEvents + 1;
    return;
  }
  // BLEServer's getGattsIf() is private in Arduino 2.0.17. Capture the public
  // registration event for this process's sole GATT application instead.
  if (event == ESP_GATTS_REG_EVT) {
    if (param->reg.status == ESP_GATT_OK) {
      xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
      serverGattIf = interface;
      xSemaphoreGive(connectionStateMutex);
    }
    return;
  }
  if (controlCharacteristic != nullptr) {
    const auto request = comms::parseApplicationControlWrite(
        event == ESP_GATTS_WRITE_EVT, param, controlCharacteristic->getHandle());
    if (request.status != comms::ProbeWriteStatus::Ignored) {
      if (request.status == comms::ProbeWriteStatus::Malformed) {
        Serial.printf("b07_control_rejected reason=malformed bytes=%u\n", request.inputLength);
        return;
      }
      xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
      const bool authenticatedPeer = !erasingBonds && comms::canAcceptControl(
          clientConnected, responseNotificationsEnabled, authenticated, kDiagnostic,
          request.connectionId, connectionId);
      const bool fits = comms::controlFitsMtu(negotiatedMtu) &&
          request.inputLength <= negotiatedMtu - 3;
      if (authenticatedPeer && fits && controlQueueCount < kControlQueueSize) {
        auto& queued = controlQueue[(controlQueueHead + controlQueueCount) % kControlQueueSize];
        queued.length = request.inputLength;
        memcpy(queued.bytes, request.value, request.inputLength);
        ++controlQueueCount;
      } else {
        ++controlQueueRejected;
        // An ATT write receipt is not an application acceptance. Explicitly
        // return busy/unsupported when this authenticated peer can receive it.
        if (authenticatedPeer && request.value[0] == 'B' && request.value[1] == '7') {
          uint8_t rejected[comms::kControlHeaderSize];
          memcpy(rejected, request.value, sizeof(rejected));
          rejected[2] = 1; rejected[3] |= 0x80; rejected[4] = kDeviceId;
          rejected[5] = fits ? comms::ControlBusy : comms::ControlUnsupported;
          submitNotification(responseCharacteristic, rejected, sizeof(rejected));
        }
      }
      xSemaphoreGive(connectionStateMutex);
      return;
    }
  }
  if (mtuProbeControlCharacteristic == nullptr) return;
  // Arduino's characteristic callback also receives EXEC_WRITE union variants.
  // The raw event check must precede every read of param->write.
  const comms::ProbeWrite write = comms::parseMtuControlWrite(
      event == ESP_GATTS_WRITE_EVT, param, mtuProbeControlCharacteristic->getHandle());
  if (write.status == comms::ProbeWriteStatus::Ignored) return;
  if (write.status == comms::ProbeWriteStatus::Malformed) {
    Serial.printf("mtu_probe_rejected reason=malformed request_bytes=%u\n", write.inputLength);
    return;
  }
  xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
  const bool ready = write.connectionId == connectionId && canNotify(mtuProbeNotificationsEnabled);
  const bool pending = mtuProbeRequestPending;
  if (ready && !pending) {
    pendingMtuProbeLength = write.requestedLength;
    mtuProbeRequestPending = true;
  }
  xSemaphoreGive(connectionStateMutex);
  if (!ready || pending) Serial.printf("mtu_probe_rejected reason=%s requested_length=%u\n",
      ready ? "pending" : "not_ready", write.requestedLength);
}

void gapCallback(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t* param) {
  // Link facts for the serial log: whether the 251-byte link-layer packets
  // (data length extension) are in use, and the connection interval the
  // laptop chose (units of 1.25 ms; timeout in units of 10 ms).
  if (event == ESP_GAP_BLE_SET_PKT_LENGTH_COMPLETE_EVT) {
    Serial.printf("ble_data_length status=%d tx_len=%u rx_len=%u uptime_ms=%lu\n",
        param->pkt_data_lenth_cmpl.status, param->pkt_data_lenth_cmpl.params.tx_len,
        param->pkt_data_lenth_cmpl.params.rx_len, static_cast<unsigned long>(millis()));
    return;
  }
  if (event == ESP_GAP_BLE_UPDATE_CONN_PARAMS_EVT) {
    Serial.printf("ble_conn_params status=%d interval=%u latency=%u timeout=%u uptime_ms=%lu\n",
        param->update_conn_params.status, param->update_conn_params.conn_int,
        param->update_conn_params.latency, param->update_conn_params.timeout,
        static_cast<unsigned long>(millis()));
    return;
  }
  if (event != ESP_GAP_BLE_REMOVE_BOND_DEV_COMPLETE_EVT) return;
  xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
  if (erasingBonds && bondErasePending != 0) {
    --bondErasePending;
    if (param->remove_bond_dev_cmpl.status != ESP_BT_STATUS_SUCCESS) ++bondEraseFailures;
  }
  xSemaphoreGive(connectionStateMutex);
}

void eraseBonds() {
  xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
  if (clientConnected || erasingBonds) {
    xSemaphoreGive(connectionStateMutex);
    Serial.println("erase_bonds_rejected disconnect_Windows_first_or_wait_for_erasure");
    return;
  }
  erasingBonds = true;
  authenticated = false;
  restartAdvertising = false;
  bondEraseFailures = 0;
  xSemaphoreGive(connectionStateMutex);
  advertising->stop();
  int count = esp_ble_get_bond_device_num();
  std::vector<esp_ble_bond_dev_t> bonds(count > 0 ? count : 0);
  const esp_err_t listed = count > 0 ? esp_ble_get_bond_device_list(&count, bonds.data()) : ESP_OK;
  if (count < 0 || listed != ESP_OK) {
    Serial.printf("erase_bonds_failed return_code=%d restart_required=1\n", listed);
    return;  // Advertising/authentication remain closed until restart.
  }
  xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
  bondErasePending = static_cast<uint32_t>(count);
  bondEraseStarted = true;
  xSemaphoreGive(connectionStateMutex);
  for (int index = 0; index < count; ++index) {
    if (esp_ble_remove_bond_device(bonds[index].bd_addr) != ESP_OK) {
      xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
      --bondErasePending;
      ++bondEraseFailures;
      xSemaphoreGive(connectionStateMutex);
    }
  }
  Serial.printf("erase_bonds_requested count=%d\n", count);
}

void readSerialCommand() {
  static char command[32];
  static size_t length = 0;
  static bool overflow = false;
  while (Serial.available()) {
    const char incoming = static_cast<char>(Serial.read());
    if (incoming == '\r') continue;
    if (incoming == '\n') {
      command[length] = '\0';
      if (!overflow && strcmp(command, "erase-bonds") == 0) eraseBonds();
      else if (length || overflow) Serial.println("serial_command_unknown supported=erase-bonds");
      length = 0;
      overflow = false;
    } else if (length < sizeof(command) - 1) command[length++] = incoming;
    else overflow = true;
  }
}

BLECharacteristic* addNotify(BLEService* service, const char* uuid, BLE2902** descriptor) {
  BLECharacteristic* characteristic = service->createCharacteristic(uuid, BLECharacteristic::PROPERTY_NOTIFY);
  *descriptor = new BLE2902();
  (*descriptor)->setCallbacks(new CccdCallbacks());
  if (!kDiagnostic) {
    characteristic->setAccessPermissions(ESP_GATT_PERM_READ_ENC_MITM);
    (*descriptor)->setAccessPermissions(ESP_GATT_PERM_READ_ENC_MITM | ESP_GATT_PERM_WRITE_ENC_MITM);
  }
  characteristic->addDescriptor(*descriptor);
  return characteristic;
}

// Called with the mutex held, on every loop pass. Sends the waiting samples,
// up to the batch limit, as one notification once fewer than
// kMaxNotificationsInFlight notifications are still below the application.
// When the link carries fewer notifications than ticks (two gloves on one
// laptop: one notification per 30 ms per glove at worst), each notification
// carries the samples of one service period instead of the surplus being
// refused (comms_send_queue.h).
void sendQueuedSamples() {
  if (sampleQueue.size() == 0 || !canNotify(sensorNotificationsEnabled)) return;
  const size_t limit = comms::packetsPerNotification(negotiatedMtu);
  if (limit == 0) return;
  const uint16_t sendable = esp_ble_get_cur_sendable_packets_num(connectionId);
  const uint16_t window = esp_ble_get_sendable_packets_num();
  if (sendable > idleSendable) idleSendable = sendable;
  if (window > idleControllerWindow) idleControllerWindow = window;
  const uint16_t inFlight = comms::notificationsInFlight(idleSendable, sendable,
                                                         idleControllerWindow, window);
  if (inFlight > inFlightPeak) inFlightPeak = inFlight;
  if (inFlight >= comms::kMaxNotificationsInFlight) {
    // Host-side work that never reaches an idle controller is a count
    // Bluedroid lost (a failed allocation leaves it raised); learn the lower
    // value instead of keeping the gate shut for the rest of the connection.
    const uint32_t nowMs = millis();
    if (window < idleControllerWindow || !gateStuck) {
      gateStuck = window >= idleControllerWindow;  // a busy controller is a real wait
      gateStuckSinceMs = nowMs;
    } else if (static_cast<uint32_t>(nowMs - gateStuckSinceMs) >= kGateRebaselineMs) {
      idleSendable = sendable;
      gateStuck = false;
      ++gateRebaselines;
    }
    return;
  }
  gateStuck = false;
  uint8_t payload[comms::kMaxSensorNotificationSize];
  const size_t count = comms::buildSensorNotification(payload, sizeof(payload), sampleQueue, limit,
                                                      kDeviceId, sensorStats);
  if (count == 0) return;
  if (submitNotification(sensorCharacteristic, payload,
                         static_cast<uint16_t>(count * comms::kPacketSize))) {
    comms::commitSensorNotification(sampleQueue, sensorStats, count);
    ++sensorNotifications;
    if (count > batchPeak) batchPeak = static_cast<uint32_t>(count);
  } else {
    ++sensorSendRefused;  // the samples stay queued, without a seq, for the next pass
  }
}

// One sensor tick, called WITHOUT the mutex: the glove module may take
// milliseconds and BLE callbacks must not wait for it. The tick is committed
// (its counters, and a sample to send queued) only if the laptop is still
// ready under the mutex. So once the laptop has turned notifications off,
// nothing in the source statistics changes (the bridge checks that).
void produceSample() {
  float values[comms::kChannelCount];
  for (float& value : values) value = NAN;  // a channel the module leaves unwritten is refused
  const bool readOk = gloveRead(values);
  const bool moving = readOk && gloveShouldSend();
  const uint32_t sampleMs = millis();
  int16_t scaled[comms::kChannelCount] = {};
  comms::SampleCounters counters = sampleCounters;  // only loop() writes it
  const comms::SampleOutcome outcome = comms::prepareSample(readOk, moving, values, scaled, counters);

  xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
  if (canNotify(sensorNotificationsEnabled) && comms::sensorFitsMtu(negotiatedMtu)) {
    sampleCounters = counters;
    if (outcome == comms::SampleSend) {
      comms::PendingSample pending;
      pending.uptimeMs = sampleMs;
      memcpy(pending.values, scaled, sizeof(pending.values));
      if (comms::enqueueSample(sampleQueue, sensorStats, pending)) ++queueOverflow;
      if (sampleQueue.size() > queuePeak) queuePeak = static_cast<uint32_t>(sampleQueue.size());
      sendQueuedSamples();
    }
  } else {
    // The link changed during the read: nothing is counted, no seq is taken.
    ++sampleRaceDropped;
  }
  xSemaphoreGive(connectionStateMutex);
}
}  // namespace

void setup() {
  // Without a transmit buffer each once-a-second report blocks loop() for
  // about 30 ms (115200 baud) and costs a 50 Hz send tick; this core takes the
  // size only before begin().
  Serial.setTxBufferSize(1024);
  Serial.begin(115200);
  // Before BLE: the glove module may calibrate for seconds (glove held still)
  // and the board advertises only after it returns. A glove that reports no
  // sensor still advertises; its ticks then count as unread. A failed try is
  // repeated (comms_glove.h), so one bad read at start-up is not fatal.
  unsigned gloveAttempts = 0;
  const bool gloveReady = comms::beginWithRetries(
      gloveBegin,
      [](unsigned failedAttempt) {
        Serial.printf("glove_begin_failed attempt=%u retry_ms=%lu\n", failedAttempt,
            static_cast<unsigned long>(comms::kGloveBeginRetryMs));
        delay(comms::kGloveBeginRetryMs);
      },
      comms::kGloveBeginAttempts, gloveAttempts);
  Serial.printf("glove_begin ok=%u module=%s gated=%u attempts=%u\n", gloveReady,
      comms::kGloveModuleName, comms::kGloveGated, gloveAttempts);
  bootId = esp_random();
  sensorStats.bootId = bootId;
  lastReportMs = millis();
  lastNotificationUs = micros();
  controls = new (std::nothrow) comms::ControlEngine(kDeviceId, bootId, sha256File, kInitialRateHz);
  if (controls == nullptr) {
    Serial.println("b07_control_state_allocation_failed");
    return;
  }
  connectionStateMutex = xSemaphoreCreateMutex();
  if (connectionStateMutex == nullptr) {
    Serial.println("ble_state_mutex_create_failed");
    return;
  }
  BLEDevice::init(kDeviceName);
  if (!configureSecurity()) return;
  BLEDevice::setCustomGapHandler(gapCallback);
  BLEDevice::setCustomGattsHandler(gattsCallback);
  const esp_err_t localMtuResult = BLEDevice::setMTU(ESP_GATT_MAX_MTU_SIZE);
  Serial.printf("ble_local_mtu_configured mtu=%u return_code=%d\n", ESP_GATT_MAX_MTU_SIZE, localMtuResult);
  server = BLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());
  // Seven characteristics and four CCCDs, with explicit handle headroom.
  BLEService* service = server->createService(BLEUUID(kServiceUuid), 32);
  counterCharacteristic = addNotify(service, kCounterUuid, &counterCccd);
  mtuProbeControlCharacteristic = service->createCharacteristic(kMtuControlUuid, BLECharacteristic::PROPERTY_WRITE);
  if (!kDiagnostic) mtuProbeControlCharacteristic->setAccessPermissions(ESP_GATT_PERM_WRITE_ENC_MITM);
  mtuProbeCharacteristic = addNotify(service, kMtuProbeUuid, &mtuProbeCccd);
  sensorCharacteristic = addNotify(service, kSensorUuid, &sensorCccd);
  sourceStatsCharacteristic = service->createCharacteristic(
      kSourceStatsUuid, BLECharacteristic::PROPERTY_READ);
  if (!kDiagnostic) {
    sourceStatsCharacteristic->setAccessPermissions(ESP_GATT_PERM_READ_ENC_MITM);
  }
  sourceStatsCharacteristic->setCallbacks(new SourceStatsCallbacks());
  controlCharacteristic = service->createCharacteristic(kControlUuid, BLECharacteristic::PROPERTY_WRITE);
  if (!kDiagnostic) controlCharacteristic->setAccessPermissions(ESP_GATT_PERM_WRITE_ENC_MITM);
  responseCharacteristic = addNotify(service, kResponseUuid, &responseCccd);
  service->start();
  advertising = BLEDevice::getAdvertising();
  advertising->addServiceUUID(kServiceUuid);
  advertising->setScanResponse(true);
  advertising->start();
  bleReady = true;
  Serial.printf("comms_ready device_id=%u packet_bytes=%u packet_version=%u rate_hz=%u protected=%u control_version=1 file_max_bytes=%lu glove=%s gated=%u\n",
      kDeviceId, static_cast<unsigned>(comms::kPacketSize), comms::kPacketVersion, controls->rateHz(),
      !kDiagnostic, static_cast<unsigned long>(comms::kControlMaxFile), comms::kGloveModuleName,
      comms::kGloveGated);
}

void loop() {
  const uint32_t nowMs = millis();
  if (!bleReady) { delay(1); return; }
  readSerialCommand();
  // The glove module's own pace (filters, movement windows), connected or not.
  gloveUpdate();
  bool shouldRestartAdvertising = false;
  bool requestDataLength = false;
  esp_bd_addr_t dataLengthPeer = {};
  xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
  // Once per connection, after authentication: ask for 251-byte link-layer
  // packets, so a notification of several samples is one packet on the air.
  // Bluedroid asks by itself when the peer advertises support; the reply
  // (ble_data_length) shows the outcome either way.
  if (clientConnected && (authenticated || kDiagnostic) && !dataLengthRequested) {
    dataLengthRequested = true;
    requestDataLength = true;
    memcpy(dataLengthPeer, connectedAddress, sizeof(dataLengthPeer));
  }
  if (erasingBonds && bondEraseStarted && bondErasePending == 0) {
    bondEraseStarted = false;
    const int remaining = esp_ble_get_bond_device_num();
    if (remaining == 0 && bondEraseFailures == 0) {
      erasingBonds = false;
      restartAdvertising = true;
      Serial.println("erase_bonds_complete remaining=0 remove_Windows_bond_then_pair_again");
    } else Serial.printf("erase_bonds_failed remaining=%d failures=%lu restart_required=1\n",
        remaining, static_cast<unsigned long>(bondEraseFailures));
  }
  if (restartAdvertising && !clientConnected && !erasingBonds) {
    restartAdvertising = false;
    shouldRestartAdvertising = true;
  }
  xSemaphoreGive(connectionStateMutex);
  if (requestDataLength) {
    const esp_err_t result = esp_ble_gap_set_pkt_data_len(dataLengthPeer, 251);
    Serial.printf("ble_data_length_requested tx_len=251 return_code=%d uptime_ms=%lu\n", result,
        static_cast<unsigned long>(nowMs));
  }
  if (shouldRestartAdvertising) {
    advertising->start();
    Serial.printf("ble_advertising_restarted boot_id=%lu counter=%lu uptime_ms=%lu\n",
        static_cast<unsigned long>(bootId), static_cast<unsigned long>(notificationCounter),
        static_cast<unsigned long>(nowMs));
  }

  xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
  controls->expire(nowMs);
  const uint32_t nowUs = micros();
  const uint32_t intervalUs = 1000000u / controls->rateHz();
  bool sampleDue = false;
  // A slow pass (gloveUpdate) delays one tick, not the rate (comms_sample.h).
  if (comms::sendTickDue(lastNotificationUs, nowUs, intervalUs)) {
    if (clientConnected && !authenticated && !kDiagnostic) ++securitySuppressed;
    if (canNotify(notificationsEnabled)) {
      uint8_t payload[4];
      comms::writeUint32Le(payload, notificationCounter);
      if (submitNotification(counterCharacteristic, payload, sizeof(payload))) {
        Serial.printf("ble_notify_submitted boot_id=%lu counter=%lu uptime_ms=%lu\n",
            static_cast<unsigned long>(bootId), static_cast<unsigned long>(notificationCounter),
            static_cast<unsigned long>(nowMs));
        ++notificationCounter;
      }
    }
    if (canNotify(sensorNotificationsEnabled)) {
      if (!comms::sensorFitsMtu(negotiatedMtu)) ++sensorMtuSuppressed;
      else sampleDue = true;
    }
  }
  xSemaphoreGive(connectionStateMutex);
  // The glove module never runs under the mutex; produceSample re-checks.
  if (sampleDue) produceSample();

  xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
  // Every pass, not only on ticks: the backlog leaves as soon as the link
  // takes it.
  sendQueuedSamples();
  // Bound bulk work to one command/chunk per loop. Sensor deadlines get the
  // first opportunity to run; callbacks only copy into a four-entry queue.
  if (controlQueueCount != 0 && canNotify(responseNotificationsEnabled)) {
    const auto& request = controlQueue[controlQueueHead];
    uint8_t response[comms::kControlMaxFrame];
    const uint16_t previousRate = controls->rateHz();
    const size_t length = controls->process(request.bytes, request.length, nowMs, response, sizeof(response));
    controlQueueHead = (controlQueueHead + 1) % kControlQueueSize;
    --controlQueueCount;
    if (controls->rateHz() != previousRate) {
      lastNotificationUs = micros();
      Serial.printf("b07_rate_changed device_id=%u rate_hz=%u\n", kDeviceId, controls->rateHz());
    }
    if (length != 0) submitNotification(responseCharacteristic, response, static_cast<uint16_t>(length));
  }
  if (mtuProbeRequestPending) {
    mtuProbeRequestPending = false;
    const uint16_t requested = pendingMtuProbeLength;
    const uint16_t allowed = negotiatedMtu > 3 ? negotiatedMtu - 3 : 0;
    if (!canNotify(mtuProbeNotificationsEnabled)) {
      Serial.printf("mtu_probe_rejected reason=not_ready requested_length=%u negotiated_mtu=%u allowed_length=%u\n", requested, negotiatedMtu, allowed);
    } else if (requested > allowed || requested > kMtuProbePayloadCapacity) {
      Serial.printf("mtu_probe_rejected requested_length=%u negotiated_mtu=%u allowed_length=%u\n", requested, negotiatedMtu, allowed);
    } else {
      uint8_t payload[kMtuProbePayloadCapacity];
      for (uint16_t i = 0; i < requested; ++i) payload[i] = static_cast<uint8_t>(i);
      if (submitNotification(mtuProbeCharacteristic, payload, requested)) {
        Serial.printf("mtu_probe_submitted requested_length=%u negotiated_mtu=%u allowed_length=%u boot_id=%lu uptime_ms=%lu\n",
            requested, negotiatedMtu, allowed, static_cast<unsigned long>(bootId), static_cast<unsigned long>(nowMs));
      }
    }
  }
  const bool observedAuthenticated = authenticated;
  const uint16_t observedMtu = negotiatedMtu;
  const bool report = static_cast<uint32_t>(nowMs - lastReportMs) >= kReportIntervalMs;
  // Send-queue figures for the report; the peaks restart each second.
  const uint32_t observedQueue = static_cast<uint32_t>(sampleQueue.size());
  const uint32_t observedQueuePeak = queuePeak, observedBatchPeak = batchPeak;
  const uint32_t observedInFlightPeak = inFlightPeak;
  const uint16_t observedIdleSendable = idleSendable, observedIdleWindow = idleControllerWindow;
  if (report) queuePeak = batchPeak = inFlightPeak = 0;
  xSemaphoreGive(connectionStateMutex);
  if (report) {
    lastReportMs = nowMs;
    Serial.printf("alive boot_id=%lu sequence=%lu uptime_ms=%lu\n", static_cast<unsigned long>(bootId),
        static_cast<unsigned long>(aliveSequence++), static_cast<unsigned long>(nowMs));
    Serial.printf("comms_stats protected=%u authenticated=%u sensor_submitted=%lu next_seq=%lu mtu=%u mtu_suppressed=%lu security_suppressed=%lu submission_errors=%lu rate_hz=%u control_queue_rejected=%lu unread=%lu idle=%lu refused=%lu clamped=%lu race_dropped=%lu\n",
        !kDiagnostic, observedAuthenticated, static_cast<unsigned long>(sensorStats.submitted),
        static_cast<unsigned long>(sensorStats.nextSequence), observedMtu, static_cast<unsigned long>(sensorMtuSuppressed),
        static_cast<unsigned long>(securitySuppressed), static_cast<unsigned long>(submissionErrors),
        controls->rateHz(), static_cast<unsigned long>(controlQueueRejected),
        static_cast<unsigned long>(sampleCounters.unread), static_cast<unsigned long>(sampleCounters.idle),
        static_cast<unsigned long>(sampleCounters.refused), static_cast<unsigned long>(sampleCounters.clamped),
        static_cast<unsigned long>(sampleRaceDropped));
    Serial.printf("comms_send notifications=%lu failures=%lu queue=%lu queue_peak=%lu batch_peak=%lu in_flight_peak=%lu idle_sendable=%u idle_window=%u send_refused=%lu overflow=%lu discarded=%lu congested=%lu rebaselines=%lu\n",
        static_cast<unsigned long>(sensorNotifications), static_cast<unsigned long>(sensorStats.failures),
        static_cast<unsigned long>(observedQueue), static_cast<unsigned long>(observedQueuePeak),
        static_cast<unsigned long>(observedBatchPeak), static_cast<unsigned long>(observedInFlightPeak),
        observedIdleSendable, observedIdleWindow, static_cast<unsigned long>(sensorSendRefused),
        static_cast<unsigned long>(queueOverflow), static_cast<unsigned long>(queueDiscarded),
        static_cast<unsigned long>(congestedEvents), static_cast<unsigned long>(gateRebaselines));
  }
  delay(1);
}

// flash.py derives the Bluetooth identity from the ESP32 factory base MAC.
static_assert(CONFIG_ESP32_UNIVERSAL_MAC_ADDRESSES == 4,
              "Sequential flash mapping requires the default four-address ESP32 SDK");
