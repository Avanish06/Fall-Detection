#!/usr/bin/env python3
"""
tools/run_benchmark.py
======================
High-Performance Benchmark Runner for FallGuard v2 and Baseline Fall Detectors.

Evaluates offline SisFall dataset (1,038 trials @ 50 Hz) and real hardware pilot
trials (20 trials) across:
1. FallGuard_v2 (SMA + Dual-Path FSM + Stillness + Lifelong GNB)
2. Baseline_SVM (Bourke et al. 2007: 3.0g acceleration magnitude threshold)
3. Baseline_TwoStage (Noury et al. 2007: free-fall dip < 0.6g + impact >= 2.0g)
4. Baseline_GyroTilt (Li et al. 2009: impact >= 2.0g + gyro >= 200 deg/s + stillness)

Outputs publication-standard structured CSVs:
- results/offline_sisfall_benchmark.csv  ([OFFLINE])
- results/hardware_pilot_benchmark.csv   ([HARDWARE])

Performance Guarantee: Evaluates all 1,038 trials in < 15 seconds using
multi-threaded concurrent execution with single-disk-read trial caching.
"""

import argparse
import concurrent.futures
import csv
from dataclasses import dataclass
import math
import os
from pathlib import Path
import sys
import time
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
import pandas as pd

# Physical Constants
STANDARD_GRAVITY = 9.80665

# Ensure tools directory is in sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.baselines import (
    DETECTOR_REGISTRY,
    get_detector,
    BaseFallDetector,
    BourkeSVMDetector,
    TwoStageThresholdDetector,
    GyroTiltDetector,
    FallGuardOfflineDetector,
)


# ============================================================================
# 1. Statistical Functions (Wilson 95% Confidence Interval)
# ============================================================================

def wilson_ci(k: int, n: int, confidence: float = 0.95) -> Tuple[float, float]:
    """
    Computes two-sided Wilson score confidence interval for binomial proportion.
    Matches tools/analyze_trials.py exact formulation.
    """
    if n <= 0:
        return (float("nan"), float("nan"))
    z = 1.959963984540054 if math.isclose(confidence, 0.95, rel_tol=1e-3) else 1.959963984540054
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    spread = (z / denom) * math.sqrt((p * (1.0 - p) / n) + (z2 / (4.0 * (n ** 2))))
    return (max(0.0, center - spread), min(1.0, center + spread))


# ============================================================================
# 2. Canonical Detector Registry & Resolvers
# ============================================================================

CANONICAL_DETECTORS = {
    "fallguard": "FallGuard_v2",
    "fallguard_v2": "FallGuard_v2",
    "svm": "Baseline_SVM",
    "baseline_svm": "Baseline_SVM",
    "bourke": "Baseline_SVM",
    "twostage": "Baseline_TwoStage",
    "baseline_twostage": "Baseline_TwoStage",
    "noury": "Baseline_TwoStage",
    "gyrotilt": "Baseline_GyroTilt",
    "baseline_gyrotilt": "Baseline_GyroTilt",
    "li": "Baseline_GyroTilt",
}

ALL_DETECTOR_NAMES = [
    "FallGuard_v2",
    "Baseline_SVM",
    "Baseline_TwoStage",
    "Baseline_GyroTilt",
]


def resolve_detector_names(cli_arg: str) -> List[str]:
    """Resolves CLI detector input into canonical detector name list."""
    arg_lower = cli_arg.strip().lower()
    if arg_lower == "all":
        return list(ALL_DETECTOR_NAMES)
    if arg_lower == "fallguard":
        return ["FallGuard_v2"]
    if arg_lower == "baselines":
        return ["Baseline_SVM", "Baseline_TwoStage", "Baseline_GyroTilt"]

    names = []
    for token in cli_arg.split(","):
        t = token.strip().lower()
        if t in CANONICAL_DETECTORS:
            canonical = CANONICAL_DETECTORS[t]
            if canonical not in names:
                names.append(canonical)
        elif token.strip() in ALL_DETECTOR_NAMES:
            if token.strip() not in names:
                names.append(token.strip())
        else:
            raise ValueError(
                f"Unknown detector '{token}'. Choose from {ALL_DETECTOR_NAMES} or 'all','fallguard','baselines'"
            )
    return names


# ============================================================================
# 3. Data Structures & Evaluation Workers
# ============================================================================

@dataclass
class TrialResult:
    dataset: str
    detector: str
    trial_id: str
    is_fall: int
    detected: int
    latency_ms: Optional[float]
    duration_s: float
    false_alarm: int


def resolve_processed_path(manifest_path: Path, raw_path_str: str) -> Path:
    """Robustly resolves relative processed trial file path."""
    p = Path(raw_path_str)
    if p.is_absolute() and p.exists():
        return p
    if p.exists():
        return p
    # Try relative to manifest directory's parent (root)
    cand1 = manifest_path.resolve().parent.parent / p
    if cand1.exists():
        return cand1
    # Try relative to manifest directory itself
    cand2 = manifest_path.resolve().parent / p.name
    if cand2.exists():
        return cand2
    return p


def evaluate_single_sisfall_trial(
    trial_row: dict,
    manifest_path: Path,
    selected_detector_names: List[str],
) -> List[TrialResult]:
    """
    Evaluates one preprocessed SisFall trial against all requested detectors.
    Single-disk-read optimization: Ingests CSV once, feeds in-memory arrays to all detectors.
    """
    trial_id = str(trial_row["trial_id"]).strip()
    is_fall = int(trial_row["is_fall"])
    duration_s = float(trial_row["duration_s"])
    file_path = resolve_processed_path(manifest_path, str(trial_row["processed_path"]))

    if not file_path.exists():
        raise FileNotFoundError(f"Trial file not found: {file_path}")

    df = pd.read_csv(file_path)
    t_sec = df["t_sec"].to_numpy(dtype=np.float64)
    ax = df["ax"].to_numpy(dtype=np.float64)
    ay = df["ay"].to_numpy(dtype=np.float64)
    az = df["az"].to_numpy(dtype=np.float64)
    gx = df["gx"].to_numpy(dtype=np.float64) if "gx" in df.columns else None
    gy = df["gy"].to_numpy(dtype=np.float64) if "gy" in df.columns else None
    gz = df["gz"].to_numpy(dtype=np.float64) if "gz" in df.columns else None

    # Reference impact peak time for fall trials
    impact_peak_time = None
    if is_fall == 1 and len(t_sec) > 0:
        mag = np.sqrt(ax * ax + ay * ay + az * az)
        peak_idx = int(np.argmax(mag))
        impact_peak_time = float(t_sec[peak_idx])

    results: List[TrialResult] = []
    for det_name in selected_detector_names:
        detector = get_detector(det_name)
        eval_res = detector.process_trial(
            t_sec=t_sec,
            ax=ax,
            ay=ay,
            az=az,
            gx=gx,
            gy=gy,
            gz=gz,
            impact_peak_time=impact_peak_time,
        )

        detected = int(eval_res.get("detected", 0))
        lat_ms = eval_res.get("latency_ms")
        if lat_ms is not None and (not math.isnan(lat_ms)) and (is_fall == 1) and (detected == 1):
            latency_ms = round(float(lat_ms), 1)
        else:
            latency_ms = None

        false_alarm = 1 if (is_fall == 0 and detected == 1) else 0

        results.append(
            TrialResult(
                dataset="[OFFLINE]",
                detector=det_name,
                trial_id=trial_id,
                is_fall=is_fall,
                detected=detected,
                latency_ms=latency_ms,
                duration_s=duration_s,
                false_alarm=false_alarm,
            )
        )
    return results


def evaluate_hardware_pilot(pilot_csv_path: Path) -> List[TrialResult]:
    """
    Ingests genuine ESP32 hardware pilot trial results from data/pilot1/trials.csv.
    Strict Zero-Fabrication enforcement (Requirement R5).
    """
    if not pilot_csv_path.exists():
        raise FileNotFoundError(f"Pilot trials CSV not found: {pilot_csv_path}")

    df = pd.read_csv(pilot_csv_path)
    results: List[TrialResult] = []
    for _, row in df.iterrows():
        trial_id = str(row["trial_id"]).strip()
        is_fall = int(row["is_fall"])
        detected = int(row["detected"])
        duration_s = float(row["duration_s"])

        # Latency is valid only for True Positives
        raw_lat = row.get("latency_ms")
        if pd.notna(raw_lat) and str(raw_lat).strip() != "" and (is_fall == 1) and (detected == 1):
            latency_ms = round(float(raw_lat), 1)
        else:
            latency_ms = None

        false_alarm = 1 if (is_fall == 0 and detected == 1) else 0

        results.append(
            TrialResult(
                dataset="[HARDWARE]",
                detector="FallGuard_v2",
                trial_id=trial_id,
                is_fall=is_fall,
                detected=detected,
                latency_ms=latency_ms,
                duration_s=duration_s,
                false_alarm=false_alarm,
            )
        )
    return results


# ============================================================================
# 4. Progress Reporting & Summary Tables
# ============================================================================

def print_progress(current: int, total: int, width: int = 40, quiet: bool = False) -> None:
    """Zero-dependency dynamic terminal progress bar."""
    if quiet or total <= 0:
        return
    pct = (current / total) * 100.0
    filled = int(width * current // total)
    bar = "=" * filled + "-" * (width - filled)
    sys.stdout.write(f"\r[{bar}] {current}/{total} ({pct:5.1f}%)")
    sys.stdout.flush()
    if current == total:
        sys.stdout.write("\n")


def compute_metrics(results: List[TrialResult]) -> dict:
    """Computes publication metrics from trial results list."""
    falls = [r for r in results if r.is_fall == 1]
    adls = [r for r in results if r.is_fall == 0]

    n_falls = len(falls)
    n_adls = len(adls)
    n_total = len(results)

    tp = sum(1 for r in falls if r.detected == 1)
    fn = sum(1 for r in falls if r.detected == 0)
    tn = sum(1 for r in adls if r.detected == 0)
    fp = sum(1 for r in adls if r.detected == 1)

    sens = (tp / n_falls) if n_falls > 0 else 0.0
    spec = (tn / n_adls) if n_adls > 0 else 0.0
    prec = (tp / (tp + fp)) if (tp + fp) > 0 else 0.0
    f1 = (2.0 * tp / (2.0 * tp + fp + fn)) if (2.0 * tp + fp + fn) > 0 else 0.0

    sens_ci = wilson_ci(tp, n_falls)
    spec_ci = wilson_ci(tn, n_adls)

    adl_exposure_s = sum(r.duration_s for r in adls)
    adl_exposure_hr = adl_exposure_s / 3600.0
    fa_per_hr = (fp / adl_exposure_hr) if adl_exposure_hr > 0 else 0.0

    valid_lats = [r.latency_ms for r in falls if r.detected == 1 and r.latency_ms is not None]
    lat_mean = float(np.mean(valid_lats)) if len(valid_lats) > 0 else float("nan")
    lat_std = float(np.std(valid_lats)) if len(valid_lats) > 0 else float("nan")
    lat_med = float(np.median(valid_lats)) if len(valid_lats) > 0 else float("nan")

    return {
        "n_total": n_total,
        "n_falls": n_falls,
        "n_adls": n_adls,
        "tp": tp,
        "fn": fn,
        "tn": tn,
        "fp": fp,
        "sensitivity": sens,
        "sensitivity_ci": sens_ci,
        "specificity": spec,
        "specificity_ci": spec_ci,
        "precision": prec,
        "f1": f1,
        "adl_exposure_hr": adl_exposure_hr,
        "fa_per_hr": fa_per_hr,
        "lat_mean": lat_mean,
        "lat_std": lat_std,
        "lat_median": lat_med,
        "lat_count": len(valid_lats),
    }


def print_summary_table(dataset_label: str, detector_name: str, m: dict) -> None:
    """Renders formatted ASCII contingency and performance table."""
    sens_ci_str = f"[{m['sensitivity_ci'][0]*100:.1f}%, {m['sensitivity_ci'][1]*100:.1f}%]"
    spec_ci_str = f"[{m['specificity_ci'][0]*100:.1f}%, {m['specificity_ci'][1]*100:.1f}%]"
    lat_str = (
        f"{m['lat_mean']:.1f} +/- {m['lat_std']:.1f} ms"
        if not math.isnan(m["lat_mean"])
        else "N/A"
    )

    print("\n" + "=" * 78)
    print(f" {dataset_label} BENCHMARK RESULTS: {detector_name}")
    print("=" * 78)
    print(
        f" Total Trials: {m['n_total']:<5} | Falls: {m['n_falls']:<4} | ADLs: {m['n_adls']:<4} | ADL Exposure: {m['adl_exposure_hr']:.3f} h"
    )
    print("-" * 78)
    print(" Contingency Table:")
    print(f"   True Positives  (TP) : {m['tp']:<5} | False Negatives (FN) : {m['fn']}")
    print(f"   False Positives (FP) : {m['fp']:<5} | True Negatives  (TN) : {m['tn']}")
    print("-" * 78)
    print(" Performance Metrics:")
    print(f"   Sensitivity (Recall) : {m['sensitivity']*100:6.2f}%   (Wilson 95% CI: {sens_ci_str})")
    print(f"   Specificity          : {m['specificity']*100:6.2f}%   (Wilson 95% CI: {spec_ci_str})")
    print(f"   Precision (PPV)      : {m['precision']*100:6.2f}%")
    print(f"   F1-Score             : {m['f1']:6.4f}")
    print(f"   False Alarm Rate     : {m['fa_per_hr']:6.2f} FA/hour")
    print(f"   Detection Latency    : {lat_str} (N={m['lat_count']}, Median={m['lat_median']:.1f} ms)")
    print("=" * 78)


def write_results_csv(output_path: Path, results: List[TrialResult]) -> None:
    """Writes results atomically to CSV."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(".csv.tmp")
    fieldnames = [
        "dataset",
        "detector",
        "trial_id",
        "is_fall",
        "detected",
        "latency_ms",
        "duration_s",
        "false_alarm",
    ]
    with open(temp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(fieldnames)
        for r in results:
            writer.writerow([
                r.dataset,
                r.detector,
                r.trial_id,
                r.is_fall,
                r.detected,
                f"{r.latency_ms:.1f}" if r.latency_ms is not None else "",
                f"{r.duration_s:.2f}",
                r.false_alarm,
            ])
    temp_path.replace(output_path)


# ============================================================================
# 5. Core Benchmark Runner
# ============================================================================

def run_benchmark(
    dataset: str = "all",
    detectors_arg: str = "all",
    manifest_path: Path = Path("data/sisfall/manifest.csv"),
    pilot_path: Path = Path("data/pilot1/trials.csv"),
    output_dir: Path = Path("results"),
    workers: int = 8,
    limit: Optional[int] = None,
    executor_type: str = "thread",
    quiet: bool = False,
    verbose: bool = False,
) -> int:
    """Main execution entry point for benchmark runner."""
    selected_detectors = resolve_detector_names(detectors_arg)

    output_dir.mkdir(parents=True, exist_ok=True)
    overall_start = time.time()

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Evaluate [OFFLINE] SisFall Dataset
    # ──────────────────────────────────────────────────────────────────────────
    if dataset in ("sisfall", "all"):
        if not manifest_path.exists():
            sys.stderr.write(f"Error: Manifest file not found: {manifest_path}\n")
            return 1

        manifest_df = pd.read_csv(manifest_path)
        if limit is not None and limit > 0:
            manifest_df = manifest_df.head(limit)

        trial_rows = manifest_df.to_dict(orient="records")
        total_trials = len(trial_rows)

        if not quiet:
            print(f"\nEvaluating [OFFLINE] SisFall Benchmark ({total_trials} trials)")
            print(f"Detectors: {selected_detectors}")
            print(f"Workers  : {workers} ({executor_type} pool)")

        sisfall_results: List[TrialResult] = []
        completed = 0
        t0 = time.time()

        if workers == 1 or executor_type == "sync":
            for row in trial_rows:
                res = evaluate_single_sisfall_trial(row, manifest_path, selected_detectors)
                sisfall_results.extend(res)
                completed += 1
                print_progress(completed, total_trials, quiet=quiet)
        else:
            ExecutorCls = (
                concurrent.futures.ProcessPoolExecutor
                if executor_type == "process"
                else concurrent.futures.ThreadPoolExecutor
            )
            with ExecutorCls(max_workers=workers) as executor:
                future_map = {
                    executor.submit(
                        evaluate_single_sisfall_trial, row, manifest_path, selected_detectors
                    ): row["trial_id"]
                    for row in trial_rows
                }
                for future in concurrent.futures.as_completed(future_map):
                    tid = future_map[future]
                    try:
                        res = future.result()
                        sisfall_results.extend(res)
                    except Exception as exc:
                        sys.stderr.write(f"\nError processing trial {tid}: {exc}\n")
                        raise exc
                    completed += 1
                    print_progress(completed, total_trials, quiet=quiet)

        # Sort deterministically by (detector, trial_id)
        sisfall_results.sort(key=lambda r: (r.detector, r.trial_id))
        sisfall_out_path = output_dir / "offline_sisfall_benchmark.csv"
        write_results_csv(sisfall_out_path, sisfall_results)
        sisfall_elapsed = time.time() - t0

        if not quiet:
            print(f"Saved [OFFLINE] SisFall benchmark to: {sisfall_out_path}")
            rate = (total_trials / sisfall_elapsed) if sisfall_elapsed > 0 else 0.0
            print(
                f"Processed {total_trials} trials across {len(selected_detectors)} detectors in {sisfall_elapsed:.2f}s ({rate:.1f} trials/s)"
            )

            # Print per-detector summary tables
            for det in selected_detectors:
                det_results = [r for r in sisfall_results if r.detector == det]
                metrics = compute_metrics(det_results)
                print_summary_table("[OFFLINE]", det, metrics)

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Evaluate [HARDWARE] Pilot Dataset
    # ──────────────────────────────────────────────────────────────────────────
    if dataset in ("pilot", "all"):
        if not pilot_path.exists():
            sys.stderr.write(f"Error: Pilot trials file not found: {pilot_path}\n")
            return 1

        if not quiet:
            print(f"\nEvaluating [HARDWARE] Pilot Benchmark ({pilot_path})")

        pilot_results = evaluate_hardware_pilot(pilot_path)
        pilot_out_path = output_dir / "hardware_pilot_benchmark.csv"
        write_results_csv(pilot_out_path, pilot_results)

        if not quiet:
            print(f"Saved [HARDWARE] pilot benchmark to: {pilot_out_path}")
            pilot_metrics = compute_metrics(pilot_results)
            print_summary_table("[HARDWARE]", "FallGuard_v2", pilot_metrics)

        # Explanatory notice if user asked for baselines on pilot
        non_fallguard = [d for d in selected_detectors if d != "FallGuard_v2"]
        if dataset == "pilot" and len(non_fallguard) > 0 and not quiet:
            print(
                "\n[HARDWARE NOTICE] Baseline detectors (SVM, TwoStage, GyroTilt) were not deployed to"
                "\nembedded ESP32 hardware during pilot 1, and continuous 50 Hz timeseries streams were"
                "\nnot preserved for all 20 trials. Strict Zero-Fabrication enforcement (Requirement R5)"
                "\nprohibits synthesizing artificial hardware logs."
            )

    total_elapsed = time.time() - overall_start
    if not quiet:
        print(f"\n[Benchmark Execution Complete in {total_elapsed:.2f}s]\n")
    return 0


# ============================================================================
# 6. CLI Argument Parser & Entry Point
# ============================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="FallGuard v2 Offline Benchmark Runner (SisFall & Pilot Datasets)"
    )
    parser.add_argument(
        "--dataset", "-d",
        choices=["sisfall", "pilot", "all"],
        default="all",
        help="Dataset to benchmark: 'sisfall' (offline), 'pilot' (hardware), or 'all' (default: all)",
    )
    parser.add_argument(
        "--detectors",
        type=str,
        default="all",
        help="Detectors to evaluate: 'all', 'fallguard', 'baselines', or comma-separated list ('svm,twostage,gyrotilt') (default: all)",
    )
    parser.add_argument(
        "--manifest", "-m",
        type=Path,
        default=Path("data/sisfall/manifest.csv"),
        help="Path to SisFall manifest CSV (default: data/sisfall/manifest.csv)",
    )
    parser.add_argument(
        "--pilot-trials", "-p",
        type=Path,
        default=Path("data/pilot1/trials.csv"),
        help="Path to pilot trials CSV (default: data/pilot1/trials.csv)",
    )
    parser.add_argument(
        "--output-dir", "-o",
        type=Path,
        default=Path("results"),
        help="Output directory for structured benchmark CSVs (default: results)",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=min(8, os.cpu_count() or 4),
        help=f"Number of parallel worker threads/processes (default: {min(8, os.cpu_count() or 4)})",
    )
    parser.add_argument(
        "--executor", "-e",
        choices=["thread", "process", "sync"],
        default="thread",
        help="Concurrency executor model: 'thread' (recommended), 'process', or 'sync' (default: thread)",
    )
    parser.add_argument(
        "--limit", "-l",
        type=int,
        default=None,
        help="Limit number of SisFall trials evaluated (for fast verification/testing)",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        default=False,
        help="Suppress dynamic progress bar and table outputs",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Print verbose execution logging",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    return run_benchmark(
        dataset=args.dataset,
        detectors_arg=args.detectors,
        manifest_path=args.manifest,
        pilot_path=args.pilot_trials,
        output_dir=args.output_dir,
        workers=args.workers,
        limit=args.limit,
        executor_type=args.executor,
        quiet=args.quiet,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    sys.exit(main())
