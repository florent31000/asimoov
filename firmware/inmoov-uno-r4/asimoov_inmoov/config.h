// Channel table for asimoov_inmoov.ino.
//
// One entry per servo. The limits here are the HARD limits: the `L` command
// can only tighten them, never widen them. Widening a range means the tendon
// can reach a mechanical stop and break the part, so a change here is a
// physical decision, made with the pulley unscrewed and the range re-measured
// by hand.
//
//   id       0-31  = PCA9685 channel (0-15 on 0x40, 16-31 on 0x41)
//            >=100 = servo wired directly to an Arduino pin
//   pin      Arduino pin, used only when id >= 100
//   name     lowercase joint name, used by the adapter to build the manifest
//   min_deg  hard lower stop, degrees
//   max_deg  hard upper stop, degrees
//   rest_deg position assumed at boot and taken by `E` if `P` was not sent
//   inverted true when the servo turns the wrong way on this joint
//   us_min   pulse width, in microseconds, for 0 degrees
//   us_max   pulse width, in microseconds, for 180 degrees

#pragma once

// Wi-Fi / TCP link. Keep it at 0 for a USB-only bench: the sketch then
// compiles without secrets.h. Set it to 1 after copying secrets.h.example to
// secrets.h and filling in the network.
#define ASIMOOV_WIFI_ENABLED 0
#define ASIMOOV_TCP_PORT 5005

#define PCA9685_ADDR_A 0x40
#define PCA9685_ADDR_B 0x41

// Servos wired straight to an Arduino pin (id >= 100).
#define MAX_DIRECT_SERVOS 4

struct ChannelConfig {
  uint8_t id;
  uint8_t pin;
  const char *name;
  int16_t min_deg;
  int16_t max_deg;
  int16_t rest_deg;
  bool inverted;
  uint16_t us_min;
  uint16_t us_max;
};

// ---------------------------------------------------------------------------
// Current bench: one finger on pin 3, the range measured on DoigtSerie.ino.
// ---------------------------------------------------------------------------
const ChannelConfig CHANNELS[] = {
    {100, 3, "finger_demo", 2, 108, 2, false, 544, 2400},
};

// ---------------------------------------------------------------------------
// Full bust, for when the PCA9685 boards are wired. Replace the table above
// with this one and re-measure every range before powering the servos.
//
// const ChannelConfig CHANNELS[] = {
//     {0,  0, "fingers_r",  0,  120, 10, false, 600, 2400},
//     {1,  0, "wrist_r",    0,  180, 90, false, 600, 2400},
//     {2,  0, "elbow_r",    0,  150, 90, false, 600, 2400},
//     {3,  0, "shoulder_r", 0,  150, 20, false, 600, 2400},
//     {8,  0, "neck_yaw",   30, 150, 90, false, 600, 2400},
//     {9,  0, "neck_pitch", 60, 120, 90, false, 600, 2400},
//     {10, 0, "jaw",        10, 60,  10, false, 600, 2400},
//     {11, 0, "eyelids",    20, 90,  20, false, 600, 2400},
// };
//
// Names the adapter recognises when it builds the manifest from `V`:
//   fingers_r  -> gesture.hand_right      fingers_l  -> gesture.hand_left
//   neck_yaw   -> gaze.pan_tilt           jaw        -> face.jaw
//   eyelids    -> face.eyelids            finger_demo -> gesture.finger_demo
// neck_yaw grows toward the robot's own LEFT; flip `inverted` if the head
// turns the wrong way rather than swapping min/max.
// ---------------------------------------------------------------------------
