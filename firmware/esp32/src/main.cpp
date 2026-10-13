#include <Arduino.h>
#include <BLE2902.h>
#include <BLEDevice.h>
#include <BLESecurity.h>
#include <BLEServer.h>
#include <esp_gatt_common_api.h>
#include <esp_system.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <mbedtls/sha256.h>
#include <vector>
#include "comms_packet.h"
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
#ifndef COMMS_LEGACY_DUMMY
#define COMMS_LEGACY_DUMMY 0
#endif

namespace {
constexpr bool kDiagnostic = COMMS_UNPROTECTED_DIAGNOSTIC != 0;
constexpr uint8_t kDeviceId = COMMS_DEVICE_ID;
static_assert(kDeviceId == 1 || kDeviceId == 2, "device ID must be 1 or 2");
static_assert(comms::kRequiredAuthMode == ESP_LE_AUTH_REQ_SC_MITM_BOND,
              "Update portable security policy for this SDK");
constexpr uint32_t kReportIntervalMs = 1000;
constexpr uint16_t kInitialRateHz = COMMS_INITIAL_RATE_HZ;
static_assert(kInitialRateHz >= 1 && kInitialRateHz <= 200, "rate must be 1..200 Hz");
constexpr bool kLegacyDummy = COMMS_LEGACY_DUMMY != 0;
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

uint32_t fixtureRandomWord() {
#ifdef COMMS_FIXTURE_SEED
  // A reproducible build mode, independent of boot identity and sample sequence.
  static uint32_t state = static_cast<uint32_t>(COMMS_FIXTURE_SEED);
  state = state * 1664525u + 1013904223u;
  return state >> 8;
#else
  return esp_random();
#endif
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

void resetSubscriptions() {
  notificationsEnabled = mtuProbeNotificationsEnabled = sensorNotificationsEnabled = false;
  responseNotificationsEnabled = false;
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
    xSemaphoreGive(connectionStateMutex);
  }
};

class SourceStatsCallbacks final : public BLECharacteristicCallbacks {
  void onRead(BLECharacteristic* characteristic) override {
    comms::SourceStats snapshot;
    xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
    snapshot = sensorStats;
    xSemaphoreGive(connectionStateMutex);

    uint8_t payload[comms::kSourceStatsSize];
    if (comms::serializeSourceStats(payload, sizeof(payload), kDeviceId, snapshot)) {
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
}  // namespace

void setup() {
  Serial.begin(115200);
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
  Serial.printf("comms_ready device_id=%u packet_bytes=32 packet_version=%u rate_hz=%u protected=%u control_version=1 file_max_bytes=%lu\n",
      kDeviceId, kLegacyDummy ? 1 : 2, controls->rateHz(), !kDiagnostic,
      static_cast<unsigned long>(comms::kControlMaxFile));
}

void loop() {
  const uint32_t nowMs = millis();
  if (!bleReady) { delay(1); return; }
  readSerialCommand();
  bool shouldRestartAdvertising = false;
  xSemaphoreTake(connectionStateMutex, portMAX_DELAY);
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
  if (static_cast<uint32_t>(nowUs - lastNotificationUs) >= intervalUs) {
    lastNotificationUs = nowUs;
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
      else {
        uint8_t payload[comms::kPacketSize];
        const uint32_t sampleSequence = comms::allocateSampleSequence(sensorStats);
        const bool serialized = kLegacyDummy
            ? comms::serializeDummyPacket(payload, sizeof(payload), kDeviceId, bootId, sampleSequence, nowMs)
            : comms::serializeFixturePacket(payload, sizeof(payload), kDeviceId, bootId, sampleSequence,
                                            nowMs, fixtureRandomWord());
        const bool submitted = serialized &&
            submitNotification(sensorCharacteristic, payload, sizeof(payload));
        comms::recordSensorSubmission(sensorStats, submitted);
      }
    }
  }
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
  xSemaphoreGive(connectionStateMutex);
  if (static_cast<uint32_t>(nowMs - lastReportMs) >= kReportIntervalMs) {
    lastReportMs = nowMs;
    Serial.printf("alive boot_id=%lu sequence=%lu uptime_ms=%lu\n", static_cast<unsigned long>(bootId),
        static_cast<unsigned long>(aliveSequence++), static_cast<unsigned long>(nowMs));
    Serial.printf("comms_stats protected=%u authenticated=%u sensor_submitted=%lu next_seq=%lu mtu=%u mtu_suppressed=%lu security_suppressed=%lu submission_errors=%lu rate_hz=%u control_queue_rejected=%lu\n",
        !kDiagnostic, observedAuthenticated, static_cast<unsigned long>(sensorStats.submitted),
        static_cast<unsigned long>(sensorStats.nextSequence), observedMtu, static_cast<unsigned long>(sensorMtuSuppressed),
        static_cast<unsigned long>(securitySuppressed), static_cast<unsigned long>(submissionErrors),
        controls->rateHz(), static_cast<unsigned long>(controlQueueRejected));
  }
  delay(1);
}

// flash.py derives the Bluetooth identity from the ESP32 factory base MAC.
static_assert(CONFIG_ESP32_UNIVERSAL_MAC_ADDRESSES == 4,
              "Sequential flash mapping requires the default four-address ESP32 SDK");
