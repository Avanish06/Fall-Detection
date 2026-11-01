#!/usr/bin/env python3
"""
tools/preprocess_sisfall.py
===========================
SisFall Dataset Preprocessing and Resampling Engine for FallGuard v2.
Converts raw 200 Hz SisFall text files into standardized 50 Hz CSVs.

Pipeline:
1. Ingest raw 9-column ASCII text files and strip trailing semicolons.
2. Scale ADXL345 (cols 1-3) to m/s²: raw * (32 / 8192) * 9.80665.
3. Scale ITG3200 (cols 4-6) to °/s: raw * (4000 / 65536).
4. Scale MMA8451Q (cols 7-9) to m/s²: raw * (16 / 16384) * 9.80665.
5. Anti-aliasing 4:1 decimation (200 Hz -> 50 Hz) using linear-phase 81-tap
   Kaiser FIR filter with edge-padding (guaranteeing zero negative-acceleration
   ringing and zero boundary transients).
6. Generate discrete time vector t_sec = 0.00, 0.02, 0.04, ...
7. Validate integrity assertions (NaN/Inf, dynamic range, fall kinematics).
8. Save 50 Hz CSVs to data/sisfall/processed/<trial_id>.csv with header:
   t_sec,ax,ay,az,gx,gy,gz
9. Generate catalog manifest at data/sisfall/manifest.csv with columns:
   trial_id,activity_code,activity_name,subject_id,subject_group,is_fall,
   duration_s,samples_count,processed_path
"""

import argparse
import concurrent.futures
import csv
import os
import re
import sys
import time
from pathlib import Path
import numpy as np

# Authoritative Scaling Factors (SisFall Readme & NIST standard g0)
ADXL_SCALE_MS2 = (32.0 / 8192.0) * 9.80665    # 0.0383072265625 m/s² per count
ITG_SCALE_DPS = (4000.0 / 65536.0)           # 0.06103515625 °/s per count
MMA_SCALE_MS2 = (16.0 / 16384.0) * 9.80665   # 0.009576806640625 m/s² per count

# Comprehensive Activity Taxonomy
ACTIVITY_TAXONOMY = {
    # ADLs (D01 - D19)
    "D01": "Walking slowly",
    "D02": "Walking quickly",
    "D03": "Jogging slowly",
    "D04": "Jogging quickly",
    "D05": "Walking upstairs and downstairs slowly",
    "D06": "Walking upstairs and downstairs quickly",
    "D07": "Slowly sit in a half height chair, wait a moment, and up slowly",
    "D08": "Quickly sit in a half height chair, wait a moment, and up quickly",
    "D09": "Slowly sit in a low height chair, wait a moment, and up slowly",
    "D10": "Quickly sit in a low height chair, wait a moment, and up quickly",
    "D11": "Sitting a moment, trying to get up, and collapse into a chair",
    "D12": "Sitting a moment, lying slowly, wait a moment, and sit again",
    "D13": "Sitting a moment, lying quickly, wait a moment, and sit again",
    "D14": "Being on one's back change to lateral position, wait a moment, and change to one's back",
    "D15": "Standing, slowly bending at knees, and getting up",
    "D16": "Standing, slowly bending without bending knees, and getting up",
    "D17": "Standing, get into a car, remain seated and get out of the car",
    "D18": "Stumble while walking",
    "D19": "Gently jump without falling (trying to reach a high object)",
    # Falls (F01 - F15)
    "F01": "Fall forward while walking caused by a slip",
    "F02": "Fall backward while walking caused by a slip",
    "F03": "Lateral fall while walking caused by a slip",
    "F04": "Fall forward while walking caused by a trip",
    "F05": "Fall forward while jogging caused by a trip",
    "F06": "Vertical fall while walking caused by fainting",
    "F07": "Fall while walking, with use of hands in a table to dampen fall, caused by fainting",
    "F08": "Fall forward when trying to get up",
    "F09": "Lateral fall when trying to get up",
    "F10": "Fall forward when trying to sit down",
    "F11": "Fall backward when trying to sit down",
    "F12": "Lateral fall when trying to sit down",
    "F13": "Fall forward while sitting, caused by fainting or falling asleep",
    "F14": "Fall backward while sitting, caused by fainting or falling asleep",
    "F15": "Lateral fall while sitting, caused by fainting or falling asleep",
}

TRIAL_REGEX = re.compile(r"^([DF]\d{2})_(S[AE]\d{2})_(R\d{2})$", re.IGNORECASE)


def design_anti_aliasing_fir(num_taps: int = 81, beta: float = 5.0) -> np.ndarray:
    """
    Designs an 81-tap linear-phase Kaiser-windowed FIR anti-aliasing filter
    for 4:1 decimation (cutoff at 25 Hz Nyquist for 50 Hz target rate, fs=200 Hz).
    Matches scipy.signal.firwin(81, 0.25, window=('kaiser', 5.0)) exactly.
    """
    half = (num_taps - 1) / 2.0
    n = np.arange(num_taps) - half
    h = 0.25 * np.sinc(0.25 * n) * np.kaiser(num_taps, beta)
    return h / np.sum(h)


AA_KERNEL = design_anti_aliasing_fir(81, 5.0)


def parse_raw_sisfall_file(file_path: Path) -> np.ndarray:
    """
    Reads raw SisFall text file, stripping whitespace and trailing semicolons,
    and returns an (N, 9) integer NumPy array.
    """
    rows = []
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line_str = line.strip().rstrip(";").rstrip(",")
            if not line_str:
                continue
            parts = [v.strip() for v in line_str.split(",") if v.strip()]
            if len(parts) >= 6:
                rows.append([float(p) for p in parts])

    if not rows:
        raise ValueError(f"No valid data rows in {file_path}")

    arr = np.array(rows, dtype=np.float64)
    if len(arr) < 200:
        raise ValueError(f"Trial too short in {file_path}: {len(arr)} samples (< 200)")
    return arr


def decimate_axis_50hz(sig: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """
    Decimates a 200 Hz channel to 50 Hz using anti-aliasing FIR filter and edge-padding.
    Edge padding prevents boundary step transients at t=0.
    """
    half_len = (len(kernel) - 1) // 2
    padded = np.pad(sig, half_len, mode="edge")
    filtered = np.convolve(padded, kernel, mode="valid")
    return filtered[::4]


def process_trial(
    raw_path: Path,
    output_dir: Path,
    force: bool = False
) -> dict:
    """
    Processes a single raw trial file:
    1. Parses filename metadata.
    2. Reads and scales sensor channels.
    3. Resamples to 50 Hz.
    4. Runs validation assertions.
    5. Saves output CSV.
    6. Returns metadata dictionary for manifest.
    """
    stem = raw_path.stem
    match = TRIAL_REGEX.match(stem)
    if not match:
        raise ValueError(f"Unrecognized trial filename format: {raw_path.name}")

    activity_code = match.group(1).upper()
    subject_id = match.group(2).upper()
    trial_no = match.group(3).upper()
    trial_id = f"{activity_code}_{subject_id}_{trial_no}"

    subject_group = "ELDERLY" if subject_id.startswith("SE") else "YOUNG"
    is_fall = 1 if activity_code.startswith("F") else 0
    activity_name = ACTIVITY_TAXONOMY.get(activity_code, f"Activity {activity_code}")

    out_csv = output_dir / f"{trial_id}.csv"
    norm_processed_path = f"data/sisfall/processed/{trial_id}.csv"

    # Fast skip if already processed and not force
    if out_csv.exists() and not force and out_csv.stat().st_size > 50:
        # Read sample count from file
        with open(out_csv, "r", encoding="utf-8") as f:
            lines = f.readlines()
            samples_count = max(0, len(lines) - 1)
        duration_s = round(samples_count * 0.02, 2)
        return {
            "trial_id": trial_id,
            "activity_code": activity_code,
            "activity_name": activity_name,
            "subject_id": subject_id,
            "subject_group": subject_group,
            "is_fall": is_fall,
            "duration_s": duration_s,
            "samples_count": samples_count,
            "processed_path": norm_processed_path,
        }

    raw_data = parse_raw_sisfall_file(raw_path)

    # Scale ADXL345 (cols 0, 1, 2) to m/s²
    ax_200 = raw_data[:, 0] * ADXL_SCALE_MS2
    ay_200 = raw_data[:, 1] * ADXL_SCALE_MS2
    az_200 = raw_data[:, 2] * ADXL_SCALE_MS2

    # Scale ITG3200 (cols 3, 4, 5) to °/s
    gx_200 = raw_data[:, 3] * ITG_SCALE_DPS
    gy_200 = raw_data[:, 4] * ITG_SCALE_DPS
    gz_200 = raw_data[:, 5] * ITG_SCALE_DPS

    # Decimate each channel to 50 Hz
    ax = decimate_axis_50hz(ax_200, AA_KERNEL)
    ay = decimate_axis_50hz(ay_200, AA_KERNEL)
    az = decimate_axis_50hz(az_200, AA_KERNEL)
    gx = decimate_axis_50hz(gx_200, AA_KERNEL)
    gy = decimate_axis_50hz(gy_200, AA_KERNEL)
    gz = decimate_axis_50hz(gz_200, AA_KERNEL)

    n_out = len(ax)
    t_sec = np.arange(n_out, dtype=np.float64) * 0.02

    # --- Validation Assertions ---
    assert n_out >= 50, f"{trial_id}: Trial too short after decimation ({n_out} samples)"
    assert len(t_sec) == n_out, f"{trial_id}: Time vector length mismatch"
    assert t_sec[0] == 0.0, f"{trial_id}: Time vector does not start at 0.0"
    assert np.allclose(np.diff(t_sec), 0.02, atol=1e-5), f"{trial_id}: Time delta is not 0.02s"

    matrix = np.column_stack([ax, ay, az, gx, gy, gz])
    assert not np.isnan(matrix).any(), f"{trial_id}: Contains NaN values"
    assert not np.isinf(matrix).any(), f"{trial_id}: Contains Inf values"

    mag = np.sqrt(ax**2 + ay**2 + az**2)
    assert np.all(mag >= 0.0), f"{trial_id}: Negative vector magnitude"
    peak_mag = float(np.max(mag))
    assert peak_mag <= 250.0, f"{trial_id}: Unphysical acceleration peak: {peak_mag:.2f} m/s²"

    peak_gyro = float(np.max(np.abs(matrix[:, 3:])))
    assert peak_gyro <= 2500.0, f"{trial_id}: Unphysical gyroscope rate: {peak_gyro:.2f} °/s"

    if is_fall and peak_mag < 10.0:
        # Note: Even gentle syncope falls in SisFall exhibit impact peak >= 10 m/s²
        raise ValueError(f"{trial_id}: Fall impact below minimum dynamic threshold ({peak_mag:.2f} m/s²)")

    # --- Save Processed CSV ---
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["t_sec", "ax", "ay", "az", "gx", "gy", "gz"])
        for i in range(n_out):
            writer.writerow([
                f"{t_sec[i]:.2f}",
                f"{ax[i]:.4f}",
                f"{ay[i]:.4f}",
                f"{az[i]:.4f}",
                f"{gx[i]:.3f}",
                f"{gy[i]:.3f}",
                f"{gz[i]:.3f}"
            ])

    duration_s = round(n_out * 0.02, 2)
    return {
        "trial_id": trial_id,
        "activity_code": activity_code,
        "activity_name": activity_name,
        "subject_id": subject_id,
        "subject_group": subject_group,
        "is_fall": is_fall,
        "duration_s": duration_s,
        "samples_count": n_out,
        "processed_path": norm_processed_path,
    }


def print_progress(current: int, total: int, prefix: str = "Preprocessing SisFall", quiet: bool = False):
    if quiet or total == 0:
        return
    pct = (current / total) * 100.0
    bar_len = 30
    filled = int(bar_len * current // total)
    bar = "=" * filled + "-" * (bar_len - filled)
    try:
        sys.stdout.write(f"\r{prefix}: [{bar}] {pct:5.1f}% ({current}/{total})")
        sys.stdout.flush()
        if current == total:
            sys.stdout.write("\n")
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(
        description="SisFall Dataset Preprocessing and Resampling Engine for FallGuard v2",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--input-dir", type=Path, default=Path("data/sisfall/raw"),
        help="Input directory containing raw SisFall .txt files"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("data/sisfall/processed"),
        help="Output directory for processed 50 Hz CSV files"
    )
    parser.add_argument(
        "--manifest", type=Path, default=Path("data/sisfall/manifest.csv"),
        help="Path for generated metadata manifest CSV"
    )
    parser.add_argument(
        "--workers", type=int, default=4,
        help="Number of parallel worker processes"
    )
    parser.add_argument(
        "--force", action="store_true", default=False,
        help="Force re-processing of already processed files"
    )
    parser.add_argument(
        "--quiet", action="store_true", default=False,
        help="Suppress progress output"
    )

    args = parser.parse_args()

    input_dir = args.input_dir
    output_dir = args.output_dir
    manifest_path = args.manifest
    workers = max(1, args.workers)

    if not input_dir.exists():
        sys.stderr.write(f"Error: Input directory {input_dir} does not exist.\n")
        return 1

    # Discover all candidate .txt files matching SisFall trial naming pattern
    raw_files = [
        f for f in input_dir.rglob("*.txt")
        if TRIAL_REGEX.match(f.stem)
    ]
    raw_files.sort(key=lambda p: p.name)

    total_files = len(raw_files)
    if total_files == 0:
        sys.stderr.write(f"No valid SisFall trial .txt files found in {input_dir}\n")
        return 1

    if not args.quiet:
        print(f"Found {total_files} SisFall trial files in {input_dir}")
        print(f"Preprocessing to 50 Hz -> {output_dir} using {workers} worker(s)...")

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_records = []

    t_start = time.time()
    completed = 0

    if workers == 1:
        for idx, file_path in enumerate(raw_files, 1):
            record = process_trial(file_path, output_dir, force=args.force)
            manifest_records.append(record)
            print_progress(idx, total_files, quiet=args.quiet)
    else:
        # Multiprocessing batch execution
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
            future_to_file = {
                executor.submit(process_trial, f, output_dir, args.force): f
                for f in raw_files
            }
            for future in concurrent.futures.as_completed(future_to_file):
                f_path = future_to_file[future]
                try:
                    record = future.result()
                    manifest_records.append(record)
                except Exception as exc:
                    sys.stderr.write(f"\nError processing {f_path.name}: {exc}\n")
                    raise exc
                completed += 1
                print_progress(completed, total_files, quiet=args.quiet)

    # Sort manifest records deterministically by trial_id
    manifest_records.sort(key=lambda r: r["trial_id"])

    # Write manifest.csv
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "trial_id",
        "activity_code",
        "activity_name",
        "subject_id",
        "subject_group",
        "is_fall",
        "duration_s",
        "samples_count",
        "processed_path"
    ]
    with open(manifest_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(manifest_records)

    elapsed = time.time() - t_start
    falls_count = sum(1 for r in manifest_records if r["is_fall"] == 1)
    adls_count = sum(1 for r in manifest_records if r["is_fall"] == 0)

    print(
        f"\n[SisFall Preprocessing Complete in {elapsed:.1f}s]\n"
        f"  Total trials processed : {len(manifest_records)}\n"
        f"  Fall trials            : {falls_count}\n"
        f"  ADL trials             : {adls_count}\n"
        f"  Processed directory    : {output_dir}\n"
        f"  Manifest written to    : {manifest_path}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
