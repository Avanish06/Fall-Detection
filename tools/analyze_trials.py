#!/usr/bin/env python3
"""
FallGuard v2 — Discrete Experimental Trials Analysis
=====================================================

Analyzes discrete experimental trial records gathered from the FallGuard
collector hardware protocol.

EXACT EVENT-MATCHING RULE:
--------------------------
Each row in the dataset represents a discrete experimental trial of duration T
conducted under supervised laboratory/experimental protocol.

1. Ground Truth (Protocol Label):
   - Defined by the 'is_fall' column as recorded by human experimenters.
   - y_i = 1 for simulated fall trials.
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

CONFIDENCE INTERVALS:
---------------------
Proportions (Sensitivity, Specificity, Precision, Accuracy) are reported with
Wilson 95% score confidence intervals (two-sided, z = 1.95996), which maintain
coverage accuracy even for small sample sizes and proportions near 0 or 1.

SAFETY & INTEGRITY CONSTRAINTS:
--------------------------------
- Prohibits ingestion of results/fallguard_combined_benchmark.csv (which is a
  continuous sample stream, not discrete trial records).
- Flags small sample sizes (N < 30) with explicit methodological warnings.
- Explicitly partitions sensor fault occurrences across Falls and ADLs.
- All outputs are formally marked as HARDWARE experimental data.
"""

import sys
import os
import math
import argparse
from pathlib import Path
import numpy as np
import pandas as pd


# Expected schema for collector's trials CSV
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
    
    # z-score for standard confidence levels
    if math.isclose(confidence, 0.95):
        z = 1.959963984540054
    elif math.isclose(confidence, 0.99):
        z = 2.5758293035489004
    elif math.isclose(confidence, 0.90):
        z = 1.6448536269514722
    else:
        # Fallback approximation
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
        description="FallGuard v2 Hardware Experimental Discrete Trials Analysis"
    )
    parser.add_argument(
        "csv_path",
        nargs="?",
        type=str,
        default=None,
        help="Path to collector trials CSV file"
    )
    parser.add_argument(
        "--csv", "-c",
        type=str,
        dest="csv_opt",
        default=None,
        help="Alternative argument for trials CSV path"
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Optional path to save formatted text report"
    )
    return parser.parse_args()


def validate_and_load_csv(csv_path_str: str) -> pd.DataFrame:
    """Validate input file against safety rules and verify column schema."""
    if not csv_path_str:
        print("\n[HARDWARE ERROR] No trials CSV file specified.")
        print("Usage: python tools/analyze_trials.py <path_to_trials.csv>")
        sys.exit(1)

    csv_path = Path(csv_path_str)
    if not csv_path.exists():
        print(f"\n[HARDWARE ERROR] File does not exist: {csv_path.resolve()}")
        sys.exit(1)

    # HARD CONSTRAINT: Prohibit fallguard_combined_benchmark.csv
    resolved_name = csv_path.resolve().name.lower()
    if "fallguard_combined_benchmark.csv" in resolved_name or "combined_benchmark" in resolved_name:
        print("\n" + "=" * 78)
        print("[HARDWARE VIOLATION ERROR] Prohibited Dataset Specified!")
        print("=" * 78)
        print(f"File: {csv_path.resolve()}")
        print("Do NOT use results/fallguard_combined_benchmark.csv with analyze_trials.py.")
        print("That file contains raw 50 Hz continuous telemetry for timeseries replay.")
        print("analyze_trials.py strictly requires discrete collector trial records containing:")
        print(f"  {', '.join(REQUIRED_COLUMNS)}")
        print("=" * 78 + "\n")
        sys.exit(1)

    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:
        print(f"\n[HARDWARE ERROR] Failed to parse CSV file: {exc}")
        sys.exit(1)

    # Normalize column names: strip whitespace and lowercase for comparison
    col_map = {c: c.strip().lower() for c in df.columns}
    normalized_cols = set(col_map.values())
    
    missing = [c for c in REQUIRED_COLUMNS if c.lower() not in normalized_cols]
    if missing:
        print("\n" + "=" * 78)
        print("[HARDWARE ERROR] Schema Mismatch: Missing Required Columns!")
        print("=" * 78)
        print(f"Input File: {csv_path.resolve()}")
        print(f"Missing columns: {missing}")
        print(f"Present columns: {list(df.columns)}")
        print("\nExpected collector trials schema requires:")
        for rc in REQUIRED_COLUMNS:
            print(f"  - {rc}")
        print("=" * 78 + "\n")
        sys.exit(1)

    # Standardize column casing to REQUIRED_COLUMNS
    rename_dict = {}
    for actual_col in df.columns:
        norm = actual_col.strip().lower()
        for req in REQUIRED_COLUMNS:
            if norm == req.lower():
                rename_dict[actual_col] = req
                break
    df = df.rename(columns=rename_dict)

    if len(df) == 0:
        print(f"\n[HARDWARE ERROR] Trials dataset in {csv_path.name} is empty (0 rows).")
        sys.exit(1)

    return df


def analyze_trials(df: pd.DataFrame) -> dict:
    """Perform full hardware trial analysis according to specification."""
    n_total = len(df)

    # Standardize boolean series
    is_fall_series = df["is_fall"].apply(parse_bool)
    detected_series = df["detected"].apply(parse_bool)
    sensor_fault_series = df["sensor_fault"].apply(parse_bool)

    # Classification outcomes
    tp_mask = is_fall_series & detected_series
    fp_mask = (~is_fall_series) & detected_series
    tn_mask = (~is_fall_series) & (~detected_series)
    fn_mask = is_fall_series & (~detected_series)

    tp = int(tp_mask.sum())
    fp = int(fp_mask.sum())
    tn = int(tn_mask.sum())
    fn = int(fn_mask.sum())

    n_falls = tp + fn
    n_adls = tn + fp

    # Point estimates
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

    # Wilson 95% Confidence Intervals
    sens_ci = wilson_ci(tp, n_falls) if n_falls > 0 else (float("nan"), float("nan"))
    spec_ci = wilson_ci(tn, n_adls) if n_adls > 0 else (float("nan"), float("nan"))
    prec_ci = wilson_ci(tp, tp + fp) if (tp + fp) > 0 else (float("nan"), float("nan"))
    acc_ci = wilson_ci(tp + tn, n_total) if n_total > 0 else (float("nan"), float("nan"))

    # Latency evaluation: ONLY where latency_ref == 'onset_marker'
    ref_series = df["latency_ref"].astype(str).str.strip().str.lower()
    onset_mask = ref_series == "onset_marker"
    
    # Ensure numeric conversion for latency_ms
    lat_numeric = pd.to_numeric(df["latency_ms"], errors="coerce")
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

    # Latencies with other references (for audit count)
    other_ref_count = int((tp_mask & (~onset_mask) & lat_numeric.notna()).sum())

    # Sensor fault breakdown
    fall_faults = int(sensor_fault_series[is_fall_series].sum())
    adl_faults = int(sensor_fault_series[~is_fall_series].sum())
    total_faults = fall_faults + adl_faults

    # Per-activity breakdown (grouped by 'key')
    activity_results = []
    for key, group in df.groupby("key", sort=True):
        grp_falls = group["is_fall"].apply(parse_bool)
        grp_det = group["detected"].apply(parse_bool)
        grp_fault = group["sensor_fault"].apply(parse_bool)

        n_k = len(group)
        k_falls = int(grp_falls.sum())
        k_adls = n_k - k_falls
        k_faults = int(grp_fault.sum())

        k_tp = int((grp_falls & grp_det).sum())
        k_fp = int(((~grp_falls) & grp_det).sum())
        k_tn = int(((~grp_falls) & (~grp_det)).sum())
        k_fn = int((grp_falls & (~grp_det)).sum())

        if k_falls > 0 and k_adls == 0:
            act_type = "FALL"
            metric_name = "Sensitivity"
            rate = (k_tp / k_falls) if k_falls > 0 else 0.0
            ci_low, ci_high = wilson_ci(k_tp, k_falls)
        elif k_adls > 0 and k_falls == 0:
            act_type = "ADL"
            metric_name = "Specificity"
            rate = (k_tn / k_adls) if k_adls > 0 else 0.0
            ci_low, ci_high = wilson_ci(k_tn, k_adls)
        else:
            act_type = "MIXED"
            metric_name = "Accuracy"
            rate = ((k_tp + k_tn) / n_k) if n_k > 0 else 0.0
            ci_low, ci_high = wilson_ci(k_tp + k_tn, n_k)

        activity_results.append({
            "key": str(key),
            "type": act_type,
            "total": n_k,
            "falls": k_falls,
            "adls": k_adls,
            "tp": k_tp,
            "fp": k_fp,
            "tn": k_tn,
            "fn": k_fn,
            "metric_name": metric_name,
            "rate": rate,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "faults": k_faults,
        })

    # Per-participant breakdown
    participant_results = []
    for pid, group in df.groupby("participant", sort=True):
        grp_falls = group["is_fall"].apply(parse_bool)
        grp_det = group["detected"].apply(parse_bool)
        grp_fault = group["sensor_fault"].apply(parse_bool)

        n_p = len(group)
        p_falls = int(grp_falls.sum())
        p_adls = n_p - p_falls
        p_faults = int(grp_fault.sum())

        p_tp = int((grp_falls & grp_det).sum())
        p_fp = int(((~grp_falls) & grp_det).sum())
        p_tn = int(((~grp_falls) & (~grp_det)).sum())
        p_fn = int((grp_falls & (~grp_det)).sum())

        p_sens = (p_tp / p_falls) if p_falls > 0 else float("nan")
        p_sens_ci = wilson_ci(p_tp, p_falls) if p_falls > 0 else (float("nan"), float("nan"))

        p_spec = (p_tn / p_adls) if p_adls > 0 else float("nan")
        p_spec_ci = wilson_ci(p_tn, p_adls) if p_adls > 0 else (float("nan"), float("nan"))

        p_acc = ((p_tp + p_tn) / n_p) if n_p > 0 else float("nan")
        p_acc_ci = wilson_ci(p_tp + p_tn, n_p) if n_p > 0 else (float("nan"), float("nan"))

        participant_results.append({
            "participant": str(pid),
            "total": n_p,
            "falls": p_falls,
            "adls": p_adls,
            "tp": p_tp,
            "fp": p_fp,
            "tn": p_tn,
            "fn": p_fn,
            "sens": p_sens,
            "sens_ci": p_sens_ci,
            "spec": p_spec,
            "spec_ci": p_spec_ci,
            "acc": p_acc,
            "acc_ci": p_acc_ci,
            "faults": p_faults,
        })

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
        "activities": activity_results,
        "participants": participant_results,
    }


def format_report(results: dict, csv_name: str) -> str:
    """Format complete evaluation report with prominent HARDWARE markers."""
    lines = []
    w = 78

    lines.append("=" * w)
    lines.append("        [HARDWARE] FALLGUARD V2 - DISCRETE TRIALS ANALYSIS REPORT        ")
    lines.append("=" * w)
    lines.append(f" Dataset Source   : {csv_name}")
    lines.append(f" Total Trials (N) : {results['n_total']}")
    lines.append(f" Fall Trials      : {results['n_falls']}")
    lines.append(f" Control ADL Runs : {results['n_adls']}")
    lines.append("-" * w)

    # Small sample size warning
    if results["n_total"] < 30:
        lines.append("")
        lines.append(" [HARDWARE WARNING] SMALL SAMPLE SIZE DETECTED:")
        lines.append(f" Total trial count (N = {results['n_total']}) is less than 30.")
        lines.append(" Statistical variance is elevated; Wilson 95% intervals will exhibit wider")
        lines.append(" bounds. Report findings as pilot/exploratory hardware evaluation.")
        lines.append("-" * w)

    # Contingency table
    tp, fp = results["tp"], results["fp"]
    fn, tn = results["fn"], results["tn"]
    lines.append("")
    lines.append(" [HARDWARE] CONTINGENCY MATRIX (DISCRETE TRIALS)")
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
    lines.append(" [HARDWARE] PERFORMANCE METRICS (WITH WILSON 95% CONFIDENCE INTERVALS)")
    lines.append(" " + "-" * 62)
    lines.append(f" Sensitivity (Recall) : {sens * 100:>6.2f}%   [95% CI: {s_low * 100:5.2f}% - {s_high * 100:5.2f}%]")
    lines.append(f" Specificity          : {spec * 100:>6.2f}%   [95% CI: {sp_low * 100:5.2f}% - {sp_high * 100:5.2f}%]")
    if not math.isnan(prec):
        lines.append(f" Precision (PPV)      : {prec * 100:>6.2f}%   [95% CI: {p_low * 100:5.2f}% - {p_high * 100:5.2f}%]")
    else:
        lines.append(" Precision (PPV)      :    N/A    [No positive detections]")
    lines.append(f" F1-Score             : {f1:>8.4f}")
    lines.append(f" Overall Accuracy     : {acc * 100:>6.2f}%   [95% CI: {a_low * 100:5.2f}% - {a_high * 100:5.2f}%]")
    lines.append(" " + "-" * 62)

    # Latency section (onset_marker only)
    lat = results["latency_stats"]
    lines.append("")
    lines.append(" [HARDWARE] ONSET-TO-CONFIRMATION LATENCY (latency_ref = 'onset_marker')")
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
    lines.append(" [HARDWARE] SENSOR INTEGRITY & FAULT SURVEILLANCE")
    lines.append(" " + "-" * 62)
    lines.append(f" Total Sensor Faults   : {sf['total']}")
    lines.append(f" Faults in Fall Trials : {sf['in_falls']} / {results['n_falls']} ({sf['rate_falls'] * 100:.1f}%)")
    lines.append(f" Faults in ADL Trials  : {sf['in_adls']} / {results['n_adls']} ({sf['rate_adls'] * 100:.1f}%)")
    lines.append(" " + "-" * 62)

    # Per-activity table
    lines.append("")
    lines.append(" [HARDWARE] PER-ACTIVITY EVALUATION (WILSON 95% CI)")
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
    lines.append(" [HARDWARE] PER-PARTICIPANT BREAKDOWN (WILSON 95% CI)")
    lines.append(" " + "-" * 76)
    lines.append(f" {'Participant':<14} | {'N':<4} | {'Sens (Falls)':<16} | {'Spec (ADLs)':<16} | {'Accuracy':<16}")
    lines.append(" " + "-" * 76)
    for part in results["participants"]:
        # Sensitivity string
        if not math.isnan(part["sens"]):
            s_str = f"{part['sens'] * 100:5.1f}% [{part['sens_ci'][0] * 100:3.0f}-{part['sens_ci'][1] * 100:3.0f}%]"
        else:
            s_str = "N/A (0 falls)"

        # Specificity string
        if not math.isnan(part["spec"]):
            sp_str = f"{part['spec'] * 100:5.1f}% [{part['spec_ci'][0] * 100:3.0f}-{part['spec_ci'][1] * 100:3.0f}%]"
        else:
            sp_str = "N/A (0 ADLs)"

        # Accuracy string
        if not math.isnan(part["acc"]):
            a_str = f"{part['acc'] * 100:5.1f}% [{part['acc_ci'][0] * 100:3.0f}-{part['acc_ci'][1] * 100:3.0f}%]"
        else:
            a_str = "N/A"

        lines.append(f" {part['participant']:<14} | {part['total']:<4} | {s_str:<16} | {sp_str:<16} | {a_str:<16}")
    lines.append(" " + "-" * 76)

    lines.append("")
    lines.append("=" * w)
    lines.append("                    END OF HARDWARE REPORT                    ")
    lines.append("=" * w)
    return "\n".join(lines)


def main():
    args = parse_arguments()
    csv_file = args.csv_opt or args.csv_path

    if not csv_file:
        print("[HARDWARE] FallGuard v2 Discrete Trials Analysis Tool")
        print("Usage:")
        print("  python tools/analyze_trials.py <path_to_collector_trials.csv>")
        print("  python tools/analyze_trials.py --csv <path_to_collector_trials.csv> [--output report.txt]")
        sys.exit(0)

    df = validate_and_load_csv(csv_file)
    results = analyze_trials(df)
    report_text = format_report(results, Path(csv_file).name)

    print(report_text)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report_text, encoding="utf-8")
        print(f"\n[HARDWARE INFO] Report successfully saved to: {out_path.resolve()}")


if __name__ == "__main__":
    main()
