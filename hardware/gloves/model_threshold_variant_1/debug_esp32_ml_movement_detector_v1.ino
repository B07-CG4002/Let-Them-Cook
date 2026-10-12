#include <Wire.h>
#include "BluetoothSerial.h"
#include "movement_model_params.h"
#include <math.h>

#if !defined(CONFIG_BT_ENABLED) || !defined(CONFIG_BLUEDROID_ENABLED)
#error Bluetooth Classic is not available on this ESP32 board.
#endif

// Flash one glove as left and change BOTH values to the right-hand values
// before flashing the other glove. The packet label identifies each COM port.
const char *BLUETOOTH_DEVICE_NAME = "SENSOR_GLOVE_LEFT";
const char *GLOVE_ID = "SENS_GL_LEFT";
BluetoothSerial SerialBT;

// ============================================================
// MPU-6500
// ============================================================

#define MPU_ADDR 0x68

#define SDA_PIN 21
#define SCL_PIN 22
const int FLEX1_PIN = 34;
const int FLEX2_PIN = 35;

// MPU-6500 registers
#define PWR_MGMT_1   0x6B
#define CONFIG       0x1A
#define GYRO_CONFIG  0x1B
#define ACCEL_CONFIG 0x1C
#define ACCEL_XOUT_H 0x3B
#define WHO_AM_I     0x75

// ============================================================
// Sensor settings
// ============================================================

// ±2g accelerometer
// 16384 LSB/g
const float ACCEL_SCALE = 16384.0f;

// ±250 degrees/sec gyroscope
// 131 LSB/(deg/sec)
const float GYRO_SCALE = 131.0f;

// ============================================================
// Mahony settings
// ============================================================

float q0 = 1.0f;
float q1 = 0.0f;
float q2 = 0.0f;
float q3 = 0.0f;

// Mahony gains
//
// Kp controls how strongly accelerometer corrects the gyro.
// Ki corrects gyro bias.
//
// Start with Ki = 0 while testing.
float Kp = 3.0f;
float Ki = 0.0f;

float integralFBx = 0.0f;
float integralFBy = 0.0f;
float integralFBz = 0.0f;

// ============================================================
// Gyroscope calibration
// ============================================================

float gyroBiasX = 0.0f;
float gyroBiasY = 0.0f;
float gyroBiasZ = 0.0f;

const int CALIBRATION_SAMPLES = 1000;

// ============================================================
// Timing
// ============================================================

unsigned long lastUpdateMicros = 0;

const unsigned long PRINT_INTERVAL_US = 20000; // 50 Hz

// Send 11 sensor values, a SENS_GL_LEFT/SENS_GL_RIGHT label, and the current
// model state. The PC uses the glove label to identify the COM port.
void sendTelemetry(
  float ax, float ay, float az,
  float gx, float gy, float gz,
  float yaw, float pitch, float roll,
  float flex1, float flex2,
  const char *movementState
) {
  char line[176];
  int length = snprintf(
    line, sizeof(line),
    "%.4f,%.4f,%.4f,%.4f,%.4f,%.4f,%.2f,%.2f,%.2f,%.4f,%.4f,%s,%s\n",
    ax, ay, az, gx, gy, gz, yaw, pitch, roll, flex1, flex2,
    GLOVE_ID, movementState
  );
  if (length > 0 && length < (int)sizeof(line)) {
    Serial.print(line);     // Useful while testing through USB.
    SerialBT.print(line);   // Used by the battery-powered Bluetooth setup.
  }
}

// ============================================================
// Shared per-glove movement model runtime
// Feature order must match train_movement_model_v1.py.
// ============================================================

const uint8_t MODEL_WINDOW_SAMPLES = 50;       // 1 second at 50 Hz
const uint16_t MODEL_UPDATE_INTERVAL_MS = 200; // Evaluate every 0.2 seconds
const uint8_t MODEL_MOVING_CONFIRM_WINDOWS = 2;
const uint8_t MODEL_IDLE_CONFIRM_WINDOWS = 3;

struct MovementSample {
  float ax, ay, az;
  float gx, gy, gz;
  float flex1, flex2;
};

class MovementDetector {
 public:
  MovementDetector() : writeIndex(0), sampleCount(0), lastInferenceMs(0),
                       moving(false), movingVotes(0), idleVotes(0),
                       lastProbability(0.0f) {}

  // Returns true whenever a new window probability is available.
  bool update(float ax, float ay, float az,
              float gx, float gy, float gz,
              float flex1, float flex2,
              uint32_t nowMs, bool &stateChanged) {
    stateChanged = false;
#if !MOVEMENT_MODEL_TRAINED
    (void)ax; (void)ay; (void)az;
    (void)gx; (void)gy; (void)gz;
    (void)flex1; (void)flex2; (void)nowMs;
    return false;
#else
    buffer[writeIndex] = {ax, ay, az, gx, gy, gz, flex1, flex2};
    writeIndex = (writeIndex + 1) % MODEL_WINDOW_SAMPLES;
    if (sampleCount < MODEL_WINDOW_SAMPLES) sampleCount++;
    if (sampleCount < MODEL_WINDOW_SAMPLES) return false;
    if ((uint32_t)(nowMs - lastInferenceMs) < MODEL_UPDATE_INTERVAL_MS) return false;
    lastInferenceMs = nowMs;

    float features[MOVEMENT_MODEL_FEATURE_COUNT];
    calculateFeatures(features);
    float score = MOVEMENT_LOGISTIC_INTERCEPT;
    for (uint8_t i = 0; i < MOVEMENT_MODEL_FEATURE_COUNT; i++) {
      float scale = MOVEMENT_FEATURE_SCALE[i];
      if (scale == 0.0f) scale = 1.0f;
      float standardized = (features[i] - MOVEMENT_FEATURE_MEAN[i]) / scale;
      score += MOVEMENT_LOGISTIC_COEFFICIENTS[i] * standardized;
    }
    if (score >= 0.0f) {
      float e = expf(-score);
      lastProbability = 1.0f / (1.0f + e);
    } else {
      float e = expf(score);
      lastProbability = e / (1.0f + e);
    }

    bool previous = moving;
    if (lastProbability >= MOVEMENT_PROBABILITY_THRESHOLD) {
      movingVotes++;
      idleVotes = 0;
      if (!moving && movingVotes >= MODEL_MOVING_CONFIRM_WINDOWS) moving = true;
    } else {
      idleVotes++;
      movingVotes = 0;
      if (moving && idleVotes >= MODEL_IDLE_CONFIRM_WINDOWS) moving = false;
    }
    stateChanged = (previous != moving);
    return true;
#endif
  }

  bool isMoving() const { return moving; }
  float probability() const { return lastProbability; }

 private:
  MovementSample buffer[MODEL_WINDOW_SAMPLES];
  uint8_t writeIndex;
  uint8_t sampleCount;
  uint32_t lastInferenceMs;
  bool moving;
  uint8_t movingVotes;
  uint8_t idleVotes;
  float lastProbability;

#if MOVEMENT_MODEL_TRAINED
  void calculateFeatures(float *features) const {
    float accelMean[3] = {0.0f, 0.0f, 0.0f};
    float accelSquareSum[3] = {0.0f, 0.0f, 0.0f};
    float gyroSquareSum[3] = {0.0f, 0.0f, 0.0f};
    float flexMin[2] = {buffer[writeIndex].flex1, buffer[writeIndex].flex2};
    float flexMax[2] = {buffer[writeIndex].flex1, buffer[writeIndex].flex2};

    for (uint8_t i = 0; i < MODEL_WINDOW_SAMPLES; i++) {
      const MovementSample &s = buffer[(writeIndex + i) % MODEL_WINDOW_SAMPLES];
      accelMean[0] += s.ax; accelMean[1] += s.ay; accelMean[2] += s.az;
      gyroSquareSum[0] += s.gx * s.gx;
      gyroSquareSum[1] += s.gy * s.gy;
      gyroSquareSum[2] += s.gz * s.gz;
      if (s.flex1 < flexMin[0]) flexMin[0] = s.flex1;
      if (s.flex1 > flexMax[0]) flexMax[0] = s.flex1;
      if (s.flex2 < flexMin[1]) flexMin[1] = s.flex2;
      if (s.flex2 > flexMax[1]) flexMax[1] = s.flex2;
    }
    for (uint8_t axis = 0; axis < 3; axis++) accelMean[axis] /= MODEL_WINDOW_SAMPLES;
    for (uint8_t i = 0; i < MODEL_WINDOW_SAMPLES; i++) {
      const MovementSample &s = buffer[(writeIndex + i) % MODEL_WINDOW_SAMPLES];
      const float accel[3] = {s.ax, s.ay, s.az};
      for (uint8_t axis = 0; axis < 3; axis++) {
        float d = accel[axis] - accelMean[axis];
        accelSquareSum[axis] += d * d;
      }
    }
    features[0] = sqrtf(accelSquareSum[0] / MODEL_WINDOW_SAMPLES);
    features[1] = sqrtf(accelSquareSum[1] / MODEL_WINDOW_SAMPLES);
    features[2] = sqrtf(accelSquareSum[2] / MODEL_WINDOW_SAMPLES);
    features[3] = sqrtf(gyroSquareSum[0] / MODEL_WINDOW_SAMPLES);
    features[4] = sqrtf(gyroSquareSum[1] / MODEL_WINDOW_SAMPLES);
    features[5] = sqrtf(gyroSquareSum[2] / MODEL_WINDOW_SAMPLES);
    features[6] = flexMax[0] - flexMin[0];
    features[7] = flexMax[1] - flexMin[1];
  }
#endif
};

MovementDetector movementDetector;

// ============================================================
// I2C helper
// ============================================================

void writeRegister(byte reg, byte value) {

  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  Wire.write(value);
  Wire.endTransmission();
}

// ============================================================
// Read MPU-6500 accelerometer + gyro
// ============================================================

bool readMPU6500(
  int16_t &ax,
  int16_t &ay,
  int16_t &az,
  int16_t &gx,
  int16_t &gy,
  int16_t &gz
) {

  Wire.beginTransmission(MPU_ADDR);
  Wire.write(ACCEL_XOUT_H);

  if (Wire.endTransmission(false) != 0) {
    return false;
  }

  uint8_t bytesReceived = Wire.requestFrom(MPU_ADDR, 14);

  if (bytesReceived != 14) {
    return false;
  }

  ax = (Wire.read() << 8) | Wire.read();
  ay = (Wire.read() << 8) | Wire.read();
  az = (Wire.read() << 8) | Wire.read();

  // Temperature
  Wire.read();
  Wire.read();

  gx = (Wire.read() << 8) | Wire.read();
  gy = (Wire.read() << 8) | Wire.read();
  gz = (Wire.read() << 8) | Wire.read();

  return true;
}

// ============================================================
// Gyroscope calibration
// ============================================================

void calibrateGyroscope() {

  Serial.println();
  Serial.println("================================");
  Serial.println("GYROSCOPE CALIBRATION");
  Serial.println("Keep the MPU-6500 COMPLETELY STILL");
  Serial.println("================================");

  delay(2000);

  long sumX = 0;
  long sumY = 0;
  long sumZ = 0;

  int16_t ax, ay, az;
  int16_t gx, gy, gz;

  for (int i = 0; i < CALIBRATION_SAMPLES; i++) {

    if (readMPU6500(ax, ay, az, gx, gy, gz)) {

      sumX += gx;
      sumY += gy;
      sumZ += gz;
    }

    delay(2);
  }

  gyroBiasX = ((float)sumX / CALIBRATION_SAMPLES) / GYRO_SCALE;
  gyroBiasY = ((float)sumY / CALIBRATION_SAMPLES) / GYRO_SCALE;
  gyroBiasZ = ((float)sumZ / CALIBRATION_SAMPLES) / GYRO_SCALE;

  Serial.println("Calibration complete.");

  Serial.print("Gyro bias X: ");
  Serial.print(gyroBiasX, 4);
  Serial.println(" deg/s");

  Serial.print("Gyro bias Y: ");
  Serial.print(gyroBiasY, 4);
  Serial.println(" deg/s");

  Serial.print("Gyro bias Z: ");
  Serial.print(gyroBiasZ, 4);
  Serial.println(" deg/s");

  Serial.println();
}

// ============================================================
// Fast inverse square root
// ============================================================

float invSqrt(float x) {

  return 1.0f / sqrtf(x);
}

// ============================================================
// Mahony 6-axis IMU update
// ============================================================

void mahonyUpdate(
  float gx,
  float gy,
  float gz,
  float ax,
  float ay,
  float az,
  float dt
) {

  // Convert gyro from degrees/sec → radians/sec
  gx *= 0.01745329252f;
  gy *= 0.01745329252f;
  gz *= 0.01745329252f;

  // ----------------------------------------------------------
  // Normalize accelerometer
  // ----------------------------------------------------------

  float accelNorm = sqrtf(
    ax * ax +
    ay * ay +
    az * az
  );

  if (accelNorm > 0.0f) {

    ax /= accelNorm;
    ay /= accelNorm;
    az /= accelNorm;

    // --------------------------------------------------------
    // Estimated direction of gravity
    // --------------------------------------------------------

    float halfvx = q1 * q3 - q0 * q2;

    float halfvy = q0 * q1 + q2 * q3;

    float halfvz =
      q0 * q0 -
      0.5f +
      q3 * q3;

    // --------------------------------------------------------
    // Error between measured and estimated gravity
    // --------------------------------------------------------

    float halfex =
      (ay * halfvz) -
      (az * halfvy);

    float halfey =
      (az * halfvx) -
      (ax * halfvz);

    float halfez =
      (ax * halfvy) -
      (ay * halfvx);

    // --------------------------------------------------------
    // Integral feedback
    // --------------------------------------------------------

    if (Ki > 0.0f) {

      integralFBx += Ki * halfex * dt;
      integralFBy += Ki * halfey * dt;
      integralFBz += Ki * halfez * dt;

      gx += integralFBx;
      gy += integralFBy;
      gz += integralFBz;

    } else {

      integralFBx = 0.0f;
      integralFBy = 0.0f;
      integralFBz = 0.0f;
    }

    // --------------------------------------------------------
    // Proportional feedback
    // --------------------------------------------------------

    gx += Kp * halfex;
    gy += Kp * halfey;
    gz += Kp * halfez;
  }

  // ----------------------------------------------------------
  // Integrate quaternion
  // ----------------------------------------------------------

  float halfDt = 0.5f * dt;

  gx *= halfDt;
  gy *= halfDt;
  gz *= halfDt;

  float qa = q0;
  float qb = q1;
  float qc = q2;

  q0 +=
    (-qb * gx) -
    (qc * gy) -
    (q3 * gz);

  q1 +=
    (qa * gx) +
    (qc * gz) -
    (q3 * gy);

  q2 +=
    (qa * gy) -
    (qb * gz) +
    (q3 * gx);

  q3 +=
    (qa * gz) +
    (qb * gy) -
    (qc * gx);

  // ----------------------------------------------------------
  // Normalize quaternion
  // ----------------------------------------------------------

  float recipNorm = invSqrt(
    q0 * q0 +
    q1 * q1 +
    q2 * q2 +
    q3 * q3
  );

  q0 *= recipNorm;
  q1 *= recipNorm;
  q2 *= recipNorm;
  q3 *= recipNorm;
}

// ============================================================
// Quaternion → Euler angles
// ============================================================

void getEulerAngles(
  float &yaw,
  float &pitch,
  float &roll
) {

  // Roll
  roll = atan2f(
    2.0f * (q0 * q1 + q2 * q3),
    1.0f - 2.0f * (q1 * q1 + q2 * q2)
  );

  // Pitch
  float sinp =
    2.0f * (q0 * q2 - q3 * q1);

  // Prevent numerical problems around ±90°
  if (fabsf(sinp) >= 1.0f) {
    pitch = copysignf(
      PI / 2.0f,
      sinp
    );
  } else {
    pitch = asinf(sinp);
  }

  // Yaw
  yaw = atan2f(
    2.0f * (q0 * q3 + q1 * q2),
    1.0f - 2.0f * (q2 * q2 + q3 * q3)
  );

  // Convert radians → degrees
  yaw *= 180.0f / PI;
  pitch *= 180.0f / PI;
  roll *= 180.0f / PI;
}

// Remove the gravity component from accelerometer readings.
// The returned values are linear acceleration in g, expressed in the
// MPU-6500 body axes. This uses the Mahony quaternion, but does not alter
// the accelerometer values used internally by the Mahony filter.
void getLinearAcceleration(
  float ax,
  float ay,
  float az,
  float &linearAx,
  float &linearAy,
  float &linearAz
) {

  // Estimated gravity direction in body coordinates.
  float gravityX = 2.0f * (q1 * q3 - q0 * q2);
  float gravityY = 2.0f * (q0 * q1 + q2 * q3);
  float gravityZ =
    2.0f * (q0 * q0 + q3 * q3) - 1.0f;

  linearAx = ax - gravityX;
  linearAy = ay - gravityY;
  linearAz = az - gravityZ;
}

// ============================================================
// Setup
// ============================================================

void setup() {

  Serial.begin(115200);
  SerialBT.begin(BLUETOOTH_DEVICE_NAME);

#if MOVEMENT_MODEL_TRAINED
  Serial.println("ML movement model loaded.");
#else
  Serial.println("ML detector disabled: train a model and replace movement_model_params.h.");
#endif

  delay(1000);

  Serial.println();
  Serial.println("================================");
  Serial.println("ESP32 + MPU-6500 + Mahony AHRS");
  Serial.println("================================");

  // I2C
  Wire.begin(SDA_PIN, SCL_PIN);

  Wire.setClock(400000);

  // ----------------------------------------------------------
  // Check WHO_AM_I
  // ---------------------------------------------------------

  Wire.beginTransmission(MPU_ADDR);
  Wire.write(WHO_AM_I);
  Wire.endTransmission(false);

  Wire.requestFrom(MPU_ADDR, 1);

  if (Wire.available()) {

    byte whoAmI = Wire.read();

    Serial.print("WHO_AM_I = 0x");
    Serial.println(whoAmI, HEX);

    if (whoAmI != 0x70) {

      Serial.println("WARNING: Expected MPU-6500 (0x70)");

    } else {

      Serial.println("MPU-6500 detected.");
    }

  } else {

    Serial.println("ERROR: Could not communicate with MPU-6500.");

    while (true) {
      delay(1000);
    }
  }

  // ----------------------------------------------------------
  // Wake MPU-6500
  // ----------------------------------------------------------

  writeRegister(PWR_MGMT_1, 0x01);

  // DLPF configuration
  writeRegister(CONFIG, 0x03);

  // Gyroscope ±250 deg/s
  writeRegister(GYRO_CONFIG, 0x00);

  // Accelerometer ±2g
  writeRegister(ACCEL_CONFIG, 0x00);

  delay(100);

  Serial.println("MPU-6500 initialized.");

  // ----------------------------------------------------------
  // Calibrate gyro
  // ----------------------------------------------------------

  calibrateGyroscope();

  Serial.println("Starting Mahony filter...");
  Serial.println();

  // ----------------------------------------------------------
  // setup flex sensors
  // ----------------------------------------------------------
  analogReadResolution(12);
  pinMode(FLEX1_PIN, INPUT);
  pinMode(FLEX2_PIN, INPUT);

  lastUpdateMicros = micros();
}

// ============================================================
// Main loop
// ============================================================

void loop() {

  unsigned long now = micros();

  float dt =
    (now - lastUpdateMicros) /
    1000000.0f;

  lastUpdateMicros = now;

  // Protect against unusually large dt
  if (dt <= 0.0f || dt > 0.1f) {
    return;
  }

  int16_t rawAx;
  int16_t rawAy;
  int16_t rawAz;

  int16_t rawGx;
  int16_t rawGy;
  int16_t rawGz;

  if (!readMPU6500(
        rawAx,
        rawAy,
        rawAz,
        rawGx,
        rawGy,
        rawGz
      )) {

    Serial.println("MPU read error!");
    delay(100);
    return;
  }

  // ----------------------------------------------------------
  // Convert raw accelerometer values → g
  // ----------------------------------------------------------

  float ax =
    (float)rawAx / ACCEL_SCALE;

  float ay =
    (float)rawAy / ACCEL_SCALE;

  float az =
    (float)rawAz / ACCEL_SCALE;

  // ----------------------------------------------------------
  // Convert raw gyro values → deg/s
  // ----------------------------------------------------------

  float gx =
    ((float)rawGx / GYRO_SCALE) -
    gyroBiasX;

  float gy =
    ((float)rawGy / GYRO_SCALE) -
    gyroBiasY;

  float gz =
    ((float)rawGz / GYRO_SCALE) -
    gyroBiasZ;

  // ----------------------------------------------------------
  // Mahony update
  // ----------------------------------------------------------

  mahonyUpdate(
    gx,
    gy,
    gz,
    ax,
    ay,
    az,
    dt
  );

  // ----------------------------------------------------------
  // Print at ~50 Hz
  // ----------------------------------------------------------

  static unsigned long lastPrint = 0;

  if (micros() - lastPrint >= PRINT_INTERVAL_US) {

    lastPrint = micros();

    float yaw;
    float pitch;
    float roll;

    getEulerAngles(yaw, pitch, roll);

    // Remove the estimated earth-gravity component only for the values
    // transmitted over serial. The Mahony filter above still uses the
    // original ax, ay, az values, so orientation is not negatively affected.
    float linearAx;
    float linearAy;
    float linearAz;

    getLinearAcceleration(
      ax,
      ay,
      az,
      linearAx,
      linearAy,
      linearAz
    );

    int flex1_adc = analogRead(FLEX1_PIN);
    int flex2_adc = analogRead(FLEX2_PIN);

    // Normalize 12-bit ADC readings to 0.0–1.0
    float flex1 = (float)flex1_adc / 4095.0f;
    float flex2 = (float)flex2_adc / 4095.0f;

    // Telemetry order:
    // ax, ay, az, gx, gy, gz, yaw, pitch, roll, flex1, flex2
    // Accelerometer values are gravity-compensated and in g.
    // Gyro values are bias-corrected deg/s.
#if MOVEMENT_MODEL_TRAINED
    bool stateChanged = false;
    if (movementDetector.update(
          linearAx, linearAy, linearAz,
          gx, gy, gz,
          flex1, flex2,
          millis(), stateChanged
        ) && stateChanged) {
      // USB debug only. Bluetooth packets also include the current state below.
      Serial.printf("MODEL_STATE,%s,%.3f\n",
                    movementDetector.isMoving() ? "MOVING" : "IDLE",
                    movementDetector.probability());
    }
#endif

    const char *movementState = movementDetector.isMoving() ? "MOVING" : "IDLE";
    sendTelemetry(
      linearAx, linearAy, linearAz,
      gx, gy, gz,
      yaw, pitch, roll,
      flex1, flex2,
      movementState
    );
  }
}
