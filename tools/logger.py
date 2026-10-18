"""
FallGuard v2 — Serial Data Logger
===================================
Reads CSV telemetry from an ESP32 fall-detection device over a serial port,
saves every sample to a timestamped CSV file, and displays a live console
summary table every 2 seconds.

CSV format expected from the ESP32 (one line per 20 ms during NORMAL phase):
    timestamp_ms,mag,jerk,phase,classifier_conf,activity,event_label

Special event lines (fall detected / alarm cancelled) use the same columns but
have non-empty `event_label` fields (e.g. "FALL_DETECTED", "ALARM_CANCELLED").

Usage
-----
    python logger.py --port COM3 --baud 115200

At runtime
----------
    * Every 2 seconds the console refreshes with aggregate statistics.
    * Type  f <Enter>  to mark the most-recent event as a TRUE FALL.
    * Type  n <Enter>  to mark the most-recent event as a NON-FALL (false alarm).
    * Press  Ctrl+C    to stop logging, print final metrics, and flush the CSV.

Post-session plotting
---------------------
    from logger import plot_session
    plot_session("results/session_20240101_120000.csv")
"""

import argparse
import csv
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import serial  # pip install pyserial

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

RESULTS_DIR = Path(__file__).parent / "results"
CSV_COLUMNS = [
    "timestamp_ms",
    "mag",
    "jerk",
    "phase",
    "classifier_conf",
    "activity",
    "event_label",
]

# Console refresh interval (seconds)
DISPLAY_INTERVAL = 2.0

# Minimum confidence column value that identifies an event row
EVENT_LABELS = {"FALL_DETECTED", "ALARM_CANCELLED"}

# ──────────────────────────────────────────────────────────────────────────────
# Session state (shared between reader thread and main thread)
# ──────────────────────────────────────────────────────────────────────────────


class SessionState:
    """Thread-safe container for all live session statistics."""

    def __init__(self):
        self._lock = threading.Lock()

        # Raw sample buffer (list of dicts matching CSV_COLUMNS)
        self.samples: list[dict] = []

        # Event tracking — each entry: {"label": str, "verdict": str | None}
        # verdict is "fall" | "non_fall" | None (unclassified)
        self.events: list[dict] = []

        # Confusion-matrix counters (updated when user types f / n)
        self.tp = 0  # true positive  — user marked as fall, was FALL_DETECTED
        self.fp = 0  # false positive — user marked as non-fall, was FALL_DETECTED
        self.tn = 0  # true negative  — user marked as non-fall, was ALARM_CANCELLED
        self.fn = 0  # false negative — user marked as fall, was ALARM_CANCELLED

        # Latest activity string reported by ESP32
        self.current_activity: str = "UNKNOWN"

    # ── accessors ──────────────────────────────────────────────────────────

    def add_sample(self, row: dict):
        with self._lock:
            self.samples.append(row)
            if row.get("event_label") in EVENT_LABELS:
                self.events.append({"label": row["event_label"], "verdict": None})
            if row.get("activity"):
                self.current_activity = row["activity"]

    def mark_last_event(self, verdict: str):
        """verdict: 'fall' | 'non_fall'"""
        with self._lock:
            # Find the most recent unclassified event
            for ev in reversed(self.events):
                if ev["verdict"] is None:
                    ev["verdict"] = verdict
                    self._update_confusion(ev["label"], verdict)
                    return
            print("  [logger] No unclassified event to mark.")

    def _update_confusion(self, event_label: str, verdict: str):
        if event_label == "FALL_DETECTED":
            if verdict == "fall":
                self.tp += 1
            else:
                self.fp += 1
        elif event_label == "ALARM_CANCELLED":
            if verdict == "non_fall":
                self.tn += 1
            else:
                self.fn += 1

    def snapshot(self) -> dict:
        """Return a point-in-time copy of all statistics."""
        with self._lock:
            fall_events = sum(1 for e in self.events if e["label"] == "FALL_DETECTED")
            cancels = sum(1 for e in self.events if e["label"] == "ALARM_CANCELLED")
            sensitivity = self._safe_div(self.tp, self.tp + self.fn)
            specificity = self._safe_div(self.tn, self.tn + self.fp)
            return {
                "total_samples": len(self.samples),
                "fall_events": fall_events,
                "cancels": cancels,
                "activity": self.current_activity,
                "tp": self.tp,
                "fp": self.fp,
                "tn": self.tn,
                "fn": self.fn,
                "sensitivity": sensitivity,
                "specificity": specificity,
            }

    @staticmethod
    def _safe_div(num: int, den: int) -> float:
        return num / den if den else float("nan")


# ──────────────────────────────────────────────────────────────────────────────
# Serial reader thread
# ──────────────────────────────────────────────────────────────────────────────


def serial_reader(port: str, baud: int, state: SessionState, stop_event: threading.Event):
    """
    Continuously reads lines from the serial port, parses them as CSV, and
    appends valid rows to *state*.  Runs in a dedicated daemon thread.
    """
    try:
        ser = serial.Serial(port, baud, timeout=1)
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
            if not line or line.startswith("#"):
                # Skip blank lines and comment/info lines from ESP32
                continue

            parts = line.split(",")
            if len(parts) < len(CSV_COLUMNS):
                # Pad missing columns (event_label is optional on normal rows)
                parts += [""] * (len(CSV_COLUMNS) - len(parts))

            row = dict(zip(CSV_COLUMNS, parts[: len(CSV_COLUMNS)]))
            state.add_sample(row)

        except serial.SerialException as exc:
            print(f"\n[ERROR] Serial read error: {exc}")
            stop_event.set()
            break
        except Exception:
            # Swallow parse errors — the ESP32 may emit non-CSV boot messages
            pass

    try:
        ser.close()
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Console display
# ──────────────────────────────────────────────────────────────────────────────

_CLEAR = "\033[2J\033[H"  # ANSI clear-screen + cursor home


def render_dashboard(state: SessionState, elapsed_s: float):
    """Print a live summary table to stdout."""
    snap = state.snapshot()

    bar = "=" * 52
    print(_CLEAR, end="")
    print(bar)
    print("  FallGuard v2 — Live Logger Dashboard")
    print(bar)
    print(f"  Elapsed          : {elapsed_s:>8.1f} s")
    print(f"  Total samples    : {snap['total_samples']:>8d}")
    print(f"  Fall events      : {snap['fall_events']:>8d}")
    print(f"  Alarms cancelled : {snap['cancels']:>8d}")
    print(f"  Current activity : {snap['activity']:>8s}")
    print(bar)
    print("  Confusion matrix (user-labelled events)")
    print(f"    TP={snap['tp']}  FP={snap['fp']}  TN={snap['tn']}  FN={snap['fn']}")
    sens = snap["sensitivity"]
    spec = snap["specificity"]
    print(f"  Sensitivity      : {sens * 100:>7.1f} %" if not _isnan(sens) else "  Sensitivity      :      N/A")
    print(f"  Specificity      : {spec * 100:>7.1f} %" if not _isnan(spec) else "  Specificity      :      N/A")
    print(bar)
    print("  Commands: [f] mark last event as TRUE FALL")
    print("            [n] mark last event as NON-FALL")
    print("            Ctrl+C to stop")
    print(bar)
    sys.stdout.flush()


def _isnan(v: float) -> bool:
    import math
    return math.isnan(v)


# ──────────────────────────────────────────────────────────────────────────────
# User input thread
# ──────────────────────────────────────────────────────────────────────────────


def input_reader(state: SessionState, stop_event: threading.Event):
    """
    Reads single-character commands from stdin in a background thread so that
    the main display loop can keep running without blocking.
    """
    while not stop_event.is_set():
        try:
            cmd = input().strip().lower()
        except EOFError:
            break
        if cmd == "f":
            state.mark_last_event("fall")
            print("  → Marked last event as TRUE FALL")
        elif cmd == "n":
            state.mark_last_event("non_fall")
            print("  → Marked last event as NON-FALL")


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
    print(f"\n[logger] Session saved → {filepath}")


# ──────────────────────────────────────────────────────────────────────────────
# Post-session plotting
# ──────────────────────────────────────────────────────────────────────────────


def plot_session(csv_path: str):
    """
    Load a saved session CSV and produce three matplotlib figures:

    1. Magnitude + Jerk over time (dual Y-axis)
    2. Classifier confidence over time
    3. Bar chart of TP / TN / FP / FN counts

    Parameters
    ----------
    csv_path : str
        Absolute or relative path to the session CSV file.
    """
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

    # ── Parse numeric columns ──────────────────────────────────────────────
    def _float(v: str) -> float:
        try:
            return float(v)
        except (ValueError, TypeError):
            return float("nan")

    t_ms = np.array([_float(r["timestamp_ms"]) for r in rows])
    t_s = (t_ms - t_ms[0]) / 1000.0  # convert to seconds relative to start
    mag = np.array([_float(r["mag"]) for r in rows])
    jerk = np.array([_float(r["jerk"]) for r in rows])
    conf = np.array([_float(r["classifier_conf"]) for r in rows])

    # ── Identify event timestamps ──────────────────────────────────────────
    fall_times = [
        (t_s[i], r["event_label"])
        for i, r in enumerate(rows)
        if r.get("event_label") in EVENT_LABELS
    ]

    fig_width = max(10, len(rows) / 200)  # scale figure width with session length

    # ── Figure 1: Magnitude + Jerk ─────────────────────────────────────────
    fig1, ax1 = plt.subplots(figsize=(fig_width, 4))
    ax2 = ax1.twinx()

    ax1.plot(t_s, mag, color="steelblue", linewidth=0.8, label="Magnitude (g)")
    ax2.plot(t_s, jerk, color="darkorange", linewidth=0.8, alpha=0.7, label="Jerk (g/s)")

    for ft, flabel in fall_times:
        color = "red" if flabel == "FALL_DETECTED" else "green"
        ax1.axvline(x=ft, color=color, linestyle="--", linewidth=1.2, alpha=0.8)

    ax1.set_xlabel("Time (s)")
    ax1.set_ylabel("Magnitude (g)", color="steelblue")
    ax2.set_ylabel("Jerk (g/s)", color="darkorange")
    ax1.set_title("FallGuard v2 — Magnitude & Jerk over Time")

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper right", fontsize=8)

    fig1.tight_layout()

    # ── Figure 2: Classifier confidence ───────────────────────────────────
    fig2, ax3 = plt.subplots(figsize=(fig_width, 3))
    ax3.plot(t_s, conf, color="purple", linewidth=0.8, label="Classifier confidence")
    ax3.set_ylim(0, 1.05)
    ax3.set_xlabel("Time (s)")
    ax3.set_ylabel("Confidence")
    ax3.set_title("FallGuard v2 — Classifier Confidence over Time")

    for ft, flabel in fall_times:
        color = "red" if flabel == "FALL_DETECTED" else "green"
        ax3.axvline(x=ft, color=color, linestyle="--", linewidth=1.2, alpha=0.8)

    ax3.legend(fontsize=8)
    fig2.tight_layout()

    # ── Figure 3: Confusion-matrix bar chart ───────────────────────────────
    # Count from event_label + derive naive counts (no user labels in CSV)
    # We store labelled verdicts only in memory, so fall back to raw event counts.
    fall_count = sum(1 for r in rows if r.get("event_label") == "FALL_DETECTED")
    cancel_count = sum(1 for r in rows if r.get("event_label") == "ALARM_CANCELLED")

    fig3, ax4 = plt.subplots(figsize=(5, 4))
    categories = ["FALL\nDETECTED", "ALARM\nCANCELLED"]
    counts = [fall_count, cancel_count]
    colors = ["tomato", "mediumseagreen"]
    bars = ax4.bar(categories, counts, color=colors, edgecolor="black", width=0.4)

    for bar, cnt in zip(bars, counts):
        ax4.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.05,
            str(cnt),
            ha="center",
            va="bottom",
            fontsize=11,
            fontweight="bold",
        )

    ax4.set_ylabel("Event count")
    ax4.set_title("FallGuard v2 — Event Summary")
    ax4.set_ylim(0, max(counts + [1]) * 1.3)
    fig3.tight_layout()

    plt.show()


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="FallGuard v2 Serial Data Logger",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", default="COM3", help="Serial port (e.g. COM3 or /dev/ttyUSB0)")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate")
    args = parser.parse_args()

    # ── Prepare output file ────────────────────────────────────────────────
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = RESULTS_DIR / f"session_{session_ts}.csv"

    state = SessionState()
    stop_event = threading.Event()

    # ── Start serial reader daemon ─────────────────────────────────────────
    reader_thread = threading.Thread(
        target=serial_reader,
        args=(args.port, args.baud, state, stop_event),
        daemon=True,
        name="serial-reader",
    )
    reader_thread.start()

    # ── Start user-input daemon ────────────────────────────────────────────
    input_thread = threading.Thread(
        target=input_reader,
        args=(state, stop_event),
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

    # ── Final summary ──────────────────────────────────────────────────────
    snap = state.snapshot()
    print("\n" + "=" * 52)
    print("  FallGuard v2 — Session Summary")
    print("=" * 52)
    print(f"  Total samples    : {snap['total_samples']}")
    print(f"  Fall events      : {snap['fall_events']}")
    print(f"  Alarms cancelled : {snap['cancels']}")
    print(f"  TP={snap['tp']}  FP={snap['fp']}  TN={snap['tn']}  FN={snap['fn']}")
    sens = snap["sensitivity"]
    spec = snap["specificity"]
    print(f"  Sensitivity      : {sens * 100:.1f} %" if not _isnan(sens) else "  Sensitivity      : N/A")
    print(f"  Specificity      : {spec * 100:.1f} %" if not _isnan(spec) else "  Specificity      : N/A")
    print("=" * 52)

    save_csv(state, csv_path)


if __name__ == "__main__":
    main()
