#pragma once

// Initialize the MPU-6500, flex sensor ADC inputs, and gyro calibration.
// Returns false promptly if the IMU is missing; also returns false if setup
// or calibration fails.
bool gloveBegin();

// Service the sensors and update the Mahony orientation filter.
// Call frequently from the firmware loop, including while disconnected.
// This function contains no delay(); a due I2C transaction is synchronous.
void gloveUpdate();

// Copy the most recent valid sample into out[11].
// Order: linear accel XYZ (g), bias-corrected gyro XYZ (deg/s),
// yaw/pitch/roll (degrees), flex 1/2 (normalized 0..1).
// Returns false when no fresh valid sample is available. On false, out is zeroed.
bool gloveRead(float out[11]);

// Return the latest smoothed movement state from the on-device classifier.
// The classifier needs a full 1-second sample window before it is ready.
bool gloveIsMoving();

// True after the classifier has filled its first window and produced a result.
bool gloveMovementReady();
