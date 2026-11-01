#!/usr/bin/env python3
"""
FallGuard v2 — Fully Offline Local Laboratory Dashboard
=========================================================

Zero-internet, USB-cable-only laboratory dashboard and experimental trial
collector for the FallGuard v2 wearable fall detection system.

Features:
- 100% Offline: Native HTML5 Canvas plotting (no external JS/CSS/CDN).
- USB Connection Surveillance: Auto-detect, manual port select, hotplug reconnect.
- Real-Time Telemetry: 50 Hz kinematic visualization with kinematic threshold lines.
- Sensor Integrity Monitoring: Live Q index gauge, motion energy E, SENSOR_FAULT banner.
- Trial Protocol Engine: Pre-flight safety check, 3-2-1 countdown, spacebar onset marker.
- Explicit Clock Reference: Latency and onset strictly computed via PC wall clock.
- Strict Event Logic: 'detected' set only on confirmed fall; 'sensor_fault' only on SENSOR_FAULT.
- Orphan Alert Tracking: Events outside trial logged to 'orphan_alerts' column.
- Persistent ACK Loop: Sends ACK every 500 ms until Normal (15s timeout) post-trial.
- Enforced Fall Window: Minimum 12 s duration for fall trials.
- Data Persistence: Autosaves to data/pilotN/ matching tools/analyze_trials.py schema.
- Replay Mode: Simulated serial feed (--replay) clearly marked NOT LIVE.
- Safety Guardrails: Confirmation modals for classifier-altering commands.
"""

import sys
import os
import re
import time
import json
import math
import argparse
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from pathlib import Path

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("\n[ERROR] 'pyserial' is not installed. Please run: pip install pyserial\n")
    sys.exit(1)

# ──────────────────────────────────────────────────────────────────────────────
# Global Configuration & Shared State
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000

state_lock = threading.Lock()
serial_lock = threading.Lock()

# Application State Flags
is_replay_mode = False
replay_source_name = ""
active_port_name = "AUTO"
serial_inst = None
stop_threads = False

# Post-Trial ACK Loop State
ack_loop_active = False
ack_timeout_warning = None

# Orphan Alert Surveillance
orphan_alerts_since_last_trial = 0
in_orphan_alert_state = False

# Connection Health Metrics
connection_status = "disconnected"  # "connected" | "no_data" | "disconnected"
last_telemetry_time = 0.0
sample_timestamps = []  # Rolling 1.0s window for Hz calculation
live_hz = 0.0

stat_metrics = {
    "loop_mean_us": 0,
    "loop_max_us": 0,
    "actual_hz": 0.0,
    "free_heap": 0,
    "min_free_heap": 0,
}

# Live Telemetry Snapshot
latest_state = {
    "connection": "disconnected",
    "port": "AUTO",
    "isReplay": False,
    "liveHz": 0.0,
    "stat": stat_metrics,
    "state": "Waiting for ESP32...",
    "stateCode": "NORMAL",
    "mag": 9.81,
    "jerk": 0.0,
    "conf": -1.0,
    "activity": "STILL",
    "fallCount": 0,
    "nonFallCount": 0,
    "theta": 0.65,
    "impThresh": 11.5,
    "jerkThresh": 220.0,
    "freeFallMag": 5.0,
    "sensorState": "OK",
    "Q": 1.0,
    "E": 0.0,
    "trialId": "NONE",
    "adaptiveTs": 2200,
    "adaptiveTc": 5000,
    "events": [],
    "lastTrialSummary": None,
    "ackLoopActive": False,
    "ackWarning": None,
}

# Rolling Ring Buffer for Canvas Visualization (stores [time_s, mag, jerk])
CHART_BUFFER_SIZE = 1800
chart_buffer = []

# Pilot Session Management (lazily initialized on first real trial save)
pilot_dir = None
active_trial = None


def get_or_create_pilot_dir(requested_pilot: str | None = None) -> Path:
    """Determine and create data/pilotN/ directory, strictly avoiding 'test/fake/synthetic'."""
    data_root = Path(__file__).parent.parent / "data"
    data_root.mkdir(parents=True, exist_ok=True)

    if requested_pilot and requested_pilot.strip():
        clean_name = re.sub(r"[^a-zA-Z0-9_\-]", "", requested_pilot.strip())
        target = data_root / clean_name
    else:
        existing = [
            d.name for d in data_root.iterdir()
            if d.is_dir() and re.match(r"^pilot\d+$", d.name)
        ]
        indices = [int(re.search(r"\d+", name).group(0)) for name in existing]
        next_idx = max(indices) + 1 if indices else 1
        target = data_root / f"pilot{next_idx}"

    target.mkdir(parents=True, exist_ok=True)
    return target


# ──────────────────────────────────────────────────────────────────────────────
# Embedded Offline HTML/CSS/JS (Zero-External Asset Architecture)
# ──────────────────────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>FallGuard v2 — Offline Lab Dashboard</title>
  <style>
    :root {
      --bg-body: #0b1120;
      --bg-card: #1e293b;
      --bg-input: #0f172a;
      --border-color: #334155;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --green: #22c55e;
      --amber: #f59e0b;
      --red: #ef4444;
      --purple: #a855f7;
      --blue: #3b82f6;
    }
    @media (prefers-color-scheme: light) {
      :root {
        --bg-body: #f8fafc;
        --bg-card: #ffffff;
        --bg-input: #f1f5f9;
        --border-color: #cbd5e1;
        --text-main: #0f172a;
        --text-muted: #64748b;
        --accent: #0284c7;
      }
    }
    [data-theme="dark"] {
      --bg-body: #0b1120;
      --bg-card: #1e293b;
      --bg-input: #0f172a;
      --border-color: #334155;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
    }
    [data-theme="light"] {
      --bg-body: #f8fafc;
      --bg-card: #ffffff;
      --bg-input: #f1f5f9;
      --border-color: #cbd5e1;
      --text-main: #0f172a;
      --text-muted: #64748b;
      --accent: #0284c7;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg-body);
      color: var(--text-main);
      padding: 14px 18px;
      line-height: 1.4;
    }

    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--border-color);
      margin-bottom: 12px;
      flex-wrap: wrap;
      gap: 10px;
    }
    .header-left { display: flex; align-items: center; gap: 10px; }
    h1 { font-size: 1.35rem; font-weight: 700; color: var(--accent); display: flex; align-items: center; gap: 6px; }
    .header-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

    .chip {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 600;
      background: var(--bg-input);
      border: 1px solid var(--border-color);
    }
    .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
    .dot-connected { background: var(--green); box-shadow: 0 0 6px var(--green); }
    .dot-nodata { background: var(--amber); box-shadow: 0 0 6px var(--amber); }
    .dot-disconnected { background: var(--red); box-shadow: 0 0 6px var(--red); }

    .banner-replay {
      background: #78350f;
      color: #fef3c7;
      border: 1px solid #b45309;
      font-weight: 700;
      text-align: center;
      padding: 8px 12px;
      border-radius: 8px;
      margin-bottom: 12px;
      display: none;
      letter-spacing: 0.5px;
    }

    .alarm-banner {
      display: none;
      padding: 14px 18px;
      border-radius: 8px;
      font-weight: 700;
      font-size: 1.05rem;
      margin-bottom: 14px;
      align-items: center;
      justify-content: space-between;
      animation: pulseAlert 1s infinite alternate;
    }
    .banner-fall { background: #7f1d1d; color: #fecaca; border: 2px solid #ef4444; }
    .banner-fault { background: #581c87; color: #f3e8ff; border: 2px solid #c084fc; }
    @keyframes pulseAlert { from { opacity: 0.85; transform: scale(0.995); } to { opacity: 1; transform: scale(1.0); } }

    .dashboard-grid {
      display: grid;
      grid-template-columns: 1fr 350px;
      gap: 14px;
    }
    @media (max-width: 980px) {
      .dashboard-grid { grid-template-columns: 1fr; }
    }

    .card {
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 12px 14px;
      margin-bottom: 12px;
    }
    .card-title {
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      color: var(--text-muted);
      font-weight: 700;
      margin-bottom: 8px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .metrics-row {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 8px;
      margin-bottom: 12px;
    }
    .metric-box {
      background: var(--bg-input);
      border: 1px solid var(--border-color);
      border-radius: 6px;
      padding: 8px 10px;
    }
    .metric-lbl { font-size: 0.65rem; color: var(--text-muted); text-transform: uppercase; font-weight: 600; }
    .metric-val { font-size: 1.25rem; font-weight: 700; color: var(--accent); }

    .chart-container {
      position: relative;
      width: 100%;
      height: 250px;
      background: var(--bg-input);
      border-radius: 6px;
      border: 1px solid var(--border-color);
    }
    canvas { width: 100%; height: 100%; display: block; }

    .chart-controls {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 8px;
      font-size: 0.8rem;
    }
    .btn-group { display: flex; gap: 4px; }
    .btn-toggle {
      background: var(--bg-input);
      border: 1px solid var(--border-color);
      color: var(--text-muted);
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 0.75rem;
      cursor: pointer;
    }
    .btn-toggle.active { background: var(--accent); color: #000; font-weight: 700; border-color: var(--accent); }

    label { font-size: 0.75rem; color: var(--text-muted); display: block; margin-bottom: 3px; font-weight: 600; }
    select, input[type="text"], input[type="number"] {
      width: 100%;
      background: var(--bg-input);
      border: 1px solid var(--border-color);
      color: var(--text-main);
      padding: 6px 10px;
      border-radius: 6px;
      font-size: 0.85rem;
      margin-bottom: 8px;
      outline: none;
    }
    select:focus, input:focus { border-color: var(--accent); }

    button {
      padding: 7px 12px;
      border-radius: 6px;
      border: none;
      font-size: 0.82rem;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: opacity 0.15s;
    }
    button:hover { opacity: 0.9; }
    button:disabled { opacity: 0.45; cursor: not-allowed; }
    .btn-primary { background: var(--accent); color: #000; }
    .btn-danger { background: var(--red); color: #fff; }
    .btn-success { background: var(--green); color: #000; }
    .btn-secondary { background: var(--bg-input); border: 1px solid var(--border-color); color: var(--text-main); }

    .countdown-overlay {
      display: none;
      text-align: center;
      padding: 16px;
      background: #0284c7;
      color: #fff;
      border-radius: 6px;
      margin-bottom: 10px;
    }
    .countdown-num { font-size: 2.2rem; font-weight: 800; line-height: 1; }

    .onset-indicator {
      display: none;
      background: #15803d;
      color: #dcfce7;
      padding: 6px 10px;
      border-radius: 4px;
      font-size: 0.8rem;
      margin-top: 6px;
      text-align: center;
      font-weight: 600;
    }

    .preflight-box {
      background: var(--bg-input);
      border: 1px solid var(--border-color);
      border-radius: 6px;
      padding: 10px;
      margin-bottom: 10px;
    }
    .preflight-item {
      display: flex;
      justify-content: space-between;
      font-size: 0.75rem;
      padding: 3px 0;
      border-bottom: 1px solid var(--border-color);
    }
    .preflight-item:last-child { border-bottom: none; }
    .pf-pass { color: var(--green); font-weight: bold; }
    .pf-fail { color: var(--red); font-weight: bold; }

    .trial-summary-box {
      display: none;
      background: #064e3b;
      color: #ecfdf5;
      border: 1px solid #10b981;
      border-radius: 6px;
      padding: 10px 12px;
      font-size: 0.78rem;
      margin-top: 8px;
    }

    .ack-status-box {
      display: none;
      background: #1e1b4b;
      color: #c7d2fe;
      border: 1px solid #6366f1;
      border-radius: 6px;
      padding: 8px 10px;
      font-size: 0.75rem;
      margin-top: 6px;
      animation: pulseAlert 1.2s infinite alternate;
    }

    .timeline-box {
      max-height: 180px;
      overflow-y: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.72rem;
      background: var(--bg-input);
      padding: 6px;
      border-radius: 6px;
      border: 1px solid var(--border-color);
    }
    .timeline-item { padding: 3px 6px; border-bottom: 1px solid var(--border-color); }
    .timeline-item:last-child { border-bottom: none; }
    .t-alert { color: #f87171; font-weight: bold; }
    .t-fault { color: #c084fc; font-weight: bold; }
    .t-trial { color: #38bdf8; }

    .modal-backdrop {
      display: none;
      position: fixed;
      top: 0; left: 0; width: 100%; height: 100%;
      background: rgba(0,0,0,0.65);
      z-index: 1000;
      justify-content: center;
      align-items: center;
    }
    .modal-box {
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 18px;
      max-width: 440px;
      width: 90%;
    }

    footer {
      margin-top: 14px;
      padding-top: 10px;
      border-top: 1px solid var(--border-color);
      display: flex;
      justify-content: space-between;
      font-size: 0.72rem;
      color: var(--text-muted);
      align-items: center;
    }
  </style>
</head>
<body>

  <!-- Replay Mode Warning -->
  <div id="replayBanner" class="banner-replay">
    ⚠️ REPLAY / NOT LIVE MODE — Streaming from prerecorded file — Disk recording disabled
  </div>

  <!-- Big Buzzer Banners -->
  <div id="fallAlarmBanner" class="alarm-banner banner-fall">
    <div>🚨 FALL CONFIRMED — BUZZER ACTIVE (PAT_CANCEL)</div>
    <button class="btn-secondary" style="background:#fff;color:#991b1b" onclick="confirmAck()">Acknowledge (ACK)</button>
  </div>
  <div id="sensorFaultBanner" class="alarm-banner banner-fault">
    <div>⚠️ SENSOR FAULT DETECTED — IMU integrity low (Q &lt; 0.50). Fall detection suppressed.</div>
    <span class="chip" style="background:#3b0764;color:#f3e8ff">3-BEEP FAULT PATTERN</span>
  </div>

  <!-- Header -->
  <header>
    <div class="header-left">
      <h1>
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
        </svg>
        FallGuard v2 Lab
      </h1>
      <span id="connChip" class="chip">
        <span id="connDot" class="dot dot-disconnected"></span>
        <span id="connText">Disconnected</span>
      </span>
      <span class="chip">Hz: <strong id="hzVal">0.0</strong></span>
      <span class="chip">Loop: <strong id="loopVal">0</strong> μs</span>
      <span class="chip">Heap: <strong id="heapVal">0</strong> B</span>
    </div>
    <div class="header-actions">
      <select id="portSelect" style="width:130px;margin:0" onchange="switchPort(this.value)">
        <option value="AUTO">Auto-Port</option>
      </select>
      <button class="btn-secondary" onclick="reconnectPort()">Reconnect</button>
      <button class="btn-secondary" onclick="toggleTheme()">Theme</button>
    </div>
  </header>

  <!-- Top Metrics Row -->
  <div class="metrics-row">
    <div class="metric-box">
      <div class="metric-lbl">Device State</div>
      <div class="metric-val" id="stateVal" style="font-size:1.05rem;color:var(--green)">NORMAL</div>
    </div>
    <div class="metric-box">
      <div class="metric-lbl">Accel Magnitude</div>
      <div class="metric-val" id="magVal">9.81 m/s²</div>
    </div>
    <div class="metric-box">
      <div class="metric-lbl">Jerk (dt)</div>
      <div class="metric-val" id="jerkVal">0.0 m/s³</div>
    </div>
    <div class="metric-box">
      <div class="metric-lbl">Quality Index (Q)</div>
      <div class="metric-val" id="qVal">1.00</div>
    </div>
    <div class="metric-box">
      <div class="metric-lbl">Energy (E)</div>
      <div class="metric-val" id="eVal">0.00</div>
    </div>
    <div class="metric-box">
      <div class="metric-lbl">Confidence P(FALL)</div>
      <div class="metric-val" id="confVal">--</div>
    </div>
  </div>

  <div class="dashboard-grid">
    <!-- Left Column: Primary Chart & Timeline -->
    <div class="main-column">
      <div class="card">
        <div class="card-title">
          <span>Live Kinematics (50 Hz)</span>
          <div class="btn-group">
            <button class="btn-toggle active" id="btnWin5" onclick="setWindow(5)">5s</button>
            <button class="btn-toggle" id="btnWin10" onclick="setWindow(10)">10s</button>
            <button class="btn-toggle" id="btnWin30" onclick="setWindow(30)">30s</button>
            <button class="btn-toggle" id="btnPause" onclick="togglePause()">Pause</button>
          </div>
        </div>
        <div class="chart-container">
          <canvas id="kinematicCanvas"></canvas>
        </div>
        <div class="chart-controls">
          <div style="display:flex;gap:12px;font-size:0.72rem;color:var(--text-muted)">
            <span style="color:#38bdf8">― Mag (m/s²)</span>
            <span style="color:#fb923c">― Jerk /10 (m/s³)</span>
            <span style="color:#f59e0b;border-bottom:1px dashed #f59e0b">-- FF (5.0)</span>
            <span style="color:#ef4444;border-bottom:1px dashed #ef4444">-- Impact (11.5)</span>
          </div>
          <span id="bufferInfo" style="font-size:0.7rem;color:var(--text-muted)">Live buffer: 0 pts</span>
        </div>
      </div>

      <div class="card">
        <div class="card-title">
          <span>Event &amp; Diagnostic Timeline</span>
          <button class="btn-secondary" style="padding:2px 6px;font-size:0.7rem" onclick="clearEvents()">Clear</button>
        </div>
        <div class="timeline-box" id="timelineList">
          <div class="timeline-item" style="color:var(--text-muted)">Waiting for telemetry...</div>
        </div>
      </div>

      <!-- Supervised Classifier Safety Controls -->
      <div class="card">
        <div class="card-title">
          <span>Online Classifier Calibration &amp; Override (Supervised)</span>
        </div>
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <button class="btn-secondary" onclick="confirmCancel()">Send CANCEL (Raises θ)</button>
          <button class="btn-secondary" onclick="confirmAck()">Send ACK</button>
          <div style="flex:1;min-width:180px;display:flex;align-items:center;gap:8px">
            <label style="margin:0">θ:</label>
            <input type="range" id="thetaRange" min="50" max="85" value="65" style="flex:1" oninput="previewTheta(this.value)">
            <span id="thetaDisplay" style="font-weight:bold;width:38px">0.65</span>
            <button class="btn-secondary" style="padding:4px 8px" onclick="confirmSetTheta()">Apply</button>
          </div>
        </div>
      </div>
    </div>

    <!-- Right Column: Pre-Flight Check, Trial Protocol, Guided Run -->
    <div class="side-column">
      <!-- Pre-Flight Verification Card -->
      <div class="card">
        <div class="card-title">
          <span>Hardware Pre-Flight Surveillance</span>
          <button class="btn-secondary" style="padding:2px 8px;font-size:0.7rem" onclick="runPreflightCheck()">Run Pre-Flight</button>
        </div>
        <div class="preflight-box">
          <div class="preflight-item">
            <span>USB Serial Connected:</span>
            <span id="pfConn" class="pf-fail">FAIL (No USB)</span>
          </div>
          <div class="preflight-item">
            <span>Sample Rate (42–58 Hz):</span>
            <span id="pfHz" class="pf-fail">FAIL (0.0 Hz)</span>
          </div>
          <div class="preflight-item">
            <span>Device State (Normal):</span>
            <span id="pfCal" class="pf-fail">FAIL (Not Normal)</span>
          </div>
          <div class="preflight-item">
            <span>IMU Sensor Health (Q &ge; 0.50):</span>
            <span id="pfFault" class="pf-fail">FAIL (Unhealthy)</span>
          </div>
        </div>
        <div id="preflightStatusMsg" style="font-size:0.72rem;color:var(--red);font-weight:600">
          🔒 Recording blocked. Run pre-flight check when hardware is ready.
        </div>
      </div>

      <!-- Trial Protocol Card -->
      <div class="card">
        <div class="card-title">
          <span>Trial Protocol Collector</span>
          <span class="chip" id="pilotChip">pilot1</span>
        </div>

        <div id="countdownBox" class="countdown-overlay">
          <div style="font-size:0.8rem">GET READY</div>
          <div class="countdown-num" id="countdownDigit">3</div>
          <div id="countdownSub" style="font-size:0.75rem;margin-top:4px">Trial starting...</div>
        </div>

        <label>Participant ID</label>
        <input type="text" id="partId" value="P01" oninput="updateTrialIdPreview()">

        <label>Activity Protocol</label>
        <select id="activitySelect" onchange="onActivityChange()">
          <!-- Falls -->
          <option value="FALL_FORWARD" data-fall="true">FALL: Forward Fall (Mat)</option>
          <option value="FALL_BACKWARD" data-fall="true">FALL: Backward Fall (Mat)</option>
          <option value="FALL_LATERAL" data-fall="true">FALL: Lateral Fall (Mat)</option>
          <option value="FALL_DIAG" data-fall="true">FALL: Diagonal Fall (Mat)</option>
          <option value="FALL_WALK" data-fall="true">FALL: Fall While Walking (Dynamic)</option>
          <option value="FALL_SYNCOPE" data-fall="true">FALL: Syncope / Faint (Optional - Slow Onset)</option>
          <!-- ADLs -->
          <option value="WALKING" data-fall="false">ADL: Normal Walking</option>
          <option value="RUN" data-fall="false">ADL: Running / Jogging</option>
          <option value="STAIRS" data-fall="false">ADL: Stairs (Ascent / Descent)</option>
          <option value="SIT_HARD" data-fall="false">ADL: Sit Down Abruptly (Hard Chair)</option>
          <option value="SIT_TO_STAND" data-fall="false">ADL: Sit to Stand (Normal)</option>
          <option value="LIE_DOWN" data-fall="false">ADL: Lying Down (Bed / Sofa)</option>
          <option value="JUMPING" data-fall="false">ADL: Vertical Jump</option>
          <option value="STUMBLE" data-fall="false">ADL: Trip / Stumble Recovery</option>
          <option value="OTHER" data-fall="false">OTHER: Custom Activity...</option>
        </select>

        <div id="customActRow" style="display:none">
          <label>Custom Activity Label</label>
          <input type="text" id="customActName" placeholder="e.g. Tying shoes">
        </div>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
          <div>
            <label>Placement</label>
            <select id="placementSelect">
              <option value="waist">Waist (Belt / Default)</option>
              <option value="chest">Chest</option>
              <option value="thigh">Thigh</option>
              <option value="pocket">Pocket</option>
              <option value="wrist">Wrist</option>
            </select>
          </div>
          <div>
            <label>Orientation</label>
            <select id="orientSelect">
              <option value="front">Front</option>
              <option value="side">Side</option>
              <option value="back">Back</option>
              <option value="rotated 90">Rotated 90°</option>
            </select>
          </div>
        </div>

        <label>Trial Duration (s) — <span id="durHint" style="color:var(--accent)">Falls &ge; 12s</span></label>
        <input type="number" id="trialDuration" value="12" min="12" max="60">

        <div style="background:var(--bg-input);padding:6px 10px;border-radius:4px;margin-bottom:8px;font-size:0.75rem">
          Next Trial ID: <strong id="trialIdPreview" style="color:var(--accent)">P01_FALL_FWD_01</strong>
        </div>

        <button class="btn-primary" id="btnRecord" style="width:100%;padding:10px" onclick="startTrialCountdown()" disabled>
          ▶ Record Trial (Pass Pre-Flight First)
        </button>

        <div id="onsetAlert" class="onset-indicator">
          [SPACE] Fall Movement Onset Recorded (PC Clock)!
        </div>

        <!-- ACK Persistent Clearance Status -->
        <div id="ackStatusBox" class="ack-status-box">
          <div style="font-weight:bold" id="ackTitle">Clearing Alarm State...</div>
          <div id="ackDetail">Sending ACK every 500 ms until device state is Normal...</div>
        </div>

        <!-- Post-Trial Outcome Summary Card -->
        <div id="trialSummaryCard" class="trial-summary-box">
          <div style="font-weight:bold;margin-bottom:4px" id="sumTitle">Trial Outcome:</div>
          <div>Detected: <strong id="sumDetected">--</strong></div>
          <div>Sensor Fault: <strong id="sumFault">--</strong></div>
          <div>Latency: <strong id="sumLatency">--</strong></div>
          <div>Samples Captured: <strong id="sumSamples">--</strong></div>
          <div>Preceding Orphan Alerts: <strong id="sumOrphans">0</strong></div>
        </div>

        <div style="margin-top:10px;font-size:0.72rem;color:var(--text-muted)">
          Timing: Movement onset and device detection use the <strong>PC clock at line receipt</strong> (<kbd style="background:#334155;color:#fff;padding:1px 5px;border-radius:3px">SPACE</kbd> = onset).
        </div>
      </div>

      <!-- Guided Protocol Planner -->
      <div class="card">
        <div class="card-title">
          <span>Guided Protocol Session</span>
        </div>
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
          <input type="number" id="sessionLenMin" value="15" min="5" max="120" style="margin:0;width:80px">
          <span style="font-size:0.8rem">min session</span>
          <button class="btn-secondary" style="flex:1" onclick="planGuidedSession()">Plan Run</button>
        </div>
        <div id="guidedPlanView" style="display:none">
          <div style="background:var(--bg-input);padding:8px;border-radius:4px;font-size:0.75rem;margin-bottom:8px">
            <div>Current Cue: <strong id="guidedNow" style="color:var(--accent)">--</strong></div>
            <div style="color:var(--text-muted)">Next: <span id="guidedNext">--</span></div>
          </div>
          <button class="btn-success" id="btnGuidedNext" style="width:100%" onclick="executeGuidedStep()">Next Guided Step</button>
        </div>
      </div>
    </div>
  </div>

  <!-- Safety Confirmation Modal -->
  <div id="confirmModal" class="modal-backdrop">
    <div class="modal-box">
      <h3 style="font-size:1.1rem;margin-bottom:8px;color:var(--amber)">Device Parameter Notice</h3>
      <p id="modalMessage" style="font-size:0.82rem;color:var(--text-muted);margin-bottom:14px">
        This action sends a serial command that alters firmware detection state.
      </p>
      <div style="display:flex;justify-content:flex-end;gap:8px">
        <button class="btn-secondary" onclick="closeModal()">Cancel</button>
        <button class="btn-danger" id="modalConfirmBtn" onclick="runConfirmedAction()">Proceed</button>
      </div>
    </div>
  </div>

  <footer>
    <span>FallGuard v2 Embedded Laboratory Suite (127.0.0.1)</span>
    <span class="chip" style="font-size:0.65rem">pilot data, not clinical</span>
  </footer>

  <script>
    // ──────────────────────────────────────────────────────────────────────────
    // Zero-External-Asset Plain Canvas Chart Engine
    // ──────────────────────────────────────────────────────────────────────────
    const canvas = document.getElementById('kinematicCanvas');
    const ctx = canvas.getContext('2d');
    let chartWindowSeconds = 5;
    let isPaused = false;
    let dataBuffer = []; // [t_rel_s, mag, jerk]

    function resizeCanvas() {
      const rect = canvas.getBoundingClientRect();
      canvas.width = rect.width * window.devicePixelRatio;
      canvas.height = rect.height * window.devicePixelRatio;
      ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
    }
    window.addEventListener('resize', resizeCanvas);
    resizeCanvas();

    function drawChart() {
      const w = canvas.getBoundingClientRect().width;
      const h = canvas.getBoundingClientRect().height;
      ctx.clearRect(0, 0, w, h);

      if (dataBuffer.length === 0) {
        ctx.fillStyle = '#64748b';
        ctx.font = '12px sans-serif';
        ctx.fillText('Awaiting live telemetry...', 14, 24);
        requestAnimationFrame(drawChart);
        return;
      }

      const nowSec = dataBuffer[dataBuffer.length - 1][0];
      const minSec = nowSec - chartWindowSeconds;
      const visible = dataBuffer.filter(d => d[0] >= minSec);

      const yMin = 0.0, yMax = 25.0;
      const toY = (v) => h - 20 - ((v - yMin) / (yMax - yMin)) * (h - 35);
      const toX = (t) => 35 + ((t - minSec) / chartWindowSeconds) * (w - 45);

      ctx.strokeStyle = '#1e293b';
      ctx.lineWidth = 1;
      ctx.fillStyle = '#64748b';
      ctx.font = '10px monospace';
      [5, 10, 15, 20].forEach(val => {
        const y = toY(val);
        ctx.beginPath();
        ctx.moveTo(35, y);
        ctx.lineTo(w, y);
        ctx.stroke();
        ctx.fillText(val.toString(), 10, y + 3);
      });

      // Free Fall: 5.0
      ctx.strokeStyle = '#f59e0b';
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(35, toY(5.0));
      ctx.lineTo(w, toY(5.0));
      ctx.stroke();

      // Impact: 11.5
      ctx.strokeStyle = '#ef4444';
      ctx.beginPath();
      ctx.moveTo(35, toY(11.5));
      ctx.lineTo(w, toY(11.5));
      ctx.stroke();
      ctx.setLineDash([]);

      if (visible.length < 2) {
        requestAnimationFrame(drawChart);
        return;
      }

      // Jerk / 10 Line (Orange)
      ctx.strokeStyle = '#fb923c';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      visible.forEach((pt, i) => {
        const x = toX(pt[0]);
        const y = toY(Math.min(25, pt[2] / 10.0));
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();

      // Acceleration Magnitude Line (Cyan)
      ctx.strokeStyle = '#38bdf8';
      ctx.lineWidth = 1.8;
      ctx.beginPath();
      visible.forEach((pt, i) => {
        const x = toX(pt[0]);
        const y = toY(Math.min(25, pt[1]));
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();

      document.getElementById('bufferInfo').textContent = `Live buffer: ${visible.length} pts (${chartWindowSeconds}s)`;
      requestAnimationFrame(drawChart);
    }
    requestAnimationFrame(drawChart);

    function setWindow(sec) {
      chartWindowSeconds = sec;
      ['btnWin5', 'btnWin10', 'btnWin30'].forEach(id => document.getElementById(id).classList.remove('active'));
      document.getElementById('btnWin' + sec).classList.add('active');
    }
    function togglePause() {
      isPaused = !isPaused;
      document.getElementById('btnPause').textContent = isPaused ? 'Resume' : 'Pause';
      document.getElementById('btnPause').classList.toggle('active', isPaused);
    }

    // ──────────────────────────────────────────────────────────────────────────
    // Pre-Flight Surveillance Engine
    // ──────────────────────────────────────────────────────────────────────────
    let preflightPassed = false;
    let cachedTelemetry = null;

    function runPreflightCheck() {
      if (!cachedTelemetry) {
        alert('Cannot run pre-flight: no telemetry received from server.');
        return;
      }
      const data = cachedTelemetry;
      const connPass = data.connection === 'connected';
      const hzPass = data.liveHz >= 42.0 && data.liveHz <= 58.0;
      const stateLower = data.state.toLowerCase();
      const statePass = stateLower.includes('normal') && !stateLower.includes('calibrat') && !stateLower.includes('alert') && !stateLower.includes('fall');
      const faultPass = data.sensorState !== 'FAULT' && data.Q >= 0.50;

      const pfConn = document.getElementById('pfConn');
      pfConn.textContent = connPass ? 'PASS (Connected)' : 'FAIL (Disconnected)';
      pfConn.className = connPass ? 'pf-pass' : 'pf-fail';

      const pfHz = document.getElementById('pfHz');
      pfHz.textContent = hzPass ? `PASS (${data.liveHz.toFixed(1)} Hz)` : `FAIL (${data.liveHz.toFixed(1)} Hz)`;
      pfHz.className = hzPass ? 'pf-pass' : 'pf-fail';

      const pfCal = document.getElementById('pfCal');
      pfCal.textContent = statePass ? 'PASS (Normal)' : `FAIL (${data.state})`;
      pfCal.className = statePass ? 'pf-pass' : 'pf-fail';

      const pfFault = document.getElementById('pfFault');
      pfFault.textContent = faultPass ? `PASS (Q=${data.Q.toFixed(2)})` : `FAIL (Fault Q=${data.Q.toFixed(2)})`;
      pfFault.className = faultPass ? 'pf-pass' : 'pf-fail';

      const msg = document.getElementById('preflightStatusMsg');
      const btn = document.getElementById('btnRecord');

      if (connPass && hzPass && statePass && faultPass) {
        preflightPassed = true;
        btn.disabled = false;
        btn.textContent = '▶ Record Trial (Ready)';
        msg.textContent = '✅ Pre-flight passed! Recording unlocked.';
        msg.style.color = 'var(--green)';
      } else {
        preflightPassed = false;
        btn.disabled = true;
        btn.textContent = '▶ Record Trial (Pass Pre-Flight First)';
        msg.textContent = '❌ Pre-flight failed. State must be Normal and hardware healthy.';
        msg.style.color = 'var(--red)';
      }
    }

    // ──────────────────────────────────────────────────────────────────────────
    // Telemetry Polling & Live State Sync
    // ──────────────────────────────────────────────────────────────────────────
    let lastEventCount = 0;
    let pendingAction = null;

    async function pollTelemetry() {
      try {
        const res = await fetch('/api/telemetry');
        if (!res.ok) throw new Error('API offline');
        const data = await res.json();
        cachedTelemetry = data;

        // Invalidate preflight if connection drops or fault occurs
        if (preflightPassed && (data.connection !== 'connected' || data.sensorState === 'FAULT')) {
          preflightPassed = false;
          const btn = document.getElementById('btnRecord');
          if (!trialRecordingActive) {
            btn.disabled = true;
            btn.textContent = '▶ Record Trial (Pre-Flight Invalidated)';
          }
          document.getElementById('preflightStatusMsg').textContent = '⚠️ Hardware condition changed. Re-run pre-flight check.';
          document.getElementById('preflightStatusMsg').style.color = 'var(--amber)';
        }

        const dot = document.getElementById('connDot');
        const txt = document.getElementById('connText');
        dot.className = 'dot dot-' + data.connection;
        txt.textContent = data.connection === 'connected' ? 'Connected' :
                          data.connection === 'no_data' ? 'No data >2s' : 'Disconnected';

        document.getElementById('hzVal').textContent = data.liveHz.toFixed(1);
        document.getElementById('loopVal').textContent = data.stat.loopMean;
        document.getElementById('heapVal').textContent = data.stat.freeHeap;

        document.getElementById('replayBanner').style.display = data.isReplay ? 'block' : 'none';

        const isFall = data.state.includes('FALL') || data.state.includes('ALERT');
        const isFault = data.sensorState === 'FAULT';
        document.getElementById('fallAlarmBanner').style.display = isFall ? 'flex' : 'none';
        document.getElementById('sensorFaultBanner').style.display = isFault ? 'flex' : 'none';

        document.getElementById('stateVal').textContent = data.state;
        document.getElementById('magVal').textContent = data.mag.toFixed(2) + ' m/s²';
        document.getElementById('jerkVal').textContent = data.jerk.toFixed(1) + ' m/s³';
        document.getElementById('qVal').textContent = data.Q.toFixed(2);
        document.getElementById('eVal').textContent = data.E.toFixed(2);
        document.getElementById('confVal').textContent = data.conf >= 0 ? (data.conf * 100).toFixed(1) + '%' : '--';

        if (!isPaused) {
          const tRel = performance.now() / 1000.0;
          dataBuffer.push([tRel, data.mag, data.jerk]);
          if (dataBuffer.length > 2500) dataBuffer.shift();
        }

        if (data.events && data.events.length !== lastEventCount) {
          lastEventCount = data.events.length;
          const list = document.getElementById('timelineList');
          list.innerHTML = data.events.map(e => {
            let cls = '';
            if (e.includes('ALERT') || e.includes('FALL')) cls = 't-alert';
            else if (e.includes('FAULT')) cls = 't-fault';
            else if (e.includes('TRIAL')) cls = 't-trial';
            return `<div class="timeline-item ${cls}">${e}</div>`;
          }).join('');
        }
      } catch (err) {
        document.getElementById('connDot').className = 'dot dot-disconnected';
        document.getElementById('connText').textContent = 'Server Unreachable';
      }
    }
    setInterval(pollTelemetry, 150);

    // ──────────────────────────────────────────────────────────────────────────
    // Trial Recording Protocol Engine
    // ──────────────────────────────────────────────────────────────────────────
    let trialCounter = 1;
    let trialRecordingActive = false;
    let onsetMarked = false;

    function getTrialActivityCode() {
      const sel = document.getElementById('activitySelect');
      const val = sel.value;
      if (val === 'FALL_FORWARD') return 'FALL_FWD';
      if (val === 'FALL_BACKWARD') return 'FALL_BWD';
      if (val === 'FALL_LATERAL') return 'FALL_LAT';
      if (val === 'FALL_DIAG') return 'FALL_DIAG';
      if (val === 'FALL_WALK') return 'FALL_WALK';
      if (val === 'FALL_SYNCOPE') return 'FALL_SYNC';
      if (val === 'WALKING') return 'WALK';
      if (val === 'RUN') return 'RUN';
      if (val === 'STAIRS') return 'STAIRS';
      if (val === 'SIT_HARD') return 'SIT_HARD';
      if (val === 'SIT_TO_STAND') return 'SIT_STAND';
      if (val === 'LIE_DOWN') return 'LIE_DOWN';
      if (val === 'JUMPING') return 'JUMP';
      if (val === 'STUMBLE') return 'STUMBLE';
      return 'OTHER';
    }

    function updateTrialIdPreview() {
      const pid = document.getElementById('partId').value.trim() || 'P01';
      const code = getTrialActivityCode();
      const num = String(trialCounter).padStart(2, '0');
      document.getElementById('trialIdPreview').textContent = `${pid}_${code}_${num}`;
    }

    function onActivityChange() {
      const sel = document.getElementById('activitySelect');
      const isFall = sel.selectedOptions[0].getAttribute('data-fall') === 'true';
      const durInput = document.getElementById('trialDuration');
      const hint = document.getElementById('durHint');

      document.getElementById('customActRow').style.display = sel.value === 'OTHER' ? 'block' : 'none';

      // Enforce minimum 12s trial window for fall trials so device has time to confirm
      if (isFall) {
        durInput.min = "12";
        if (parseInt(durInput.value, 10) < 12) {
          durInput.value = "12";
        }
        hint.textContent = "Falls ≥ 12s enforced";
        hint.style.color = "var(--accent)";
      } else {
        durInput.min = "4";
        hint.textContent = "ADL standard";
        hint.style.color = "var(--text-muted)";
      }

      updateTrialIdPreview();
    }

    function startTrialCountdown() {
      if (!preflightPassed || !cachedTelemetry || !cachedTelemetry.state.toLowerCase().includes('normal')) {
        alert('Recording is blocked: Device state must be Normal and Pre-Flight check must pass.');
        return;
      }
      if (trialRecordingActive) return;

      const box = document.getElementById('countdownBox');
      const digit = document.getElementById('countdownDigit');
      const sub = document.getElementById('countdownSub');
      const btn = document.getElementById('btnRecord');

      btn.disabled = true;
      box.style.display = 'block';
      onsetMarked = false;
      document.getElementById('onsetAlert').style.display = 'none';
      document.getElementById('trialSummaryCard').style.display = 'none';
      document.getElementById('ackStatusBox').style.display = 'none';

      let count = 3;
      digit.textContent = count;
      sub.textContent = 'Prepare for motion...';

      const timer = setInterval(() => {
        count--;
        if (count > 0) {
          digit.textContent = count;
        } else if (count === 0) {
          digit.textContent = 'GO!';
          sub.textContent = 'RECORDING TRIAL NOW';
          executeTrialStart();
        } else {
          clearInterval(timer);
          runTrialTimer();
        }
      }, 1000);
    }

    async function executeTrialStart() {
      trialRecordingActive = true;
      const trialId = document.getElementById('trialIdPreview').textContent;
      const sel = document.getElementById('activitySelect');
      const isFall = sel.selectedOptions[0].getAttribute('data-fall') === 'true';
      let durSec = parseInt(document.getElementById('trialDuration').value, 10) || 12;
      if (isFall && durSec < 12) durSec = 12;

      const part = document.getElementById('partId').value.trim() || 'P01';
      const placement = document.getElementById('placementSelect').value;
      const orientation = document.getElementById('orientSelect').value;

      await fetch('/api/trial/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          trial_id: trialId,
          participant: part,
          key: sel.value,
          activity: sel.value === 'OTHER' ? document.getElementById('customActName').value : sel.value,
          is_fall: isFall,
          placement: placement,
          orientation: orientation,
          duration_s: durSec
        })
      });
    }

    function runTrialTimer() {
      const sel = document.getElementById('activitySelect');
      const isFall = sel.selectedOptions[0].getAttribute('data-fall') === 'true';
      let durSec = parseInt(document.getElementById('trialDuration').value, 10) || 12;
      if (isFall && durSec < 12) durSec = 12;

      const digit = document.getElementById('countdownDigit');
      const sub = document.getElementById('countdownSub');
      let remain = durSec;

      digit.textContent = remain + 's';
      sub.textContent = 'Press SPACE to mark fall movement onset';

      const interval = setInterval(async () => {
        remain--;
        if (remain > 0) {
          digit.textContent = remain + 's';
        } else {
          clearInterval(interval);
          document.getElementById('countdownBox').style.display = 'none';
          await finishTrial();
        }
      }, 1000);
    }

    async function finishTrial() {
      trialRecordingActive = false;
      // Pre-flight check is strictly invalidated upon trial finish
      preflightPassed = false;
      const btn = document.getElementById('btnRecord');
      btn.disabled = true;
      btn.textContent = '▶ Record Trial (Pass Pre-Flight First)';

      const res = await fetch('/api/trial/end', { method: 'POST' });
      const summary = await res.json();

      // Display on-screen trial outcome
      const sumCard = document.getElementById('trialSummaryCard');
      sumCard.style.display = 'block';
      document.getElementById('sumTitle').textContent = `Outcome for ${summary.trial_id}:`;
      document.getElementById('sumDetected').textContent = summary.detected ? 'YES (Confirmed)' : 'NO (Unconfirmed)';
      document.getElementById('sumDetected').style.color = summary.detected ? '#f87171' : '#4ade80';
      document.getElementById('sumFault').textContent = summary.sensor_fault ? 'FAULT DETECTED' : 'NONE (Healthy)';
      document.getElementById('sumFault').style.color = summary.sensor_fault ? '#c084fc' : '#4ade80';
      document.getElementById('sumLatency').textContent = summary.latency_ms ? `${summary.latency_ms} ms (${summary.latency_ref})` : 'N/A';
      document.getElementById('sumSamples').textContent = summary.samples;
      document.getElementById('sumOrphans').textContent = summary.orphan_alerts;

      // Start Post-Trial ACK Clearance Monitor
      monitorPostTrialAck();

      trialCounter++;
      updateTrialIdPreview();
    }

    function monitorPostTrialAck() {
      const ackBox = document.getElementById('ackStatusBox');
      const ackTitle = document.getElementById('ackTitle');
      const ackDetail = document.getElementById('ackDetail');
      ackBox.style.display = 'block';

      const pollAck = setInterval(async () => {
        try {
          const res = await fetch('/api/device/ack_status');
          const data = await res.json();

          if (data.in_progress) {
            ackTitle.textContent = 'Clearing Alarms (ACK Loop Active)...';
            ackDetail.textContent = `Sending ACK every 500ms... Current device state: ${data.state}`;
          } else {
            clearInterval(pollAck);
            if (data.warning) {
              ackTitle.textContent = '⚠️ Alarm Clearance Warning';
              ackDetail.textContent = data.warning;
              ackBox.style.borderColor = 'var(--red)';
              ackBox.style.color = '#fecaca';
            } else {
              ackTitle.textContent = '✅ Device State Returned to Normal';
              ackDetail.textContent = 'Alarms cleared. Verifying pre-flight conditions...';
              ackBox.style.borderColor = 'var(--green)';
              ackBox.style.color = '#dcfce7';
              runPreflightCheck();
            }
          }
        } catch (e) {
          clearInterval(pollAck);
        }
      }, 400);
    }

    // Spacebar Onset Marker (Strictly PC Wall Clock)
    window.addEventListener('keydown', (e) => {
      if (e.code === 'Space' && trialRecordingActive && !onsetMarked) {
        e.preventDefault();
        onsetMarked = true;
        fetch('/api/trial/mark_onset', { method: 'POST' });
        const el = document.getElementById('onsetAlert');
        el.style.display = 'block';
        setTimeout(() => { el.style.display = 'none'; }, 2500);
      }
    });

    // ──────────────────────────────────────────────────────────────────────────
    // Guided Protocol Session Planner
    // ──────────────────────────────────────────────────────────────────────────
    let guidedQueue = [];
    let guidedIndex = 0;

    function planGuidedSession() {
      const mins = parseInt(document.getElementById('sessionLenMin').value, 10) || 15;
      const totalSec = mins * 60;
      const trialBlockSec = 12 + 8;
      const totalTrials = Math.max(4, Math.floor(totalSec / trialBlockSec));

      const fallTypes = ['FALL_FORWARD', 'FALL_BACKWARD', 'FALL_LATERAL', 'FALL_DIAG', 'FALL_WALK'];
      const adlTypes = ['WALKING', 'RUN', 'STAIRS', 'SIT_HARD', 'SIT_TO_STAND', 'LIE_DOWN', 'JUMPING', 'STUMBLE'];

      guidedQueue = [];
      for (let i = 0; i < totalTrials; i++) {
        const isFall = (i % 3 === 0);
        const act = isFall ? fallTypes[i % fallTypes.length] : adlTypes[i % adlTypes.length];
        guidedQueue.push(act);
      }
      guidedIndex = 0;

      document.getElementById('guidedPlanView').style.display = 'block';
      updateGuidedView();
    }

    function updateGuidedView() {
      if (guidedIndex < guidedQueue.length) {
        document.getElementById('guidedNow').textContent = guidedQueue[guidedIndex];
        document.getElementById('guidedNext').textContent = (guidedIndex + 1 < guidedQueue.length) ? guidedQueue[guidedIndex + 1] : 'Session Complete';
      } else {
        document.getElementById('guidedNow').textContent = 'All Planned Trials Complete!';
        document.getElementById('guidedNext').textContent = 'None';
      }
    }

    function executeGuidedStep() {
      if (!preflightPassed) {
        alert('Cannot start next guided trial: Device must be Normal and Pre-Flight check must pass.');
        return;
      }
      if (guidedIndex >= guidedQueue.length) return;
      const act = guidedQueue[guidedIndex];
      document.getElementById('activitySelect').value = act;
      onActivityChange();
      startTrialCountdown();
      guidedIndex++;
      updateGuidedView();
    }

    // ──────────────────────────────────────────────────────────────────────────
    // Confirmation Modals
    // ──────────────────────────────────────────────────────────────────────────
    function confirmCancel() {
      pendingAction = () => fetch('/api/device/cancel', { method: 'POST' });
      document.getElementById('modalMessage').textContent =
        'Notice: Sending CANCEL trains the online Naive Bayes classifier with a Non-Fall label and increases theta (+0.02). Use only for verified false alarms.';
      document.getElementById('confirmModal').style.display = 'flex';
    }
    function confirmAck() {
      fetch('/api/device/ack', { method: 'POST' });
    }
    function previewTheta(v) {
      document.getElementById('thetaDisplay').textContent = (v / 100.0).toFixed(2);
    }
    function confirmSetTheta() {
      const val = (document.getElementById('thetaRange').value / 100.0).toFixed(2);
      pendingAction = () => fetch('/api/device/set_theta?v=' + val, { method: 'POST' });
      document.getElementById('modalMessage').textContent =
        `Notice: Setting theta to ${val} modifies the classifier decision threshold directly on the device.`;
      document.getElementById('confirmModal').style.display = 'flex';
    }
    function closeModal() {
      document.getElementById('confirmModal').style.display = 'none';
      pendingAction = null;
    }
    function runConfirmedAction() {
      if (pendingAction) pendingAction();
      closeModal();
    }

    // ──────────────────────────────────────────────────────────────────────────
    // Ports & Theme
    // ──────────────────────────────────────────────────────────────────────────
    async function loadPorts() {
      try {
        const res = await fetch('/api/ports');
        const list = await res.json();
        const sel = document.getElementById('portSelect');
        sel.innerHTML = '<option value="AUTO">Auto-Port</option>';
        list.forEach(p => {
          const opt = document.createElement('option');
          opt.value = p.device;
          opt.textContent = `${p.device} (${p.description.slice(0, 16)})`;
          sel.appendChild(opt);
        });
      } catch (e) {}
    }
    loadPorts();

    function switchPort(p) { fetch('/api/ports/switch?port=' + encodeURIComponent(p), { method: 'POST' }); }
    function reconnectPort() { fetch('/api/ports/reconnect', { method: 'POST' }); }
    function clearEvents() { fetch('/api/events/clear', { method: 'POST' }); }

    function toggleTheme() {
      const cur = document.documentElement.getAttribute('data-theme') || 'dark';
      const next = cur === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
    }
  </script>
</body>
</html>
"""


# ──────────────────────────────────────────────────────────────────────────────
# Startup Self-Check: Strict Offline Verification
# ──────────────────────────────────────────────────────────────────────────────

def verify_offline_compliance(html_content: str):
    """
    Ensure the dashboard contains NO external HTTP/HTTPS assets.
    Excludes standard XML namespaces (e.g. xmlns="http://www.w3.org/2000/svg").
    Fails loudly if any external dependency is found.
    """
    url_pattern = re.compile(r'(?:src|href|url)\s*=\s*["\'](https?://[^"\']+)["\']', re.IGNORECASE)
    matches = url_pattern.findall(html_content)
    external_assets = [m for m in matches if "w3.org" not in m]

    if external_assets:
        raise RuntimeError(
            f"[OFFLINE VIOLATION ERROR] Found external network URLs in dashboard:\n" +
            "\n".join(f"  - {u}" for u in external_assets)
        )
    print("[OFFLINE CHECK] offline OK — zero external network assets detected.")


# ──────────────────────────────────────────────────────────────────────────────
# Serial Communications & Data Parsing
# ──────────────────────────────────────────────────────────────────────────────

def send_serial_cmd(cmd: str):
    """Safely deliver a command string to the ESP32 over serial."""
    global serial_inst
    with serial_lock:
        if serial_inst and serial_inst.is_open:
            try:
                serial_inst.write(cmd.encode("utf-8"))
            except Exception as e:
                print(f"[SERIAL WRITE ERROR] {e}")


def ack_until_normal_worker():
    """Keeps sending ACK every 500 ms until device state is Normal (timeout 15s)."""
    global ack_loop_active, ack_timeout_warning
    ack_loop_active = True
    ack_timeout_warning = None
    start_t = time.time()

    print("[ACK LOOP] Initiating post-trial alarm clearance loop...")
    while time.time() - start_t < 15.0:
        with state_lock:
            st = latest_state["state"].strip().lower()
        if "normal" in st and "calibrat" not in st and "alert" not in st and "fall" not in st:
            ack_loop_active = False
            print("[ACK LOOP] Device successfully returned to Normal state.")
            return

        send_serial_cmd("ACK\n")
        time.sleep(0.5)

    ack_loop_active = False
    ack_timeout_warning = "Device did not return to Normal state within 15 seconds. Please press device button or reset."
    print(f"[ACK WARNING] {ack_timeout_warning}")


def parse_telemetry_line(line: str):
    """Parse incoming firmware telemetry lines matching the v2 protocol."""
    global latest_state, last_telemetry_time, sample_timestamps, live_hz, active_trial
    global orphan_alerts_since_last_trial, in_orphan_alert_state

    now = time.time()
    last_telemetry_time = now
    sample_timestamps.append(now)

    # Maintain 1-second sliding window for Hz
    sample_timestamps = [t for t in sample_timestamps if now - t <= 1.0]
    live_hz = float(len(sample_timestamps))

    line = line.strip()
    if not line:
        return

    # Diagnostic Statistics Line: # STAT,<loop_mean>,<loop_max>,<hz>,<free_heap>,<min_heap>
    if line.startswith("# STAT,"):
        parts = line.split(",")
        if len(parts) >= 6:
            try:
                stat_metrics["loop_mean_us"] = int(float(parts[1]))
                stat_metrics["loop_max_us"] = int(float(parts[2]))
                stat_metrics["actual_hz"] = float(parts[3])
                stat_metrics["free_heap"] = int(float(parts[4]))
                stat_metrics["min_free_heap"] = int(float(parts[5]))
            except ValueError:
                pass
        return

    # Event Line: EVENT,<ms>,<msg>
    if line.startswith("EVENT,"):
        parts = line.split(",")
        msg = ",".join(parts[2:]) if len(parts) > 2 else line
        timestamp_s = int(parts[1]) / 1000.0 if len(parts) > 1 and parts[1].isdigit() else time.time()
        event_str = f"{timestamp_s:.1f}s — {msg}"

        with state_lock:
            latest_state["events"].insert(0, event_str)
            if len(latest_state["events"]) > 50:
                latest_state["events"].pop()

            if active_trial is not None:
                if "SENSOR_FAULT" in msg:
                    active_trial["sensor_fault"] = True
            else:
                # Orphan event outside active trial window
                if "ALERT_SENT" in msg or "FALL_CONFIRMED" in msg:
                    orphan_alerts_since_last_trial += 1
                    print(f"[ORPHAN EVENT] Alert event outside trial: {msg} (Total orphans: {orphan_alerts_since_last_trial})")
        return

    # Extended Telemetry Line:
    # TELEMETRY,<ms>,<mag>,<jerk>,<stateStr>,<conf>,<activity>,<fallCount>,<nonFallCount>,<theta>,<impThresh>,<jerkThresh>,<trial_id>,<Q>,<E>,<sensor_state>
    if line.startswith("TELEMETRY,"):
        parts = line.split(",")
        if len(parts) >= 11:
            try:
                ms = int(parts[1])
                mag = float(parts[2])
                jerk = float(parts[3])
                state_str = parts[4].strip()
                conf = float(parts[5])
                act = parts[6].strip()
                fc = int(parts[7])
                nfc = int(parts[8])
                th = float(parts[9])
                imp = float(parts[10])
                jk = float(parts[11]) if len(parts) > 11 else 220.0
                t_id = parts[12].strip() if len(parts) > 12 else "NONE"
                q_val = float(parts[13]) if len(parts) > 13 else 1.0
                e_val = float(parts[14]) if len(parts) > 14 else 0.0
                sensor_st = parts[15].strip() if len(parts) > 15 else "OK"

                now_wall_ms = int(now * 1000)

                is_alert_state = (
                    "FALL_CONFIRMED" in state_str or "ALERT_SENT" in state_str or 
                    state_str.startswith("FALL CONFIRMED") or state_str.startswith("ALERT SENT")
                )

                with state_lock:
                    latest_state.update({
                        "connection": "connected",
                        "liveHz": live_hz,
                        "stat": dict(stat_metrics),
                        "state": state_str,
                        "mag": mag,
                        "jerk": jerk,
                        "conf": conf,
                        "activity": act,
                        "fallCount": fc,
                        "nonFallCount": nfc,
                        "theta": th,
                        "impThresh": imp,
                        "jerkThresh": jk,
                        "trialId": t_id,
                        "Q": q_val,
                        "E": e_val,
                        "sensorState": sensor_st,
                    })

                    # Trial Surveillance Rules:
                    # 1. 'detected' set ONLY during active trial by FALL_CONFIRMED or ALERT_SENT
                    # 2. Alerts outside active trial are logged as orphan_alerts
                    if active_trial is not None:
                        active_trial["samples"] += 1
                        if is_alert_state:
                            active_trial["detected"] = True
                            if active_trial["detect_wall_ms"] is None:
                                active_trial["detect_wall_ms"] = now_wall_ms  # PC Clock

                        if sensor_st == "FAULT" or state_str == "SENSOR_FAULT":
                            active_trial["sensor_fault"] = True

                        active_trial["samples_data"].append([
                            active_trial["trial_id"], now_wall_ms, ms, mag, jerk, state_str, conf, act, q_val, e_val
                        ])
                    else:
                        # Outside active trial window: monitor orphan alert transitions
                        if is_alert_state:
                            if not in_orphan_alert_state:
                                in_orphan_alert_state = True
                                orphan_alerts_since_last_trial += 1
                                print(f"[ORPHAN ALERT] Alert outside trial: {state_str} (Total orphans: {orphan_alerts_since_last_trial})")
                        else:
                            in_orphan_alert_state = False
            except Exception:
                pass


def auto_detect_serial_port() -> str | None:
    """Scan available serial ports for known USB-to-UART bridge controllers."""
    ports = list(serial.tools.list_ports.comports())
    for p in ports:
        desc = (p.description or "").lower()
        if "cp210" in desc or "ch340" in desc or "uart" in desc or "usb serial" in desc:
            return p.device
    for p in ports:
        if "com6" in (p.device or "").lower():
            return p.device
    return ports[0].device if ports else None


def serial_supervision_thread():
    """Continuous thread for hotplug detection and serial communication."""
    global serial_inst, connection_status, active_port_name

    while not stop_threads:
        if is_replay_mode:
            time.sleep(0.5)
            continue

        target_port = None
        if active_port_name == "AUTO":
            target_port = auto_detect_serial_port()
        else:
            target_port = active_port_name

        if not target_port:
            connection_status = "disconnected"
            time.sleep(1.5)
            continue

        try:
            with serial_lock:
                serial_inst = serial.Serial(target_port, 115200, timeout=1.0)
            connection_status = "connected"
            print(f"[SERIAL] Connected to ESP32 on {target_port} @ 115200 baud.")

            while not stop_threads and (active_port_name in ("AUTO", target_port)):
                try:
                    line = serial_inst.readline().decode("utf-8", errors="replace")
                    if line:
                        parse_telemetry_line(line)
                    else:
                        if time.time() - last_telemetry_time > 2.0:
                            connection_status = "no_data"
                except (serial.SerialException, OSError):
                    print(f"[SERIAL] Lost connection to {target_port}. Attempting auto-reconnect...")
                    break
        except Exception as e:
            connection_status = "disconnected"
            time.sleep(1.5)
        finally:
            with serial_lock:
                if serial_inst:
                    try:
                        serial_inst.close()
                    except Exception:
                        pass
                    serial_inst = None


def replay_worker_thread(csv_path: Path):
    """Replays prerecorded CSV telemetry strictly for UI verification (Never saves to data/)."""
    global connection_status, is_replay_mode
    is_replay_mode = True
    connection_status = "connected"

    print(f"[REPLAY] Starting mock replay from {csv_path.name} @ ~50 Hz...")
    while not stop_threads:
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                header = f.readline()
                for line in f:
                    if stop_threads:
                        break
                    parts = line.strip().split(",")
                    if len(parts) >= 6:
                        ms = parts[0]
                        mag = parts[1]
                        jerk = parts[2]
                        st = parts[3]
                        cf = parts[4]
                        ac = parts[5]
                        sim_line = f"TELEMETRY,{ms},{mag},{jerk},{st},{cf},{ac},0,0,0.65,11.5,220.0,NONE,1.0,0.0,OK"
                        parse_telemetry_line(sim_line)
                    time.sleep(0.02)  # 50 Hz emulation
        except Exception as e:
            print(f"[REPLAY ERROR] {e}")
            time.sleep(1.0)


# ──────────────────────────────────────────────────────────────────────────────
# HTTP Server Request Handler
# ──────────────────────────────────────────────────────────────────────────────

class OfflineDashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))

        elif path == "/api/telemetry":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            with state_lock:
                latest_state["connection"] = connection_status
                latest_state["isReplay"] = is_replay_mode
                copy_state = dict(latest_state)
            self.wfile.write(json.dumps(copy_state).encode("utf-8"))

        elif path == "/api/device/ack_status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            with state_lock:
                st = latest_state["state"]
                is_norm = ("normal" in st.lower() and "calibrat" not in st.lower() and "alert" not in st.lower() and "fall" not in st.lower())
                resp = {
                    "in_progress": ack_loop_active,
                    "warning": ack_timeout_warning,
                    "state": st,
                    "isNormal": is_norm,
                }
            self.wfile.write(json.dumps(resp).encode("utf-8"))

        elif path == "/api/ports":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            ports_list = [
                {"device": p.device, "description": p.description or ""}
                for p in serial.tools.list_ports.comports()
            ]
            self.wfile.write(json.dumps(ports_list).encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else ""

        global active_port_name, active_trial, pilot_dir, orphan_alerts_since_last_trial

        if path == "/api/ports/switch":
            port = params.get("port", ["AUTO"])[0]
            active_port_name = port
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        elif path == "/api/ports/reconnect":
            with serial_lock:
                if serial_inst:
                    try:
                        serial_inst.close()
                    except Exception:
                        pass
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        elif path == "/api/device/cancel":
            send_serial_cmd("CANCEL\n")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        elif path == "/api/device/ack":
            send_serial_cmd("ACK\n")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        elif path == "/api/device/set_theta":
            v = params.get("v", ["0.65"])[0]
            send_serial_cmd(f"SET_THETA {v}\n")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        elif path == "/api/events/clear":
            with state_lock:
                latest_state["events"] = []
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        elif path == "/api/trial/start":
            data = json.loads(body) if body else {}
            tid = data.get("trial_id", "TRIAL_01")
            part = data.get("participant", "P01")
            key = data.get("key", "WALKING")
            act = data.get("activity", "WALKING")
            is_f = bool(data.get("is_fall", False))
            pl = data.get("placement", "waist")
            ori = data.get("orientation", "front")
            dur = float(data.get("duration_s", 10.0))

            # Enforce minimum 12 seconds for fall trials
            if is_f:
                dur = max(12.0, dur)

            send_serial_cmd(f"TRIAL,{tid}\n")
            cue_ms = int(time.time() * 1000)  # PC Clock

            with state_lock:
                active_trial = {
                    "trial_id": tid,
                    "participant": part,
                    "key": key,
                    "activity": act,
                    "is_fall": is_f,
                    "placement": pl,
                    "orientation": ori,
                    "duration_s": dur,
                    "cue_wall_ms": cue_ms,
                    "onset_wall_ms": None,
                    "detect_wall_ms": None,
                    "samples": 0,
                    "detected": False,
                    "sensor_fault": False,
                    "samples_data": [],
                }

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        elif path == "/api/trial/mark_onset":
            with state_lock:
                if active_trial is not None and active_trial["onset_wall_ms"] is None:
                    active_trial["onset_wall_ms"] = int(time.time() * 1000)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        elif path == "/api/trial/end":
            send_serial_cmd("TRIAL,\n")

            with state_lock:
                if active_trial is not None:
                    tr = active_trial
                    active_trial = None
                else:
                    tr = None

            if not tr:
                self.send_response(400)
                self.end_headers()
                return

            # Start persistent ACK loop in background
            threading.Thread(target=ack_until_normal_worker, daemon=True).start()

            # Latency strictly computed via PC wall clock difference
            latency_ms = ""
            latency_ref = "none"
            if tr["detected"] and tr["onset_wall_ms"] and tr["detect_wall_ms"]:
                diff = tr["detect_wall_ms"] - tr["onset_wall_ms"]
                if diff > 0:
                    latency_ms = str(diff)
                    latency_ref = "onset_marker"

            orphans_to_log = orphan_alerts_since_last_trial
            orphan_alerts_since_last_trial = 0  # Reset for next trial

            summary = {
                "trial_id": tr["trial_id"],
                "detected": tr["detected"],
                "sensor_fault": tr["sensor_fault"],
                "latency_ms": latency_ms,
                "latency_ref": latency_ref,
                "samples": tr["samples"],
                "orphan_alerts": orphans_to_log,
            }

            with state_lock:
                latest_state["lastTrialSummary"] = summary

            # Autosave directly to data/pilotN/ (suppressed in replay mode)
            if not is_replay_mode:
                if not pilot_dir:
                    pilot_dir = get_or_create_pilot_dir()

                trials_csv = pilot_dir / "trials.csv"
                write_header = not trials_csv.exists()
                with open(trials_csv, "a", encoding="utf-8") as f:
                    if write_header:
                        f.write("trial_id,participant,key,activity,is_fall,placement,orientation,cue_wall_ms,onset_wall_ms,duration_s,samples,detected,sensor_fault,latency_ms,latency_ref,orphan_alerts\n")
                    f.write(
                        f"{tr['trial_id']},{tr['participant']},{tr['key']},{tr['activity']},{tr['is_fall']},{tr['placement']},{tr['orientation']},"
                        f"{tr['cue_wall_ms']},{tr['onset_wall_ms'] or ''},{tr['duration_s']},{tr['samples']},{tr['detected']},{tr['sensor_fault']},"
                        f"{latency_ms},{latency_ref},{orphans_to_log}\n"
                    )

                samples_csv = pilot_dir / "samples.csv"
                write_sample_header = not samples_csv.exists()
                with open(samples_csv, "a", encoding="utf-8") as f:
                    if write_sample_header:
                        f.write("trial_id,wall_ms,device_ms,mag,jerk,state,conf,activity,Q,E\n")
                    for s in tr["samples_data"]:
                        f.write(",".join(map(str, s)) + "\n")

                print(f"[AUTOSAVE] Saved {tr['trial_id']} ({tr['samples']} samples, {orphans_to_log} orphan alerts) to {pilot_dir.name}/")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(summary).encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Main Application Entrypoint
# ──────────────────────────────────────────────────────────────────────────────

def main():
    global is_replay_mode, active_port_name, pilot_dir, stop_threads

    parser = argparse.ArgumentParser(description="FallGuard v2 Offline USB Lab Dashboard")
    parser.add_argument("pos_port", nargs="?", default=None, help="Optional positional serial port (e.g. COM6)")
    parser.add_argument("--port", "-p", type=str, default="AUTO", help="USB Serial port (e.g. COM6 or AUTO)")
    parser.add_argument("--web-port", "-w", type=int, default=DEFAULT_PORT, help="Local HTTP server port (default: 5000)")
    parser.add_argument("--replay", type=str, default=None, help="Replay prerecorded telemetry CSV (MOCK ONLY — NOT LIVE)")
    parser.add_argument("--pilot", type=str, default=None, help="Pilot session identifier (e.g. pilot1)")
    args = parser.parse_args()

    # Step 1: Strict Offline Compliance Verification
    verify_offline_compliance(DASHBOARD_HTML)

    active_port_name = args.pos_port if args.pos_port is not None else args.port

    # Step 2: Serial / Replay Thread Launch
    if args.replay:
        rep_file = Path(args.replay).resolve()
        if not rep_file.exists():
            print(f"[ERROR] Replay CSV file not found: {rep_file}")
            sys.exit(1)
        th = threading.Thread(target=replay_worker_thread, args=(rep_file,), daemon=True)
        th.start()
    else:
        th = threading.Thread(target=serial_supervision_thread, daemon=True)
        th.start()

    # Step 3: Local HTTP Server (127.0.0.1 Bound)
    server_address = (DEFAULT_HOST, args.web_port)
    try:
        httpd = HTTPServer(server_address, OfflineDashboardHandler)
    except OSError as e:
        print(f"[ERROR] Port {args.web_port} is busy: {e}. Try --web-port 5001.")
        sys.exit(1)

    url = f"http://{DEFAULT_HOST}:{args.web_port}"
    print("=" * 70)
    print("  FallGuard v2 — Offline Laboratory Dashboard")
    print("=" * 70)
    print(f"  URL              : {url}")
    print(f"  Bind Address     : {DEFAULT_HOST} (Localhost Only)")
    print(f"  Serial Port      : {active_port_name}")
    if args.replay:
        print(f"  Mode             : REPLAY / NOT LIVE ({Path(args.replay).name})")
    else:
        print("  Mode             : LIVE HARDWARE SURVEILLANCE")
    print("=" * 70)

    # Launch browser on startup
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] Dashboard stopped by user.")
    finally:
        stop_threads = True
        with serial_lock:
            if serial_inst and serial_inst.is_open:
                serial_inst.close()


if __name__ == "__main__":
    main()
