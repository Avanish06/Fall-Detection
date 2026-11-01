# FallGuard v2 — Embedded Wearable Fall Detection System

`[ESP32]` `[MPU6050]` `[Embedded C++]` `[Adaptive Kinematics]` `[Sensor Integrity]` `[Online ML]` `[MIT License]`

---

## 1. Overview

**FallGuard v2** is an embedded fall-detection system running on the **ESP32 microcontroller** paired with an **InvenSense MPU6050** 6-axis IMU. The firmware performs real-time deterministic fall classification at **50 Hz** using on-chip processing, without cloud reliance or external DSP chips.

The system features:
1. **Multi-Criterion Sensor Integrity (`USE_INTEGRITY`):** Continuous I2C, range, freeze, and delta monitoring to prevent false triggers during sensor detachment or bus faults.
2. **Dual-Path Kinematic Gating:**
   - **Path 1 (Classical Free-Fall):** Free-fall dip ($a < 5.0\text{ m/s}^2$, $\ge 40\text{ ms}$) followed by impact ($a \ge 22.0\text{ m/s}^2$, $J \ge 500\text{ m/s}^3$).
   - **Path 2 (`USE_PATH2` Direct High-Jerk):** Slip and trip detection ($J \ge 280\text{ m/s}^3$, $a \ge 11.5\text{ m/s}^2$) for low-clearance falls without free-fall.
3. **Adaptive Thresholds (`USE_ADAPTIVE`):** Motion energy $E_k$ scales stillness verification interval $T_s \in [1500, 3000]\text{ ms}$; impact severity scales cancellation window $T_c \in [3000, 8000]\text{ ms}$.
4. **Lifelong Online Learning (`USE_GNB`):** Welford Gaussian Naive Bayes classifier with cold-start lockout prevention (`classifierTrained` gating).

---

## 2. Hardware Connections & Pinout

| ESP32 Pin | Peripheral Pin | Function | Notes |
| :--- | :--- | :--- | :--- |
| **3V3** | MPU6050 `VCC` | Power Supply | 3.3V regulated power rail |
| **GND** | MPU6050 `GND` | Ground | Common system ground |
| **GPIO 21** | MPU6050 `SDA` | I2C Data | 400 kHz Fast-Mode I2C |
| **GPIO 22** | MPU6050 `SCL` | I2C Clock | 400 kHz Fast-Mode I2C |
| **GPIO 2** | Pushbutton Pin A | Cancel / Calibrate Input | `INPUT_PULLUP` (Active LOW to GND) |
| **GND** | Pushbutton Pin B | Button Return | Direct to ground |
| **GPIO 5** | Buzzer (+) | Acoustic Alarm Output | Active PWM / Digital Out |
| **GND** | Buzzer (-) | Buzzer Ground | Direct to ground |

---

## 3. Getting Started

### 3.1 Network Credentials Setup (`secrets.h`)
Copy `firmware/secrets.example.h` to `firmware/secrets.h` and configure your local Wi-Fi credentials:
```bash
cp firmware/secrets.example.h firmware/secrets.h
```
Edit `firmware/secrets.h`:
```c
#ifndef SECRETS_H
#define SECRETS_H

const char* const WIFI_SSID     = "Your_WiFi_SSID";
const char* const WIFI_PASSWORD = "Your_WiFi_Password";

#endif
```

### 3.2 Flashing Firmware
1. Open `firmware/FallGuard_v2.ino` in the Arduino IDE or VS Code with ESP32 board support.
2. Select target board: **ESP32 Dev Module**.
3. Compile and flash over USB Serial (115200 baud).

---

## 4. System Timing & Kinematic Parameters

| Parameter | Value | Units | Description |
| :--- | :---: | :---: | :--- |
| **Sampling Rate** | `50` | $\text{Hz}$ | Sensor sample interval ($20\text{ ms}$) |
| **Calibration Duration** | `20000` | $\text{ms}$ | Initial rest calibration phase ($20\text{ s}$) |
| **Free-Fall Threshold** | `5.0` | $\text{m/s}^2$ | Maximum acceleration during free-fall |
| **Free-Fall Min Duration** | `40` | $\text{ms}$ | Minimum consecutive zero-g duration |
| **Impact Search Window** | `1400` | $\text{ms}$ | Post-free-fall impact acceptance interval |
| **Impact Acceleration Thresh** | `22.0` | $\text{m/s}^2$ | Peak resultant acceleration threshold |
| **Impact Jerk Thresh** | `500.0` | $\text{m/s}^3$ | Peak jerk rate threshold |
| **Path 2 Jerk Thresh** | `280.0` | $\text{m/s}^3$ | Direct high-jerk trigger threshold |
| **Path 2 Accel Thresh** | `11.5` | $\text{m/s}^2$ | Direct high-jerk minimum acceleration |
| **Stillness Window ($T_s$)** | `1500 - 3000` | $\text{ms}$ | Quiescent rest period ($2200\text{ ms}$ fixed default) |
| **Alarm Cancel Window ($T_c$)** | `3000 - 8000` | $\text{ms}$ | User cancellation period ($5000\text{ ms}$ fixed default) |

---

## 5. Software Tools & Verification

### 5.1 Verification Tests
Execute the standalone unit test suite and state machine simulator:
```bash
python test_fallguard.py
```
Execute the hardware connection stubs:
```bash
python test_hardware_stubs.py
```

### 5.2 USB Serial Logger & Trial Protocol Tool
Capture real-time telemetry, record per-trial CSV datasets, and add clinical ground-truth annotations:
```bash
python tools/logger.py --port COM3 --baud 115200
```
Interactive commands:
- `t <id>`: Start recording a labeled trial (e.g. `t T01_FALL`).
- `l`: Open interactive metadata prompt writing to `results/annotations.csv`.
- `f` / `n`: Mark last event as Fall / Non-Fall.

### 5.3 Local USB Web Dashboard
Launch the zero-dependency local browser dashboard for real-time visualization:
```bash
python tools/local_dashboard.py COM3
```
Dashboard is hosted at `http://localhost:5000`.

### 5.4 Offline Performance Analysis
Analyze recorded experimental sessions and benchmark datasets:
```bash
python tools/analyze_results.py
```

---

## 6. Clinical & Benchmark Performance Metrics

> **Empirical Validation Note:** Comprehensive clinical metrics across human subjects (elderly adults vs. active young adults) are **not yet measured on physical subject protocol**. Offline evaluation on benchmark telemetry is computed via `tools/analyze_results.py`.

### 6.1 Hardware Sensor Fault Tolerance (Physical Unplug Tests)
Physical fault injection trials (MPU6050 I2C bus disconnection during continuous 50 Hz execution) were conducted to verify fault suppression:
- **Trials Conducted:** 3 physical hardware disconnect trials.
- **Fault Detection:** 3 / 3 (100%) asserted `SENSOR_FAULT` ($Q = 0.48 < 0.50$).
- **Alarm Suppression:** 0 false fall alarms (`FALL_CONFIRMED = 0`, `ALERT_SENT = 0`).
- **Recovery:** 3 / 3 (100%) automatically restored to `NORMAL` upon bus reconnection ($Q > 0.90$).
- **Detailed Report & Logs:** See [docs/sensor_fault_injection_experiments.md](docs/sensor_fault_injection_experiments.md) and [docs/fault_injection_trials.csv](docs/fault_injection_trials.csv).
