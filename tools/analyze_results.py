#!/usr/bin/env python3
"""
FallGuard v2 - Offline Results & Benchmark Analysis
====================================================
Ingests benchmark telemetry CSV, verifies ground-truth annotations,
matches fall detections against ground truth activity segments within
a temporal tolerance window, computes event-level classification metrics,
and renders a publication-ready confusion matrix figure.

Usage:
    python tools/analyze_results.py
    python tools/analyze_results.py --input results/fallguard_combined_benchmark.csv
"""

import os
import sys
import argparse
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="FallGuard v2 Offline Benchmark & Performance Analysis"
    )
    parser.add_argument(
        "--input", "-i",
        type=str,
        default="results/fallguard_combined_benchmark.csv",
        help="Path to benchmark CSV file (default: results/fallguard_combined_benchmark.csv)"
    )
    parser.add_argument(
        "--output-fig", "-o",
        type=str,
        default="results/figures/confusion_matrix.png",
        help="Path for confusion matrix PNG (default: results/figures/confusion_matrix.png)"
    )
    parser.add_argument(
        "--window-before",
        type=float,
        default=1.0,
        help="Pre-onset matching window in seconds (default: 1.0s)"
    )
    parser.add_argument(
        "--window-after",
        type=float,
        default=5.5,
        help="Post-onset matching window in seconds (default: 5.5s)"
    )
    return parser.parse_args()


def check_and_load_data(csv_path: Path) -> pd.DataFrame:
    """
    Safely load CSV (strictly read-only) and verify ground-truth labels.
    """
    if not csv_path.exists():
        print(f"\n[ERROR] Benchmark file not found: {csv_path.resolve()}")
        sys.exit(1)

    print(f"[INFO] Ingesting benchmark dataset (read-only): {csv_path.resolve()}")
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        print(f"[ERROR] Failed to read CSV file: {e}")
        sys.exit(1)

    # Check for required ground_truth column
    if 'ground_truth' not in df.columns:
        print("\n" + "=" * 70)
        print("[ERROR] Ground-truth annotations are missing from the benchmark CSV!")
        print("The dataset does not contain a 'ground_truth' column.")
        print("To run performance analysis, ground-truth annotations must be added:")
        print("  1. Add a 'ground_truth' column to the CSV (e.g. NORMAL, WALKING_ADL,")
        print("     JUMP_ADL, STUMBLE_ADL, FALL).")
        print("  2. Or record trials with activity annotations via tools/logger.py --annotate.")
        print("=" * 70 + "\n")
        sys.exit(1)

    # Check if annotations are empty or all null/unlabeled
    valid_labels = df['ground_truth'].dropna().astype(str).str.strip()
    valid_labels = valid_labels[~valid_labels.isin(['', 'UNKNOWN', 'nan', 'None'])]
    if len(valid_labels) == 0:
        print("\n" + "=" * 70)
        print("[ERROR] Ground-truth annotations are present but empty/unlabeled!")
        print("All values in 'ground_truth' are empty, NaN, or UNKNOWN.")
        print("Please annotate the activity segments before running evaluation.")
        print("=" * 70 + "\n")
        sys.exit(1)

    return df


def extract_segments(df: pd.DataFrame):
    """
    Extract contiguous activity segments from ground_truth column.
    """
    df = df.copy()
    df['block_id'] = (df['ground_truth'] != df['ground_truth'].shift(1)).cumsum()
    segments = []
    for bid, group in df.groupby('block_id'):
        gt = group['ground_truth'].iloc[0]
        t_start = group['timestamp_ms'].iloc[0]
        t_end = group['timestamp_ms'].iloc[-1]
        segments.append({
            'block_id': bid,
            'ground_truth': str(gt).strip(),
            'start_ms': t_start,
            'end_ms': t_end,
            'duration_ms': t_end - t_start,
            'row_count': len(group)
        })
    return segments


def extract_detection_events(df: pd.DataFrame):
    """
    Extract discrete state transition events from telemetry.
    """
    df = df.copy()
    df['prev_state'] = df['state'].shift(1, fill_value='')

    # Impact transitions
    impact_mask = (df['state'].str.startswith('Impact')) & (~df['prev_state'].str.startswith('Impact'))
    impacts = df[impact_mask][['timestamp_ms', 'mag', 'jerk']].to_dict('records')

    # Fall confirmed transitions
    conf_mask = (df['state'].str.startswith('FALL CONFIRMED')) & (~df['prev_state'].str.startswith('FALL CONFIRMED'))
    confirmed = df[conf_mask][['timestamp_ms', 'mag', 'jerk']].to_dict('records')

    # Alert sent transitions
    alert_mask = (df['state'].str.startswith('ALERT SENT')) & (~df['prev_state'].str.startswith('ALERT SENT'))
    alerts = df[alert_mask][['timestamp_ms', 'mag', 'jerk']].to_dict('records')

    return {
        'impacts': impacts,
        'confirmed': confirmed,
        'alerts': alerts
    }


def evaluate_benchmark(segments, detections, window_before_s=1.0, window_after_s=5.5):
    """
    Match fall detections against ground truth segments within temporal windows.
    """
    win_pre_ms = window_before_s * 1000.0
    win_post_ms = window_after_s * 1000.0

    confirmed_events = detections['confirmed']
    impact_events = detections['impacts']

    fall_details = []
    matched_detections = set()

    tp = 0
    fn = 0
    tn = 0
    fp = 0

    impact_latencies = []
    conf_latencies = []

    for seg in segments:
        gt = seg['ground_truth']
        t_start = seg['start_ms']
        t_end = seg['end_ms']

        if gt == 'FALL':
            # Matching window: [t_start - win_pre_ms, max(t_start + win_post_ms, t_end)]
            w_start = t_start - win_pre_ms
            w_end = max(t_start + win_post_ms, t_end)

            matched_conf = None
            for idx, ev in enumerate(confirmed_events):
                if w_start <= ev['timestamp_ms'] <= w_end:
                    matched_conf = ev
                    matched_detections.add(idx)
                    break

            # Find corresponding impact event
            matched_imp = None
            for ev in impact_events:
                if w_start <= ev['timestamp_ms'] <= w_end:
                    matched_imp = ev
                    break

            if matched_conf is not None or matched_imp is not None:
                tp += 1
                imp_ts = matched_imp['timestamp_ms'] if matched_imp else None
                conf_ts = matched_conf['timestamp_ms'] if matched_conf else None

                imp_lat = (imp_ts - t_start) if imp_ts is not None else None
                conf_lat = (conf_ts - t_start) if conf_ts is not None else None

                if imp_lat is not None:
                    impact_latencies.append(imp_lat)
                if conf_lat is not None:
                    conf_latencies.append(conf_lat)

                fall_details.append({
                    'segment_id': seg['block_id'],
                    'onset_ms': t_start,
                    'end_ms': t_end,
                    'impact_ms': imp_ts,
                    'conf_ms': conf_ts,
                    'impact_lat_ms': imp_lat,
                    'conf_lat_ms': conf_lat,
                    'status': 'TP (Detected)'
                })
            else:
                fn += 1
                fall_details.append({
                    'segment_id': seg['block_id'],
                    'onset_ms': t_start,
                    'end_ms': t_end,
                    'impact_ms': None,
                    'conf_ms': None,
                    'impact_lat_ms': None,
                    'conf_lat_ms': None,
                    'status': 'FN (Missed)'
                })
        else:
            # Non-fall segment (ADL / NORMAL)
            # Check if any confirmed fall event occurred during this segment
            seg_fp = False
            for idx, ev in enumerate(confirmed_events):
                if t_start <= ev['timestamp_ms'] <= t_end:
                    seg_fp = True
                    matched_detections.add(idx)
                    break
            if seg_fp:
                fp += 1
            else:
                tn += 1

    # Check for any rogue confirmed detections outside segments
    for idx, ev in enumerate(confirmed_events):
        if idx not in matched_detections:
            fp += 1

    return {
        'tp': tp,
        'fp': fp,
        'tn': tn,
        'fn': fn,
        'fall_details': fall_details,
        'impact_latencies': impact_latencies,
        'conf_latencies': conf_latencies
    }


def compute_metrics(eval_res, total_duration_ms):
    tp = eval_res['tp']
    fp = eval_res['fp']
    tn = eval_res['tn']
    fn = eval_res['fn']

    total = tp + fp + tn + fn
    sensitivity = (tp / (tp + fn)) if (tp + fn) > 0 else 0.0
    specificity = (tn / (tn + fp)) if (tn + fp) > 0 else 0.0
    precision = (tp / (tp + fp)) if (tp + fp) > 0 else 0.0
    f1 = (2 * precision * sensitivity / (precision + sensitivity)) if (precision + sensitivity) > 0 else 0.0
    accuracy = ((tp + tn) / total) if total > 0 else 0.0

    duration_hours = total_duration_ms / (1000.0 * 3600.0)
    far_per_hour = (fp / duration_hours) if duration_hours > 0 else 0.0

    imp_lats = eval_res['impact_latencies']
    conf_lats = eval_res['conf_latencies']

    imp_mean = float(np.mean(imp_lats)) if len(imp_lats) > 0 else 0.0
    imp_std = float(np.std(imp_lats)) if len(imp_lats) > 0 else 0.0

    conf_mean = float(np.mean(conf_lats)) if len(conf_lats) > 0 else 0.0
    conf_std = float(np.std(conf_lats)) if len(conf_lats) > 0 else 0.0

    return {
        'tp': tp,
        'fp': fp,
        'tn': tn,
        'fn': fn,
        'total': total,
        'sensitivity': sensitivity,
        'specificity': specificity,
        'precision': precision,
        'f1': f1,
        'accuracy': accuracy,
        'duration_hours': duration_hours,
        'duration_sec': total_duration_ms / 1000.0,
        'far_per_hour': far_per_hour,
        'imp_mean_ms': imp_mean,
        'imp_std_ms': imp_std,
        'conf_mean_ms': conf_mean,
        'conf_std_ms': conf_std,
    }


def render_terminal_summary(df, segments, metrics, fall_details):
    """
    Print an ASCII/Unicode terminal summary table.
    """
    total_time_s = metrics['duration_sec']
    mins = int(total_time_s // 60)
    secs = total_time_s % 60

    seg_counts = {}
    for s in segments:
        gt = s['ground_truth']
        seg_counts[gt] = seg_counts.get(gt, 0) + 1

    print("\n" + "=" * 78)
    print("                     FALLGUARD V2 BENCHMARK ANALYSIS")
    print("=" * 78)
    print(f" Dataset Samples:      {len(df):,d} samples @ ~50 Hz")
    print(f" Total Duration:       {total_time_s:.2f} s ({mins}m {secs:04.1f}s = {metrics['duration_hours']:.4f} hours)")
    print(f" Total Activity Blocks:{len(segments)} segments")
    print(" Activity Distribution:")
    for act, cnt in sorted(seg_counts.items()):
        print(f"   - {act:<16}: {cnt:>2} segments")
    print("-" * 78)

    print(" GROUND-TRUTH FALL EVENT AUDIT:")
    print(f" {'ID':<4} {'Onset (ms)':<12} {'Impact (ms)':<12} {'Conf (ms)':<12} {'Imp Latency':<14} {'Conf Latency':<14} {'Status'}")
    print(" " + "-" * 76)
    for idx, f in enumerate(fall_details, 1):
        onset = f"{f['onset_ms']:,}"
        imp = f"{f['impact_ms']:,}" if f['impact_ms'] else "N/A"
        conf = f"{f['conf_ms']:,}" if f['conf_ms'] else "N/A"
        imp_lat = f"{f['impact_lat_ms']} ms" if f['impact_lat_ms'] is not None else "N/A"
        conf_lat = f"{f['conf_lat_ms']} ms" if f['conf_lat_ms'] is not None else "N/A"
        status = f['status']
        print(f" #{idx:<3} {onset:<12} {imp:<12} {conf:<12} {imp_lat:<14} {conf_lat:<14} {status}")
    print("-" * 78)

    print(" EVENT-LEVEL CONFUSION MATRIX:")
    print(f"                     Predicted Fall     Predicted Non-Fall     Total")
    print(f"   Actual Fall       {metrics['tp']:>8d} (TP)       {metrics['fn']:>8d} (FN)     {metrics['tp'] + metrics['fn']:>8d}")
    print(f"   Actual Non-Fall   {metrics['fp']:>8d} (FP)       {metrics['tn']:>8d} (TN)     {metrics['fp'] + metrics['tn']:>8d}")
    print(f"   Total             {metrics['tp'] + metrics['fp']:>8d}            {metrics['fn'] + metrics['tn']:>8d}          {metrics['total']:>8d}")
    print("-" * 78)

    print(" CLASSIFICATION PERFORMANCE METRICS:")
    print(f"   Sensitivity (Recall): {metrics['sensitivity'] * 100.0:>6.2f}%   ({metrics['tp']}/{metrics['tp'] + metrics['fn']} falls detected)")
    print(f"   Specificity:          {metrics['specificity'] * 100.0:>6.2f}%   ({metrics['tn']}/{metrics['tn'] + metrics['fp']} non-falls rejected)")
    print(f"   Precision (PPV):      {metrics['precision'] * 100.0:>6.2f}%")
    print(f"   F1-Score:             {metrics['f1']:>8.4f}")
    print(f"   Accuracy:             {metrics['accuracy'] * 100.0:>6.2f}%   ({metrics['tp'] + metrics['tn']}/{metrics['total']} correct events)")
    print(f"   False Alarm Rate:     {metrics['far_per_hour']:>8.2f} false alarms / hour")
    print(f"   Impact Trigger Latency: {metrics['imp_mean_ms']:>6.1f} +/- {metrics['imp_std_ms']:>5.1f} ms (from ground-truth onset)")
    print(f"   Confirmation Latency:   {metrics['conf_mean_ms']:>6.1f} +/- {metrics['conf_std_ms']:>5.1f} ms (after stillness verification)")
    print("=" * 78 + "\n")


def generate_confusion_matrix_plot(metrics, output_path: Path):
    """
    Render and save a publication-quality confusion matrix heatmap.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cm = np.array([
        [metrics['tp'], metrics['fn']],
        [metrics['fp'], metrics['tn']]
    ])

    fig, ax = plt.subplots(figsize=(7, 6), dpi=300)

    # Color map
    im = ax.imshow(cm, interpolation='nearest', cmap=plt.cm.Blues)
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.ax.tick_params(labelsize=10)

    # Labels
    classes = ['Fall', 'Non-Fall']
    tick_marks = np.arange(len(classes))
    ax.set_xticks(tick_marks)
    ax.set_xticklabels(classes, fontsize=11, fontweight='bold')
    ax.set_yticks(tick_marks)
    ax.set_yticklabels(classes, fontsize=11, fontweight='bold')

    ax.set_xlabel('Predicted Event', fontsize=12, fontweight='bold', labelpad=10)
    ax.set_ylabel('Actual Event', fontsize=12, fontweight='bold', labelpad=10)

    # Title & metrics summary subtitle
    plt.title(
        'FallGuard v2 Benchmark Confusion Matrix\n'
        f"Sensitivity: {metrics['sensitivity']*100:.1f}% | Specificity: {metrics['specificity']*100:.1f}% | F1: {metrics['f1']:.3f}",
        fontsize=12, pad=15, fontweight='bold'
    )

    # Cell annotations
    cell_names = [
        [f"True Positive\n(TP = {metrics['tp']})", f"False Negative\n(FN = {metrics['fn']})"],
        [f"False Positive\n(FP = {metrics['fp']})", f"True Negative\n(TN = {metrics['tn']})"]
    ]

    thresh = cm.max() / 2.0
    for i in range(2):
        for j in range(2):
            color = "white" if cm[i, j] > thresh else "black"
            ax.text(
                j, i, cell_names[i][j],
                ha="center", va="center",
                color=color, fontsize=11, fontweight='bold'
            )

    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    plt.close()
    print(f"[INFO] Confusion matrix figure successfully generated:\n       {output_path.resolve()}")


def main():
    args = parse_arguments()
    csv_path = Path(args.input)
    fig_path = Path(args.output_fig)

    # 1. Load and validate benchmark data
    df = check_and_load_data(csv_path)

    # 2. Extract segments and events
    segments = extract_segments(df)
    detections = extract_detection_events(df)

    # 3. Evaluate benchmark matches
    eval_res = evaluate_benchmark(
        segments,
        detections,
        window_before_s=args.window_before,
        window_after_s=args.window_after
    )

    # 4. Compute classification metrics
    total_duration_ms = float(df['timestamp_ms'].max() - df['timestamp_ms'].min())
    metrics = compute_metrics(eval_res, total_duration_ms)

    # 5. Output formatted terminal summary
    render_terminal_summary(df, segments, metrics, eval_res['fall_details'])

    # 6. Generate confusion matrix figure
    generate_confusion_matrix_plot(metrics, fig_path)


if __name__ == '__main__':
    main()
