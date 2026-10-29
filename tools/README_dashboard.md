# FallGuard v2 — Offline Laboratory Dashboard Setup & Usage

A fully offline, zero-network laboratory dashboard and trial protocol recorder for FallGuard v2. Communicates with the ESP32 exclusively over USB Serial (115200 baud).

---

## 1. Prerequisites (Windows)
- Python 3.10+ installed and on PATH.
- `pyserial` library:
  ```powershell
  pip install pyserial
  ```
- **Zero other dependencies required.** No external CSS, fonts, or JS CDNs are used. The dashboard runs entirely from the Python standard library.

---

## 2. Launching the Dashboard

### A. Live Hardware Mode (Default)
Connect your ESP32 to the PC via USB and run:
```powershell
cd C:\Users\kushw\.gemini\antigravity\scratch\FallGuard-v2
python tools\local_dashboard.py
```
- **Auto-Port Detection:** The server automatically scans COM ports for CP210x, CH340, and USB-UART bridges.
- **Explicit Port:** If you wish to target a specific port (e.g., `COM6`):
  ```powershell
  python tools\local_dashboard.py --port COM6
  ```
- The dashboard will open automatically in your browser at:
  ```text
  http://127.0.0.1:5000
  ```

### B. Offline Mock Replay Mode (Testing Without ESP32)
To verify visualization and protocol functions offline without physical hardware:
```powershell
python tools\local_dashboard.py --replay results\fallguard_session_20261023_184319.csv
```
- A prominent yellow banner indicates **`REPLAY / NOT LIVE MODE`**.
- Replay mode strictly disallows writing trials to the experimental `data/` folder.

---

## 3. Protocol Operations

### A. Individual Trial Recording
1. Select the **Participant ID** (e.g. `P01`) and **Activity** (e.g., `FALL_FORWARD`, `WALKING`).
2. Verify Placement (`waist`) and Orientation (`front`).
3. Click **▶ Record Trial (3-2-1)**:
   - On-screen 3-2-1 countdown prepares the subject.
   - At "GO", the dashboard sends `TRIAL,<id>` to the ESP32.
4. **Marking Fall Onset:** During a fall trial, press the <kbd>SPACEBAR</kbd> at the exact moment of physical movement onset. The dashboard registers `onset_wall_ms`.
5. At the end of the trial:
   - The device is automatically sent `ACK` (never `CANCEL`, protecting classifier weights).
   - The trial summary is autosaved immediately to `data/pilotN/trials.csv`.
   - Raw high-rate samples are appended to `data/pilotN/samples.csv`.

### B. Guided Session Planner
1. Enter your planned session duration in minutes (e.g. `15 min`).
2. Click **Plan Run** to generate a balanced sequence of fall and ADL trials interspersed with 8-second rest intervals.
3. Follow the **Do Now** and **Next** prompts and click **Next Guided Step**.

---

## 4. Safety & Classification Safeguards
- **CANCEL Button:** Modifying the classifier requires a confirmation modal. Sending `CANCEL` increments $\theta$ (+0.02) and trains with a Non-Fall label.
- **SET_THETA Slider:** Adjusting $\theta$ requires explicit confirmation.
- **Sensor Fault Banner:** If the IMU quality index falls below $Q < 0.50$, a purple `SENSOR FAULT` banner flashes (distinct from red fall alerts), signaling that fall detection is temporarily suppressed.

---

## 5. Offline Data Output
All completed trials are autosaved to:
```text
data/
└── pilot1/
    ├── trials.csv   <- Discrete trial records (compatible with tools/analyze_trials.py)
    └── samples.csv  <- 50 Hz timeseries telemetry
```
