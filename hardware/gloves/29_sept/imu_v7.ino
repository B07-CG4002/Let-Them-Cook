#include <Wire.h>
#include "BluetoothSerial.h"

#if !defined(CONFIG_BT_ENABLED) || !defined(CONFIG_BLUEDROID_ENABLED)
#error Bluetooth Classic is not available on this ESP32 board.
#endif

// Flash one glove as left and change BOTH values to the right-hand values
// before flashing the other glove. The packet label identifies each COM port.
const char *BLUETOOTH_DEVICE_NAME = "SENSOR_GLOVE_RIGHT";
const char *GLOVE_ID = "SENS_GL_RIGHT";
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

// Send 11 sensor values plus a SENS_GL_LEFT/SENS_GL_RIGHT label. The PC uses
// the label only to discover the correct outgoing Bluetooth COM port.
void sendTelemetry(
  float ax, float ay, float az,
  float gx, float gy, float gz,
  float yaw, float pitch, float roll,
  float flex1, float flex2
) {
  char line[160];
  int length = snprintf(
    line, sizeof(line),
    "%.4f,%.4f,%.4f,%.4f,%.4f,%.4f,%.2f,%.2f,%.2f,%.4f,%.4f,%s\n",
    ax, ay, az, gx, gy, gz, yaw, pitch, roll, flex1, flex2, GLOVE_ID
  );
  if (length > 0 && length < (int)sizeof(line)) {
    Serial.print(line);     // Useful while testing through USB.
    SerialBT.print(line);   // Used by the battery-powered Bluetooth setup.
  }
}

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
    sendTelemetry(
      linearAx, linearAy, linearAz,
      gx, gy, gz,
      yaw, pitch, roll,
      flex1, flex2
    );
  }
}
