// =============================================================================
// sinew_ems.ino
//
// Firmware for the Sinew assistive-grasp prototype. Runs on an Arduino Micro.
// Drives three relays that gate the positive output of a Belifu EMS unit into
// three dorsal-hand electrodes: index, middle, and pinky metacarpals.
//
// This is the lowest safety layer of the stack. It enforces:
//   1. Mutex: only one relay ON at a time. Turning one ON forces the others OFF.
//   2. Watchdog: if no serial command arrives for 3000 ms, all relays go OFF.
//   3. Per-finger cap: any relay ON for more than 2000 ms continuously auto-OFF.
//   4. Boot state: all relays OFF before the serial port is even opened.
//
// The physical kill switch on the EMS positive lead bypasses everything here.
// This firmware is the second line of defense, not the first.
//
// Serial: 115200 baud, newline-terminated ASCII. Sends "READY\n" on boot.
//
// Commands (uppercase, newline-terminated):
//   FINGER:INDEX:ON      energize index relay, force others OFF
//   FINGER:INDEX:OFF     de-energize index relay
//   FINGER:MIDDLE:ON     energize middle relay, force others OFF
//   FINGER:MIDDLE:OFF    de-energize middle relay
//   FINGER:PINKY:ON      energize pinky relay, force others OFF
//   FINGER:PINKY:OFF     de-energize pinky relay
//   ALL:OFF              force all relays OFF
//   PING                 respond with "PONG"
//
// Responses:
//   OK:<original command>    valid command accepted and applied
//   ERR:<reason>             invalid command, no state change
//   WATCHDOG                 no command in 3000 ms, all relays forced OFF
//   TIMEOUT:<FINGER>         per-finger 2000 ms cap hit, that relay forced OFF
//   PONG                     response to PING
//   READY                    sent once on boot
//
// Relay polarity assumption: the relay modules used here are ACTIVE-LOW.
// digitalWrite(pin, LOW) energizes the relay coil and closes the EMS channel.
// digitalWrite(pin, HIGH) de-energizes the relay and opens the channel.
// If the relay module in use is active-HIGH instead, flip RELAY_ON and
// RELAY_OFF below. Verify by watching the onboard LED on the relay module
// and listening for the click when sending FINGER:INDEX:ON on a fresh boot.
// =============================================================================

// ---------- Pin assignments ----------
const uint8_t PIN_INDEX  = 4;
const uint8_t PIN_MIDDLE = 5;
const uint8_t PIN_PINKY  = 6;

// ---------- Relay polarity ----------
// Active-LOW relay modules: drive the IN pin LOW to energize the coil.
const uint8_t RELAY_ON  = LOW;
const uint8_t RELAY_OFF = HIGH;

// ---------- Timing constants ----------
const unsigned long WATCHDOG_MS     = 3000UL;  // no serial for this long -> all OFF
const unsigned long PER_FINGER_CAP  = 2000UL;  // any single relay ON no longer than this

// ---------- Finger indexing ----------
enum Finger : uint8_t {
  F_INDEX  = 0,
  F_MIDDLE = 1,
  F_PINKY  = 2,
  F_COUNT  = 3
};

const uint8_t FINGER_PINS[F_COUNT] = { PIN_INDEX, PIN_MIDDLE, PIN_PINKY };
const char*   FINGER_NAMES[F_COUNT] = { "INDEX", "MIDDLE", "PINKY" };

// ---------- Relay state tracking ----------
bool          relayOn[F_COUNT]      = { false, false, false };
unsigned long relayOnSince[F_COUNT] = { 0, 0, 0 };  // millis() when it went ON

// ---------- Watchdog tracking ----------
unsigned long lastCommandMs = 0;
bool          watchdogTripped = false;  // latched until next valid command

// ---------- Serial input buffer ----------
const uint8_t BUF_MAX = 48;
char    inBuf[BUF_MAX];
uint8_t inLen = 0;

// =============================================================================
// Relay primitives
// =============================================================================

// Drive a single relay to the requested state and update bookkeeping.
// Does NOT enforce the mutex. Call setFinger() or allOff() for safe changes.
void writeRelay(uint8_t idx, bool on) {
  digitalWrite(FINGER_PINS[idx], on ? RELAY_ON : RELAY_OFF);
  if (on && !relayOn[idx]) {
    relayOnSince[idx] = millis();
  }
  relayOn[idx] = on;
}

// Force every relay OFF. Safe to call anywhere.
void allOff() {
  for (uint8_t i = 0; i < F_COUNT; i++) {
    writeRelay(i, false);
  }
}

// Set a single finger to the requested state while enforcing the channel mutex.
// Turning a finger ON forces the other two OFF first, so the EMS unit only
// ever sees one channel closed at a time.
void setFinger(uint8_t target, bool on) {
  if (on) {
    for (uint8_t i = 0; i < F_COUNT; i++) {
      if (i != target && relayOn[i]) {
        writeRelay(i, false);
      }
    }
    writeRelay(target, true);
  } else {
    writeRelay(target, false);
  }
}

// =============================================================================
// Serial helpers
// =============================================================================

void sendLine(const char* s) {
  Serial.print(s);
  Serial.print('\n');
}

void sendOk(const char* cmd) {
  Serial.print(F("OK:"));
  Serial.print(cmd);
  Serial.print('\n');
}

void sendErr(const char* reason) {
  Serial.print(F("ERR:"));
  Serial.print(reason);
  Serial.print('\n');
}

// =============================================================================
// Command parsing
// =============================================================================
//
// Parser is strict: commands must match exactly. No whitespace trimming beyond
// stripping the terminating \r or \n. Empty lines are ignored silently to
// tolerate CRLF from serial monitors.

void handleCommand(const char* cmd) {
  if (cmd[0] == '\0') {
    return;  // blank line, ignore
  }

  // A valid command arrived. Clear the watchdog latch and refresh the timer.
  lastCommandMs = millis();
  watchdogTripped = false;

  if (strcmp(cmd, "PING") == 0) {
    sendLine("PONG");
    return;
  }

  if (strcmp(cmd, "ALL:OFF") == 0) {
    allOff();
    sendOk(cmd);
    return;
  }

  if (strncmp(cmd, "FINGER:", 7) == 0) {
    const char* rest = cmd + 7;

    int8_t which = -1;
    uint8_t restOffset = 0;
    if (strncmp(rest, "INDEX:", 6) == 0) {
      which = F_INDEX;
      restOffset = 6;
    } else if (strncmp(rest, "MIDDLE:", 7) == 0) {
      which = F_MIDDLE;
      restOffset = 7;
    } else if (strncmp(rest, "PINKY:", 6) == 0) {
      which = F_PINKY;
      restOffset = 6;
    } else {
      sendErr("unknown_finger");
      return;
    }

    const char* action = rest + restOffset;
    if (strcmp(action, "ON") == 0) {
      setFinger(which, true);
      sendOk(cmd);
    } else if (strcmp(action, "OFF") == 0) {
      setFinger(which, false);
      sendOk(cmd);
    } else {
      sendErr("unknown_action");
    }
    return;
  }

  sendErr("unknown_command");
}

// =============================================================================
// Serial line assembly
// =============================================================================

void pumpSerial() {
  while (Serial.available() > 0) {
    char c = (char) Serial.read();

    if (c == '\n' || c == '\r') {
      if (inLen > 0) {
        inBuf[inLen] = '\0';
        handleCommand(inBuf);
        inLen = 0;
      }
      // lone \r or \n on empty buffer: ignore (handles CRLF)
      continue;
    }

    if (inLen >= BUF_MAX - 1) {
      // Overflow. Drop the buffer and report. Prevents stuck state on noise.
      inLen = 0;
      sendErr("overflow");
      continue;
    }

    inBuf[inLen++] = c;
  }
}

// =============================================================================
// Safety supervisors (run every loop iteration)
// =============================================================================

// If any relay has been ON for longer than PER_FINGER_CAP, force it OFF
// and report. Independent per finger so a long INDEX does not mask MIDDLE.
void enforcePerFingerCap() {
  unsigned long now = millis();
  for (uint8_t i = 0; i < F_COUNT; i++) {
    if (relayOn[i] && (now - relayOnSince[i]) >= PER_FINGER_CAP) {
      writeRelay(i, false);
      Serial.print(F("TIMEOUT:"));
      Serial.print(FINGER_NAMES[i]);
      Serial.print('\n');
    }
  }
}

// If no command has arrived in WATCHDOG_MS, force everything OFF and latch
// until the next valid command. Latch avoids spamming WATCHDOG every loop.
void enforceWatchdog() {
  if (watchdogTripped) {
    return;
  }
  if ((millis() - lastCommandMs) >= WATCHDOG_MS) {
    allOff();
    watchdogTripped = true;
    sendLine("WATCHDOG");
  }
}

// =============================================================================
// setup / loop
// =============================================================================

void setup() {
  // Boot state FIRST: configure pins as outputs driven to RELAY_OFF before
  // we do anything else. This guarantees no accidental stim on power-up even
  // if the USB host is slow to enumerate.
  for (uint8_t i = 0; i < F_COUNT; i++) {
    pinMode(FINGER_PINS[i], OUTPUT);
    digitalWrite(FINGER_PINS[i], RELAY_OFF);
    relayOn[i] = false;
    relayOnSince[i] = 0;
  }

  Serial.begin(115200);
  // Arduino Micro uses native USB. Wait briefly for the host, but do not
  // block forever: the firmware must still run if no host is attached.
  unsigned long t0 = millis();
  while (!Serial && (millis() - t0) < 1500UL) {
    ; // wait up to 1.5s for USB CDC
  }

  lastCommandMs = millis();  // arm watchdog from boot
  watchdogTripped = false;

  sendLine("READY");
}

void loop() {
  pumpSerial();
  enforcePerFingerCap();
  enforceWatchdog();
  // No delay(). Main loop stays responsive so the watchdog and per-finger
  // cap fire within a millisecond or two of their deadlines.
}
