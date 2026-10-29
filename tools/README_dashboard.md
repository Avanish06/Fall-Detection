# FallGuard v2 — Offline Laboratory Dashboard Setup & Usage

A fully offline, zero-network laboratory dashboard and trial protocol recorder for FallGuard v2. Communicates with the ESP32 exclusively over USB Serial (115200 baud).

---

## 1. Prerequisites (Windows)
- Python 3.10+ installed and on PATH.
- `pyserial` library:
  ```powershell
  pip install pyserial
  ```
- **Zero external dependencies:** No external CSS, fonts, or JS CDNs are referenced. The dashboard runs entirely from the Python standard library with an embedded HTML5 `<canvas>` rendering engine.

---

## 2. Launching the Dashboard

### A. Live Hardware Mode (Default)
Connect your ESP32 to the PC via USB and run:
```powershell
cd C:\Users\kushw\.gemini\antigravity\scratch\FallGuard-v2
python tools\local_dashboard.py
```
- **Auto-Port Detection:** The server automatically scans COM ports for CP210x, CH340, and USB-UART bridges.
- **Explicit Port:** To select a specific port directly:
  ```powershell
  python tools\local_dashboard.py --port COM6
  ```
- Opens automatically in your browser at:
  ```text
  http://127.0.0.1:5000
  ```

### B. Offline Mock Replay Mode (Testing Without Hardware)
To inspect visualization, timeline events, and UI features offline:
```powershell
python tools\local_dashboard.py --replay results\fallguard_session_20261023_184319.csv
```
- A yellow banner indicates **`REPLAY / NOT LIVE MODE`**.
- Replay mode strictly suppresses writing to the experimental `data/` folder.

---

## 3. Protocol Operations & Timing Architecture

### A. Timing & Latency Measurement (PC Clock Guarantee)
> **Critical Measurement Protocol:**
> All latency calculations and timing bookmarks strictly use the **PC system clock** (`int(time.time() * 1000)`) at the exact millisecond the telemetry line arrives over serial or when the user marks an event:
> 1. **Movement Onset ($t_{\text{onset}}$):** Captured from the PC wall clock when <kbd>SPACEBAR</kbd> is pressed.
> 2. **Device Detection ($t_{\text{detect}}$):** Captured from the PC wall clock at the exact arrival timestamp of the line confirming `FALL_CONFIRMED` or `ALERT_SENT`.
> 3. **Latency:** Computed as $\text{Latency} = t_{\text{detect}} - t_{\text{onset}}$ (`latency_ref = 'onset_marker'`). Device internal `millis()` is never used for latency matching to eliminate hardware clock drift and rollover discrepancies.

### B. Pre-Flight Verification Check
Before any trial can be recorded, the **Pre-Flight Surveillance Engine** must pass:
1. **USB Serial Connected:** Active communication with ESP32.
2. **Sample Rate:** Live telemetry within $45 - 55\text{ Hz}$ nominal range.
3. **Calibration Done:** Device state is not in calibration mode (`CALIBRATING`).
4. **No Sensor Fault:** $Q \ge 0.50$ and sensor state is `OK`.

> **Note:** The **Record Trial** button remains locked until the pre-flight check is run and passes. If hardware conditions degrade during recording, the lock is reinstated automatically.

### C. Protocol Activities, Placements & Orientations
- **Falls:**
  - `FALL_FORWARD`: Forward fall on mat
  - `FALL_BACKWARD`: Backward fall on mat
  - `FALL_LATERAL`: Lateral (side) fall on mat
  - `FALL_DIAG`: Diagonal fall on mat
  - `FALL_WALK`: Dynamic fall occurring during continuous walking
  - `FALL_SYNCOPE`: Slow-onset syncope / faint *(Optional)*
- **Activities of Daily Living (ADLs):**
  - `WALKING`: Steady-state walking
  - `RUN`: Jogging / running
  - `STAIRS`: Stair climbing (ascent / descent)
  - `SIT_HARD`: Abrupt sitting down on a hard chair
  - `SIT_TO_STAND`: Controlled sit-to-stand transitions
  - `LIE_DOWN`: Lying down on bed or mat
  - `JUMPING`: Vertical jumping
  - `STUMBLE`: Trip / stumble recovery
  - `OTHER`: User-specified custom activity
- **Placements:** `waist` (belt/default), `chest`, `thigh`, `pocket`, `wrist`.
- **Orientations:** `front`, `side`, `back`, `rotated 90`.

### D. Detection & Fault Logic
- `'detected'` is set to `True` **strictly** if `FALL_CONFIRMED` or `ALERT_SENT` arrives during the active trial window. Intermediate candidate states do not trigger detection.
- `'sensor_fault'` is set to `True` **strictly** if `SENSOR_FAULT` is triggered during the trial window.
- Both status counts are displayed in an outcome card on-screen immediately when each trial concludes.

---

## 4. Experimental Output Schema
Completed trials are autosaved to:
```text
data/
└── pilot1/
    ├── trials.csv   <- Discrete trial records (schema matches tools/analyze_trials.py)
    └── samples.csv  <- 50 Hz timeseries telemetry
```
- **`trials.csv` columns:** `trial_id, participant, key, activity, is_fall, placement, orientation, cue_wall_ms, onset_wall_ms, duration_s, samples, detected, sensor_fault, latency_ms, latency_ref`
- **`samples.csv` columns:** `trial_id, wall_ms, device_ms, mag, jerk, state, conf, activity, Q, E`
