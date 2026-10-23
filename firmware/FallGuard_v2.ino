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

#include <WiFi.h>
#include <WebServer.h>
#include <Wire.h>
#include <math.h>

// --- WiFi credentials ------------------------------------------------------
const char* WIFI_SSID     = "PingNaMileDubara";
const char* WIFI_PASSWORD = "123avanish";

// --- Pin assignments --------------------------------------------------------
const int MPU_ADDR   = 0x68;
const int BUZZER_PIN = 5;
const int BUTTON_PIN = 2;

// --- Sampling ---------------------------------------------------------------
const int    SAMPLE_RATE_HZ      = 50;               // Hz
const int    SAMPLE_INTERVAL_MS  = 1000 / SAMPLE_RATE_HZ; // 20 ms
const int    WINDOW_SIZE         = 25;               // 500 ms window

// --- SMA filter -------------------------------------------------------------
const int    SMA_SIZE            = 5;

// --- Phase timings ----------------------------------------------------------
const float  FREE_FALL_MAG       = 5.0f;   // m/s^2 - downward acceleration dip
const unsigned long FF_MIN_MS    = 40;     // free-fall must last 40ms (2 samples)
const unsigned long FF_WINDOW_MS = 1400;   // impact must follow within this window

const float  STILL_LOW           = 7.5f;   // m/s^2 - stillness band
const float  STILL_HIGH          = 12.0f;
const unsigned long STILL_MS     = 2200;   // must stay still 2.2s (raised from 1800 to reject jumps/stumbles)

const unsigned long CANCEL_MS    = 5000;   // user cancel window after confirm

// --- Jerk threshold (m/s^3 at 50 Hz) ----------------------------------------
float JERK_THRESHOLD   = 220.0f;

// --- Personal impact threshold (m/s^2) --------------------------------------
float IMPACT_THRESHOLD = 11.5f;

// --- Adaptive confidence threshold theta ------------------------------------
float theta = 0.65f;
const float THETA_MAX  = 0.85f;
const float THETA_MIN  = 0.50f;
const float THETA_UP   = 0.02f;
const float THETA_DOWN = 0.01f;

// --- Calibration ------------------------------------------------------------
const unsigned long CAL_DURATION_MS = 20000; // 20 s
float calMaxMag  = 0.0f;
float calMaxJerk = 0.0f;
unsigned long calStart = 0;

// --- State machine ----------------------------------------------------------
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

// --- Sliding window ---------------------------------------------------------
float winMag[WINDOW_SIZE];
float winJerk[WINDOW_SIZE];
int   winHead    = 0;
bool  winFull    = false;

// --- SMA filter ring buffer --------------------------------------------------
float smaBuffer[SMA_SIZE];
int   smaHead    = 0;
float smaSum     = 0.0f;

// --- Activity inference -----------------------------------------------------
enum Activity { ACT_STILL, ACT_WALKING, ACT_RUNNING };
Activity currentActivity = ACT_STILL;

long  actCount = 0;
float actMeanMag  = 9.81f;
float actM2Mag    = 0.0f;
float actVarMag   = 0.0f;

// --- Online Gaussian Naive Bayes classifier ---------------------------------
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

// --- Event log --------------------------------------------------------------
const int LOG_SIZE = 12;
String eventLog[LOG_SIZE];
int    logHead = 0;

// --- Timing bookmarks -------------------------------------------------------
unsigned long ffStart        = 0;
unsigned long impactTime     = 0;
unsigned long stillnessStart = 0;
unsigned long confirmedTime  = 0;
unsigned long lastSampleMs   = 0;

bool inFFDip = false;

// --- Button state -----------------------------------------------------------
bool lastRaw = HIGH, debounced = HIGH;
unsigned long debounceTs = 0;
unsigned long pressStart = 0;
bool holdTriggered  = false;
bool quickPress     = false;
bool holdPress      = false;
const unsigned long DEBOUNCE_MS   = 50;
const unsigned long HOLD_PRESS_MS = 1500;

// --- Remote cancel ----------------------------------------------------------
volatile bool remoteCancelReq = false;

// --- Web server -------------------------------------------------------------
WebServer server(80);

float liveMag  = 9.81f;
float liveJerk = 0.0f;

// ============================================================================
//  UTILITY - MPU6050 raw read
// ============================================================================

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
  return sqrtf(ax * ax + ay * ay + az * az) * 9.81f;
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

float readSmoothed(float prevMag) {
  float raw     = readRawMagnitude();
  float smooth  = applySmoothing(raw);
  liveJerk = fabsf(smooth - prevMag) / (SAMPLE_INTERVAL_MS / 1000.0f);
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
//  FEATURE LAYER
// ============================================================================

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

// ============================================================================
//  ONLINE GAUSSIAN NAIVE BAYES
// ============================================================================

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
  inFFDip        = false;
  stillnessStart = 0;
  state          = NORMAL;
  lastClassConf  = -1.0f;
  logEvent(reason);
}

// ============================================================================
//  MAIN STATE MACHINE
// ============================================================================

void runFallDetection() {
  float prevMag = liveMag;
  liveMag  = readSmoothed(prevMag);
  float jerk = liveJerk;
  pushWindow(liveMag, jerk);

  if (state == NORMAL || state == CALIBRATING) {
    updateActivityBaseline(liveMag);
  }

  readButton();

  // Read USB Serial commands from dashboard
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

  if (remoteCancelReq) {
    remoteCancelReq = false;
    if (state != NORMAL && state != CALIBRATING) {
      if (lastClassConf >= 0 || state == IMPACT) {
        updateClassStats(nonFallStats, lastFeatures);
        theta = fminf(THETA_MAX, theta + THETA_UP);
        logEvent("CANCEL_REMOTE theta=" + String(theta, 2));
      }
      resetToNormal("CANCEL_REMOTE");
    }
  }

  // Stream telemetry to USB Serial
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
    case CALIBRATING: {
      if (liveMag > calMaxMag)  calMaxMag  = liveMag;
      if (jerk    > calMaxJerk) calMaxJerk = jerk;

      if (millis() - calStart >= CAL_DURATION_MS) {
        // Capped calibration bounds so routine movement cannot over-inflate thresholds
        IMPACT_THRESHOLD = constrain(calMaxMag  * 1.15f, 11.0f, 13.0f);
        JERK_THRESHOLD   = constrain(calMaxJerk * 1.15f, 180.0f, 260.0f);
        state = NORMAL;
        for (int i = 0; i < PAT_CAL_N; i++) {
          digitalWrite(BUZZER_PIN, (i % 2 == 0) ? HIGH : LOW);
          delay(PAT_CAL[i]);
        }
        buzzerOff();
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
        }
      } else {
        inFFDip = false;
      }

      // Path 2: Direct High-Jerk Trigger (for sudden slips/trips without sustained zero-g dip)
      if (jerk >= 280.0f && liveMag >= 11.5f) {
        extractFeatures(lastFeatures); // Snapshot impact pulse immediately
        impactTime     = millis();
        stillnessStart = 0;
        inFFDip        = false;
        state          = IMPACT;
      }
      break;
    }

    case FREE_FALL: {
      // Use 150ms rolling peak jerk to eliminate phase mismatch between jerk and magnitude
      float recentMaxJerk = getRecentMaxJerk(150);

      if (liveMag >= IMPACT_THRESHOLD && (jerk >= JERK_THRESHOLD || recentMaxJerk >= JERK_THRESHOLD)) {
        extractFeatures(lastFeatures); // Snapshot impact pulse immediately
        impactTime     = millis();
        stillnessStart = 0;
        inFFDip        = false;
        state          = IMPACT;
      } else if (millis() - ffStart >= FF_WINDOW_MS) {
        resetToNormal("FF_TIMEOUT_NO_IMPACT");
      }
      break;
    }

    case IMPACT: {
      if (quickPress) {
        updateClassStats(nonFallStats, lastFeatures);
        theta = fminf(THETA_MAX, theta + THETA_UP);
        resetToNormal("CANCEL_USER_IMPACT");
        break;
      }

      unsigned long timeSinceImpact = millis() - impactTime;

      // Independent Stillness Timer
      if (isStill) {
        if (stillnessStart == 0) stillnessStart = millis();
        if (millis() - stillnessStart >= STILL_MS) {
          lastClassConf = classify(lastFeatures); // Classify the snapshotted impact
          state = CANDIDATE_FALL;
          break;
        }
      } else {
        stillnessStart = 0; // Reset stillness clock if moving
      }

      // Movement Failsafe: if actively moving 5s after impact, reset to normal
      if (timeSinceImpact >= 5000 && !isStill) {
        updateClassStats(nonFallStats, lastFeatures);
        resetToNormal("IMPACT_TIMEOUT_MOVING");
      }
      break;
    }

    case CANDIDATE_FALL: {
      if (lastClassConf >= theta) {
        state         = FALL_CONFIRMED;
        confirmedTime = millis();
        logEvent("FALL_CONFIRMED,conf=" + String(lastClassConf, 3) +
                 ",theta=" + String(theta, 2));
        Serial.println("EVENT," + String(millis()) +
                       ",FALL_CONFIRMED," + String(lastClassConf, 3));
      } else {
        updateClassStats(nonFallStats, lastFeatures);
        theta = fminf(THETA_MAX, theta + THETA_UP * 0.5f);
        resetToNormal("CLASSIFIER_REJECTED,conf=" + String(lastClassConf, 3));
      }
      break;
    }

    case FALL_CONFIRMED: {
      playPattern(PAT_CANCEL, PAT_CANCEL_N);
      if (quickPress) {
        updateClassStats(nonFallStats, lastFeatures);
        theta = fminf(THETA_MAX, theta + THETA_UP);
        logEvent("CANCEL_USER,theta=" + String(theta, 2));
        resetToNormal("CANCEL_USER");
        break;
      }
      if (millis() - confirmedTime >= CANCEL_MS) {
        state = ALERT_SENT;
        updateClassStats(fallStats, lastFeatures);
        theta = fmaxf(THETA_MIN, theta - THETA_DOWN);
        logEvent("ALERT_SENT,theta=" + String(theta, 2) +
                 ",fallCount=" + String(fallStats.count) +
                 ",nonFallCount=" + String(nonFallStats.count));
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

  initClassStats(fallStats);
  initClassStats(nonFallStats);

  Wire.begin(21, 22);
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x6B);
  Wire.write(0x00);
  Wire.endTransmission(true);

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
  unsigned long now = millis();
  if (now - lastSampleMs >= SAMPLE_INTERVAL_MS) {
    lastSampleMs = now;
    runFallDetection();
  }
  server.handleClient();
}