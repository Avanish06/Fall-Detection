"""
FallGuard v2 — Serial Data Logger & Trial Protocol Tool
=========================================================
Reads CSV telemetry from an ESP32 fall-detection device over a serial port,
saves every sample to a timestamped session CSV file, handles per-trial CSV
logging on TRIAL,<id> commands/events, and provides an interactive annotation
prompt appending to annotations.csv.

CSV format supported:
    - Extended Telemetry (Part E):
      TELEMETRY,<ms>,<mag>,<jerk>,<state>,<conf>,<activity>,<fallCount>,<nonFallCount>,<theta>,<impThresh>,<jerkThresh>,<trial_id>,<Q>,<E>,<sensor_state>
    - Legacy / Standard Sample rows

Usage
-----
    python logger.py --port COM3 --baud 115200

At runtime
----------
    * Every 2 seconds the console refreshes with aggregate statistics.
    * Type  f <Enter>       to mark the most-recent event as a TRUE FALL.
    * Type  n <Enter>       to mark the most-recent event as a NON-FALL.
    * Type  t <id> <Enter>  to start a trial with given ID (e.g. t T01_FALL).
    * Type  l <Enter>       to open the interactive trial annotation prompt.
    * Press Ctrl+C          to stop logging, print final metrics, and flush CSVs.
"""

import argparse
import csv
import math
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import serial  # pip install pyserial
except ImportError:
    serial = None

# ──────────────────────────────────────────────────────────────────────────────
# Paths & Columns
# ──────────────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "results"

CSV_COLUMNS = [
    "pc_wallclock",
    "timestamp_ms",
    "mag",
    "jerk",
    "phase",
    "classifier_conf",
    "activity",
    "fallCount",
    "nonFallCount",
    "theta",
    "impThresh",
    "jerkThresh",
    "trial_id",
    "Q",
    "E",
    "sensor_state",
    "event_label",
]

ANNOTATION_COLUMNS = [
    "trial_id",
    "activity",
    "is_fall",
    "onset_wallclock",
    "placement",
    "orientation",
    "speed",
    "notes",
]

# Console refresh interval (seconds)
DISPLAY_INTERVAL = 2.0

# Minimum confidence column value that identifies an event row
EVENT_LABELS = {"FALL_DETECTED", "FALL_CONFIRMED", "ALARM_CANCELLED", "CANCEL_USER", "ALERT_SENT"}


def _isnan(val: float) -> bool:
    return math.isnan(val)


# ──────────────────────────────────────────────────────────────────────────────
# Annotations Persistence
# ──────────────────────────────────────────────────────────────────────────────


def record_annotation(
    trial_id: str,
    activity: str,
    is_fall: int | bool | str,
    onset_wallclock: str,
    placement: str = "waist",
    orientation: str = "none",
    speed: str = "normal",
    notes: str = "",
    annotations_path: Path | None = None,
) -> Path:
    """Append a structured annotation record to annotations.csv."""
    if annotations_path is None:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        annotations_path = RESULTS_DIR / "annotations.csv"

    file_exists = annotations_path.exists()
    fall_flag = 1 if str(is_fall).lower() in ("1", "true", "yes", "y", "fall") else 0

    with open(annotations_path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=ANNOTATION_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "trial_id": trial_id,
            "activity": activity,
            "is_fall": fall_flag,
            "onset_wallclock": onset_wallclock,
            "placement": placement,
            "orientation": orientation,
            "speed": speed,
            "notes": notes,
        })
    print(f"\n[annotation] Saved annotation for trial '{trial_id}' -> {annotations_path}")
    return annotations_path


def interactive_label_prompt(state, default_trial_id: str = "") -> dict:
    """Prompt user interactively on console for trial annotation."""
    tid = default_trial_id or getattr(state, "current_trial_id", "") or "TRIAL_01"
    print("\n" + "=" * 48)
    print("  FallGuard v2 — Interactive Trial Annotation")
    print("=" * 48)
    try:
        trial_id = input(f"  Trial ID [{tid}]: ").strip() or tid
        activity = input("  Activity (e.g. Walking, Stumble, Slip_Forward) [Walking]: ").strip() or "Walking"
        is_fall_str = input("  Is Fall? (1=yes / 0=no) [0]: ").strip() or "0"
        onset = getattr(state, "current_trial_onset", "") or datetime.now().isoformat()
        onset_input = input(f"  Onset Wallclock [{onset}]: ").strip() or onset
        placement = input("  Placement (waist/chest/wrist/thigh) [waist]: ").strip() or "waist"
        orientation = input("  Orientation (forward/backward/lateral/none) [none]: ").strip() or "none"
        speed = input("  Speed (slow/normal/fast) [normal]: ").strip() or "normal"
        notes = input("  Notes []: ").strip()
    except EOFError:
        trial_id = tid
        activity = "Walking"
        is_fall_str = "0"
        onset_input = datetime.now().isoformat()
        placement = "waist"
        orientation = "none"
        speed = "normal"
        notes = ""

    is_fall = 1 if is_fall_str in ("1", "true", "yes", "y") else 0

    record_annotation(
        trial_id=trial_id,
        activity=activity,
        is_fall=is_fall,
        onset_wallclock=onset_input,
        placement=placement,
        orientation=orientation,
        speed=speed,
        notes=notes,
    )
    return {
        "trial_id": trial_id,
        "activity": activity,
        "is_fall": is_fall,
        "onset_wallclock": onset_input,
        "placement": placement,
        "orientation": orientation,
        "speed": speed,
        "notes": notes,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Session state (shared between reader thread and main thread)
# ──────────────────────────────────────────────────────────────────────────────


class SessionState:
    """Thread-safe container for all live session statistics and trial logging."""

    def __init__(self):
        self._lock = threading.Lock()

        # Raw sample buffer (list of dicts matching CSV_COLUMNS)
        self.samples: list[dict] = []

        # Event tracking — each entry: {"label": str, "verdict": str | None}
        self.events: list[dict] = []

        # Confusion-matrix counters
        self.tp = 0
        self.fp = 0
        self.tn = 0
        self.fn = 0

        # Latest activity & sensor integrity
        self.current_activity: str = "UNKNOWN"
        self.sensor_state: str = "OK"
        self.q_value: float = 1.0
        self.e_value: float = 1.0

        # Per-trial CSV logging
        self.current_trial_id: str = ""
        self.current_trial_onset: str = ""
        self.current_trial_file = None
        self.current_trial_writer = None
        self.active_trial_path: Path | None = None

    def start_trial(self, trial_id: str):
        with self._lock:
            if self.current_trial_file:
                try:
                    self.current_trial_file.close()
                except Exception:
                    pass
                self.current_trial_file = None
                self.current_trial_writer = None

            tid = trial_id.strip()
            self.current_trial_id = tid
            self.current_trial_onset = datetime.now().isoformat()

            if tid and tid.upper() not in ("NONE", "CLEAR", "END", ""):
                RESULTS_DIR.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"trial_{tid}_{ts}.csv"
                self.active_trial_path = RESULTS_DIR / filename
                self.current_trial_file = open(self.active_trial_path, "w", newline="", encoding="utf-8")
                self.current_trial_writer = csv.DictWriter(self.current_trial_file, fieldnames=CSV_COLUMNS)
                self.current_trial_writer.writeheader()
                print(f"\n[logger] Per-trial CSV opened: {filename}")
            else:
                self.current_trial_id = ""
                self.active_trial_path = None
                print("\n[logger] Trial tracking stopped.")

    def add_sample(self, row: dict):
        with self._lock:
            self.samples.append(row)
            if self.current_trial_writer and self.current_trial_file:
                self.current_trial_writer.writerow(row)
                self.current_trial_file.flush()

            label = row.get("event_label", "")
            if label in EVENT_LABELS:
                self.events.append({"label": label, "verdict": None})
            if row.get("activity"):
                self.current_activity = row["activity"]
            if row.get("sensor_state"):
                self.sensor_state = row["sensor_state"]
            if row.get("Q"):
                try:
                    self.q_value = float(row["Q"])
                except (ValueError, TypeError):
                    pass
            if row.get("E"):
                try:
                    self.e_value = float(row["E"])
                except (ValueError, TypeError):
                    pass

    def mark_last_event(self, verdict: str):
        """verdict: 'fall' | 'non_fall'"""
        with self._lock:
            for ev in reversed(self.events):
                if ev["verdict"] is None:
                    ev["verdict"] = verdict
                    self._update_confusion(ev["label"], verdict)
                    return
            print("  [logger] No unclassified event to mark.")

    def _update_confusion(self, event_label: str, verdict: str):
        if event_label in ("FALL_DETECTED", "FALL_CONFIRMED", "ALERT_SENT"):
            if verdict == "fall":
                self.tp += 1
            else:
                self.fp += 1
        elif event_label in ("ALARM_CANCELLED", "CANCEL_USER"):
            if verdict == "non_fall":
                self.tn += 1
            else:
                self.fn += 1

    def close(self):
        with self._lock:
            if self.current_trial_file:
                try:
                    self.current_trial_file.close()
                except Exception:
                    pass
                self.current_trial_file = None
                self.current_trial_writer = None

    def snapshot(self) -> dict:
        """Return a point-in-time copy of session metrics."""
        with self._lock:
            total = len(self.samples)
            tp, fp, tn, fn = self.tp, self.fp, self.tn, self.fn
            act = self.current_activity
            tid = self.current_trial_id
            st = self.sensor_state
            q = self.q_value
            e = self.e_value

        sens = tp / (tp + fn) if (tp + fn) > 0 else float("nan")
        spec = tn / (tn + fp) if (tn + fp) > 0 else float("nan")
        prec = tp / (tp + fp) if (tp + fp) > 0 else float("nan")
        acc = (tp + tn) / (tp + tn + fp + fn) if (tp + tn + fp + fn) > 0 else float("nan")
        f1 = (2 * prec * sens / (prec + sens)) if (not _isnan(prec) and not _isnan(sens) and (prec + sens) > 0) else float("nan")

        return {
            "total_samples": total,
            "fall_events": tp + fp,
            "cancels": tn + fn,
            "tp": tp,
            "fp": fp,
            "tn": tn,
            "fn": fn,
            "sensitivity": sens,
            "specificity": spec,
            "precision": prec,
            "accuracy": acc,
            "f1": f1,
            "current_activity": act,
            "current_trial_id": tid,
            "sensor_state": st,
            "Q": q,
            "E": e,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Serial reader thread
# ──────────────────────────────────────────────────────────────────────────────


def serial_reader(port: str, baud: int, state: SessionState, stop_event: threading.Event, ser_out: list = None):
    """
    Continuously reads lines from the serial port, parses them as CSV, and
    appends valid rows to *state*. Runs in a dedicated daemon thread.
    """
    if serial is None:
        print("\n[ERROR] pyserial is not installed. Run: pip install pyserial")
        stop_event.set()
        return

    try:
        ser = serial.Serial(port, baud, timeout=1)
        if ser_out is not None:
            ser_out.append(ser)
    except serial.SerialException as exc:
        print(f"\n[ERROR] Cannot open serial port {port}: {exc}")
        stop_event.set()
        return

    print(f"[logger] Connected to {port} @ {baud} baud.\n")

    while not stop_event.is_set():
        try:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            wallclock = datetime.now().isoformat()

            # Diagnostic STAT lines
            if line.startswith("# STAT,"):
                continue
            if line.startswith("#"):
                continue

            # Event lines from ESP32
            if line.startswith("EVENT,"):
                parts = line.split(",")
                ms = parts[1] if len(parts) > 1 else ""
                event_name = parts[2] if len(parts) > 2 else ""

                if event_name == "TRIAL_SET" and len(parts) > 3:
                    state.start_trial(parts[3])

                row = {col: "" for col in CSV_COLUMNS}
                row["pc_wallclock"] = wallclock
                row["timestamp_ms"] = ms
                row["event_label"] = event_name
                row["trial_id"] = state.current_trial_id
                state.add_sample(row)
                continue

            # Part E Extended Telemetry line
            if line.startswith("TELEMETRY,"):
                parts = line.split(",")
                # Indices: 1:ms, 2:mag, 3:jerk, 4:state, 5:conf, 6:act, 7:fc, 8:nfc, 9:theta, 10:imp, 11:jk, 12:trial, 13:Q, 14:E, 15:sensor_state
                ms = parts[1] if len(parts) > 1 else ""
                mag = parts[2] if len(parts) > 2 else ""
                jerk = parts[3] if len(parts) > 3 else ""
                phase = parts[4] if len(parts) > 4 else ""
                conf = parts[5] if len(parts) > 5 else ""
                act = parts[6] if len(parts) > 6 else ""
                fc = parts[7] if len(parts) > 7 else ""
                nfc = parts[8] if len(parts) > 8 else ""
                th = parts[9] if len(parts) > 9 else ""
                imp = parts[10] if len(parts) > 10 else ""
                jk = parts[11] if len(parts) > 11 else ""
                trial_id = parts[12].strip() if len(parts) > 12 else state.current_trial_id
                q_val = parts[13].strip() if len(parts) > 13 else "1.00"
                e_val = parts[14].strip() if len(parts) > 14 else "1.00"
                sensor_st = parts[15].strip() if len(parts) > 15 else "OK"

                row = {
                    "pc_wallclock": wallclock,
                    "timestamp_ms": ms,
                    "mag": mag,
                    "jerk": jerk,
                    "phase": phase,
                    "classifier_conf": conf,
                    "activity": act,
                    "fallCount": fc,
                    "nonFallCount": nfc,
                    "theta": th,
                    "impThresh": imp,
                    "jerkThresh": jk,
                    "trial_id": trial_id,
                    "Q": q_val,
                    "E": e_val,
                    "sensor_state": sensor_st,
                    "event_label": "",
                }
                state.add_sample(row)
                continue

            # Fallback legacy CSV line
            parts = line.split(",")
            row = {col: "" for col in CSV_COLUMNS}
            row["pc_wallclock"] = wallclock
            if len(parts) >= 1: row["timestamp_ms"] = parts[0]
            if len(parts) >= 2: row["mag"] = parts[1]
            if len(parts) >= 3: row["jerk"] = parts[2]
            if len(parts) >= 4: row["phase"] = parts[3]
            if len(parts) >= 5: row["classifier_conf"] = parts[4]
            if len(parts) >= 6: row["activity"] = parts[5]
            if len(parts) >= 7: row["event_label"] = parts[6]
            row["trial_id"] = state.current_trial_id
            state.add_sample(row)

        except serial.SerialException as exc:
            print(f"\n[ERROR] Serial read error: {exc}")
            stop_event.set()
            break
        except Exception:
            pass

    try:
        ser.close()
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Console display
# ──────────────────────────────────────────────────────────────────────────────

_CLEAR = "\033[2J\033[H"


def render_dashboard(state: SessionState, elapsed_s: float):
    """Print a live summary table to stdout."""
    snap = state.snapshot()

    bar = "=" * 54
    print(_CLEAR, end="")
    print(bar)
    print("  FallGuard v2 — Live Logger Dashboard (Part E)")
    print(bar)
    print(f"  Elapsed          : {elapsed_s:>8.1f} s")
    print(f"  Active Trial     : {snap['current_trial_id'] or 'NONE':>8}")
    print(f"  Sensor State     : {snap['sensor_state']:>8}  (Q={snap['Q']:.2f}, E={snap['E']:.2f})")
    print(f"  Total samples    : {snap['total_samples']:>8d}")
    print(f"  Fall events      : {snap['fall_events']:>8d}")
    print(f"  Alarms cancelled : {snap['cancels']:>8d}")
    print(f"  Activity         : {snap['current_activity']:>8}")
    print("-" * 54)
    print(f"  Confusion counts : TP={snap['tp']}  FP={snap['fp']}  TN={snap['tn']}  FN={snap['fn']}")
    sens = snap["sensitivity"]
    spec = snap["specificity"]
    prec = snap["precision"]
    f1 = snap["f1"]
    print(f"  Sensitivity      : {sens * 100:>6.1f} %" if not _isnan(sens) else "  Sensitivity      :    N/A")
    print(f"  Specificity      : {spec * 100:>6.1f} %" if not _isnan(spec) else "  Specificity      :    N/A")
    print(f"  Precision        : {prec * 100:>6.1f} %" if not _isnan(prec) else "  Precision        :    N/A")
    print(f"  F1 Score         : {f1:>8.3f}" if not _isnan(f1) else "  F1 Score         :    N/A")
    print(bar)
    print("  Commands: [f]=Fall  [n]=Non-Fall  [t <id>]=Start Trial  [l]=Label  [q]=Quit")


# ──────────────────────────────────────────────────────────────────────────────
# User input thread
# ──────────────────────────────────────────────────────────────────────────────


def input_reader(state: SessionState, stop_event: threading.Event, ser_holder: list = None):
    """Reads interactive commands from stdin."""
    while not stop_event.is_set():
        try:
            line = input().strip()
        except EOFError:
            break
        if not line:
            continue

        cmd_parts = line.split(maxsplit=1)
        cmd = cmd_parts[0].lower()
        arg = cmd_parts[1].strip() if len(cmd_parts) > 1 else ""

        if cmd == "f":
            state.mark_last_event("fall")
            print("  -> Marked last event as TRUE FALL")
        elif cmd == "n":
            state.mark_last_event("non_fall")
            print("  -> Marked last event as NON-FALL")
        elif cmd in ("t", "trial"):
            if arg:
                state.start_trial(arg)
                if ser_holder and ser_holder[0] and ser_holder[0].is_open:
                    try:
                        ser_holder[0].write(f"TRIAL,{arg}\n".encode("utf-8"))
                    except Exception:
                        pass
            else:
                print("  Usage: t <trial_id> (e.g. t T01)")
        elif cmd in ("l", "label"):
            interactive_label_prompt(state, default_trial_id=arg)
        elif cmd in ("q", "quit"):
            stop_event.set()
            break


# ──────────────────────────────────────────────────────────────────────────────
# CSV persistence
# ──────────────────────────────────────────────────────────────────────────────


def save_csv(state: SessionState, filepath: Path):
    """Write all collected samples to a CSV file."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        with state._lock:
            writer.writerows(state.samples)
    print(f"\n[logger] Session saved -> {filepath}")


# ──────────────────────────────────────────────────────────────────────────────
# Post-session plotting
# ──────────────────────────────────────────────────────────────────────────────


def plot_session(csv_path: str):
    """Load a saved session CSV and produce matplotlib figures."""
    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("[plot] matplotlib and numpy are required. pip install matplotlib numpy")
        return

    rows: list[dict] = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(row)

    if not rows:
        print("[plot] CSV is empty — nothing to plot.")
        return

    def _float(v: str) -> float:
        try:
            return float(v)
        except (ValueError, TypeError):
            return float("nan")

    t_ms = np.array([_float(r.get("timestamp_ms", "0")) for r in rows])
    t_s = (t_ms - t_ms[0]) / 1000.0 if len(t_ms) > 0 and not np.isnan(t_ms[0]) else np.arange(len(rows)) * 0.02
    mag = np.array([_float(r.get("mag", "0")) for r in rows])
    jerk = np.array([_float(r.get("jerk", "0")) for r in rows])
    conf = np.array([_float(r.get("classifier_conf", "0")) for r in rows])

    fall_times = [
        (t_s[i], r.get("event_label"))
        for i, r in enumerate(rows)
        if r.get("event_label") in EVENT_LABELS
    ]

    fig_width = max(10, len(rows) / 200)

    fig1, ax1 = plt.subplots(figsize=(fig_width, 4))
    ax2 = ax1.twinx()

    ax1.plot(t_s, mag, color="steelblue", linewidth=0.8, label="Magnitude (m/s²)")
    ax2.plot(t_s, jerk, color="darkorange", linewidth=0.8, alpha=0.7, label="Jerk (m/s³)")

    for ft, flabel in fall_times:
        color = "red" if flabel in ("FALL_DETECTED", "FALL_CONFIRMED", "ALERT_SENT") else "green"
        ax1.axvline(x=ft, color=color, linestyle="--", linewidth=1.2, alpha=0.8)

    ax1.set_xlabel("Time (s)")
    ax1.set_ylabel("Magnitude (m/s²)", color="steelblue")
    ax2.set_ylabel("Jerk (m/s³)", color="darkorange")
    ax1.set_title("FallGuard v2 — Kinematics over Time")

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper right", fontsize=8)
    fig1.tight_layout()

    fig2, ax3 = plt.subplots(figsize=(fig_width, 3))
    ax3.plot(t_s, conf, color="purple", linewidth=0.8, label="Classifier confidence")
    ax3.set_ylim(0, 1.05)
    ax3.set_xlabel("Time (s)")
    ax3.set_ylabel("Confidence")
    ax3.set_title("FallGuard v2 — Classifier Confidence over Time")

    for ft, flabel in fall_times:
        color = "red" if flabel in ("FALL_DETECTED", "FALL_CONFIRMED", "ALERT_SENT") else "green"
        ax3.axvline(x=ft, color=color, linestyle="--", linewidth=1.2, alpha=0.8)

    ax3.legend(fontsize=8)
    fig2.tight_layout()

    plt.show()


# ──────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ──────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="FallGuard v2 Serial Data Logger & Protocol Tool",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", default="COM3", help="Serial port (e.g. COM3 or /dev/ttyUSB0)")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = RESULTS_DIR / f"session_{session_ts}.csv"

    state = SessionState()
    stop_event = threading.Event()
    ser_holder = []

    reader_thread = threading.Thread(
        target=serial_reader,
        args=(args.port, args.baud, state, stop_event, ser_holder),
        daemon=True,
        name="serial-reader",
    )
    reader_thread.start()

    input_thread = threading.Thread(
        target=input_reader,
        args=(state, stop_event, ser_holder),
        daemon=True,
        name="input-reader",
    )
    input_thread.start()

    start_time = time.monotonic()

    try:
        while not stop_event.is_set():
            render_dashboard(state, time.monotonic() - start_time)
            time.sleep(DISPLAY_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        state.close()

    snap = state.snapshot()
    print("\n" + "=" * 54)
    print("  FallGuard v2 — Session Summary")
    print("=" * 54)
    print(f"  Total samples    : {snap['total_samples']}")
    print(f"  Fall events      : {snap['fall_events']}")
    print(f"  Alarms cancelled : {snap['cancels']}")
    print(f"  TP={snap['tp']}  FP={snap['fp']}  TN={snap['tn']}  FN={snap['fn']}")
    sens = snap["sensitivity"]
    spec = snap["specificity"]
    print(f"  Sensitivity      : {sens * 100:.1f} %" if not _isnan(sens) else "  Sensitivity      : N/A")
    print(f"  Specificity      : {spec * 100:.1f} %" if not _isnan(spec) else "  Specificity      : N/A")
    print("=" * 54)

    save_csv(state, csv_path)


if __name__ == "__main__":
    main()
