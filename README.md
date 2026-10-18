# FallGuard v2

`[ESP32]` `[MPU6050]` `[Fall Detection]` `[Embedded ML]` `[Python Logger]` `[MIT License]`

---

## Overview

**FallGuard v2** is a paper-grade wearable fall detection system built on the **ESP32 microcontroller** and the **MPU6050** 6-axis IMU. It implements a complete on-device signal processing and classification pipeline — no cloud, no external DSP chip — making it suitable for low-power, real-time elderly care and occupational safety applications.

The system fuses a **3-phase rule engine** (pre-impact free-fall detection → impact detection → post-impact posture check) with an **Online Gaussian Naïve Bayes classifier** trained incrementally using **Welford's Algorithm** to distinguish genuine falls from everyday high-acceleration activities such as jumping, sitting down abruptly, or stumbling. An adaptive confidence threshold continuously re-calibrates itself to the wearer's baseline activity level.

---

## Key Features

- **3-Phase Rule Engine** — sequential free-fall → impact → rest validation minimises false positives
- **Welford's Online Gaussian NB** — fully incremental classifier, no off-device training, O(1) memory per class
- **Jerk-based discrimination** — rate-of-change of acceleration rejects impulsive non-fall events (jumps, stumbles)
- **Activity-aware baseline** — the system infers SITTING / WALKING / RUNNING from rolling statistics and adjusts detection thresholds accordingly
- **Live serial dashboard** — real-time telemetry printed at 50 Hz to the Arduino Serial Monitor
- **Python logger** (`tools/logger.py`) — PC-side data recorder with live console metrics table and matplotlib session plots
- **Alarm cancel button** — physical push-button lets the user suppress false alarms within a 10-second window
- **Passive buzzer alert** — audible alarm on confirmed fall event

---

## Hardware Required

| Component | Notes |
|-----------|-------|
| ESP32 Dev Board | Any 38-pin variant (e.g. DOIT ESP32 DEVKIT V1) |
| MPU6050 IMU | GY-521 breakout module |
| Passive buzzer | 5 V passive buzzer |
| Tactile push button | Momentary SPST, connected with internal pull-up |
| Micro USB cable | For flashing and serial telemetry |
| Breadboard + jumpers | Standard 400-point breadboard |

---

## Wiring Table

| Component | Signal | ESP32 Pin |
|-----------|--------|-----------|
| MPU6050 | VCC | 3.3 V |
| MPU6050 | GND | GND |
| MPU6050 | SDA | GPIO 21 |
| MPU6050 | SCL | GPIO 22 |
| Passive buzzer | + (positive) | GPIO 5 |
| Passive buzzer | − (negative) | GND |
| Push button | One leg | GPIO 2 |
| Push button | Other leg | GND |

> **Note:** GPIO 2 is configured as `INPUT_PULLUP` in firmware. No external resistor is required for the button.

---

## Setup

1. **Install Arduino IDE** — download from [arduino.cc](https://www.arduino.cc/en/software) (version 2.x recommended).

2. **Add the ESP32 board package**
   - Open *File → Preferences*
   - Add the following URL to *Additional Boards Manager URLs*:
     ```
     https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
     ```
   - Open *Tools → Board → Boards Manager*, search for **esp32**, and install.

3. **Wire libraries** — the `Wire` library (I²C for MPU6050) is **built in** to the ESP32 Arduino core. No separate installation is needed.

4. **Flash the firmware**
   - Open `firmware/FallGuard_v2.ino` in Arduino IDE.
   - Select your board under *Tools → Board → ESP32 Arduino → ESP32 Dev Module*.
   - Select the correct COM port under *Tools → Port*.
   - Click **Upload** (Ctrl+U).

5. **Open Serial Monitor** at **115200 baud** to view live telemetry immediately after boot.

---

## Usage

### Calibration Phase (first ~5 seconds after boot)

On power-up the device enters **CALIBRATION** mode. Place the device on a flat surface or hold it still. The ESP32 computes the initial rolling mean and variance of the acceleration magnitude using Welford's algorithm. The Serial Monitor prints:

```
# FallGuard v2 booting...
# Calibrating... keep device still
# Calibration done. theta=0.72 baseline_mag=1.003
```

### Detection Phases

| Phase | Trigger | Duration | Description |
|-------|---------|----------|-------------|
| `NORMAL` | — | continuous | Samples IMU at 50 Hz, updates rolling stats, prints CSV telemetry |
| `PRE_IMPACT` | `mag < FREE_FALL_THRESH` for ≥ 3 consecutive samples | up to 500 ms | Possible free-fall detected; jerk discriminator active |
| `IMPACT` | `mag > IMPACT_THRESH` while in PRE_IMPACT | — | Impact pulse detected; classifier runs feature extraction over 40-sample window |
| `POST_IMPACT` | Classifier confidence ≥ θ | 10 s alarm window | Buzzer sounds; waits for user button press to cancel |

### Button Behaviour

- **Press within 10 s** of alarm → alarm cancelled (`ALARM_CANCELLED` logged), system returns to NORMAL.
- **No press within 10 s** → alarm persists; a `FALL_CONFIRMED` event is logged (reserved for future caregiver alert integration).

---

## Dashboard

Open the **Arduino IDE Serial Monitor** (or any serial terminal at 115200 baud) to see the live CSV stream:

```
timestamp_ms,mag,jerk,phase,classifier_conf,activity,event_label
12340,1.002,0.012,NORMAL,0.00,WALKING,
12360,0.998,0.009,NORMAL,0.00,WALKING,
...
13500,0.211,4.320,PRE_IMPACT,0.00,WALKING,
13520,3.841,18.23,IMPACT,0.91,WALKING,FALL_DETECTED
```

| Column | Description |
|--------|-------------|
| `timestamp_ms` | Milliseconds since boot |
| `mag` | Resultant acceleration magnitude (g) |
| `jerk` | Rate of change of magnitude (g/s) |
| `phase` | Current state-machine phase |
| `classifier_conf` | GNB posterior probability for FALL class |
| `activity` | Inferred activity (SITTING/WALKING/RUNNING) |
| `event_label` | `FALL_DETECTED` or `ALARM_CANCELLED` (empty on normal rows) |

---

## Python Logger

### Install dependencies

```bash
pip install pyserial matplotlib numpy
```

### Run the logger

```bash
python tools/logger.py --port COM3 --baud 115200
```

The logger saves a timestamped CSV to `results/session_YYYYMMDD_HHMMSS.csv` and displays a refreshing console dashboard every 2 seconds.

During a session:
- Type **`f` + Enter** → mark the most recent event as a **true fall** (TP/FN update)
- Type **`n` + Enter** → mark the most recent event as a **non-fall** (FP/TN update)
- **Ctrl+C** → stop logging, print final metrics, flush CSV

### Generate plots from a saved session

```python
from tools.logger import plot_session
plot_session("results/session_20240101_120000.csv")
```

Three figures are generated:

| Plot | Description |
|------|-------------|
| Magnitude + Jerk | Dual Y-axis time series; event markers in red/green |
| Classifier confidence | Confidence over time with event markers |
| Event bar chart | Raw count of FALL_DETECTED vs ALARM_CANCELLED events |

---

## Algorithm Overview

### Welford's Online Gaussian Naïve Bayes

Rather than pre-computing class statistics from a fixed training set, FallGuard v2 updates the mean $\mu$ and variance $\sigma^2$ of each feature per class **incrementally** using Welford's one-pass algorithm:

$$
\delta = x - \mu_n, \quad \mu_{n+1} = \mu_n + \frac{\delta}{n+1}, \quad M_{n+1} = M_n + \delta \cdot (x - \mu_{n+1}), \quad \sigma^2 = \frac{M}{n}
$$

This requires only **5 floats per feature per class** (count, mean, M-accumulator, and the derived variance), making it viable on ESP32's 320 KB SRAM.

### Jerk Discrimination

The **jerk** $J(t) = |a(t) - a(t-1)| / \Delta t$ measures how *abruptly* the acceleration changes. Genuine falls produce a characteristic sustained jerk signature (free-fall ramp → sharp impact spike). Jumps and stumbles produce a very short jerk spike without the preceding low-magnitude free-fall phase. The 3-phase engine rejects events that don't meet the temporal pattern.

### Adaptive Confidence Threshold θ

The threshold $\theta$ is initialised to 0.7 and updated after every confirmed event:

$$
\theta \leftarrow \theta + \alpha \cdot (\text{feedback} - \theta)
$$

where $\alpha = 0.1$ is the learning rate and **feedback** is 1.0 for a true fall or 0.0 for a non-fall, driven by the button press. This allows the device to personalise over time to each wearer.

---

## Evaluation Metrics

Fill in after running validation trials:

| Metric | Value |
|--------|-------|
| Sensitivity (Recall) | — |
| Specificity | — |
| Detection latency (ms) | — |
| False positives / hour | — |
| True negatives / hour | — |

---

## Paper Citation

If you use FallGuard v2 in academic work, please cite:

```bibtex
@misc{fallguard2024,
  author    = {Author, A. and Author, B.},
  title     = {{FallGuard v2}: Adaptive Wearable Fall Detection with Online Gaussian Na\"{i}ve Bayes on ESP32},
  year      = {2024},
  note      = {GitHub: https://github.com/yourname/FallGuard-v2}
}
```

---

## File Structure

```
FallGuard-v2/
├── firmware/
│   └── FallGuard_v2.ino        # Arduino sketch (ESP32 firmware)
├── tools/
│   └── logger.py               # Python serial logger + plotter
├── docs/
│   └── architecture.md         # Detailed algorithm & architecture doc
├── results/
│   └── .gitkeep                # Directory placeholder; CSVs saved here
└── README.md
```

---

## License

This project is released under the **MIT License**.

```
MIT License

Copyright (c) 2024 FallGuard v2 Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
