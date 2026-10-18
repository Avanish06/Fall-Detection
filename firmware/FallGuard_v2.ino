/*
  ============================================================
  FallGuard v2 — Adaptive Wearable Fall Detection System
  ============================================================
  Target:    ESP32 (waist-mounted)
  Sensors:   MPU6050 (6-DOF IMU)
  Actuators: Passive buzzer, push button
  Network:   WiFi + HTTP web dashboard (Chart.js live graphs)

  Architecture layers:
    1. Signal Layer    — SMA filter, jerk, 25-sample sliding window
    2. Feature Layer   — 6 discriminative features per window
    3. Rule Engine     — Free-fall → Impact (jerk-gated) → Stillness FSM
    4. Classifier      — Online Gaussian Naive Bayes (Welford's algorithm)
    5. Activity Layer  — SITTING / WALKING / RUNNING inference
    6. Adaptive Layer  — Confidence threshold θ updated from user feedback
    7. Alert Layer     — Buzzer patterns + WiFi dashboard + event log

  Key novelty:
    • Jerk discriminator: falls produce a sharp jerk spike (>500 m/s³);
      sitting/jumping produce smooth jerk (<300 m/s³) — rejected at rule-engine
    • Online GNB uses Welford's running mean/variance: O(1) time, O(1) memory.
      Classifier gets smarter every time the user confirms or cancels an event.
    • Adaptive θ: false-alarm → θ rises (stricter); confirmed fall → θ falls
      (more sensitive). Bounded to [0.50, 0.85].

  Wiring:
    MPU6050  VCC → 3.3V    GND → GND
             SDA → GPIO21  SCL → GPIO22
    Buzzer   (+) → GPIO5   (-) → GND
    Button       → GPIO2   (other leg → GND, INPUT_PULLUP)

  Serial output (for logger.py) every SAMPLE_INTERVAL_MS during NORMAL mode:
    "DATA,<ms>,<mag>,<jerk>,<phase>,<conf>,<activity>,<event>"
  Event lines (on state transitions):
    "EVENT,<ms>,FALL_CONFIRMED,<conf>"
    "EVENT,<ms>,ALERT_SENT"
    "EVENT,<ms>,CANCEL_USER"
    "EVENT,<ms>,CANCEL_REMOTE"
    "EVENT,<ms>,CALIBRATION_DONE,<threshold>,<jerkThresh>"

  Author:  FallGuard Team
  Version: 2.0.0
  ============================================================
*/

#include <WiFi.h>
#include <WebServer.h>
#include <Wire.h>
#include <math.h>

// ─── WiFi credentials ──────────────────────────────────────────────────────
const char* WIFI_SSID     = "PingNaMileDubara";
const char* WIFI_PASSWORD = "123avanish";

// ─── Pin assignments ────────────────────────────────────────────────────────
const int MPU_ADDR   = 0x68;
const int BUZZER_PIN = 5;
const int BUTTON_PIN = 2;

// ─── Sampling ───────────────────────────────────────────────────────────────
const int    SAMPLE_RATE_HZ      = 50;               // Hz
const int    SAMPLE_INTERVAL_MS  = 1000 / SAMPLE_RATE_HZ; // 20 ms
const int    WINDOW_SIZE         = 25;               // 500 ms window

// ─── SMA filter ─────────────────────────────────────────────────────────────
const int    SMA_SIZE            = 5;

// ─── Phase timings ──────────────────────────────────────────────────────────
const float  FREE_FALL_MAG       = 5.0f;   // m/s² — downward acceleration dip (your fall dipped to ~3.5; sitting stays > 8.0)
const unsigned long FF_MIN_MS    = 60;     // free-fall must last this long (60ms)
const unsigned long FF_WINDOW_MS = 1400;   // impact must follow within this window (1.4s)

const float  STILL_LOW           = 7.5f;   // m/s² — stillness band widened slightly for bed/soft surfaces
const float  STILL_HIGH          = 12.0f;
const unsigned long STILL_MS     = 1800;   // 1.8s sustained stillness

const unsigned long CANCEL_MS    = 5000;   // user cancel window after confirm

// ─── Jerk threshold (m/s³ at 50 Hz) ────────────────────────────────────────
// Falls: jerk > 220 m/s³; jumping: jerk <= 180 m/s³; sitting: jerk <= 100 m/s³
float JERK_THRESHOLD   = 220.0f;

// ─── Personal impact threshold (m/s²) ──────────────────────────────────────
float IMPACT_THRESHOLD = 11.5f;    // default; overwritten after calibration

// ─── Adaptive confidence threshold θ ────────────────────────────────────────
float theta = 0.65f;
const float THETA_MAX  = 0.85f;
const float THETA_MIN  = 0.50f;
const float THETA_UP   = 0.02f;   // false-alarm → stricter
const float THETA_DOWN = 0.01f;   // confirmed fall → slightly more sensitive

// ─── Calibration ────────────────────────────────────────────────────────────
const unsigned long CAL_DURATION_MS = 20000; // 20 s
float calMaxMag  = 0.0f;
float calMaxJerk = 0.0f;
unsigned long calStart = 0;

// ─── State machine ──────────────────────────────────────────────────────────
enum FallState {
  CALIBRATING,
  NORMAL,
  FREE_FALL,
  IMPACT,
  CANDIDATE_FALL,   // classifier is voting
  FALL_CONFIRMED,   // 5-second cancel window
  ALERT_SENT
};
FallState state = CALIBRATING;

// ─── Sliding window ─────────────────────────────────────────────────────────
float winMag[WINDOW_SIZE];
float winJerk[WINDOW_SIZE];
int   winHead    = 0;
bool  winFull    = false;

// ─── SMA filter ring buffer ──────────────────────────────────────────────────
float smaBuffer[SMA_SIZE];
int   smaHead    = 0;
float smaSum     = 0.0f;

// ─── Activity inference ─────────────────────────────────────────────────────
enum Activity { ACT_STILL, ACT_WALKING, ACT_RUNNING };
Activity currentActivity = ACT_STILL;

// Rolling baseline for activity inference (Welford's, online)
long  actCount = 0;
float actMeanMag  = 9.81f;
float actM2Mag    = 0.0f;
float actVarMag   = 0.0f;

// ─── Online Gaussian Naive Bayes classifier ─────────────────────────────────
// Features (6):
//  [0] mean_mag        — average magnitude in window
//  [1] max_mag         — peak impact
//  [2] min_mag         — free-fall dip
//  [3] var_mag         — signal variance
//  [4] peak_to_valley  — max_mag / max(min_mag, 0.1)
//  [5] max_jerk        — sharpness of impact spike
const int N_FEATURES = 6;

struct ClassStats {
  long  count;
  float mean[N_FEATURES];
  float M2[N_FEATURES];     // sum of squared deviations (Welford)
  float var[N_FEATURES];    // running variance
};

ClassStats fallStats;
ClassStats nonFallStats;

// Classifier last result
float lastClassConf = -1.0f; // -1 = not yet run
float lastFeatures[N_FEATURES];

// ─── Event log (ring buffer, last 12 events) ────────────────────────────────
const int LOG_SIZE = 12;
String eventLog[LOG_SIZE];
int    logHead = 0;

// ─── Timing bookmarks ───────────────────────────────────────────────────────
unsigned long ffStart       = 0;
unsigned long impactTime    = 0;
unsigned long confirmedTime = 0;
unsigned long lastSampleMs  = 0;

bool inFFDip = false;

// ─── Button state ───────────────────────────────────────────────────────────
bool lastRaw = HIGH, debounced = HIGH;
unsigned long debounceTs = 0;
unsigned long pressStart = 0;
bool holdTriggered  = false;
bool quickPress     = false;
bool holdPress      = false;
const unsigned long DEBOUNCE_MS   = 50;
const unsigned long HOLD_PRESS_MS = 1500;

// ─── Remote cancel (from dashboard) ────────────────────────────────────────
volatile bool remoteCancelReq = false;

// ─── Web server ─────────────────────────────────────────────────────────────
WebServer server(80);

// ─── Live data for dashboard JSON ───────────────────────────────────────────
float liveMag  = 9.81f;
float liveJerk = 0.0f;

// ════════════════════════════════════════════════════════════════════════════
//  UTILITY — MPU6050 raw read
// ════════════════════════════════════════════════════════════════════════════

float readRawMagnitude() {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x3B);
  Wire.endTransmission(false);
  Wire.requestFrom(MPU_ADDR, 6, true);
  int16_t rx = (int16_t)(Wire.read() << 8 | Wire.read());
  int16_t ry = (int16_t)(Wire.read() << 8 | Wire.read());
  int16_t rz = (int16_t)(Wire.read() << 8 | Wire.read());
  float ax = rx / 16384.0f;
  float ay = ry / 16384.0f;
  float az = rz / 16384.0f;
  return sqrtf(ax*ax + ay*ay + az*az) * 9.81f; // convert g → m/s²
}

// ════════════════════════════════════════════════════════════════════════════
//  SIGNAL LAYER — SMA filter + jerk
// ════════════════════════════════════════════════════════════════════════════

float applySmoothing(float raw) {
  smaSum -= smaBuffer[smaHead];
  smaBuffer[smaHead] = raw;
  smaSum += raw;
  smaHead = (smaHead + 1) % SMA_SIZE;
  return smaSum / SMA_SIZE;
}

// Returns smoothed magnitude; updates global liveJerk
float readSmoothed(float prevMag) {
  float raw     = readRawMagnitude();
  float smooth  = applySmoothing(raw);
  // jerk = |Δmag / Δt|; Δt = SAMPLE_INTERVAL_MS / 1000
  liveJerk = fabsf(smooth - prevMag) / (SAMPLE_INTERVAL_MS / 1000.0f);
  return smooth;
}

// Push a sample into the sliding window
void pushWindow(float mag, float jerk) {
  winMag[winHead]  = mag;
  winJerk[winHead] = jerk;
  winHead = (winHead + 1) % WINDOW_SIZE;
  if (winHead == 0) winFull = true;
}

// ════════════════════════════════════════════════════════════════════════════
//  FEATURE LAYER — extract 6 features from current window
// ════════════════════════════════════════════════════════════════════════════

void extractFeatures(float feats[N_FEATURES]) {
  int n = winFull ? WINDOW_SIZE : winHead;
  if (n < 2) { for (int i=0;i<N_FEATURES;i++) feats[i]=0; return; }

  float sum=0, mx=-1e9f, mn=1e9f, mxJ=0;
  for (int i=0; i<n; i++) {
    sum += winMag[i];
    if (winMag[i]  > mx) mx  = winMag[i];
    if (winMag[i]  < mn) mn  = winMag[i];
    if (winJerk[i] > mxJ) mxJ = winJerk[i];
  }
  float mean = sum / n;

  float varAcc = 0;
  for (int i=0; i<n; i++) {
    float d = winMag[i] - mean;
    varAcc += d*d;
  }
  float var = varAcc / (n - 1);

  feats[0] = mean;
  feats[1] = mx;
  feats[2] = mn;
  feats[3] = var;
  feats[4] = mx / fmaxf(mn, 0.1f);  // peak-to-valley ratio
  feats[5] = mxJ;                   // max jerk in window
}

// ════════════════════════════════════════════════════════════════════════════
//  ONLINE GAUSSIAN NAIVE BAYES — Welford's incremental update
// ════════════════════════════════════════════════════════════════════════════

void initClassStats(ClassStats& cs) {
  cs.count = 0;
  for (int i=0; i<N_FEATURES; i++) {
    cs.mean[i] = 0;
    cs.M2[i]   = 0;
    cs.var[i]  = 1.0f; // small prior variance to avoid div-by-zero
  }
}

/**
 * Welford's Online Algorithm — incremental mean & variance update.
 * Mathematically equivalent to batch computation, O(1) time and space.
 * Reference: Welford, B.P. (1962). "Note on a method for calculating
 * corrected sums of squares and products." Technometrics, 4(3), 419-420.
 */
void updateClassStats(ClassStats& cs, float feats[N_FEATURES]) {
  cs.count++;
  for (int i=0; i<N_FEATURES; i++) {
    float delta  = feats[i] - cs.mean[i];
    cs.mean[i]  += delta / cs.count;
    float delta2 = feats[i] - cs.mean[i];
    cs.M2[i]    += delta * delta2;
    cs.var[i]    = (cs.count > 1) ? cs.M2[i] / (cs.count - 1) : 1.0f;
  }
}

/**
 * Log-probability of feature vector given a class distribution.
 * Uses Gaussian assumption: P(x|C) = N(x; μ_C, σ²_C)
 * Computed in log-space for numerical stability.
 */
float logProb(ClassStats& cs, float feats[N_FEATURES]) {
  if (cs.count < 2) return 0.0f;
  // log prior: log P(C) ≈ log count_C / total
  float total = (float)(fallStats.count + nonFallStats.count + 1);
  float logP  = logf((float)cs.count / total + 1e-9f);
  for (int i=0; i<N_FEATURES; i++) {
    float var  = fmaxf(cs.var[i], 0.01f); // floor variance
    float diff = feats[i] - cs.mean[i];
    // log N(x; μ, σ²) = -0.5 * log(2πσ²) - (x-μ)² / (2σ²)
    logP -= 0.5f * logf(2.0f * M_PI * var);
    logP -= (diff * diff) / (2.0f * var);
  }
  return logP;
}

/**
 * Classify features. Returns P(FALL | features) in [0, 1].
 * If not enough training data, returns a prior of 0.75 to lean on
 * the rule engine (which already pre-filtered the event).
 */
float classify(float feats[N_FEATURES]) {
  if (fallStats.count < 3 || nonFallStats.count < 3) {
    return 0.75f; // insufficient data → trust rule engine
  }
  float logFall    = logProb(fallStats,    feats);
  float logNonFall = logProb(nonFallStats, feats);
  // Numeric softmax: subtract max for stability
  float maxL    = fmaxf(logFall, logNonFall);
  float expFall = expf(logFall    - maxL);
  float expNF   = expf(logNonFall - maxL);
  return expFall / (expFall + expNF + 1e-9f);
}

// ════════════════════════════════════════════════════════════════════════════
//  ACTIVITY INFERENCE — Welford's baseline on rolling magnitude
// ════════════════════════════════════════════════════════════════════════════

void updateActivityBaseline(float mag) {
  actCount++;
  float delta  = mag - actMeanMag;
  actMeanMag  += delta / actCount;
  float delta2 = mag - actMeanMag;
  actM2Mag    += delta * delta2;
  actVarMag    = (actCount > 1) ? actM2Mag / (actCount - 1) : 0.0f;
  // Infer activity from variance of the last rolling window
  // High variance → vigorous movement; low → still
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

// ════════════════════════════════════════════════════════════════════════════
//  EVENT LOG
// ════════════════════════════════════════════════════════════════════════════

void logEvent(String msg) {
  String entry = String(millis() / 1000) + "s — " + msg;
  eventLog[logHead] = entry;
  logHead = (logHead + 1) % LOG_SIZE;
  Serial.println("EVENT," + String(millis()) + "," + msg);
}

// ════════════════════════════════════════════════════════════════════════════
//  BUZZER — non-blocking patterns
// ════════════════════════════════════════════════════════════════════════════

// cancelPattern: three short beeps then a long pause
const unsigned long PAT_CANCEL[] = {150, 150, 150, 150, 150, 550};
const int           PAT_CANCEL_N = 6;

// alertPattern: long on, short off
const unsigned long PAT_ALERT[]  = {500, 100};
const int           PAT_ALERT_N  = 2;

// calPattern: two quick chirps (played once)
const unsigned long PAT_CAL[]    = {80, 80, 80, 500};
const int           PAT_CAL_N    = 4;

void playPattern(const unsigned long* pat, int n) {
  unsigned long period = 0;
  for (int i=0; i<n; i++) period += pat[i];
  unsigned long t = millis() % period;
  unsigned long acc = 0;
  for (int i=0; i<n; i++) {
    acc += pat[i];
    if (t < acc) {
      digitalWrite(BUZZER_PIN, (i % 2 == 0) ? HIGH : LOW);
      return;
    }
  }
}

void buzzerOff() { digitalWrite(BUZZER_PIN, LOW); }

// ════════════════════════════════════════════════════════════════════════════
//  BUTTON — debounce + hold detection
// ════════════════════════════════════════════════════════════════════════════

void readButton() {
  quickPress = false;
  holdPress  = false;
  bool raw = digitalRead(BUTTON_PIN);
  if (raw != lastRaw) debounceTs = millis();
  if (millis() - debounceTs > DEBOUNCE_MS) {
    if (raw != debounced) {
      debounced = raw;
      if (debounced == LOW) {
        pressStart   = millis();
        holdTriggered = false;
      } else {
        unsigned long heldFor = millis() - pressStart;
        if (!holdTriggered && heldFor < HOLD_PRESS_MS) quickPress = true;
      }
    }
  }
  if (debounced == LOW && !holdTriggered &&
      millis() - pressStart >= HOLD_PRESS_MS) {
    holdTriggered = true;
    holdPress     = true;
  }
  lastRaw = raw;
}

// ════════════════════════════════════════════════════════════════════════════
//  RESET
// ════════════════════════════════════════════════════════════════════════════

void resetToNormal(String reason) {
  buzzerOff();
  inFFDip    = false;
  state      = NORMAL;
  lastClassConf = -1.0f;
  logEvent(reason);
}

// ════════════════════════════════════════════════════════════════════════════
//  MAIN STATE MACHINE
// ════════════════════════════════════════════════════════════════════════════

void runFallDetection() {
  // --- Sample ---
  float prevMag = liveMag;
  liveMag  = readSmoothed(prevMag);
  float jerk = liveJerk;
  pushWindow(liveMag, jerk);

  // --- Activity baseline (only during NORMAL to avoid polluting with fall data) ---
  if (state == NORMAL || state == CALIBRATING) {
    updateActivityBaseline(liveMag);
  }

  readButton();

  // Read USB Serial commands from laptop dashboard
  while (Serial.available() > 0) {
    String cmd = Serial.readStringUntil('\n');
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
    }
  }

  // Remote cancel from dashboard
  if (remoteCancelReq) {
    remoteCancelReq = false;
    if (state != NORMAL && state != CALIBRATING) {
      if (lastClassConf >= 0) {
        updateClassStats(nonFallStats, lastFeatures);
        theta = fminf(THETA_MAX, theta + THETA_UP);
        logEvent("CANCEL_REMOTE theta=" + String(theta, 2));
      }
      resetToNormal("CANCEL_REMOTE");
    }
  }

  // Stream real-time telemetry over USB Serial for laptop dashboard
  Serial.print("TELEMETRY,");
  Serial.print(millis()); Serial.print(",");
  Serial.print(liveMag,  2); Serial.print(",");
  Serial.print(jerk,     1); Serial.print(",");
  Serial.print(stateStr());   Serial.print(",");
  Serial.print(lastClassConf >= 0 ? lastClassConf : 0.0f, 3); Serial.print(",");
  Serial.print(activityName()); Serial.print(",");
  Serial.print(fallStats.count); Serial.print(",");
  Serial.print(nonFallStats.count); Serial.print(",");
  Serial.print(theta, 3); Serial.print(",");
  Serial.print(IMPACT_THRESHOLD, 1); Serial.print(",");
  Serial.println(JERK_THRESHOLD, 1);

  bool isStill = (liveMag >= STILL_LOW && liveMag <= STILL_HIGH);

  switch (state) {

    // ── CALIBRATING ──────────────────────────────────────────────────────────
    case CALIBRATING: {
      if (liveMag > calMaxMag)  calMaxMag  = liveMag;
      if (jerk    > calMaxJerk) calMaxJerk = jerk;

      if (millis() - calStart >= CAL_DURATION_MS) {
        IMPACT_THRESHOLD = constrain(calMaxMag  * 1.25f, 11.5f, 25.0f);
        JERK_THRESHOLD   = constrain(calMaxJerk * 1.30f, 220.0f, 600.0f);
        state = NORMAL;
        // Play calibration done chirp
        for (int i=0; i<PAT_CAL_N; i++) {
          digitalWrite(BUZZER_PIN, (i%2==0) ? HIGH : LOW);
          delay(PAT_CAL[i]);
        }
        buzzerOff();
        logEvent("CALIBRATION_DONE,impactThresh=" + String(IMPACT_THRESHOLD,1) +
                 ",jerkThresh=" + String(JERK_THRESHOLD,1));
        Serial.println("EVENT," + String(millis()) +
                       ",CALIBRATION_DONE," +
                       String(IMPACT_THRESHOLD,1) + "," +
                       String(JERK_THRESHOLD,1));
      }
      break;
    }

    // ── NORMAL ────────────────────────────────────────────────────────────────
    case NORMAL: {
      buzzerOff();
      if (liveMag < FREE_FALL_MAG) {
        if (!inFFDip) { inFFDip = true; ffStart = millis(); }
        if (millis() - ffStart >= FF_MIN_MS) {
          state = FREE_FALL;
        }
      } else {
        inFFDip = false;
      }
      break;
    }

    // ── FREE_FALL ─────────────────────────────────────────────────────────────
    case FREE_FALL: {
      // Waiting for impact: magnitude spike AND jerk spike
      if (liveMag >= IMPACT_THRESHOLD && jerk >= JERK_THRESHOLD) {
        // Both impact magnitude AND jerk confirmed — this is the key
        // discriminator: jump landings rarely exceed JERK_THRESHOLD
        state      = IMPACT;
        impactTime = millis();
      } else if (millis() - ffStart >= FF_WINDOW_MS) {
        // No qualifying impact within window — false alarm (arm swing, etc.)
        resetToNormal("FF_TIMEOUT_NO_IMPACT");
        inFFDip = false;
      }
      break;
    }

    // ── IMPACT ───────────────────────────────────────────────────────────────
    case IMPACT: {
      if (quickPress) {
        // Early cancel during impact check
        extractFeatures(lastFeatures);
        updateClassStats(nonFallStats, lastFeatures);
        theta = fminf(THETA_MAX, theta + THETA_UP);
        resetToNormal("CANCEL_USER_IMPACT");
        break;
      }
      unsigned long elapsed = millis() - impactTime;
      if (elapsed < STILL_MS) {
        // Not yet enough stillness time — check if still
        if (!isStill) impactTime = millis(); // reset stillness clock if they move
      } else {
        // 2 seconds of sustained stillness after impact — candidate fall
        extractFeatures(lastFeatures);
        lastClassConf = classify(lastFeatures);
        state = CANDIDATE_FALL;
      }
      // Failsafe: if moving around vigorously 5s after impact, reset
      if (elapsed > 5000 && !isStill) {
        extractFeatures(lastFeatures);
        updateClassStats(nonFallStats, lastFeatures);
        resetToNormal("IMPACT_TIMEOUT_MOVING");
      }
      break;
    }

    // ── CANDIDATE_FALL ────────────────────────────────────────────────────────
    case CANDIDATE_FALL: {
      // Classifier vote already in lastClassConf
      if (lastClassConf >= theta) {
        // Classifier agrees with rule engine → confirm fall
        state         = FALL_CONFIRMED;
        confirmedTime = millis();
        logEvent("FALL_CONFIRMED,conf=" + String(lastClassConf, 3) +
                 ",theta=" + String(theta, 2));
        Serial.println("EVENT," + String(millis()) +
                       ",FALL_CONFIRMED," + String(lastClassConf, 3));
      } else {
        // Rule engine triggered but classifier disagrees
        // → log as ambiguous non-fall, update classifier, reset
        updateClassStats(nonFallStats, lastFeatures);
        theta = fminf(THETA_MAX, theta + THETA_UP * 0.5f);
        resetToNormal("CLASSIFIER_REJECTED,conf=" + String(lastClassConf, 3));
      }
      break;
    }

    // ── FALL_CONFIRMED ────────────────────────────────────────────────────────
    case FALL_CONFIRMED: {
      playPattern(PAT_CANCEL, PAT_CANCEL_N);
      if (quickPress) {
        // User cancelled → definitely a false alarm
        updateClassStats(nonFallStats, lastFeatures);
        theta = fminf(THETA_MAX, theta + THETA_UP);
        logEvent("CANCEL_USER,theta=" + String(theta, 2));
        resetToNormal("CANCEL_USER");
        break;
      }
      if (millis() - confirmedTime >= CANCEL_MS) {
        // No cancel → escalate to alert
        state = ALERT_SENT;
        // Update classifier: confirmed fall
        updateClassStats(fallStats, lastFeatures);
        theta = fmaxf(THETA_MIN, theta - THETA_DOWN);
        logEvent("ALERT_SENT,theta=" + String(theta, 2) +
                 ",fallCount=" + String(fallStats.count) +
                 ",nonFallCount=" + String(nonFallStats.count));
        Serial.println("EVENT," + String(millis()) + ",ALERT_SENT");
      }
      break;
    }

    // ── ALERT_SENT ────────────────────────────────────────────────────────────
    case ALERT_SENT: {
      playPattern(PAT_ALERT, PAT_ALERT_N);
      // Hold button to acknowledge and reset
      if (holdPress) {
        resetToNormal("ALERT_ACKNOWLEDGED");
      }
      break;
    }
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  WEB DASHBOARD — HTML with Chart.js live plot
// ════════════════════════════════════════════════════════════════════════════

String stateStr() {
  switch (state) {
    case CALIBRATING:    return "Calibrating — sit/stand/walk normally";
    case NORMAL:         return "Normal";
    case FREE_FALL:      return "Free-fall detected...";
    case IMPACT:         return "Impact detected — checking stillness...";
    case CANDIDATE_FALL: return "Classifying...";
    case FALL_CONFIRMED: return "FALL CONFIRMED — press button to cancel";
    case ALERT_SENT:     return "ALERT SENT — hold button to acknowledge";
  }
  return "Unknown";
}

String buildEventLogJSON() {
  String out = "[";
  // Collect non-empty entries in order
  for (int i=0; i<LOG_SIZE; i++) {
    int idx = (logHead - 1 - i + LOG_SIZE) % LOG_SIZE;
    if (eventLog[idx].length() == 0) continue;
    if (out.length() > 1) out += ",";
    out += "\"" + eventLog[idx] + "\"";
  }
  out += "]";
  return out;
}

// Dashboard HTML — embedded in PROGMEM to save RAM
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
    .badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:.7rem;font-weight:700;margin-left:8px}
    .b-still{background:#334155;color:#94a3b8}
    .b-walk{background:#14532d;color:#4ade80}
    .b-run{background:#7c2d12;color:#fb923c}
  </style>
</head>
<body>
<h1>⛑ FallGuard v2 — Live Dashboard</h1>

<div class="grid">
  <div class="card"><div class="label">Status</div>
    <div class="status" id="status">Loading...</div></div>
  <div class="card"><div class="label">Acceleration (m/s²)</div>
    <div class="value" id="accel">--</div></div>
  <div class="card"><div class="label">Jerk (m/s³)</div>
    <div class="value" id="jerk">--</div></div>
  <div class="card"><div class="label">Classifier Confidence</div>
    <div class="value" id="conf">--</div></div>
  <div class="card"><div class="label">Activity</div>
    <div class="value" id="activity">--</div></div>
  <div class="card"><div class="label">Falls Learned / Non-Falls</div>
    <div class="value" id="counts">--</div></div>
  <div class="card"><div class="label">Adaptive Threshold θ</div>
    <div class="value" id="theta">--</div></div>
  <div class="card"><div class="label">Impact / Jerk Thresholds</div>
    <div class="value" id="thresholds" style="font-size:1rem">--</div></div>
</div>

<div class="chart-card">
  <div class="label" style="margin-bottom:10px">Live Signal — Magnitude &amp; Jerk</div>
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
  <div class="label">Manual θ Nudge (drag to adjust classifier sensitivity)</div>
  <div style="display:flex;align-items:center;margin-top:10px">
    <input type="range" id="thetaSlider" min="50" max="85" value="65"
           oninput="setTheta(this.value/100)">
    <span class="theta-val" id="sliderVal">0.65</span>
  </div>
</div>

<div class="row">
  <button onclick="cancelAlert()">🚫 Cancel / False Alarm</button>
  <button class="btn-ack" onclick="ackAlert()">✅ Acknowledge Alert</button>
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
  { label: 'Magnitude (m/s²)', data: magData,  borderColor: '#38bdf8', borderWidth: 1.5, pointRadius: 0, tension: 0.3 },
  { label: 'Jerk /10 (m/s³)', data: jerkData, borderColor: '#fb923c', borderWidth: 1.5, pointRadius: 0, tension: 0.3 }
], 'm/s²');

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
    push(jerkData, d.jerk / 10); // scaled for readability
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

    // Sync slider if remote changed theta
    if (!document.getElementById('thetaSlider').matches(':active')) {
      document.getElementById('thetaSlider').value = Math.round(d.theta * 100);
      document.getElementById('sliderVal').textContent = d.theta.toFixed(2);
    }

    // Event log
    let logHtml = '';
    d.log.forEach((e,i) => {
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

// ════════════════════════════════════════════════════════════════════════════
//  WEB SERVER HANDLERS
// ════════════════════════════════════════════════════════════════════════════

void handleRoot() {
  server.send_P(200, "text/html", DASHBOARD);
}

void handleData() {
  // Collect recent event log entries
  String evJSON = buildEventLogJSON();

  String json = "{";
  json += "\"state\":\"" + stateStr() + "\",";
  json += "\"mag\":"      + String(liveMag,       2) + ",";
  json += "\"jerk\":"     + String(liveJerk,      1) + ",";
  json += "\"conf\":"     + String(lastClassConf, 3) + ",";
  json += "\"activity\":\"" + activityName() + "\",";
  json += "\"fallCount\":"   + String(fallStats.count)    + ",";
  json += "\"nonFallCount\":" + String(nonFallStats.count) + ",";
  json += "\"theta\":"       + String(theta,         3) + ",";
  json += "\"impThresh\":"   + String(IMPACT_THRESHOLD, 1) + ",";
  json += "\"jerkThresh\":"  + String(JERK_THRESHOLD,   1) + ",";
  json += "\"log\":"         + evJSON;
  json += "}";
  server.send(200, "application/json", json);
}

void handleCancel() {
  remoteCancelReq = true;
  server.send(200, "text/plain", "OK");
}

void handleAck() {
  // Same as hold-button: acknowledge after alert
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

// ════════════════════════════════════════════════════════════════════════════
//  SETUP
// ════════════════════════════════════════════════════════════════════════════

void setup() {
  Serial.begin(115200);
  Serial.println("\n=== FallGuard v2 Booting ===");

  // Pins
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  buzzerOff();

  // Init SMA buffer
  for (int i=0; i<SMA_SIZE;    i++) smaBuffer[i]   = 9.81f;
  smaSum = 9.81f * SMA_SIZE;

  // Init sliding window
  for (int i=0; i<WINDOW_SIZE; i++) { winMag[i]=9.81f; winJerk[i]=0; }

  // Init event log
  for (int i=0; i<LOG_SIZE; i++) eventLog[i] = "";

  // Init classifier
  initClassStats(fallStats);
  initClassStats(nonFallStats);

  // MPU6050 — wake up (register 0x6B = 0)
  Wire.begin(21, 22);
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x6B);
  Wire.write(0x00);
  Wire.endTransmission(true);

  // Optional: set accelerometer range to ±8g (register 0x1C = 0x10)
  // This gives better headroom for detecting high-impact falls
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x1C);
  Wire.write(0x10); // AFS_SEL = 2 → ±8g → scale = 4096 LSB/g
  Wire.endTransmission(true);
  // NOTE: if using ±8g range, update readRawMagnitude divisor from 16384 → 4096
  // For ±2g (default, divisor 16384) comment out the block above.
  // Default ±2g is fine for most falls; use ±8g for athletic/heavy users.

  Serial.print("Connecting to WiFi");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 20) {
    delay(500); Serial.print("."); tries++;
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi connected! Dashboard: http://" + WiFi.localIP().toString());
  } else {
    Serial.println("\nWiFi failed — running offline (buzzer only)");
  }

  // Web routes
  server.on("/",         handleRoot);
  server.on("/data",     handleData);
  server.on("/cancel",   handleCancel);
  server.on("/ack",      handleAck);
  server.on("/setTheta", handleSetTheta);
  server.begin();

  // Begin calibration
  calStart = millis();
  state    = CALIBRATING;
  Serial.println("\n=== CALIBRATION PHASE (20 seconds) ===");
  Serial.println("Please sit down, stand up, and walk a few steps.");
  Serial.println("The system is recording your personal movement baseline.");
  Serial.println("FORMAT: DATA,<ms>,<mag>,<jerk>,<phase>,<conf>,<activity>,<label>");
}

// ════════════════════════════════════════════════════════════════════════════
//  LOOP
// ════════════════════════════════════════════════════════════════════════════

void loop() {
  // Maintain 50 Hz sampling rate
  unsigned long now = millis();
  if (now - lastSampleMs >= SAMPLE_INTERVAL_MS) {
    lastSampleMs = now;
    runFallDetection();
  }
  server.handleClient();
}
