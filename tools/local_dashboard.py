"""
FallGuard v2 — USB Local Web Dashboard
=======================================
Reads ESP32 telemetry directly from USB Serial (e.g. COM6)
and serves a live web dashboard on http://localhost:5000

Features:
  - 100% offline, zero Wi-Fi or phone hotspot needed
  - Real-time Chart.js graph of Magnitude & Jerk (50 Hz)
  - Live Classifier Confidence plot & lifelong learning stats
  - Two-way controls: Cancel false alarm, Acknowledge alert, Adjust theta
  - CSV recording for research paper evaluation & plotting
"""

import sys
import os
import time
import json
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("\n[ERROR] 'pyserial' is not installed. Run: pip install pyserial\n")
    sys.exit(1)

# Global State
state_lock = threading.Lock()
telemetry_history = []
MAX_HISTORY = 180  # Keep last ~180 points for smooth graphing

latest_state = {
    "state": "Waiting for ESP32...",
    "mag": 9.81,
    "jerk": 0.0,
    "conf": -1.0,
    "activity": "STILL",
    "fallCount": 0,
    "nonFallCount": 0,
    "theta": 0.65,
    "impThresh": 22.0,
    "jerkThresh": 500.0,
    "events": []
}

serial_inst = None
csv_file = None
csv_writer = None
session_start_time = None

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>FallGuard v2 — USB Local Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Segoe UI', Arial, sans-serif; background: #0b1120; color: #e2e8f0; padding: 18px; }
    header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; border-bottom: 1px solid #1e293b; padding-bottom: 12px; }
    h1 { font-size: 1.5rem; font-weight: 700; color: #38bdf8; display: flex; align-items: center; gap: 8px; }
    .usb-badge { background: #0284c7; color: #fff; font-size: 0.75rem; padding: 3px 10px; border-radius: 999px; font-weight: 600; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; margin-bottom: 16px; }
    .card { background: #1e293b; border-radius: 10px; padding: 16px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); border: 1px solid #334155; }
    .label { font-size: 0.7rem; text-transform: uppercase; color: #94a3b8; letter-spacing: 1px; margin-bottom: 6px; font-weight: 600; }
    .value { font-size: 1.6rem; font-weight: 700; color: #38bdf8; }
    .status { font-size: 1.05rem; font-weight: 700; }
    .s-normal { color: #4ade80; }
    .s-warn { color: #fb923c; }
    .s-danger { color: #f87171; animation: pulse 1s infinite alternate; }
    .s-cal { color: #60a5fa; }
    @keyframes pulse { from { opacity: 0.7; } to { opacity: 1; transform: scale(1.02); } }
    .chart-card { background: #1e293b; border-radius: 10px; padding: 16px; margin-bottom: 14px; border: 1px solid #334155; }
    canvas { width: 100%!important; height: 210px!important; }
    .log-card { background: #1e293b; border-radius: 10px; padding: 16px; margin-bottom: 14px; border: 1px solid #334155; max-height: 220px; overflow-y: auto; }
    .log-entry { font-family: 'Consolas', monospace; font-size: 0.8rem; color: #94a3b8; padding: 4px 0; border-bottom: 1px solid #334155; }
    .log-entry:first-child { color: #f1f5f9; font-weight: bold; }
    .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 14px; }
    button { color: #fff; border: none; padding: 12px 20px; border-radius: 8px; font-size: 0.95rem; cursor: pointer; font-weight: 600; flex: 1; transition: background 0.2s; }
    .btn-cancel { background: #dc2626; } .btn-cancel:hover { background: #b91c1c; }
    .btn-ack { background: #2563eb; } .btn-ack:hover { background: #1d4ed8; }
    .btn-export { background: #059669; } .btn-export:hover { background: #047857; }
    input[type=range] { width: 100%; accent-color: #38bdf8; }
    .theta-val { font-size: 1.1rem; font-weight: 700; color: #38bdf8; margin-left: 10px; }
  </style>
</head>
<body>
<header>
  <h1><span>🛡️</span> FallGuard v2 </h1>
  <span class="usb-badge">⚡ USB Serial Mode (COM6)</span>
</header>

<div class="grid">
  <div class="card"><div class="label">System Status</div>
    <div class="status" id="status">Connecting...</div></div>
  <div class="card"><div class="label">Acceleration (m/s²)</div>
    <div class="value" id="accel">--</div></div>
  <div class="card"><div class="label">Jerk (m/s³)</div>
    <div class="value" id="jerk">--</div></div>
  <div class="card"><div class="label">Classifier Confidence</div>
    <div class="value" id="conf">--</div></div>
  <div class="card"><div class="label">Inferred Activity</div>
    <div class="value" id="activity">--</div></div>
  <div class="card"><div class="label">Learned (Falls / Non-Falls)</div>
    <div class="value" id="counts">--</div></div>
  <div class="card"><div class="label">Adaptive Threshold θ</div>
    <div class="value" id="theta">--</div></div>
  <div class="card"><div class="label">Impact / Jerk Thresholds</div>
    <div class="value" id="thresholds" style="font-size:1.05rem">--</div></div>
</div>

<div class="chart-card">
  <div class="label" style="margin-bottom:10px">Live Kinematics — Magnitude (m/s²) &amp; Jerk (/10 m/s³)</div>
  <canvas id="sigChart"></canvas>
</div>

<div class="chart-card">
  <div class="label" style="margin-bottom:10px">Classifier Confidence History — P(FALL)</div>
  <canvas id="confChart"></canvas>
</div>

<div class="log-card">
  <div class="label" style="margin-bottom:8px">Event &amp; Lifelong Learning Log</div>
  <div id="logList"><div class="log-entry">Waiting for events from ESP32...</div></div>
</div>

<div class="card" style="margin-bottom:14px">
  <div class="label">Sensitivity Threshold θ (drag to test adaptive tuning)</div>
  <div style="display:flex;align-items:center;margin-top:8px">
    <input type="range" id="thetaSlider" min="50" max="85" value="65" oninput="setTheta(this.value/100)">
    <span class="theta-val" id="sliderVal">0.65</span>
  </div>
</div>

<div class="row">
  <button class="btn-cancel" onclick="cancelAlert()">🚫 Cancel / False Alarm</button>
  <button class="btn-ack" onclick="ackAlert()">✅ Acknowledge Alert</button>
  <button class="btn-export" onclick="exportData()">💾 Save CSV for Paper</button>
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
  { label: 'Magnitude (m/s²)', data: magData, borderColor: '#38bdf8', borderWidth: 1.6, pointRadius: 0, tension: 0.3 },
  { label: 'Jerk / 10 (m/s³)', data: jerkData, borderColor: '#fb923c', borderWidth: 1.6, pointRadius: 0, tension: 0.3 }
], 'm/s²');

const confChart = mkChart('confChart', [
  { label: 'Classifier Conf', data: confData, borderColor: '#a78bfa', borderWidth: 1.6, pointRadius: 2, tension: 0.3, fill: { target: 'origin', above: 'rgba(167,139,250,.12)' } }
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
    const r = await fetch('/data');
    const d = await r.json();
    const t = new Date().toLocaleTimeString();

    push(labels, t);
    push(magData, d.mag);
    push(jerkData, d.jerk / 10.0);
    push(confData, d.conf >= 0 ? d.conf : null);

    sigChart.update('none');
    confChart.update('none');

    const el = document.getElementById('status');
    el.textContent = d.state;
    el.className = 'status ' + stateClass(d.state);

    document.getElementById('accel').textContent    = d.mag.toFixed(2);
    document.getElementById('jerk').textContent     = d.jerk.toFixed(1);
    document.getElementById('conf').textContent     = d.conf >= 0 ? (d.conf * 100).toFixed(1) + '%' : 'N/A';
    document.getElementById('activity').textContent = d.activity;
    document.getElementById('counts').textContent   = d.fallCount + ' / ' + d.nonFallCount;
    document.getElementById('theta').textContent    = d.theta.toFixed(2);
    document.getElementById('thresholds').textContent =
      'Impact: ' + d.impThresh.toFixed(1) + ' | Jerk: ' + d.jerkThresh.toFixed(0);

    if (!document.getElementById('thetaSlider').matches(':active')) {
      document.getElementById('thetaSlider').value = Math.round(d.theta * 100);
      document.getElementById('sliderVal').textContent = d.theta.toFixed(2);
    }

    if (d.events && d.events.length > 0) {
      let logHtml = '';
      d.events.forEach(e => { logHtml += `<div class="log-entry">${e}</div>`; });
      document.getElementById('logList').innerHTML = logHtml;
    }
  } catch(e) {
    document.getElementById('status').textContent = 'Connecting to USB Serial...';
  }
};

const cancelAlert = async () => { await fetch('/cancel'); };
const ackAlert    = async () => { await fetch('/ack'); };
const setTheta    = async (v) => {
  document.getElementById('sliderVal').textContent = v.toFixed(2);
  await fetch('/setTheta?v=' + v);
};
const exportData  = () => {
  // Use direct navigation to avoid popup blockers and trigger instant file download
  window.location.href = '/export';
};

setInterval(refresh, 250);
refresh();
</script>
</body>
</html>
"""

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode('utf-8'))

        elif path == "/data":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with state_lock:
                data_copy = dict(latest_state)
            self.wfile.write(json.dumps(data_copy).encode('utf-8'))

        elif path == "/cancel":
            send_serial_cmd("CANCEL\n")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        elif path == "/ack":
            send_serial_cmd("ACK\n")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        elif path == "/setTheta":
            v = params.get('v', ['0.65'])[0]
            send_serial_cmd(f"SET_THETA {v}\n")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        elif path == "/export":
            results_dir = os.path.join(os.path.dirname(__file__), "results")
            os.makedirs(results_dir, exist_ok=True)
            filename = f"fallguard_session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            export_path = os.path.join(results_dir, filename)

            with state_lock:
                hist_copy = list(telemetry_history)

            csv_text = "timestamp_ms,mag,jerk,state,conf,activity,fallCount,nonFallCount,theta,impThresh,jerkThresh\n"
            for row in hist_copy:
                csv_text += ",".join(map(str, row)) + "\n"

            # 1. Save local copy on disk inside results/
            try:
                with open(export_path, 'w', encoding='utf-8') as f:
                    f.write(csv_text)
                print(f"[EXPORT] Saved {len(hist_copy)} records to: {export_path}")
            except Exception as e:
                print(f"[EXPORT ERROR] {e}")

            # 2. Trigger browser download directly
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(csv_text.encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress routine HTTP 200 logs to keep console clean
        pass

def send_serial_cmd(cmd):
    global serial_inst
    if serial_inst and serial_inst.is_open:
        try:
            serial_inst.write(cmd.encode('utf-8'))
        except Exception as e:
            print(f"[SERIAL WRITE ERROR] {e}")

def parse_line(line):
    global latest_state, telemetry_history
    line = line.strip()
    if not line:
        return

    # Handle EVENT lines
    if line.startswith("EVENT,"):
        parts = line.split(",")
        msg = ",".join(parts[2:]) if len(parts) > 2 else line
        timestamp_s = int(parts[1]) / 1000.0 if len(parts) > 1 and parts[1].isdigit() else time.time()
        event_str = f"{timestamp_s:.1f}s — {msg}"
        with state_lock:
            latest_state["events"].insert(0, event_str)
            if len(latest_state["events"]) > 20:
                latest_state["events"].pop()
        return

    # Handle TELEMETRY lines (New extended format)
    # TELEMETRY,<ms>,<mag>,<jerk>,<stateStr>,<conf>,<activity>,<fallCount>,<nonFallCount>,<theta>,<impThresh>,<jerkThresh>
    if line.startswith("TELEMETRY,"):
        parts = line.split(",")
        if len(parts) >= 11:
            try:
                ms = int(parts[1])
                mag = float(parts[2])
                jerk = float(parts[3])
                state_str = parts[4]
                conf = float(parts[5])
                act = parts[6]
                fc = int(parts[7])
                nfc = int(parts[8])
                th = float(parts[9])
                imp = float(parts[10])
                jk = float(parts[11]) if len(parts) > 11 else 500.0

                with state_lock:
                    latest_state.update({
                        "state": state_str,
                        "mag": mag,
                        "jerk": jerk,
                        "conf": conf,
                        "activity": act,
                        "fallCount": fc,
                        "nonFallCount": nfc,
                        "theta": th,
                        "impThresh": imp,
                        "jerkThresh": jk
                    })
                    telemetry_history.append([ms, mag, jerk, state_str, conf, act, fc, nfc, th, imp, jk])
                    if len(telemetry_history) > 2000:
                        telemetry_history.pop(0)
            except Exception:
                pass
        return

    # Handle legacy DATA lines:
    # DATA,<ms>,<mag>,<jerk>,<state>,<conf>,<activity>,<label>
    if line.startswith("DATA,"):
        parts = line.split(",")
        if len(parts) >= 7:
            try:
                ms = int(parts[1])
                mag = float(parts[2])
                jerk = float(parts[3])
                state_code = parts[4]
                conf = float(parts[5])
                act = parts[6]
                state_names = {"0":"Calibrating", "1":"Normal", "2":"Free-fall detected", "3":"Impact detected", "4":"Classifying", "5":"FALL CONFIRMED", "6":"ALERT SENT"}
                s_name = state_names.get(state_code, f"State {state_code}")

                with state_lock:
                    latest_state["mag"] = mag
                    latest_state["jerk"] = jerk
                    latest_state["state"] = s_name
                    latest_state["conf"] = conf
                    latest_state["activity"] = act
                    telemetry_history.append([ms, mag, jerk, s_name, conf, act, 0, 0, 0.65, 22.0, 500.0])
                    if len(telemetry_history) > 2000:
                        telemetry_history.pop(0)
            except Exception:
                pass

def serial_reader_thread(port_name):
    global serial_inst
    print(f"\n[INFO] Connecting to ESP32 on {port_name} at 115200 baud...")

    while True:
        try:
            serial_inst = serial.Serial(port_name, 115200, timeout=1)
            print(f"[SUCCESS] Connected to {port_name}! Reading live telemetry...\n")
            while True:
                line = serial_inst.readline().decode('utf-8', errors='ignore')
                if line:
                    parse_line(line)
        except serial.SerialException as e:
            print(f"[RETRY] Port {port_name} not available (is Arduino Serial Monitor open?): {e}")
            time.sleep(2)
        except Exception as e:
            print(f"[ERROR] {e}")
            time.sleep(2)

def find_esp32_port():
    ports = list(serial.tools.list_ports.comports())
    for p in ports:
        if "CP210" in p.description or "CH340" in p.description or "UART" in p.description or "USB" in p.description:
            return p.device
    for p in ports:
        if "COM6" in p.device:
            return "COM6"
    return "COM6"

if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else find_esp32_port()

    # Start Serial Reader Thread
    t = threading.Thread(target=serial_reader_thread, args=(port,), daemon=True)
    t.start()

    # Start HTTP Server on port 5000
    server_port = 5000
    server_address = ('127.0.0.1', server_port)
    httpd = HTTPServer(server_address, DashboardHandler)

    url = f"http://localhost:{server_port}"
    print("=" * 60)
    print(f"  FallGuard v2 — Local USB Dashboard running!")
    print(f"  URL: {url}")
    print(f"  Port: {port}")
    print("  Press Ctrl+C in this terminal to stop.")
    print("=" * 60)

    # Open browser automatically after 1 second
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] Dashboard stopped by user.")
        if serial_inst and serial_inst.is_open:
            serial_inst.close()
