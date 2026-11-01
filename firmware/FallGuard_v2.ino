/*
  ============================================================
  FallGuard v2 — Adaptive Wearable Fall Detection System
  ============================================================
  Target:    ESP32 (waist-mounted)
  Sensors:   MPU6050 (6-DOF IMU)
  Actuators: Passive buzzer, push button
  Network:   WiFi + HTTP web dashboard (Chart.js live graphs)

  Wiring:
    MPU6050  VCC -> 3.3V    GND -> GND
             SDA -> GPIO21  SCL -> GPIO22
    Buzzer   (+) -> GPIO5   (-) -> GND
    Button       -> GPIO2   (other leg -> GND, INPUT_PULLUP)
  ============================================================
*/

#include <Wire.h>
#include <math.h>

// ============================================================================
//  CONFIGURATION & COMPILE-TIME FEATURE FLAGS
// ============================================================================

// --- Compile-Time Feature Flags ---
#define USE_WIFI       1
#define USE_GNB        1
#define USE_INTEGRITY  1
#define USE_ADAPTIVE   1
#define USE_STILLNESS  1
#define USE_PATH2      1

// --- WiFi Configuration ---
#if USE_WIFI
#include <WiFi.h>
#include <WebServer.h>
#include "secrets.h"
#endif

// --- Phase timings & thresholds ---
const float         FREE_FALL_MAG       = 5.0f;    // m/s^2 - downward acceleration dip
const unsigned long FF_MIN_MS           = 40;      // free-fall must last 40ms (2 samples)
const unsigned long FF_WINDOW_MS        = 1400;    // impact must follow within this window

const float         STILL_LOW           = 7.5f;    // m/s^2 - stillness band lower bound
const float         STILL_HIGH          = 12.0f;   // m/s^2 - stillness band upper bound
const unsigned long STILL_MS            = 2200;    // stillness duration baseline

const unsigned long CANCEL_MS           = 5000;    // user cancel window after confirm

float               JERK_THRESHOLD      = 220.0f;  // jerk threshold (m/s^3 at 50 Hz)
float               IMPACT_THRESHOLD    = 11.5f;   // personal impact threshold (m/s^2)

const float         JERK_PATH2          = 280.0f;  // Path 2 direct high-jerk trigger (m/s^3)
const float         MAG_PATH2           = 11.5f;   // Path 2 direct impact magnitude (m/s^2)

// --- Sampling ---
const int           SAMPLE_RATE_HZ      = 50;               // Hz
const int           SAMPLE_INTERVAL_MS  = 1000 / SAMPLE_RATE_HZ; // 20 ms
const int           WINDOW_SIZE         = 25;               // 500 ms window
const int           SMA_SIZE            = 5;

// --- Hardware Pins ---
const int           MPU_ADDR            = 0x68;
const int           BUZZER_PIN          = 5;
const int           BUTTON_PIN          = 2;

// --- Adaptive Confidence Threshold theta ---
float               theta               = 0.65f;
const float         THETA_MAX           = 0.85f;
const float         THETA_MIN           = 0.50f;
const float         THETA_UP            = 0.02f;
const float         THETA_DOWN          = 0.01f;
const float         E0                  = 1.0f;

// --- Calibration Timing & Button ---
const unsigned long CAL_DURATION_MS     = 20000;  // 20 s
const unsigned long DEBOUNCE_MS         = 50;
const unsigned long HOLD_PRESS_MS       = 1500;
const int           LOG_SIZE            = 12;

// ============================================================================
//  GLOBAL STATE & BUFFERS
// ============================================================================

float calMaxMag  = 0.0f;
float calMaxJerk = 0.0f;
unsigned long calStart = 0;
bool calDoneBuzzerActive = false;
unsigned long calDoneBuzzerStart = 0;
const unsigned long CAL_DONE_BUZZER_DURATION = 740; // 80+80+80+500 ms

enum FallState {
  CALIBRATING,
  NORMAL,
  FREE_FALL,
  IMPACT,
  CANDIDATE_FALL,
  FALL_CONFIRMED,
  ALERT_SENT
};
FallState state = CALIBRATING;

// --- Part E & C: Telemetry, Performance & Adaptive Tracking ---
float E_k = 1.0f;
float a_peak = 0.0f;
float J_peak = 0.0f;
String currentTrialId = "";
unsigned long lastStatPrintMs = 0;
unsigned long statLoopCount = 0;
unsigned long statSampleCount = 0;
unsigned long statLoopUsSum = 0;
unsigned long statLoopUsMax = 0;

// --- Sensor Integrity State (USE_INTEGRITY) ---
#if USE_INTEGRITY
float Q = 1.0f;
bool  sensorFault = false;
unsigned long faultStartTime = 0;
unsigned long stillnessAccumMs = 0;
bool validCandidateActive = false;

// Metrics independent of unplug duration
int badSampleStreak = 0;
int faultDetectSamples = 0;
unsigned long firstGoodSampleTime = 0;

const int AXIS_HIST_SIZE = 25;
int16_t histX[AXIS_HIST_SIZE];
int16_t histY[AXIS_HIST_SIZE];
int16_t histZ[AXIS_HIST_SIZE];
int axisHistHead = 0;
int axisHistCount = 0;
float current_vk = 1.0f;

const unsigned long PAT_FAULT[] = {100, 100, 100, 100, 100, 600};
const int           PAT_FAULT_N = 6;
#endif

// --- Sliding window ---
float winMag[WINDOW_SIZE];
float winJerk[WINDOW_SIZE];
int   winHead    = 0;
bool  winFull    = false;

// --- SMA filter ring buffer ---
float smaBuffer[SMA_SIZE];
int   smaHead    = 0;
float smaSum     = 0.0f;

// --- Activity inference ---
enum Activity { ACT_STILL, ACT_WALKING, ACT_RUNNING };
Activity currentActivity = ACT_STILL;

long  actCount = 0;
float actMeanMag  = 9.81f;
float actM2Mag    = 0.0f;
float actVarMag   = 0.0f;

// --- Online Gaussian Naive Bayes classifier (USE_GNB) ---
#if USE_GNB
const int N_FEATURES = 6;

struct ClassStats {
  long  count;
  float mean[N_FEATURES];
  float M2[N_FEATURES];
  float var[N_FEATURES];
};

ClassStats fallStats;
ClassStats nonFallStats;

float lastClassConf = -1.0f;
float lastFeatures[N_FEATURES];
#endif

// --- Event log ---
String eventLog[LOG_SIZE];
int    logHead = 0;

// --- Timing bookmarks ---
unsigned long ffStart        = 0;
unsigned long impactTime     = 0;
unsigned long stillnessStart = 0;
unsigned long confirmedTime  = 0;
unsigned long lastSampleUs   = 0;

bool inFFDip = false;

// --- Button state ---
bool lastRaw = HIGH, debounced = HIGH;
unsigned long debounceTs = 0;
unsigned long pressStart = 0;
bool holdTriggered  = false;
bool quickPress     = false;
bool holdPress      = false;

// --- Remote cancel & non-blocking Serial command buffer ---
volatile bool remoteCancelReq = false;
String serialRxBuf = "";

// --- Web server ---
#if USE_WIFI
WebServer server(80);
#endif

float liveMag  = 9.81f;
float liveJerk = 0.0f;

// ============================================================================
//  UTILITY - MPU6050 IMU read
// ============================================================================

bool readIMU(int16_t& rx, int16_t& ry, int16_t& rz, float& mag) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x3B);
  uint8_t txStatus = Wire.endTransmission(false);
  uint8_t readCount = Wire.requestFrom(MPU_ADDR, 6, true);
  bool i2c_ok = (txStatus == 0) && (readCount == 6) && (Wire.available() >= 6);
  if (i2c_ok) {
    rx = (int16_t)(Wire.read() << 8 | Wire.read());
    ry = (int16_t)(Wire.read() << 8 | Wire.read());
    rz = (int16_t)(Wire.read() << 8 | Wire.read());

    // ±2g clips at impact; ±8g needed per IEEE paper Section III-A
    // Scale factor: 4096 LSB/g at ±8g sensitivity
    float ax = rx / 4096.0f;
    float ay = ry / 4096.0f;
    float az = rz / 4096.0f;
    mag = sqrtf(ax * ax + ay * ay + az * az) * 9.81f;
    return true;
  }
  rx = 0; ry = 0; rz = 0;
  mag = 0.0f;
  return false;
}

float readRawMagnitude() {
  int16_t rx, ry, rz;
  float mag;
  readIMU(rx, ry, rz, mag);
  return mag;
}

// ============================================================================
//  SIGNAL LAYER
// ============================================================================

float applySmoothing(float raw) {
  smaSum -= smaBuffer[smaHead];
  smaBuffer[smaHead] = raw;
  smaSum += raw;
  smaHead = (smaHead + 1) % SMA_SIZE;
  return smaSum / SMA_SIZE;
}

float readSmoothed(float prevMag, unsigned long dt_us) {
  int16_t rx = 0, ry = 0, rz = 0;
  float raw = 0.0f;
  bool i2c_ok = readIMU(rx, ry, rz, raw);

#if USE_INTEGRITY
  // Per-sample validity v_k:
  // i2c_ok AND mag >= 2.0 m/s^2 AND no axis >= 32700 LSB AND
  // each axis changed > 2 LSB over last 25 samples AND measured_dt <= 40000 us
  histX[axisHistHead] = rx;
  histY[axisHistHead] = ry;
  histZ[axisHistHead] = rz;
  axisHistHead = (axisHistHead + 1) % AXIS_HIST_SIZE;
  if (axisHistCount < AXIS_HIST_SIZE) axisHistCount++;

  bool dynamic_ok = true;
  if (axisHistCount >= AXIS_HIST_SIZE) {
    int16_t minX = histX[0], maxX = histX[0];
    int16_t minY = histY[0], maxY = histY[0];
    int16_t minZ = histZ[0], maxZ = histZ[0];
    for (int i = 1; i < AXIS_HIST_SIZE; i++) {
      if (histX[i] < minX) minX = histX[i];
      if (histX[i] > maxX) maxX = histX[i];
      if (histY[i] < minY) minY = histY[i];
      if (histY[i] > maxY) maxY = histY[i];
      if (histZ[i] < minZ) minZ = histZ[i];
      if (histZ[i] > maxZ) maxZ = histZ[i];
    }
    dynamic_ok = ((maxX - minX) > 2) && ((maxY - minY) > 2) && ((maxZ - minZ) > 2);
  }

  bool axis_limit_ok = (abs(rx) < 32700) && (abs(ry) < 32700) && (abs(rz) < 32700);
  bool mag_ok        = (raw >= 2.0f);
  bool dt_ok         = (dt_us <= 40000UL);

  current_vk = (i2c_ok && mag_ok && axis_limit_ok && dynamic_ok && dt_ok) ? 1.0f : 0.0f;

  if (!sensorFault) {
    if (current_vk < 0.5f) {
      badSampleStreak++;
    } else {
      badSampleStreak = 0;
    }
  } else {
    // While in sensorFault: track arrival of the first good sample after replug
    if (current_vk > 0.5f) {
      if (firstGoodSampleTime == 0) {
        firstGoodSampleTime = millis();
      }
    } else {
      firstGoodSampleTime = 0; // Reset if intermittent glitches occur before stable connection
    }
  }

  // Quality index Q update: Q = 0.9 * Q + 0.1 * v_k
  Q = 0.9f * Q + 0.1f * current_vk;

  if (!sensorFault && Q < 0.5f) {
    sensorFault = true;
    faultStartTime = millis();
    faultDetectSamples = badSampleStreak;
    firstGoodSampleTime = 0;

    logEvent("SENSOR_FAULT,Q=" + String(Q, 2) + ",fault_detect_samples=" + String(faultDetectSamples));
    Serial.println("EVENT," + String(millis()) + ",SENSOR_FAULT," + String(Q, 2) + "," + String(faultDetectSamples));

    // Only log POSSIBLE_FALL_DURING_FAULT if candidate was started from VALID samples before fault
    if (validCandidateActive && (state == FREE_FALL || state == IMPACT || state == CANDIDATE_FALL || inFFDip)) {
      logEvent("POSSIBLE_FALL_DURING_FAULT");
      Serial.println("EVENT," + String(millis()) + ",POSSIBLE_FALL_DURING_FAULT");
    }

    // Reset free-fall/impact candidate to NORMAL on entering SENSOR_FAULT
    if (state == FREE_FALL || state == IMPACT || state == CANDIDATE_FALL) {
      state = NORMAL;
    }
    inFFDip = false;
    validCandidateActive = false;

  } else if (sensorFault && Q > 0.9f) {
    sensorFault = false;
    unsigned long fault_duration_ms = millis() - faultStartTime;
    unsigned long recover_ms_after_good = (firstGoodSampleTime > 0) ? (millis() - firstGoodSampleTime) : 0;
    badSampleStreak = 0;

    logEvent("SENSOR_RECOVERED,fault_duration_ms=" + String(fault_duration_ms) + ",recover_ms_after_good=" + String(recover_ms_after_good));
    Serial.println("EVENT," + String(millis()) + ",SENSOR_RECOVERED," + String(fault_duration_ms) + "," + String(recover_ms_after_good));

    // Reset free-fall/impact candidate to NORMAL on SENSOR_RECOVERED
    if (state == FREE_FALL || state == IMPACT || state == CANDIDATE_FALL) {
      state = NORMAL;
    }
    inFFDip = false;
    validCandidateActive = false;
  }
#endif

  float smooth  = applySmoothing(raw);
  float dt_s    = (dt_us > 0) ? ((float)dt_us / 1000000.0f) : (SAMPLE_INTERVAL_MS / 1000.0f);
  liveJerk = fabsf(smooth - prevMag) / dt_s;
  return smooth;
}

void pushWindow(float mag, float jerk) {
  winMag[winHead]  = mag;
  winJerk[winHead] = jerk;
  winHead = (winHead + 1) % WINDOW_SIZE;
  if (winHead == 0) winFull = true;
}

float getRecentMaxJerk(unsigned long ms) {
  int count = ms / SAMPLE_INTERVAL_MS;
  int available = winFull ? WINDOW_SIZE : winHead;
  if (count > available) count = available;
  float maxJ = 0.0f;
  for (int i = 0; i < count; i++) {
    int idx = (winHead - 1 - (i % WINDOW_SIZE) + 2 * WINDOW_SIZE) % WINDOW_SIZE;
    if (winJerk[idx] > maxJ) maxJ = winJerk[idx];
  }
  return maxJ;
}

// ============================================================================
//  FEATURE LAYER & ONLINE GAUSSIAN NAIVE BAYES (USE_GNB)
// ============================================================================

#if USE_GNB
void extractFeatures(float feats[N_FEATURES]) {
  int n = winFull ? WINDOW_SIZE : winHead;
  if (n < 2) { for (int i = 0; i < N_FEATURES; i++) feats[i] = 0; return; }

  float sum = 0, mx = -1e9f, mn = 1e9f, mxJ = 0;
  for (int i = 0; i < n; i++) {
    sum += winMag[i];
    if (winMag[i]  > mx) mx  = winMag[i];
    if (winMag[i]  < mn) mn  = winMag[i];
    if (winJerk[i] > mxJ) mxJ = winJerk[i];
  }
  float mean = sum / n;

  float varAcc = 0;
  for (int i = 0; i < n; i++) {
    float d = winMag[i] - mean;
    varAcc += d * d;
  }
  float var = varAcc / (n - 1);

  feats[0] = mean;
  feats[1] = mx;
  feats[2] = mn;
  feats[3] = var;
  feats[4] = mx / fmaxf(mn, 0.1f);
  feats[5] = mxJ;
}

void initClassStats(ClassStats& cs) {
  cs.count = 0;
  for (int i = 0; i < N_FEATURES; i++) {
    cs.mean[i] = 0;
    cs.M2[i]   = 0;
    cs.var[i]  = 1.0f;
  }
}

void updateClassStats(ClassStats& cs, float feats[N_FEATURES]) {
  cs.count++;
  for (int i = 0; i < N_FEATURES; i++) {
    float delta  = feats[i] - cs.mean[i];
    cs.mean[i]  += delta / cs.count;
    float delta2 = feats[i] - cs.mean[i];
    cs.M2[i]    += delta * delta2;
    cs.var[i]    = (cs.count > 1) ? cs.M2[i] / (cs.count - 1) : 1.0f;
  }
}

float logProb(ClassStats& cs, float feats[N_FEATURES]) {
  if (cs.count < 2) return 0.0f;
  float total = (float)(fallStats.count + nonFallStats.count + 1);
  float logP  = logf((float)cs.count / total + 1e-9f);
  for (int i = 0; i < N_FEATURES; i++) {
    float var  = fmaxf(cs.var[i], 0.01f);
    float diff = feats[i] - cs.mean[i];
    logP -= 0.5f * logf(2.0f * M_PI * var);
    logP -= (diff * diff) / (2.0f * var);
  }
  return logP;
}

float classify(float feats[N_FEATURES]) {
  if (fallStats.count < 3 || nonFallStats.count < 3) {
    return 0.75f;
  }
  float logFall    = logProb(fallStats,    feats);
  float logNonFall = logProb(nonFallStats, feats);
  float maxL    = fmaxf(logFall, logNonFall);
  float expFall = expf(logFall    - maxL);
  float expNF   = expf(logNonFall - maxL);
  return expFall / (expFall + expNF + 1e-9f);
}
#endif // USE_GNB

// ============================================================================
//  ACTIVITY INFERENCE
// ============================================================================

void updateActivityBaseline(float mag) {
  actCount++;
  float delta  = mag - actMeanMag;
  actMeanMag  += delta / actCount;
  float delta2 = mag - actMeanMag;
  actM2Mag    += delta * delta2;
  actVarMag    = (actCount > 1) ? actM2Mag / (actCount - 1) : 0.0f;

  if (actVarMag < 0.5f)       currentActivity = ACT_STILL;
  else if (actVarMag < 8.0f)  currentActivity = ACT_WALKING;
  else                        currentActivity = ACT_RUNNING;
}

String activityName() {
  switch (currentActivity) {
    case ACT_STILL:   return "STILL";
    case ACT_WALKING: return "WALKING";
    case ACT_RUNNING: return "RUNNING";
  }
  return "UNKNOWN";
}

// ============================================================================
//  EVENT LOG & BUZZER
// ============================================================================

void logEvent(String msg) {
  String entry = String(millis() / 1000) + "s - " + msg;
  eventLog[logHead] = entry;
  logHead = (logHead + 1) % LOG_SIZE;
  Serial.println("EVENT," + String(millis()) + "," + msg);
}

const unsigned long PAT_CANCEL[] = {150, 150, 150, 150, 150, 550};
const int           PAT_CANCEL_N = 6;
const unsigned long PAT_ALERT[]  = {500, 100};
const int           PAT_ALERT_N  = 2;
const unsigned long PAT_CAL[]    = {80, 80, 80, 500};
const int           PAT_CAL_N    = 4;

void playPattern(const unsigned long* pat, int n) {
  unsigned long period = 0;
  for (int i = 0; i < n; i++) period += pat[i];
  unsigned long t = millis() % period;
  unsigned long acc = 0;
  for (int i = 0; i < n; i++) {
    acc += pat[i];
    if (t < acc) {
      digitalWrite(BUZZER_PIN, (i % 2 == 0) ? HIGH : LOW);
      return;
    }
  }
}

void buzzerOff() { digitalWrite(BUZZER_PIN, LOW); }

// ============================================================================
//  BUTTON & RESET
// ============================================================================

void readButton() {
  quickPress = false;
  holdPress  = false;
  bool raw = digitalRead(BUTTON_PIN);
  if (raw != lastRaw) debounceTs = millis();
  if (millis() - debounceTs > DEBOUNCE_MS) {
    if (raw != debounced) {
      debounced = raw;
      if (debounced == LOW) {
        pressStart    = millis();
        holdTriggered = false;
      } else {
        unsigned long heldFor = millis() - pressStart;
        if (!holdTriggered && heldFor < HOLD_PRESS_MS) quickPress = true;
      }
    }
  }
  if (debounced == LOW && !holdTriggered && millis() - pressStart >= HOLD_PRESS_MS) {
    holdTriggered = true;
    holdPress     = true;
  }
  lastRaw = raw;
}

void resetToNormal(String reason) {
  buzzerOff();
  calDoneBuzzerActive = false;
  inFFDip        = false;
  stillnessStart = 0;
  a_peak         = 0.0f;
  J_peak         = 0.0f;
#if USE_INTEGRITY
  stillnessAccumMs = 0;
  validCandidateActive = false;
#endif
  state          = NORMAL;
#if USE_GNB
  lastClassConf  = -1.0f;
#endif
  logEvent(reason);
}

// ============================================================================
//  NON-BLOCKING SERIAL COMMAND HANDLER
// ============================================================================

void processSerialCommand(String cmd) {
  cmd.trim();
  if (cmd == "CANCEL") {
    remoteCancelReq = true;
  } else if (cmd == "ACK") {
    if (state == ALERT_SENT) {
      resetToNormal("ALERT_ACKNOWLEDGED_SERIAL");
    }
  } else if (cmd.startsWith("SET_THETA ")) {
    float v = cmd.substring(10).toFloat();
    theta = constrain(v, THETA_MIN, THETA_MAX);
    logEvent("THETA_SET_SERIAL=" + String(theta, 2));
  } else if (cmd.startsWith("TRIAL,")) {
    currentTrialId = cmd.substring(6);
    currentTrialId.trim();
    logEvent("TRIAL_SET=" + currentTrialId);
    Serial.println("EVENT," + String(millis()) + ",TRIAL_SET," + currentTrialId);
  } else if (cmd == "TRIAL" || cmd == "TRIAL,") {
    currentTrialId = "";
    logEvent("TRIAL_CLEARED");
    Serial.println("EVENT," + String(millis()) + ",TRIAL_CLEARED");
  }
#if USE_GNB
  else if (cmd == "TRAIN,FALL") {
    updateClassStats(fallStats, lastFeatures);
    logEvent("TRAINED_FALL,count=" + String(fallStats.count));
    Serial.println("EVENT," + String(millis()) + ",TRAINED_FALL," + String(fallStats.count));
  } else if (cmd == "TRAIN,NONFALL") {
    updateClassStats(nonFallStats, lastFeatures);
    logEvent("TRAINED_NONFALL,count=" + String(nonFallStats.count));
    Serial.println("EVENT," + String(millis()) + ",TRAINED_NONFALL," + String(nonFallStats.count));
  }
#endif
}

void checkSerialCommands() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (serialRxBuf.length() > 0) {
        processSerialCommand(serialRxBuf);
        serialRxBuf = "";
      }
    } else {
      if (serialRxBuf.length() < 64) {
        serialRxBuf += c;
      }
    }
  }
}

// ============================================================================
//  MAIN STATE MACHINE
// ============================================================================

void runFallDetection(unsigned long dt_us) {
  float prevMag = liveMag;
  liveMag  = readSmoothed(prevMag, dt_us);
  float jerk = liveJerk;
  pushWindow(liveMag, jerk);

  // Windowed energy update: E_k = (1 - 0.02)*E_{k-1} + 0.02*(a_smooth - 9.81)^2 on valid samples
#if USE_INTEGRITY
  if (current_vk > 0.5f) {
    E_k = 0.98f * E_k + 0.02f * (liveMag - 9.81f) * (liveMag - 9.81f);
  }
#else
  E_k = 0.98f * E_k + 0.02f * (liveMag - 9.81f) * (liveMag - 9.81f);
#endif

  if (state == NORMAL || state == CALIBRATING) {
    updateActivityBaseline(liveMag);
  }

  readButton();

  if (remoteCancelReq) {
    remoteCancelReq = false;
    if (state != NORMAL && state != CALIBRATING) {
#if USE_GNB
      theta = fminf(THETA_MAX, theta + THETA_UP);
      logEvent("CANCEL_REMOTE theta=" + String(theta, 2));
#endif
      resetToNormal("CANCEL_REMOTE");
    }
  }

  // Stream telemetry to USB Serial
  Serial.print("TELEMETRY,");
  Serial.print(millis()); Serial.print(",");
  Serial.print(liveMag,  2); Serial.print(",");
  Serial.print(jerk,     1); Serial.print(",");
  Serial.print(stateStr());   Serial.print(",");
#if USE_GNB
  Serial.print(lastClassConf >= 0 ? lastClassConf : 0.0f, 3); Serial.print(",");
#else
  Serial.print(0.0f, 3); Serial.print(",");
#endif
  Serial.print(activityName()); Serial.print(",");
#if USE_GNB
  Serial.print(fallStats.count); Serial.print(",");
  Serial.print(nonFallStats.count); Serial.print(",");
  Serial.print(theta, 3); Serial.print(",");
#else
  Serial.print(0); Serial.print(",");
  Serial.print(0); Serial.print(",");
  Serial.print(0.65f, 3); Serial.print(",");
#endif
  Serial.print(IMPACT_THRESHOLD, 1); Serial.print(",");
  Serial.print(JERK_THRESHOLD, 1);   Serial.print(",");
  Serial.print(currentTrialId.length() > 0 ? currentTrialId : "NONE"); Serial.print(",");
#if USE_INTEGRITY
  Serial.print(Q, 2); Serial.print(",");
#else
  Serial.print(1.00f, 2); Serial.print(",");
#endif
  Serial.print(E_k, 2); Serial.print(",");
#if USE_INTEGRITY
  Serial.println(sensorFault ? "FAULT" : "OK");
#else
  Serial.println("OK");
#endif

#if USE_INTEGRITY
  // Requirement 1: Do not feed invalid samples (v_k = 0) to state machine. Hold state instead.
  if (current_vk < 0.5f || sensorFault) {
    return;
  }
#endif

  bool isStill = (liveMag >= STILL_LOW && liveMag <= STILL_HIGH);

  switch (state) {
    case CALIBRATING: {
      if (liveMag > calMaxMag)  calMaxMag  = liveMag;
      if (jerk    > calMaxJerk) calMaxJerk = jerk;

      if (millis() - calStart >= CAL_DURATION_MS) {
        // Capped calibration bounds so routine movement cannot over-inflate thresholds
        IMPACT_THRESHOLD = constrain(calMaxMag  * 1.15f, 11.0f, 13.0f);
        JERK_THRESHOLD   = constrain(calMaxJerk * 1.15f, 180.0f, 260.0f);
        state = NORMAL;
        calDoneBuzzerActive = true;
        calDoneBuzzerStart  = millis();
        logEvent("CALIBRATION_DONE,impactThresh=" + String(IMPACT_THRESHOLD, 1) +
                 ",jerkThresh=" + String(JERK_THRESHOLD, 1));
        Serial.println("EVENT," + String(millis()) +
                       ",CALIBRATION_DONE," +
                       String(IMPACT_THRESHOLD, 1) + "," +
                       String(JERK_THRESHOLD, 1));
      }
      break;
    }

    case NORMAL: {
      buzzerOff();

      // Path 1: Standard Free-fall dip detection
      if (liveMag < FREE_FALL_MAG) {
        if (!inFFDip) { inFFDip = true; ffStart = millis(); }
        if (millis() - ffStart >= FF_MIN_MS) {
          state = FREE_FALL;
#if USE_INTEGRITY
          validCandidateActive = true;
#endif
        }
      } else {
        inFFDip = false;
      }

      // Path 2: Direct High-Jerk Trigger (for sudden slips/trips without sustained zero-g dip)
#if USE_PATH2
      if (jerk >= JERK_PATH2 && liveMag >= MAG_PATH2) {
#if USE_GNB
        extractFeatures(lastFeatures); // Snapshot impact pulse immediately
#endif
        impactTime     = millis();
        a_peak         = liveMag;
        J_peak         = jerk;
        stillnessStart = 0;
#if USE_INTEGRITY
        stillnessAccumMs = 0;
        validCandidateActive = true;
#endif
        inFFDip        = false;
        state          = IMPACT;
      }
#endif
      break;
    }

    case FREE_FALL: {
      // Use 150ms rolling peak jerk to eliminate phase mismatch between jerk and magnitude
      float recentMaxJerk = getRecentMaxJerk(150);

      if (liveMag >= IMPACT_THRESHOLD && (jerk >= JERK_THRESHOLD || recentMaxJerk >= JERK_THRESHOLD)) {
#if USE_GNB
        extractFeatures(lastFeatures); // Snapshot impact pulse immediately
#endif
        impactTime     = millis();
        a_peak         = liveMag;
        J_peak         = (jerk > recentMaxJerk) ? jerk : recentMaxJerk;
        stillnessStart = 0;
#if USE_INTEGRITY
        stillnessAccumMs = 0;
#endif
        inFFDip        = false;
        state          = IMPACT;
      } else if (millis() - ffStart >= FF_WINDOW_MS) {
        resetToNormal("FF_TIMEOUT_NO_IMPACT");
      }
      break;
    }

    case IMPACT: {
      if (quickPress) {
#if USE_GNB
        theta = fminf(THETA_MAX, theta + THETA_UP);
#endif
        resetToNormal("CANCEL_USER_IMPACT");
        break;
      }

      if (liveMag > a_peak) a_peak = liveMag;
      if (jerk > J_peak)    J_peak = jerk;

      unsigned long timeSinceImpact = millis() - impactTime;

#if USE_ADAPTIVE
      unsigned long current_still_ms = (unsigned long)(1500.0f + 1500.0f * (E_k / (E_k + E0)));
#else
      unsigned long current_still_ms = STILL_MS;
#endif

      // Independent Stillness Timer
#if USE_INTEGRITY
      unsigned long dt_ms = dt_us / 1000UL;
      if (dt_ms == 0) dt_ms = SAMPLE_INTERVAL_MS;
      if (current_vk > 0.5f && isStill) {
        stillnessAccumMs += dt_ms;
        if (stillnessAccumMs >= current_still_ms) {
#if USE_GNB
          lastClassConf = classify(lastFeatures); // Classify the snapshotted impact
          state = CANDIDATE_FALL;
#else
          state = FALL_CONFIRMED;
          confirmedTime = millis();
          logEvent("FALL_CONFIRMED,rule_engine_only");
          Serial.println("EVENT," + String(millis()) + ",FALL_CONFIRMED,1.000");
#endif
          stillnessAccumMs = 0;
          break;
        }
      } else if (!isStill) {
        stillnessAccumMs = 0; // Reset stillness clock if moving
      }
#else
      if (isStill) {
        if (stillnessStart == 0) stillnessStart = millis();
        if (millis() - stillnessStart >= current_still_ms) {
#if USE_GNB
          lastClassConf = classify(lastFeatures); // Classify the snapshotted impact
          state = CANDIDATE_FALL;
#else
          state = FALL_CONFIRMED;
          confirmedTime = millis();
          logEvent("FALL_CONFIRMED,rule_engine_only");
          Serial.println("EVENT," + String(millis()) + ",FALL_CONFIRMED,1.000");
#endif
          break;
        }
      } else {
        stillnessStart = 0; // Reset stillness clock if moving
      }
#endif

      // Movement Failsafe: if actively moving 5s after impact, reset to normal
      if (timeSinceImpact >= 5000 && !isStill) {
        resetToNormal("IMPACT_TIMEOUT_MOVING");
      }
      break;
    }

    case CANDIDATE_FALL: {
#if USE_GNB
#if USE_INTEGRITY
      if (sensorFault) {
        // Suppress FALL_CONFIRMED during sensor fault
        resetToNormal("SUPPRESSED_FAULT");
        break;
      }
#endif
      bool classifierTrained = (fallStats.count >= 3 && nonFallStats.count >= 3);
      if (!classifierTrained || lastClassConf >= theta) {
        state         = FALL_CONFIRMED;
        confirmedTime = millis();
        logEvent("FALL_CONFIRMED,conf=" + String(lastClassConf, 3) +
                 ",theta=" + String(theta, 2) +
                 (classifierTrained ? "" : ",untrained=1"));
        Serial.println("EVENT," + String(millis()) +
                       ",FALL_CONFIRMED," + String(lastClassConf, 3));
      } else {
        theta = fminf(THETA_MAX, theta + THETA_UP * 0.5f);
        resetToNormal("CLASSIFIER_REJECTED,conf=" + String(lastClassConf, 3));
      }
#else
      state = FALL_CONFIRMED;
      confirmedTime = millis();
#endif
      break;
    }

    case FALL_CONFIRMED: {
#if USE_INTEGRITY
      if (sensorFault) {
        // Suppress ALERT_SENT during sensor fault
        break;
      }
#endif
      playPattern(PAT_CANCEL, PAT_CANCEL_N);
      if (quickPress) {
#if USE_GNB
        theta = fminf(THETA_MAX, theta + THETA_UP);
        logEvent("CANCEL_USER,theta=" + String(theta, 2));
#else
        logEvent("CANCEL_USER");
#endif
        resetToNormal("CANCEL_USER");
        break;
      }
#if USE_ADAPTIVE
      float m = fminf(a_peak / IMPACT_THRESHOLD, J_peak / JERK_THRESHOLD);
      float m_clip = constrain((m - 1.0f) / 0.5f, 0.0f, 1.0f);
      unsigned long current_cancel_ms = (unsigned long)(8000.0f - 5000.0f * m_clip);
#else
      unsigned long current_cancel_ms = CANCEL_MS;
#endif

      if (millis() - confirmedTime >= current_cancel_ms) {
        state = ALERT_SENT;
#if USE_GNB
        theta = fmaxf(THETA_MIN, theta - THETA_DOWN);
        logEvent("ALERT_SENT,theta=" + String(theta, 2) +
                 ",fallCount=" + String(fallStats.count) +
                 ",nonFallCount=" + String(nonFallStats.count));
#else
        logEvent("ALERT_SENT");
#endif
        Serial.println("EVENT," + String(millis()) + ",ALERT_SENT");
      }
      break;
    }

    case ALERT_SENT: {
      playPattern(PAT_ALERT, PAT_ALERT_N);
      if (holdPress) {
        resetToNormal("ALERT_ACKNOWLEDGED");
      }
      break;
    }
  }
}

// ============================================================================
//  WEB DASHBOARD
// ============================================================================

String stateStr() {
  switch (state) {
    case CALIBRATING:    return "Calibrating - sit/stand/walk normally";
    case NORMAL:         return "Normal";
    case FREE_FALL:      return "Free-fall detected...";
    case IMPACT:         return "Impact detected - checking stillness...";
    case CANDIDATE_FALL: return "Classifying...";
    case FALL_CONFIRMED: return "FALL CONFIRMED - press button to cancel";
    case ALERT_SENT:     return "ALERT SENT - hold button to acknowledge";
  }
  return "Unknown";
}

String buildEventLogJSON() {
  String out = "[";
  for (int i = 0; i < LOG_SIZE; i++) {
    int idx = (logHead - 1 - i + LOG_SIZE) % LOG_SIZE;
    if (eventLog[idx].length() == 0) continue;
    if (out.length() > 1) out += ",";
    out += "\"" + eventLog[idx] + "\"";
  }
  out += "]";
  return out;
}

#if USE_WIFI
const char DASHBOARD[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>FallGuard v2</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',Arial,sans-serif;background:#0f172a;color:#e2e8f0;padding:16px}
    h1{font-size:1.5rem;font-weight:700;color:#38bdf8;margin-bottom:14px;letter-spacing:.5px}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:14px}
    .card{background:#1e293b;border-radius:10px;padding:16px;box-shadow:0 2px 8px rgba(0,0,0,.4)}
    .label{font-size:.7rem;text-transform:uppercase;color:#94a3b8;letter-spacing:1px;margin-bottom:6px}
    .value{font-size:1.6rem;font-weight:700;color:#38bdf8}
    .status{font-size:1rem;font-weight:600}
    .s-normal{color:#4ade80}.s-warn{color:#fb923c}.s-danger{color:#f87171}.s-cal{color:#60a5fa}
    canvas{width:100%!important;height:200px!important}
    .chart-card{background:#1e293b;border-radius:10px;padding:16px;margin-bottom:12px}
    .log-card{background:#1e293b;border-radius:10px;padding:16px;margin-bottom:12px}
    .log-entry{font-family:monospace;font-size:.75rem;color:#94a3b8;padding:2px 0;border-bottom:1px solid #334155}
    .log-entry:first-child{color:#e2e8f0}
    .row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
    button{background:#dc2626;color:#fff;border:none;padding:12px 20px;border-radius:8px;font-size:.9rem;cursor:pointer;font-weight:600;flex:1}
    button:hover{background:#b91c1c}
    .btn-ack{background:#1d4ed8}.btn-ack:hover{background:#1e40af}
    input[type=range]{width:100%;accent-color:#38bdf8}
    .theta-val{font-size:1.1rem;font-weight:700;color:#38bdf8;margin-left:8px}
  </style>
</head>
<body>
<h1>FallGuard v2 - Live Dashboard</h1>

<div class="grid">
  <div class="card"><div class="label">Status</div>
    <div class="status" id="status">Loading...</div></div>
  <div class="card"><div class="label">Acceleration (m/s2)</div>
    <div class="value" id="accel">--</div></div>
  <div class="card"><div class="label">Jerk (m/s3)</div>
    <div class="value" id="jerk">--</div></div>
  <div class="card"><div class="label">Classifier Confidence</div>
    <div class="value" id="conf">--</div></div>
  <div class="card"><div class="label">Activity</div>
    <div class="value" id="activity">--</div></div>
  <div class="card"><div class="label">Falls Learned / Non-Falls</div>
    <div class="value" id="counts">--</div></div>
  <div class="card"><div class="label">Adaptive Threshold theta</div>
    <div class="value" id="theta">--</div></div>
  <div class="card"><div class="label">Impact / Jerk Thresholds</div>
    <div class="value" id="thresholds" style="font-size:1rem">--</div></div>
</div>

<div class="chart-card">
  <div class="label" style="margin-bottom:10px">Live Signal - Magnitude &amp; Jerk</div>
  <canvas id="sigChart"></canvas>
</div>

<div class="chart-card">
  <div class="label" style="margin-bottom:10px">Classifier Confidence History</div>
  <canvas id="confChart"></canvas>
</div>

<div class="log-card">
  <div class="label" style="margin-bottom:8px">Event Log</div>
  <div id="logList"></div>
</div>

<div class="card" style="margin-bottom:12px">
  <div class="label">Manual theta Nudge (adjust sensitivity)</div>
  <div style="display:flex;align-items:center;margin-top:10px">
    <input type="range" id="thetaSlider" min="50" max="85" value="65"
           oninput="setTheta(this.value/100)">
    <span class="theta-val" id="sliderVal">0.65</span>
  </div>
</div>

<div class="row">
  <button onclick="cancelAlert()">Cancel / False Alarm</button>
  <button class="btn-ack" onclick="ackAlert()">Acknowledge Alert</button>
</div>

<script>
const MAX_PTS = 150;
const labels = [];
const magData = [], jerkData = [], confData = [];

const mkChart = (id, datasets, yLabel) => {
  return new Chart(document.getElementById(id), {
    type: 'line',
    data: { labels, datasets },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 } } } },
      scales: {
        x: { ticks: { color: '#64748b', maxTicksLimit: 8 }, grid: { color: '#1e293b' } },
        y: { ticks: { color: '#94a3b8' }, grid: { color: '#334155' },
             title: { display: true, text: yLabel, color: '#94a3b8' } }
      }
    }
  });
};

const sigChart = mkChart('sigChart', [
  { label: 'Magnitude (m/s2)', data: magData,  borderColor: '#38bdf8', borderWidth: 1.5, pointRadius: 0, tension: 0.3 },
  { label: 'Jerk /10 (m/s3)', data: jerkData, borderColor: '#fb923c', borderWidth: 1.5, pointRadius: 0, tension: 0.3 }
], 'm/s2');

const confChart = mkChart('confChart', [
  { label: 'Classifier Conf', data: confData, borderColor: '#a78bfa', borderWidth: 1.5, pointRadius: 2, tension: 0.3, fill: { target: 'origin', above: 'rgba(167,139,250,.10)' } }
], 'P(FALL)');

const push = (arr, val) => { arr.push(val); if (arr.length > MAX_PTS) arr.shift(); };

const stateClass = (s) => {
  if (s.includes('ALERT')) return 's-danger';
  if (s.includes('FALL') || s.includes('Impact') || s.includes('Class')) return 's-warn';
  if (s.includes('Calib')) return 's-cal';
  return 's-normal';
};

const refresh = async () => {
  try {
    const r = await fetch('/data'); const d = await r.json();
    const t = new Date().toLocaleTimeString();
    push(labels, t);
    push(magData,  d.mag);
    push(jerkData, d.jerk / 10);
    push(confData, d.conf >= 0 ? d.conf : null);
    sigChart.update('none');
    confChart.update('none');

    const el = document.getElementById('status');
    el.textContent = d.state;
    el.className = 'status ' + stateClass(d.state);

    document.getElementById('accel').textContent    = d.mag.toFixed(2);
    document.getElementById('jerk').textContent     = d.jerk.toFixed(1);
    document.getElementById('conf').textContent     = d.conf >= 0 ? (d.conf*100).toFixed(1)+'%' : 'N/A';
    document.getElementById('activity').textContent = d.activity;
    document.getElementById('counts').textContent   = d.fallCount + ' / ' + d.nonFallCount;
    document.getElementById('theta').textContent    = d.theta.toFixed(2);
    document.getElementById('thresholds').textContent =
      'Impact: ' + d.impThresh.toFixed(1) + ' | Jerk: ' + d.jerkThresh.toFixed(0);

    if (!document.getElementById('thetaSlider').matches(':active')) {
      document.getElementById('thetaSlider').value = Math.round(d.theta * 100);
      document.getElementById('sliderVal').textContent = d.theta.toFixed(2);
    }

    let logHtml = '';
    d.log.forEach((e) => {
      logHtml += `<div class="log-entry">${e}</div>`;
    });
    document.getElementById('logList').innerHTML = logHtml;
  } catch(e) {
    document.getElementById('status').textContent = 'Connection lost...';
    document.getElementById('status').className = 'status s-danger';
  }
};

const cancelAlert = async () => { await fetch('/cancel'); };
const ackAlert    = async () => { await fetch('/ack'); };
const setTheta    = async (v) => {
  document.getElementById('sliderVal').textContent = v.toFixed(2);
  await fetch('/setTheta?v=' + v);
};

setInterval(refresh, 500);
refresh();
</script>
</body>
</html>
)rawliteral";

// ============================================================================
//  WEB SERVER HANDLERS
// ============================================================================

void handleRoot() {
  server.send_P(200, "text/html", DASHBOARD);
}

void handleData() {
  String evJSON = buildEventLogJSON();

  String json = "{";
  json += "\"state\":\"" + stateStr() + "\",";
  json += "\"mag\":"      + String(liveMag,       2) + ",";
  json += "\"jerk\":"     + String(liveJerk,      1) + ",";
#if USE_GNB
  json += "\"conf\":"     + String(lastClassConf, 3) + ",";
  json += "\"fallCount\":"   + String(fallStats.count)    + ",";
  json += "\"nonFallCount\":" + String(nonFallStats.count) + ",";
  json += "\"theta\":"       + String(theta,         3) + ",";
#else
  json += "\"conf\":0.0,";
  json += "\"fallCount\":0,";
  json += "\"nonFallCount\":0,";
  json += "\"theta\":0.65,";
#endif
  json += "\"activity\":\"" + activityName() + "\",";
  json += "\"impThresh\":"   + String(IMPACT_THRESHOLD, 1) + ",";
  json += "\"jerkThresh\":"  + String(JERK_THRESHOLD,   1) + ",";
  json += "\"trialId\":\""   + (currentTrialId.length() > 0 ? currentTrialId : "NONE") + "\",";
#if USE_INTEGRITY
  json += "\"Q\":"           + String(Q, 2) + ",";
  json += "\"sensorState\":\"" + String(sensorFault ? "FAULT" : "OK") + "\",";
#else
  json += "\"Q\":1.00,";
  json += "\"sensorState\":\"OK\",";
#endif
  json += "\"E\":"           + String(E_k, 2) + ",";
  json += "\"log\":"         + evJSON;
  json += "}";
  server.send(200, "application/json", json);
}

void handleCancel() {
  remoteCancelReq = true;
  server.send(200, "text/plain", "OK");
}

void handleAck() {
  if (state == ALERT_SENT) {
    resetToNormal("ALERT_ACKNOWLEDGED_REMOTE");
  }
  server.send(200, "text/plain", "OK");
}

void handleSetTheta() {
  if (server.hasArg("v")) {
    float v = server.arg("v").toFloat();
    theta = constrain(v, THETA_MIN, THETA_MAX);
    logEvent("THETA_SET_MANUAL=" + String(theta, 2));
  }
  server.send(200, "text/plain", "OK");
}
#endif // USE_WIFI

// ============================================================================
//  SETUP
// ============================================================================

void setup() {
  Serial.begin(115200);
  Serial.println("\n=== FallGuard v2 Booting ===");

  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  buzzerOff();

  for (int i = 0; i < SMA_SIZE; i++) smaBuffer[i] = 9.81f;
  smaSum = 9.81f * SMA_SIZE;

  for (int i = 0; i < WINDOW_SIZE; i++) { winMag[i] = 9.81f; winJerk[i] = 0; }

  for (int i = 0; i < LOG_SIZE; i++) eventLog[i] = "";

#if USE_GNB
  initClassStats(fallStats);
  initClassStats(nonFallStats);
#endif

  // MPU6050 wake up (register 0x6B = 0)
  Wire.begin(21, 22);
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x6B);
  Wire.write(0x00);
  Wire.endTransmission(true);

  // ±2g clips at impact; ±8g needed per IEEE paper Section III-A
  // Register 0x1C = 0x10 sets full-scale range to ±8g (AFS_SEL=2, 4096 LSB/g)
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x1C);
  Wire.write(0x10);
  Wire.endTransmission(true);
  Serial.println("MPU6050 configured to ±8g range (scale factor 4096 LSB/g).");

#if USE_INTEGRITY
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x75); // WHO_AM_I register
  Wire.endTransmission(false);
  Wire.requestFrom(MPU_ADDR, 1, true);
  uint8_t whoami = Wire.available() ? Wire.read() : 0;
  Serial.print("MPU6050 WHO_AM_I (0x75): 0x");
  Serial.println(whoami, HEX);
#endif

#if USE_WIFI
  Serial.print("Connecting to WiFi");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 20) {
    delay(500); Serial.print("."); tries++;
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi connected! Dashboard: http://" + WiFi.localIP().toString());
  } else {
    Serial.println("\nWiFi failed - running offline (buzzer only)");
  }

  server.on("/",         handleRoot);
  server.on("/data",     handleData);
  server.on("/cancel",   handleCancel);
  server.on("/ack",      handleAck);
  server.on("/setTheta", handleSetTheta);
  server.begin();
#else
  Serial.println("WiFi disabled by configuration (USE_WIFI=0).");
#endif

  calStart = millis();
  state    = CALIBRATING;
  Serial.println("\n=== CALIBRATION PHASE (20 seconds) ===");
  Serial.println("Please sit down, stand up, and walk a few steps.");
  Serial.println("FORMAT: DATA,<ms>,<mag>,<jerk>,<phase>,<conf>,<activity>,<label>");
}

// ============================================================================
//  LOOP
// ============================================================================

void loop() {
  unsigned long loopStartUs = micros();

  checkSerialCommands();

  unsigned long now_us = micros();
  if (now_us - lastSampleUs >= (unsigned long)SAMPLE_INTERVAL_MS * 1000UL) {
    unsigned long dt_us = (lastSampleUs == 0) ? (SAMPLE_INTERVAL_MS * 1000UL) : (now_us - lastSampleUs);
    lastSampleUs = now_us;
    statSampleCount++;
    runFallDetection(dt_us);
  }

#if USE_INTEGRITY
  if (sensorFault) {
    playPattern(PAT_FAULT, PAT_FAULT_N);
  }
#endif

  if (calDoneBuzzerActive) {
    if (millis() - calDoneBuzzerStart < CAL_DONE_BUZZER_DURATION) {
      playPattern(PAT_CAL, PAT_CAL_N);
    } else {
      buzzerOff();
      calDoneBuzzerActive = false;
    }
  }

#if USE_WIFI
  server.handleClient();
#endif

  // --- Performance Statistics Reporting (every 5 seconds) ---
  unsigned long loopDurationUs = micros() - loopStartUs;
  statLoopCount++;
  statLoopUsSum += loopDurationUs;
  if (loopDurationUs > statLoopUsMax) {
    statLoopUsMax = loopDurationUs;
  }

  if (millis() - lastStatPrintMs >= 5000) {
    float loop_us_mean = statLoopCount > 0 ? (float)statLoopUsSum / statLoopCount : 0.0f;
    unsigned long loop_us_max = statLoopUsMax;
    float dt_s = (millis() - lastStatPrintMs) / 1000.0f;
    float actual_hz = dt_s > 0 ? (float)statSampleCount / dt_s : 0.0f;
    uint32_t free_heap = ESP.getFreeHeap();
    uint32_t min_free_heap = ESP.getMinFreeHeap();

    Serial.print("# STAT,");
    Serial.print(loop_us_mean, 1); Serial.print(",");
    Serial.print(loop_us_max);      Serial.print(",");
    Serial.print(actual_hz, 1);     Serial.print(",");
    Serial.print(free_heap);        Serial.print(",");
    Serial.println(min_free_heap);

    lastStatPrintMs = millis();
    statLoopCount = 0;
    statSampleCount = 0;
    statLoopUsSum = 0;
    statLoopUsMax = 0;
  }
}
