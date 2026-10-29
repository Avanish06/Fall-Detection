#!/usr/bin/env python3
"""
FallGuard v2 — Discrete Experimental Trials Analysis
=====================================================

Analyzes discrete experimental trial records gathered from the FallGuard
experimental trial protocol.

EXACT EVENT-MATCHING RULE:
--------------------------
Each row in the dataset represents a discrete experimental trial of duration T
conducted under supervised protocol.

1. Ground Truth (Protocol Label):
   - Defined by the 'is_fall' column as recorded by human experimenters.
   - y_i = 1 for simulated/actual fall trials.
   - y_i = 0 for Activities of Daily Living (ADL) control trials.

2. Device Detection:
   - Defined by the 'detected' column recorded from the FallGuard system.
   - y_hat_i = 1 if FallGuard confirmed a fall and issued an alert during trial i.
   - y_hat_i = 0 if no alert was triggered during trial i.

3. Classification Outcomes:
   - True Positive (TP)  : is_fall == True  AND detected == True
   - False Positive (FP) : is_fall == False AND detected == True   (False Alarm)
   - True Negative (TN)  : is_fall == False AND detected == False  (Correct Rejection)
   - False Negative (FN) : is_fall == True  AND detected == False  (Missed Fall)

4. Latency Matching Rule:
   - Latency (latency_ms) is measured strictly between the verified physical
     onset marker (t_onset) and the firmware confirmation timestamp (t_confirm).
   - Latency statistics (Mean, SD, Median, Min, Max) are computed ONLY for
     True Positive fall trials where latency_ref == 'onset_marker' and latency_ms > 0.
   - Trials with surrogate or unverified timing references are excluded from
     latency metric computation to preserve IEEE measurement validity.

5. False Alarm Exposure Rule:
   - False alarm rate is computed by summing duration_s across all negative
     (ADL) trials: Exposure_Hours = sum(duration_s_ADL) / 3600.
   - FA Rate = FP / Exposure_Hours.
   - Reported with the explicit methodological qualification:
     "Short-exposure experimental protocol; not a continuous daily-living estimate."

CONFIDENCE INTERVALS:
---------------------
Proportions (Sensitivity, Specificity, Precision, Accuracy) are reported with
Wilson 95% score confidence intervals (two-sided, z = 1.95996), which maintain
coverage accuracy even for small sample sizes and proportions near 0 or 1.

PROVENANCE & INTEGRITY CONSTRAINTS:
-----------------------------------
- Requires an explicit --source flag (hardware | offline | simulated | synthetic).
- If --source synthetic or the CSV path contains 'test', 'fake', or 'synthetic',
  marks every section with 'SYNTHETIC - NOT FOR PUBLICATION' and refuses to write
  output to results/.
- Prohibits ingestion of results/fallguard_combined_benchmark.csv (which is a
  continuous sample stream, not discrete trial records).
- Strictly requires an explicit CSV path; never reads from Downloads or default paths.
"""

import sys
import os
import math
import argparse
from pathlib import Path
import numpy as np
import pandas as pd


# Expected baseline schema for collector's trials CSV
REQUIRED_COLUMNS = [
    "trial_id",
    "participant",
    "key",
    "is_fall",
    "detected",
    "sensor_fault",
    "latency_ms",
    "latency_ref",
    "placement",
    "orientation",
]

ALLOWED_SOURCES = ["hardware", "offline", "simulated", "synthetic"]


def wilson_ci(k: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """
    Compute two-sided Wilson score confidence interval for a binomial proportion.
    
    Parameters:
        k: Number of successes / positives
        n: Total number of trials
        confidence: Confidence level (default: 0.95)
        
    Returns:
        (lower_bound, upper_bound) as floats in [0.0, 1.0].
        Returns (nan, nan) if n == 0.
    """
    if n <= 0:
        return (float("nan"), float("nan"))
    
    if math.isclose(confidence, 0.95):
        z = 1.959963984540054
    elif math.isclose(confidence, 0.99):
        z = 2.5758293035489004
    elif math.isclose(confidence, 0.90):
        z = 1.6448536269514722
    else:
        z = 1.959963984540054

    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    spread = (z / denom) * math.sqrt((p * (1.0 - p) / n) + (z2 / (4.0 * (n ** 2))))
    lower = max(0.0, center - spread)
    upper = min(1.0, center + spread)
    return (lower, upper)


def parse_bool(val) -> bool:
    """Parse flexible boolean representations into standard Python bool."""
    if pd.isna(val):
        return False
    if isinstance(val, (bool, np.bool_)):
        return bool(val)
    s = str(val).strip().lower()
    return s in ("true", "1", "1.0", "t", "yes", "y", "fall", "detected")


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="FallGuard v2 Discrete Experimental Trials Analysis"
    )
    parser.add_argument(
        "csv_path",
        nargs="?",
        type=str,
        default=None,
        help="Path to collector trials CSV file (explicit path required)"
    )
    parser.add_argument(
        "--csv", "-c",
        type=str,
        dest="csv_opt",
        default=None,
        help="Alternative argument for trials CSV path"
    )
    parser.add_argument(
        "--source", "-s",
        type=str.lower,
        choices=ALLOWED_SOURCES,
        required=True,
        help="Data provenance source (hardware | offline | simulated | synthetic). Required."
    )
    parser.add_argument(
        "--split-by",
        type=str.lower,
        choices=["participant"],
        default=None,
        help="Cross-validation / stratification mode (e.g. participant for leave-one-participant-out)"
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Optional path to save formatted text report"
    )
    return parser.parse_args()


def validate_and_load_csv(csv_path_str: str) -> tuple[pd.DataFrame, Path]:
    """Validate input file against safety rules, provenance rules, and column schema."""
    if not csv_path_str:
        print("\n[ERROR] No trials CSV file specified.")
        print("analyze_trials.py strictly requires an explicit path to a discrete trials CSV.")
        print("Usage: python tools/analyze_trials.py --source <source> <path_to_trials.csv>")
        sys.exit(1)

    csv_path = Path(csv_path_str).resolve()
    if not csv_path.exists():
        print(f"\n[ERROR] File does not exist: {csv_path}")
        sys.exit(1)

    # Reject reading from default Downloads folder directly
    user_home = Path.home().resolve()
    downloads_dir = (user_home / "Downloads").resolve()
    try:
        csv_path.relative_to(downloads_dir)
        is_in_downloads = True
    except ValueError:
        is_in_downloads = False

    if is_in_downloads:
        print("\n" + "=" * 78)
        print("[ERROR] Refusing to read trial dataset from user Downloads directory!")
        print("=" * 78)
        print(f"Path: {csv_path}")
        print("Test/trial files must be placed within a dedicated repository experiment directory.")
        print("Please move or reference the trial CSV from an explicit project directory.")
        print("=" * 78 + "\n")
        sys.exit(1)

    # HARD CONSTRAINT: Prohibit fallguard_combined_benchmark.csv
    resolved_name = csv_path.name.lower()
    if "fallguard_combined_benchmark.csv" in resolved_name or "combined_benchmark" in resolved_name:
        print("\n" + "=" * 78)
        print("[VIOLATION ERROR] Prohibited Dataset Specified!")
        print("=" * 78)
        print(f"File: {csv_path}")
        print("Do NOT use results/fallguard_combined_benchmark.csv with analyze_trials.py.")
        print("That file contains raw 50 Hz continuous telemetry for timeseries replay.")
        print("analyze_trials.py strictly requires discrete collector trial records containing:")
        print(f"  {', '.join(REQUIRED_COLUMNS)}")
        print("=" * 78 + "\n")
        sys.exit(1)

    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:
        print(f"\n[ERROR] Failed to parse CSV file: {exc}")
        sys.exit(1)

    # Normalize column names: strip whitespace and lowercase for comparison
    col_map = {c: c.strip().lower() for c in df.columns}
    normalized_cols = set(col_map.values())
    
    missing = [c for c in REQUIRED_COLUMNS if c.lower() not in normalized_cols]
    if missing:
        print("\n" + "=" * 78)
        print("[ERROR] Schema Mismatch: Missing Required Columns!")
        print("=" * 78)
        print(f"Input File: {csv_path}")
        print(f"Missing columns: {missing}")
        print(f"Present columns: {list(df.columns)}")
        print("\nExpected collector trials schema requires:")
        for rc in REQUIRED_COLUMNS:
            print(f"  - {rc}")
        print("=" * 78 + "\n")
        sys.exit(1)

    # Standardize column casing to REQUIRED_COLUMNS + optional duration columns
    rename_dict = {}
    for actual_col in df.columns:
        norm = actual_col.strip().lower()
        matched = False
        for req in REQUIRED_COLUMNS:
            if norm == req.lower():
                rename_dict[actual_col] = req
                matched = True
                break
        if not matched:
            if norm in ("duration_s", "duration_seconds", "duration"):
                rename_dict[actual_col] = "duration_s"

    df = df.rename(columns=rename_dict)

    if len(df) == 0:
        print(f"\n[ERROR] Trials dataset in {csv_path.name} is empty (0 rows).")
        sys.exit(1)

    return df, csv_path


def analyze_subset(df_sub: pd.DataFrame) -> dict:
    """Analyze a single subset of trials (for global or fold evaluation)."""
    n_total = len(df_sub)

    is_fall = df_sub["is_fall"].apply(parse_bool)
    detected = df_sub["detected"].apply(parse_bool)
    fault = df_sub["sensor_fault"].apply(parse_bool)

    tp_mask = is_fall & detected
    fp_mask = (~is_fall) & detected
    tn_mask = (~is_fall) & (~detected)
    fn_mask = is_fall & (~detected)

    tp = int(tp_mask.sum())
    fp = int(fp_mask.sum())
    tn = int(tn_mask.sum())
    fn = int(fn_mask.sum())

    n_falls = tp + fn
    n_adls = tn + fp

    sensitivity = (tp / n_falls) if n_falls > 0 else float("nan")
    specificity = (tn / n_adls) if n_adls > 0 else float("nan")
    precision = (tp / (tp + fp)) if (tp + fp) > 0 else float("nan")
    accuracy = ((tp + tn) / n_total) if n_total > 0 else float("nan")

    if not math.isnan(sensitivity) and not math.isnan(precision) and (precision + sensitivity) > 0:
        f1_score = 2.0 * (precision * sensitivity) / (precision + sensitivity)
    elif tp == 0 and (fn > 0 or fp > 0):
        f1_score = 0.0
    else:
        f1_score = float("nan")

    sens_ci = wilson_ci(tp, n_falls) if n_falls > 0 else (float("nan"), float("nan"))
    spec_ci = wilson_ci(tn, n_adls) if n_adls > 0 else (float("nan"), float("nan"))
    prec_ci = wilson_ci(tp, tp + fp) if (tp + fp) > 0 else (float("nan"), float("nan"))
    acc_ci = wilson_ci(tp + tn, n_total) if n_total > 0 else (float("nan"), float("nan"))

    # False alarm rate based on ADL exposure
    if "duration_s" in df_sub.columns:
        adl_durations = pd.to_numeric(df_sub.loc[~is_fall, "duration_s"], errors="coerce").fillna(0.0)
        adl_exposure_s = float(adl_durations.sum())
        adl_exposure_h = adl_exposure_s / 3600.0
        fa_per_hour = (fp / adl_exposure_h) if adl_exposure_h > 0 else float("nan")
    else:
        adl_exposure_s = float("nan")
        adl_exposure_h = float("nan")
        fa_per_hour = float("nan")

    # Latency: onset_marker only
    ref_series = df_sub["latency_ref"].astype(str).str.strip().str.lower()
    onset_mask = ref_series == "onset_marker"
    lat_numeric = pd.to_numeric(df_sub["latency_ms"], errors="coerce")
    valid_lat_mask = tp_mask & onset_mask & lat_numeric.notna() & (lat_numeric > 0)
    valid_latencies = lat_numeric[valid_lat_mask].values

    if len(valid_latencies) > 0:
        lat_count = len(valid_latencies)
        lat_mean = float(np.mean(valid_latencies))
        lat_sd = float(np.std(valid_latencies, ddof=1)) if lat_count > 1 else 0.0
        lat_median = float(np.median(valid_latencies))
        lat_min = float(np.min(valid_latencies))
        lat_max = float(np.max(valid_latencies))
    else:
        lat_count = 0
        lat_mean = float("nan")
        lat_sd = float("nan")
        lat_median = float("nan")
        lat_min = float("nan")
        lat_max = float("nan")

    other_ref_count = int((tp_mask & (~onset_mask) & lat_numeric.notna()).sum())

    fall_faults = int(fault[is_fall].sum())
    adl_faults = int(fault[~is_fall].sum())
    total_faults = fall_faults + adl_faults

    return {
        "n_total": n_total,
        "n_falls": n_falls,
        "n_adls": n_adls,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "sensitivity": sensitivity,
        "sens_ci": sens_ci,
        "specificity": specificity,
        "spec_ci": spec_ci,
        "precision": precision,
        "prec_ci": prec_ci,
        "f1_score": f1_score,
        "accuracy": accuracy,
        "acc_ci": acc_ci,
        "adl_exposure_s": adl_exposure_s,
        "adl_exposure_h": adl_exposure_h,
        "fa_per_hour": fa_per_hour,
        "latency_stats": {
            "count": lat_count,
            "mean": lat_mean,
            "sd": lat_sd,
            "median": lat_median,
            "min": lat_min,
            "max": lat_max,
            "other_ref_count": other_ref_count,
        },
        "sensor_faults": {
            "total": total_faults,
            "in_falls": fall_faults,
            "in_adls": adl_faults,
            "rate_falls": (fall_faults / n_falls) if n_falls > 0 else 0.0,
            "rate_adls": (adl_faults / n_adls) if n_adls > 0 else 0.0,
        },
    }


def analyze_trials(df: pd.DataFrame, split_by: str | None = None) -> dict:
    """Perform full trial analysis with optional leave-one-participant-out split."""
    global_results = analyze_subset(df)

    # Per-activity breakdown (grouped by 'key')
    activity_results = []
    for key, group in df.groupby("key", sort=True):
        grp_res = analyze_subset(group)
        k_falls = grp_res["n_falls"]
        k_adls = grp_res["n_adls"]

        if k_falls > 0 and k_adls == 0:
            act_type = "FALL"
            metric_name = "Sensitivity"
            rate = grp_res["sensitivity"]
            ci_low, ci_high = grp_res["sens_ci"]
        elif k_adls > 0 and k_falls == 0:
            act_type = "ADL"
            metric_name = "Specificity"
            rate = grp_res["specificity"]
            ci_low, ci_high = grp_res["spec_ci"]
        else:
            act_type = "MIXED"
            metric_name = "Accuracy"
            rate = grp_res["accuracy"]
            ci_low, ci_high = grp_res["acc_ci"]

        activity_results.append({
            "key": str(key),
            "type": act_type,
            "total": grp_res["n_total"],
            "falls": k_falls,
            "adls": k_adls,
            "tp": grp_res["tp"],
            "fp": grp_res["fp"],
            "tn": grp_res["tn"],
            "fn": grp_res["fn"],
            "metric_name": metric_name,
            "rate": rate,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "faults": grp_res["sensor_faults"]["total"],
        })

    # Per-participant breakdown
    participant_results = []
    for pid, group in df.groupby("participant", sort=True):
        p_res = analyze_subset(group)
        participant_results.append({
            "participant": str(pid),
            "total": p_res["n_total"],
            "falls": p_res["n_falls"],
            "adls": p_res["n_adls"],
            "tp": p_res["tp"],
            "fp": p_res["fp"],
            "tn": p_res["tn"],
            "fn": p_res["fn"],
            "sens": p_res["sensitivity"],
            "sens_ci": p_res["sens_ci"],
            "spec": p_res["specificity"],
            "spec_ci": p_res["spec_ci"],
            "acc": p_res["accuracy"],
            "acc_ci": p_res["acc_ci"],
            "faults": p_res["sensor_faults"]["total"],
        })

    # Leave-One-Participant-Out (LOPO) cross-validation evaluation
    lopo_results = None
    if split_by == "participant":
        unique_participants = sorted(df["participant"].unique())
        folds = []
        for pid in unique_participants:
            test_mask = df["participant"] == pid
            test_sub = df[test_mask]
            train_sub = df[~test_mask]

            fold_test_res = analyze_subset(test_sub)
            folds.append({
                "held_out_participant": str(pid),
                "n_test": len(test_sub),
                "n_train": len(train_sub),
                "tp": fold_test_res["tp"],
                "fp": fold_test_res["fp"],
                "tn": fold_test_res["tn"],
                "fn": fold_test_res["fn"],
                "sens": fold_test_res["sensitivity"],
                "spec": fold_test_res["specificity"],
                "prec": fold_test_res["precision"],
                "acc": fold_test_res["accuracy"],
            })

        # Macro-averages across held-out folds (excluding NaNs)
        valid_sens = [f["sens"] for f in folds if not math.isnan(f["sens"])]
        valid_spec = [f["spec"] for f in folds if not math.isnan(f["spec"])]
        valid_acc = [f["acc"] for f in folds if not math.isnan(f["acc"])]

        macro_sens_mean = float(np.mean(valid_sens)) if valid_sens else float("nan")
        macro_sens_sd = float(np.std(valid_sens, ddof=1)) if len(valid_sens) > 1 else 0.0

        macro_spec_mean = float(np.mean(valid_spec)) if valid_spec else float("nan")
        macro_spec_sd = float(np.std(valid_spec, ddof=1)) if len(valid_spec) > 1 else 0.0

        macro_acc_mean = float(np.mean(valid_acc)) if valid_acc else float("nan")
        macro_acc_sd = float(np.std(valid_acc, ddof=1)) if len(valid_acc) > 1 else 0.0

        lopo_results = {
            "folds": folds,
            "macro_sens_mean": macro_sens_mean,
            "macro_sens_sd": macro_sens_sd,
            "macro_spec_mean": macro_spec_mean,
            "macro_spec_sd": macro_spec_sd,
            "macro_acc_mean": macro_acc_mean,
            "macro_acc_sd": macro_acc_sd,
        }

    global_results["activities"] = activity_results
    global_results["participants"] = participant_results
    global_results["lopo"] = lopo_results
    return global_results


def format_report(results: dict, csv_name: str, source: str, is_synthetic: bool) -> str:
    """Format evaluation report with dynamic source tags and synthetic warnings."""
    lines = []
    w = 78
    tag = f"[{source.upper()}]"
    synth_banner = " *** SYNTHETIC - NOT FOR PUBLICATION ***" if is_synthetic else ""

    lines.append("=" * w)
    lines.append(f"        {tag} FALLGUARD V2 - DISCRETE TRIALS ANALYSIS REPORT        ")
    if is_synthetic:
        lines.append(f"          {synth_banner.strip()}          ")
    lines.append("=" * w)
    lines.append(f" Provenance Source: {source.upper()}{synth_banner}")
    lines.append(f" Dataset File     : {csv_name}")
    lines.append(f" Total Trials (N) : {results['n_total']}")
    lines.append(f" Fall Trials      : {results['n_falls']}")
    lines.append(f" Control ADL Runs : {results['n_adls']}")
    lines.append("-" * w)

    # Small sample size warning
    if results["n_total"] < 30:
        lines.append("")
        lines.append(f" {tag}{synth_banner} WARNING - SMALL SAMPLE SIZE DETECTED:")
        lines.append(f" Total trial count (N = {results['n_total']}) is less than 30.")
        lines.append(" Statistical variance is elevated; Wilson 95% intervals will exhibit wider")
        lines.append(" bounds. Report findings as exploratory evaluation.")
        lines.append("-" * w)

    # Contingency matrix
    tp, fp = results["tp"], results["fp"]
    fn, tn = results["fn"], results["tn"]
    lines.append("")
    lines.append(f" {tag}{synth_banner} CONTINGENCY MATRIX (DISCRETE TRIALS)")
    lines.append(" " + "-" * 62)
    lines.append(f" {'Actual / Predicted':<24} | {'Detected (Fall)':<16} | {'Not Detected (ADL)':<18}")
    lines.append(" " + "-" * 62)
    lines.append(f" {'Ground Truth Fall':<24} | {tp:<5} (TP)         | {fn:<5} (FN)")
    lines.append(f" {'Ground Truth ADL':<24} | {fp:<5} (FP)         | {tn:<5} (TN)")
    lines.append(" " + "-" * 62)

    # Primary metrics
    sens, (s_low, s_high) = results["sensitivity"], results["sens_ci"]
    spec, (sp_low, sp_high) = results["specificity"], results["spec_ci"]
    prec, (p_low, p_high) = results["precision"], results["prec_ci"]
    acc, (a_low, a_high) = results["accuracy"], results["acc_ci"]
    f1 = results["f1_score"]

    lines.append("")
    lines.append(f" {tag}{synth_banner} PERFORMANCE METRICS (WILSON 95% CONFIDENCE INTERVALS)")
    lines.append(" " + "-" * 62)
    lines.append(f" Sensitivity (Recall) : {sens * 100:>6.2f}%   [95% CI: {s_low * 100:5.2f}% - {s_high * 100:5.2f}%]")
    lines.append(f" Specificity          : {spec * 100:>6.2f}%   [95% CI: {sp_low * 100:5.2f}% - {sp_high * 100:5.2f}%]")
    if not math.isnan(prec):
        lines.append(f" Precision (PPV)      : {prec * 100:>6.2f}%   [95% CI: {p_low * 100:5.2f}% - {p_high * 100:5.2f}%]")
    else:
        lines.append(" Precision (PPV)      :    N/A    [No positive detections]")
    lines.append(f" F1-Score             : {f1:>8.4f}")
    lines.append(f" Overall Accuracy     : {acc * 100:>6.2f}%   [95% CI: {a_low * 100:5.2f}% - {a_high * 100:5.2f}%]")

    # False Alarms per hour
    lines.append(" " + "-" * 62)
    if not math.isnan(results["fa_per_hour"]):
        lines.append(f" False Alarms / Hour  : {results['fa_per_hour']:>6.2f} FA/hr ({fp} FP in {results['adl_exposure_s']:.1f} s = {results['adl_exposure_h']:.4f} h ADL)")
        lines.append("                        [NOTE: Protocol-based short-exposure; not a continuous daily-living estimate.]")
    else:
        lines.append(" False Alarms / Hour  :    N/A    (No duration_s column or zero ADL exposure recorded)")
    lines.append(" " + "-" * 62)

    # Latency section (onset_marker only)
    lat = results["latency_stats"]
    lines.append("")
    lines.append(f" {tag}{synth_banner} ONSET-TO-CONFIRMATION LATENCY (latency_ref = 'onset_marker')")
    lines.append(" " + "-" * 62)
    if lat["count"] > 0:
        lines.append(f" Valid Trials Analyzed : {lat['count']} True Positive fall(s)")
        lines.append(f" Mean Latency          : {lat['mean']:.1f} ms")
        lines.append(f" Standard Deviation    : {lat['sd']:.1f} ms")
        lines.append(f" Median Latency        : {lat['median']:.1f} ms")
        lines.append(f" Range [Min, Max]      : [{lat['min']:.1f} ms, {lat['max']:.1f} ms]")
    else:
        lines.append(" Valid Trials Analyzed : 0")
        lines.append(" [NOTE] No True Positive fall trials contained numeric latency_ms with")
        lines.append("        latency_ref == 'onset_marker'.")
    if lat["other_ref_count"] > 0:
        lines.append(f" [EXCLUDED NON-ONSET]  : {lat['other_ref_count']} trial(s) excluded due to non-onset reference.")
    lines.append(" " + "-" * 62)

    # Sensor faults
    sf = results["sensor_faults"]
    lines.append("")
    lines.append(f" {tag}{synth_banner} SENSOR INTEGRITY & FAULT SURVEILLANCE")
    lines.append(" " + "-" * 62)
    lines.append(f" Total Sensor Faults   : {sf['total']}")
    lines.append(f" Faults in Fall Trials : {sf['in_falls']} / {results['n_falls']} ({sf['rate_falls'] * 100:.1f}%)")
    lines.append(f" Faults in ADL Trials  : {sf['in_adls']} / {results['n_adls']} ({sf['rate_adls'] * 100:.1f}%)")
    lines.append(" " + "-" * 62)

    # Per-activity table
    lines.append("")
    lines.append(f" {tag}{synth_banner} PER-ACTIVITY EVALUATION (WILSON 95% CI)")
    lines.append(" " + "-" * 76)
    lines.append(f" {'Activity Key':<20} | {'Type':<5} | {'N':<4} | {'TP/TN':<6} | {'Metric':<11} | {'Rate':<7} | {'Wilson 95% CI':<16}")
    lines.append(" " + "-" * 76)
    for act in results["activities"]:
        rate_str = f"{act['rate'] * 100:5.1f}%"
        ci_str = f"[{act['ci_low'] * 100:4.1f}%, {act['ci_high'] * 100:4.1f}%]"
        tptn_str = f"{act['tp'] if act['type'] == 'FALL' else act['tn']}/{act['total']}"
        lines.append(
            f" {act['key']:<20} | {act['type']:<5} | {act['total']:<4} | {tptn_str:<6} | {act['metric_name']:<11} | {rate_str:<7} | {ci_str:<16}"
        )
    lines.append(" " + "-" * 76)

    # Per-participant table
    lines.append("")
    lines.append(f" {tag}{synth_banner} PER-PARTICIPANT BREAKDOWN (WILSON 95% CI)")
    lines.append(" " + "-" * 76)
    lines.append(f" {'Participant':<14} | {'N':<4} | {'Sens (Falls)':<16} | {'Spec (ADLs)':<16} | {'Accuracy':<16}")
    lines.append(" " + "-" * 76)
    for part in results["participants"]:
        if not math.isnan(part["sens"]):
            s_str = f"{part['sens'] * 100:5.1f}% [{part['sens_ci'][0] * 100:3.0f}-{part['sens_ci'][1] * 100:3.0f}%]"
        else:
            s_str = "N/A (0 falls)"

        if not math.isnan(part["spec"]):
            sp_str = f"{part['spec'] * 100:5.1f}% [{part['spec_ci'][0] * 100:3.0f}-{part['spec_ci'][1] * 100:3.0f}%]"
        else:
            sp_str = "N/A (0 ADLs)"

        if not math.isnan(part["acc"]):
            a_str = f"{part['acc'] * 100:5.1f}% [{part['acc_ci'][0] * 100:3.0f}-{part['acc_ci'][1] * 100:3.0f}%]"
        else:
            a_str = "N/A"

        lines.append(f" {part['participant']:<14} | {part['total']:<4} | {s_str:<16} | {sp_str:<16} | {a_str:<16}")
    lines.append(" " + "-" * 76)

    # Leave-One-Participant-Out Table (if split-by participant requested)
    if results["lopo"] is not None:
        lopo = results["lopo"]
        lines.append("")
        lines.append(f" {tag}{synth_banner} LEAVE-ONE-PARTICIPANT-OUT (LOPO) CROSS-VALIDATION")
        lines.append(" " + "-" * 76)
        lines.append(f" {'Held-Out Fold':<16} | {'N_test':<6} | {'Sens (Recall)':<14} | {'Specificity':<14} | {'Accuracy':<14}")
        lines.append(" " + "-" * 76)
        for fold in lopo["folds"]:
            sens_val = f"{fold['sens'] * 100:5.1f}%" if not math.isnan(fold["sens"]) else "N/A"
            spec_val = f"{fold['spec'] * 100:5.1f}%" if not math.isnan(fold["spec"]) else "N/A"
            acc_val = f"{fold['acc'] * 100:5.1f}%" if not math.isnan(fold["acc"]) else "N/A"
            lines.append(
                f" {fold['held_out_participant']:<16} | {fold['n_test']:<6} | {sens_val:<14} | {spec_val:<14} | {acc_val:<14}"
            )
        lines.append(" " + "-" * 76)
        lines.append(" MACRO-AVERAGE SUMMARY ACROSS HELD-OUT PARTICIPANTS:")
        lines.append(f"   Macro Sensitivity : {lopo['macro_sens_mean'] * 100:5.1f}% +/- {lopo['macro_sens_sd'] * 100:4.1f}%")
        lines.append(f"   Macro Specificity : {lopo['macro_spec_mean'] * 100:5.1f}% +/- {lopo['macro_spec_sd'] * 100:4.1f}%")
        lines.append(f"   Macro Accuracy    : {lopo['macro_acc_mean'] * 100:5.1f}% +/- {lopo['macro_acc_sd'] * 100:4.1f}%")
        lines.append(" " + "-" * 76)

    lines.append("")
    lines.append("=" * w)
    lines.append(f"               END OF {tag} REPORT{synth_banner}               ")
    lines.append("=" * w)
    return "\n".join(lines)


def main():
    args = parse_arguments()
    csv_file = args.csv_opt or args.csv_path

    if not csv_file:
        print("\n[ERROR] No trials CSV file specified.")
        print("analyze_trials.py strictly requires an explicit path to a discrete trials CSV.")
        print("Usage: python tools/analyze_trials.py --source <hardware|offline|simulated|synthetic> <path_to_trials.csv>")
        sys.exit(1)

    df, csv_path = validate_and_load_csv(csv_file)

    # Check synthetic status
    csv_str_lower = str(csv_path).lower()
    is_synthetic = (
        args.source == "synthetic"
        or "test" in csv_str_lower
        or "fake" in csv_str_lower
        or "synthetic" in csv_str_lower
    )

    # Check output safety if synthetic
    if is_synthetic and args.output:
        resolved_out = Path(args.output).resolve()
        results_dir = (Path(__file__).parent.parent / "results").resolve()
        is_subpath = False
        try:
            resolved_out.relative_to(results_dir)
            is_subpath = True
        except ValueError:
            pass
        if is_subpath or "results" in resolved_out.parts:
            print("\n" + "=" * 78)
            print(f"[{args.source.upper()} SAFETY ERROR] Refusing to write synthetic data/report to {args.output}!")
            print("=" * 78)
            print("Files derived from synthetic, test, or fake sources cannot be saved to results/.")
            print("Please specify an output location outside the publication results/ directory.")
            print("=" * 78 + "\n")
            sys.exit(1)

    results = analyze_trials(df, split_by=args.split_by)
    report_text = format_report(results, csv_path.name, args.source, is_synthetic)

    print(report_text)

    if args.output:
        out_path = Path(args.output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report_text, encoding="utf-8")
        print(f"\n[{args.source.upper()} INFO] Report successfully saved to: {out_path}")


if __name__ == "__main__":
    main()
