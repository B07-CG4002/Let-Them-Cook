#pragma once
#ifndef COMMS_FLEX3_GLOVE
#define COMMS_FLEX3_GLOVE 0
#endif
constexpr unsigned GLOVE_SENSOR_CHANNEL_COUNT = COMMS_FLEX3_GLOVE ? 12 : 11;

// Initialize the MPU-6500, flex sensor ADC inputs, and gyro calibration.
// Returns false promptly if the IMU is missing; also returns false if setup
// or calibration fails.
bool gloveBegin();

// Service the sensors and update the Mahony orientation filter.
// Call frequently from the firmware loop, including while disconnected.
// This function contains no delay(); a due I2C transaction is synchronous.
void gloveUpdate();

// Copy the most recent valid sample into out[GLOVE_SENSOR_CHANNEL_COUNT].
// Order: linear accel XYZ (g), bias-corrected gyro XYZ (deg/s),
// yaw/pitch/roll (degrees), flex 1/2, and optional flex3 on GPIO39 (raw ADC / 4095, not bend calibration).
// Returns false when no fresh valid sample is available. On false, out is zeroed.
bool gloveRead(float out[GLOVE_SENSOR_CHANNEL_COUNT]);

// Return the latest smoothed movement state from the on-device classifier.
// The classifier needs a full 1-second sample window before it is ready.
bool gloveIsMoving();

// True after the classifier has filled its first window and produced a result.
bool gloveMovementReady();
