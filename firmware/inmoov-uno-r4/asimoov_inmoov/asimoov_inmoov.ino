// ASIMOOV InMoov firmware -- Arduino UNO R4 WiFi.
//
// Line protocol over USB serial (115200) and TCP (WiFiServer, port 5005).
// See protocol.md for the command set, config.h for the channel table.
//
// Safety rules inherited from InMoov/finger-starter/DoigtSerie.ino:
// nothing is attached at boot, the pulse width is written before attach() so
// the servo never jumps to mid-travel, and every angle is constrained by the
// hard limits of config.h.

#include <Adafruit_PWMServoDriver.h>
#include <Servo.h>
#include <Wire.h>

#include "Arduino_LED_Matrix.h"
#include "config.h"

#if ASIMOOV_WIFI_ENABLED
#include <WiFiS3.h>
#if __has_include("secrets.h")
#include "secrets.h"
#else
#error "ASIMOOV_WIFI_ENABLED is 1: copy secrets.h.example to secrets.h"
#endif
#endif

const char FW_NAME[] = "asimoov-inmoov";
const char FW_VERSION[] = "1.0.0";
const int PROTOCOL_VERSION = 1;
const char BOARD_NAME[] = "uno_r4_wifi";

const unsigned long TICK_MS = 20;            // 50 Hz interpolation
const unsigned long HOLD_AFTER_MS = 2000;    // no command -> freeze
const unsigned long DETACH_AFTER_MS = 15000; // no command -> release servos
const unsigned long ESTOP_DETACH_MS = 300;   // `!` -> freeze then release
const unsigned long JAW_MS = 40;
const unsigned long MAX_MOVE_MS = 20000;
const size_t LINE_MAX = 160;

const uint8_t CHANNEL_COUNT = sizeof(CHANNELS) / sizeof(CHANNELS[0]);

struct ChannelState {
  float current;
  float start;
  float target;
  unsigned long t0;
  unsigned long duration_ms;
  int16_t soft_min;
  int16_t soft_max;
  bool attached;
};

ChannelState state[CHANNEL_COUNT];
uint8_t directIndex[CHANNEL_COUNT];
Servo directServos[MAX_DIRECT_SERVOS];

Adafruit_PWMServoDriver pwmA(PCA9685_ADDR_A);
Adafruit_PWMServoDriver pwmB(PCA9685_ADDR_B);
bool usePwmA = false;
bool usePwmB = false;

ArduinoLEDMatrix matrix;

unsigned long lastCommandMs = 0;
unsigned long lastTickMs = 0;
unsigned long estopAtMs = 0;
bool estopped = false;
bool estopDetached = true;
bool holding = false;
int jawIndex = -1;

char serialLine[LINE_MAX + 1];
size_t serialLen = 0;
bool serialOverflow = false;

#if ASIMOOV_WIFI_ENABLED
WiFiServer tcpServer(ASIMOOV_TCP_PORT);
WiFiClient tcpClient;
char tcpLine[LINE_MAX + 1];
size_t tcpLen = 0;
bool tcpOverflow = false;
#endif

// ---------------------------------------------------------------------------
// LED matrix
// ---------------------------------------------------------------------------

byte FACE_BITMAPS[8][8][12] = {
    // neutral
    {{0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
     {0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0},
     {0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
     {0, 0, 0, 1, 1, 1, 1, 1, 1, 0, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0}},
    // happy
    {{0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
     {0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0},
     {0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
     {0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0},
     {0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0},
     {0, 0, 0, 1, 1, 1, 1, 1, 1, 0, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0}},
    // excited
    {{0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
     {0, 0, 1, 1, 0, 0, 0, 0, 1, 1, 0, 0},
     {0, 0, 1, 1, 0, 0, 0, 0, 1, 1, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
     {0, 0, 0, 1, 1, 1, 1, 1, 1, 0, 0, 0},
     {0, 0, 0, 1, 1, 1, 1, 1, 1, 0, 0, 0},
     {0, 0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0}},
    // curious
    {{0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0},
     {0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
     {0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0, 0},
     {0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0},
     {0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0}},
    // sad
    {{0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
     {0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0},
     {0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
     {0, 0, 0, 1, 1, 1, 1, 1, 1, 0, 0, 0},
     {0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0},
     {0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0}},
    // angry
    {{0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0},
     {0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0},
     {0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
     {0, 0, 0, 1, 1, 1, 1, 1, 1, 0, 0, 0},
     {0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0}},
    // love
    {{0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
     {0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 0, 0},
     {0, 0, 1, 1, 1, 0, 0, 1, 1, 1, 0, 0},
     {0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
     {0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0},
     {0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0}},
    // sleeping
    {{0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
     {0, 0, 1, 1, 1, 0, 0, 1, 1, 1, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1},
     {0, 0, 0, 1, 1, 1, 0, 0, 0, 0, 0, 0},
     {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0}},
};

// vocab.EMOTIONS of the contracts, in order. "annoyed" has no bitmap of its
// own and reuses the angry face.
const char *EMOTION_NAMES[] = {"neutral", "happy",   "excited", "curious", "annoyed",
                               "sad",     "angry",   "love",    "sleeping"};
const uint8_t EMOTION_BITMAP[] = {0, 1, 2, 3, 5, 4, 5, 6, 7};
const uint8_t EMOTION_COUNT = sizeof(EMOTION_BITMAP) / sizeof(EMOTION_BITMAP[0]);

int shownBitmap = -1;

// Redrawing the matrix busies the charlieplexing timer and can make a servo
// twitch, so only redraw on a real change (same reason as DoigtSerie.ino).
void showBitmap(uint8_t index) {
  if ((int)index == shownBitmap) {
    return;
  }
  shownBitmap = index;
  matrix.renderBitmap(FACE_BITMAPS[index], 8, 12);
}

// ---------------------------------------------------------------------------
// Channels
// ---------------------------------------------------------------------------

int channelIndex(int id) {
  for (uint8_t i = 0; i < CHANNEL_COUNT; i++) {
    if (CHANNELS[i].id == id) {
      return i;
    }
  }
  return -1;
}

int degToUs(uint8_t i, float deg) {
  const ChannelConfig &c = CHANNELS[i];
  float effective = c.inverted ? (180.0f - deg) : deg;
  long us = c.us_min + (long)((c.us_max - c.us_min) * (effective / 180.0f));
  return constrain(us, (long)c.us_min, (long)c.us_max);
}

Adafruit_PWMServoDriver *driverFor(uint8_t id) { return id < 16 ? &pwmA : &pwmB; }

// True when the channel is a direct servo without a free slot in
// directServos: more than MAX_DIRECT_SERVOS direct channels in config.h.
bool unusable(uint8_t i) { return CHANNELS[i].id >= 100 && directIndex[i] == 0xFF; }

void writeChannel(uint8_t i) {
  if (!state[i].attached || unusable(i)) {
    return;
  }
  const ChannelConfig &c = CHANNELS[i];
  int us = degToUs(i, state[i].current);
  if (c.id >= 100) {
    directServos[directIndex[i]].writeMicroseconds(us);
  } else {
    driverFor(c.id)->writeMicroseconds(c.id % 16, us);
  }
}

void attachChannel(uint8_t i) {
  if (state[i].attached || unusable(i)) {
    return;
  }
  const ChannelConfig &c = CHANNELS[i];
  if (c.id >= 100) {
    Servo &servo = directServos[directIndex[i]];
    // Pulse width first, attach second: attaching straight away would emit a
    // 1500 us pulse, i.e. mid-travel, and yank the joint.
    servo.writeMicroseconds(degToUs(i, state[i].current));
    servo.attach(c.pin, c.us_min, c.us_max);
  }
  state[i].attached = true;
  writeChannel(i);
}

void detachChannel(uint8_t i) {
  if (!state[i].attached || unusable(i)) {
    state[i].attached = false;
    return;
  }
  const ChannelConfig &c = CHANNELS[i];
  if (c.id >= 100) {
    directServos[directIndex[i]].detach();
  } else {
    driverFor(c.id)->setPWM(c.id % 16, 0, 4096); // full off, no pulse
  }
  state[i].attached = false;
}

void detachAll() {
  for (uint8_t i = 0; i < CHANNEL_COUNT; i++) {
    detachChannel(i);
  }
}

void freezeAll() {
  for (uint8_t i = 0; i < CHANNEL_COUNT; i++) {
    state[i].target = state[i].current;
    state[i].duration_ms = 0;
  }
}

void startMove(uint8_t i, float deg, unsigned long duration_ms) {
  ChannelState &s = state[i];
  s.start = s.current;
  s.target = constrain(deg, (float)s.soft_min, (float)s.soft_max);
  s.t0 = millis();
  s.duration_ms = duration_ms;
  if (duration_ms == 0) {
    s.current = s.target;
    writeChannel(i);
  }
}

void tick(unsigned long now) {
  for (uint8_t i = 0; i < CHANNEL_COUNT; i++) {
    ChannelState &s = state[i];
    if (s.duration_ms == 0) {
      continue;
    }
    unsigned long dt = now - s.t0;
    float u = dt >= s.duration_ms ? 1.0f : (float)dt / (float)s.duration_ms;
    float eased = u * u * (3.0f - 2.0f * u); // ease-in-out
    s.current = s.start + (s.target - s.start) * eased;
    if (u >= 1.0f) {
      s.current = s.target;
      s.duration_ms = 0;
    }
    writeChannel(i);
  }
}

// ---------------------------------------------------------------------------
// Parsing helpers
// ---------------------------------------------------------------------------

void trimLine(char *line) {
  size_t len = strlen(line);
  while (len > 0 && (line[len - 1] == ' ' || line[len - 1] == '\r' || line[len - 1] == '\t')) {
    line[--len] = '\0';
  }
}

char *skipSpaces(char *text) {
  while (*text == ' ' || *text == '\t') {
    text++;
  }
  return text;
}

// A command's arguments must end the line: a tail is a typo, not a comment.
bool endOfArgs(char *cursor) { return *skipSpaces(cursor) == '\0'; }

// Reads an integer at *cursor, advances it. Returns false if there is no digit.
bool readInt(char **cursor, long *out) {
  char *start = skipSpaces(*cursor);
  char *end = NULL;
  long value = strtol(start, &end, 10);
  if (end == start) {
    return false;
  }
  *cursor = end;
  *out = value;
  return true;
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

void cmdState(char *arg, Print &out) {
  if (!endOfArgs(arg)) {
    out.println("ERR bad args");
    return;
  }
  for (uint8_t i = 0; i < CHANNEL_COUNT; i++) {
    out.print("ST ");
    out.print(CHANNELS[i].id);
    out.print(' ');
    out.print(CHANNELS[i].name);
    out.print(' ');
    out.print((int)lroundf(state[i].current));
    out.print(' ');
    out.print(state[i].soft_min);
    out.print(' ');
    out.print(state[i].soft_max);
    out.print(' ');
    out.print(state[i].attached ? 1 : 0);
    out.print(' ');
    out.println(state[i].duration_ms > 0 ? 1 : 0);
  }
  out.println("END");
}

void cmdVersion(char *arg, Print &out) {
  if (!endOfArgs(arg)) {
    out.println("ERR bad args");
    return;
  }
  out.print("V ");
  out.print(FW_NAME);
  out.print(' ');
  out.print(FW_VERSION);
  out.print(' ');
  out.print(PROTOCOL_VERSION);
  out.print(' ');
  out.print(BOARD_NAME);
  out.print(' ');
  out.println(CHANNEL_COUNT);
  for (uint8_t i = 0; i < CHANNEL_COUNT; i++) {
    out.print("CH ");
    out.print(CHANNELS[i].id);
    out.print(' ');
    out.print(CHANNELS[i].name);
    out.print(' ');
    out.print(state[i].soft_min);
    out.print(' ');
    out.print(state[i].soft_max);
    out.print(' ');
    out.println(CHANNELS[i].rest_deg);
  }
  out.println("END");
}

void cmdAttach(char *arg, bool enable, Print &out) {
  arg = skipSpaces(arg);
  if (strcasecmp(arg, "all") == 0) {
    for (uint8_t i = 0; i < CHANNEL_COUNT; i++) {
      if (enable) {
        attachChannel(i);
      } else {
        detachChannel(i);
      }
    }
  } else {
    long id = 0;
    char *cursor = arg;
    if (!readInt(&cursor, &id) || !endOfArgs(cursor)) {
      out.println("ERR bad args");
      return;
    }
    int i = channelIndex(id);
    if (i < 0) {
      out.println("ERR unknown channel");
      return;
    }
    if (enable) {
      attachChannel(i);
    } else {
      detachChannel(i);
    }
  }
  if (enable) {
    estopped = false;
    estopDetached = true;
  }
  out.println("OK");
}

void cmdDeclare(char *arg, Print &out) {
  char *cursor = arg;
  long id = 0;
  long deg = 0;
  if (!readInt(&cursor, &id) || !readInt(&cursor, &deg) || !endOfArgs(cursor)) {
    out.println("ERR bad args");
    return;
  }
  int i = channelIndex(id);
  if (i < 0) {
    out.println("ERR unknown channel");
    return;
  }
  // Declare only: no pulse is emitted, the reference is simply recalibrated.
  float value = constrain((float)deg, (float)state[i].soft_min, (float)state[i].soft_max);
  state[i].current = value;
  state[i].start = value;
  state[i].target = value;
  state[i].duration_ms = 0;
  out.println("OK");
}

void cmdSet(char *arg, Print &out) {
  if (estopped) {
    out.println("ERR estop");
    return;
  }
  char *cursor = arg;
  long id = 0;
  long deg = 0;
  if (!readInt(&cursor, &id) || !readInt(&cursor, &deg) || !endOfArgs(cursor)) {
    out.println("ERR bad args");
    return;
  }
  int i = channelIndex(id);
  if (i < 0) {
    out.println("ERR unknown channel");
    return;
  }
  startMove(i, (float)deg, 0);
  out.println("OK");
}

void cmdMove(char *arg, Print &out) {
  if (estopped) {
    out.println("ERR estop");
    return;
  }
  // M <id>:<deg>[,<id>:<deg>...] T<ms>
  char *cursor = skipSpaces(arg);
  int indexes[CHANNEL_COUNT];
  long degrees[CHANNEL_COUNT];
  uint8_t count = 0;

  while (*cursor != '\0' && *cursor != 'T' && *cursor != 't') {
    long id = 0;
    long deg = 0;
    if (!readInt(&cursor, &id)) {
      out.println("ERR bad args");
      return;
    }
    if (*cursor != ':') {
      out.println("ERR bad args");
      return;
    }
    cursor++;
    if (!readInt(&cursor, &deg)) {
      out.println("ERR bad args");
      return;
    }
    int i = channelIndex(id);
    if (i < 0) {
      out.println("ERR unknown channel");
      return;
    }
    if (count >= CHANNEL_COUNT) {
      out.println("ERR too many channels");
      return;
    }
    indexes[count] = i;
    degrees[count] = deg;
    count++;
    cursor = skipSpaces(cursor);
    if (*cursor == ',') {
      cursor = skipSpaces(cursor + 1);
    }
  }

  if (count == 0 || (*cursor != 'T' && *cursor != 't')) {
    out.println("ERR bad args");
    return;
  }
  cursor++;
  long duration = 0;
  if (!readInt(&cursor, &duration) || duration < 0 || duration > (long)MAX_MOVE_MS ||
      !endOfArgs(cursor)) {
    out.println("ERR bad args");
    return;
  }

  for (uint8_t k = 0; k < count; k++) {
    startMove(indexes[k], (float)degrees[k], (unsigned long)duration);
  }
  out.println("OK");
}

void cmdLimits(char *arg, Print &out) {
  char *cursor = arg;
  long id = 0;
  long lo = 0;
  long hi = 0;
  if (!readInt(&cursor, &id) || !readInt(&cursor, &lo) || !readInt(&cursor, &hi) ||
      !endOfArgs(cursor)) {
    out.println("ERR bad args");
    return;
  }
  int i = channelIndex(id);
  if (i < 0) {
    out.println("ERR unknown channel");
    return;
  }
  if (lo > hi) {
    out.println("ERR bad args");
    return;
  }
  // Tighten only: the hard limits of config.h always win.
  state[i].soft_min = max((int16_t)lo, CHANNELS[i].min_deg);
  state[i].soft_max = min((int16_t)hi, CHANNELS[i].max_deg);
  state[i].current = constrain(state[i].current, (float)state[i].soft_min, (float)state[i].soft_max);
  state[i].target = constrain(state[i].target, (float)state[i].soft_min, (float)state[i].soft_max);
  out.println("OK");
}

void cmdJaw(char *arg, Print &out) {
  if (estopped) {
    out.println("ERR estop");
    return;
  }
  if (jawIndex < 0) {
    out.println("ERR no jaw");
    return;
  }
  char *cursor = arg;
  long amount = 0;
  if (!readInt(&cursor, &amount) || !endOfArgs(cursor)) {
    out.println("ERR bad args");
    return;
  }
  amount = constrain(amount, 0L, 100L);
  ChannelState &s = state[jawIndex];
  float deg = s.soft_min + (s.soft_max - s.soft_min) * (amount / 100.0f);
  startMove((uint8_t)jawIndex, deg, JAW_MS);
  out.println("OK");
}

void cmdFace(char *arg, Print &out) {
  arg = skipSpaces(arg);
  for (uint8_t i = 0; i < EMOTION_COUNT; i++) {
    if (strcasecmp(arg, EMOTION_NAMES[i]) == 0) {
      showBitmap(EMOTION_BITMAP[i]);
      out.println("OK");
      return;
    }
  }
  out.println("ERR unknown emotion");
}

void cmdEstop(char *arg, Print &out) {
  if (!endOfArgs(arg)) {
    out.println("ERR bad args");
    return;
  }
  freezeAll();
  estopped = true;
  estopDetached = false;
  estopAtMs = millis();
  out.println("OK");
}

void handleLine(char *line, Print &out) {
  trimLine(line);
  char *start = skipSpaces(line);
  if (*start == '\0' || *start == '#') {
    return;
  }
  lastCommandMs = millis();
  holding = false;

  char command = toupper(start[0]);
  char *arg = start + 1;

  switch (command) {
    case '?':
      cmdState(arg, out);
      break;
    case 'V':
      cmdVersion(arg, out);
      break;
    case 'E':
      cmdAttach(arg, true, out);
      break;
    case 'D':
      cmdAttach(arg, false, out);
      break;
    case 'P':
      cmdDeclare(arg, out);
      break;
    case 'S':
      cmdSet(arg, out);
      break;
    case 'M':
      cmdMove(arg, out);
      break;
    case 'L':
      cmdLimits(arg, out);
      break;
    case 'J':
      cmdJaw(arg, out);
      break;
    case 'F':
      cmdFace(arg, out);
      break;
    case 'K':
      out.println(endOfArgs(arg) ? "OK" : "ERR bad args");
      break;
    case '!':
      cmdEstop(arg, out);
      break;
    default:
      out.println("ERR unknown command");
      break;
  }
}

// ---------------------------------------------------------------------------
// Links
// ---------------------------------------------------------------------------

// One reply per line, overflow included: the error is emitted at the end of
// the line and its tail is dropped, never parsed as a second command.
void feed(char byteRead, char *buffer, size_t *length, bool *overflow, Print &out) {
  if (byteRead == '\n') {
    if (*overflow) {
      *overflow = false;
      *length = 0;
      out.println("ERR line too long");
      return;
    }
    buffer[*length] = '\0';
    *length = 0;
    handleLine(buffer, out);
    return;
  }
  if (*overflow) {
    return;
  }
  if (*length < LINE_MAX) {
    buffer[(*length)++] = byteRead;
  } else {
    *overflow = true;
    *length = 0;
  }
}

void pumpSerial() {
  while (Serial.available() > 0) {
    feed((char)Serial.read(), serialLine, &serialLen, &serialOverflow, Serial);
  }
}

#if ASIMOOV_WIFI_ENABLED
void pumpTcp() {
  if (!tcpClient || !tcpClient.connected()) {
    tcpClient = tcpServer.available();
    if (tcpClient) {
      tcpLen = 0;
      tcpOverflow = false;
      tcpClient.println("# asimoov-inmoov connected");
    }
    return;
  }
  while (tcpClient.available() > 0) {
    feed((char)tcpClient.read(), tcpLine, &tcpLen, &tcpOverflow, tcpClient);
  }
}

void startWiFi() {
  if (WiFi.status() == WL_NO_MODULE) {
    Serial.println("# no wifi module, serial only");
    return;
  }
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long deadline = millis() + 10000;
  while (WiFi.status() != WL_CONNECTED && millis() < deadline) {
    delay(100);
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("# wifi not connected, serial only");
    return;
  }
  tcpServer.begin();
  Serial.print("# tcp ");
  Serial.print(WiFi.localIP());
  Serial.print(':');
  Serial.println(ASIMOOV_TCP_PORT);
}
#endif

// ---------------------------------------------------------------------------
// Watchdog
// ---------------------------------------------------------------------------

// An unsolicited `# ...` notice goes to every link, not only to the serial
// monitor: a runtime on TCP must learn about a watchdog trip too.
void notice(const char *text) {
  Serial.println(text);
#if ASIMOOV_WIFI_ENABLED
  if (tcpClient && tcpClient.connected()) {
    tcpClient.println(text);
  }
#endif
}

bool anyAttached() {
  for (uint8_t i = 0; i < CHANNEL_COUNT; i++) {
    if (state[i].attached) {
      return true;
    }
  }
  return false;
}

void watchdog(unsigned long now) {
  if (estopped && !estopDetached && now - estopAtMs >= ESTOP_DETACH_MS) {
    detachAll();
    estopDetached = true;
    notice("# estop detached");
    return;
  }
  if (!anyAttached()) {
    return;
  }
  unsigned long silence = now - lastCommandMs;
  if (silence >= DETACH_AFTER_MS) {
    detachAll();
    notice("# watchdog detach");
    return;
  }
  if (silence >= HOLD_AFTER_MS && !holding) {
    freezeAll();
    holding = true;
    notice("# watchdog hold");
  }
}

// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  matrix.begin();
  showBitmap(0);

  uint8_t direct = 0;
  for (uint8_t i = 0; i < CHANNEL_COUNT; i++) {
    const ChannelConfig &c = CHANNELS[i];
    state[i].current = c.rest_deg;
    state[i].start = c.rest_deg;
    state[i].target = c.rest_deg;
    state[i].t0 = 0;
    state[i].duration_ms = 0;
    state[i].soft_min = c.min_deg;
    state[i].soft_max = c.max_deg;
    state[i].attached = false;
    directIndex[i] = 0xFF;
    if (c.id >= 100 && direct < MAX_DIRECT_SERVOS) {
      directIndex[i] = direct++;
    } else if (c.id < 16) {
      usePwmA = true;
    } else if (c.id < 32) {
      usePwmB = true;
    }
    if (strcmp(c.name, "jaw") == 0) {
      jawIndex = i;
    }
  }

  if (usePwmA || usePwmB) {
    Wire.begin();
  }
  if (usePwmA) {
    pwmA.begin();
    pwmA.setOscillatorFrequency(27000000);
    pwmA.setPWMFreq(50);
  }
  if (usePwmB) {
    pwmB.begin();
    pwmB.setOscillatorFrequency(27000000);
    pwmB.setPWMFreq(50);
  }

  // No attach() here: that is the whole safety story, nothing moves until `E`.
  lastCommandMs = millis();
  lastTickMs = lastCommandMs;
  Serial.print("# ");
  Serial.print(FW_NAME);
  Serial.print(' ');
  Serial.print(FW_VERSION);
  Serial.println(" ready, all servos detached");

#if ASIMOOV_WIFI_ENABLED
  startWiFi();
#endif
}

void loop() {
  pumpSerial();
#if ASIMOOV_WIFI_ENABLED
  pumpTcp();
#endif

  unsigned long now = millis();
  if (now - lastTickMs >= TICK_MS) {
    lastTickMs = now;
    tick(now);
    watchdog(now);
  }
}
