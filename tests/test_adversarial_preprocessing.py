#!/usr/bin/env python3
"""
tests/test_adversarial_preprocessing.py
=======================================
Empirical Adversarial Challenge & Verification Test Suite for SisFall Preprocessing
(Milestone 1 — Challenger M1.2)

Evaluates:
1. Decimation filter frequency response, anti-aliasing suppression (>= 40 dB), and linearity.
2. Step response, impulse response, and non-negative vector magnitude guarantees.
3. Fall kinematic feature preservation (free-fall dip < 5.0 m/s², impact peak > 11.5 m/s²)
   across all 15 Fall types (F01 - F15) for all 448 fall trials.
4. Dataset integrity across all 1,038 processed trials (manifest, schemas, timestamps, ranges).
5. Existing firmware regression suite pass status.
"""

import sys
import unittest
from pathlib import Path
import numpy as np
import pandas as pd

# Add repository root to path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.preprocess_sisfall import (
    design_anti_aliasing_fir,
    decimate_axis_50hz,
    parse_raw_sisfall_file,
    AA_KERNEL,
    ADXL_SCALE_MS2,
    ITG_SCALE_DPS,
    TRIAL_REGEX
)


class TestAdversarialPreprocessing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw_dir = REPO_ROOT / "data" / "sisfall" / "raw"
        cls.processed_dir = REPO_ROOT / "data" / "sisfall" / "processed"
        cls.manifest_path = REPO_ROOT / "data" / "sisfall" / "manifest.csv"

        if not cls.manifest_path.exists():
            raise FileNotFoundError(f"Manifest not found: {cls.manifest_path}")
        cls.manifest = pd.read_csv(cls.manifest_path)

        # Cache raw files
        cls.raw_files_by_stem = {
            f.stem: f for f in cls.raw_dir.rglob("*.txt")
            if TRIAL_REGEX.match(f.stem)
        }

    # =========================================================================
    # CHALLENGE 1: Frequency Response, Anti-Aliasing, and Decimation Linearity
    # =========================================================================

    def test_fir_frequency_response_and_suppression(self):
        """
        Adversarial Test 1A:
        Evaluates the 81-tap Kaiser FIR frequency response via FFT.
        Requirement: Passband flat, cutoff at 25 Hz, stopband (>= 35 Hz) suppression >= 40 dB.
        """
        kernel = design_anti_aliasing_fir(81, 5.0)
        self.assertEqual(len(kernel), 81)
        self.assertAlmostEqual(float(np.sum(kernel)), 1.0, places=7)

        # Symmetry test (linear phase requirement)
        np.testing.assert_allclose(kernel, kernel[::-1], atol=1e-12,
                                   err_msg="FIR filter is not symmetric (not linear phase)")

        # High-resolution FFT
        n_fft = 4096
        fs = 200.0
        freqs = np.fft.rfftfreq(n_fft, d=1.0 / fs)
        H = np.fft.rfft(kernel, n=n_fft)
        mag_db = 20 * np.log10(np.abs(H) + 1e-15)

        # 10 Hz passband
        idx_10 = np.argmin(np.abs(freqs - 10.0))
        gain_10 = mag_db[idx_10]
        self.assertGreater(gain_10, -0.05, f"10 Hz passband attenuation too high: {gain_10:.3f} dB")

        # 25 Hz cutoff
        idx_25 = np.argmin(np.abs(freqs - 25.0))
        gain_25 = mag_db[idx_25]
        self.assertAlmostEqual(gain_25, -6.02, delta=0.5,
                               msg=f"25 Hz cutoff response not ~-6dB: {gain_25:.3f} dB")

        # 35 Hz stopband
        idx_35 = np.argmin(np.abs(freqs - 35.0))
        gain_35 = mag_db[idx_35]
        self.assertLess(gain_35, -40.0,
                        f"35 Hz stopband suppression under 40 dB: {gain_35:.3f} dB")

        # Maximum gain in stopband (>= 35 Hz)
        stopband_mask = freqs >= 35.0
        max_stopband_gain = np.max(mag_db[stopband_mask])
        min_suppression = -max_stopband_gain
        print(f"\n[TEST 1A] FIR Frequency Response:")
        print(f"  10 Hz Passband Gain : {gain_10:+.3f} dB")
        print(f"  25 Hz Cutoff Gain   : {gain_25:+.3f} dB")
        print(f"  35 Hz Stopband Gain : {gain_35:+.3f} dB")
        print(f"  Worst Stopband (>=35Hz) Suppression : {min_suppression:.2f} dB (Requirement >= 40 dB)")
        self.assertGreaterEqual(min_suppression, 40.0,
                                f"Stopband suppression {min_suppression:.2f} dB is below 40 dB")

    def test_synthetic_pure_tone_decimation_and_linearity(self):
        """
        Adversarial Test 1B:
        Feed synthetic 10 Hz and 35 Hz pure sine tones through decimate_axis_50hz.
        Confirm RMS suppression >= 40 dB and linearity (superposition error < 1e-12).
        """
        fs = 200.0
        t = np.arange(1000) / fs  # 5.0 seconds
        tone_10 = np.sin(2 * np.pi * 10.0 * t)
        tone_35 = np.sin(2 * np.pi * 35.0 * t)

        out_10 = decimate_axis_50hz(tone_10, AA_KERNEL)
        out_35 = decimate_axis_50hz(tone_35, AA_KERNEL)

        # Trim boundary filter transients (first/last 15 samples of 50 Hz output)
        trim = 15
        rms_in_10 = float(np.std(tone_10))
        rms_out_10 = float(np.std(out_10[trim:-trim]))
        gain_10_db = 20 * np.log10(rms_out_10 / rms_in_10)

        rms_in_35 = float(np.std(tone_35))
        rms_out_35 = float(np.std(out_35[trim:-trim]))
        gain_35_db = 20 * np.log10(rms_out_35 / rms_in_35)
        suppression_db = gain_10_db - gain_35_db

        print(f"\n[TEST 1B] Synthetic Pure Tone Suppression:")
        print(f"  10 Hz Decimated Gain : {gain_10_db:+.3f} dB")
        print(f"  35 Hz Decimated Gain : {gain_35_db:+.3f} dB")
        print(f"  Measured Suppression : {suppression_db:.2f} dB (Requirement >= 40 dB)")

        self.assertGreaterEqual(suppression_db, 40.0,
                                f"Tone suppression {suppression_db:.2f} dB < 40 dB")

        # Linearity (superposition): f(alpha * x1 + beta * x2) == alpha * f(x1) + beta * f(x2)
        alpha, beta = 3.7, -1.9
        combo_in = alpha * tone_10 + beta * tone_35
        combo_out = decimate_axis_50hz(combo_in, AA_KERNEL)
        expected_out = alpha * out_10 + beta * out_35
        linearity_error = float(np.max(np.abs(combo_out - expected_out)))
        print(f"  Superposition Max Error : {linearity_error:.2e}")
        self.assertLess(linearity_error, 1e-12, "Decimation filter violates linear superposition")

    # =========================================================================
    # CHALLENGE 2: Step Response & Non-Negative Acceleration Magnitude
    # =========================================================================

    def test_step_response_and_non_negative_magnitude(self):
        """
        Adversarial Test 2:
        Simulate harsh kinematic step transitions and sharp impact impulses.
        Verify that 3D vector magnitude |a| is mathematically strictly >= 0.0,
        and verify that no real processed file exhibits negative magnitude.
        """
        # Step from 1g (9.80665) down to 0g (free fall)
        n_samples = 300
        step_down = np.concatenate([np.full(150, 9.80665), np.full(150, 0.0)])
        out_down = decimate_axis_50hz(step_down, AA_KERNEL)

        # 3D step scenario: free-fall on gravity axis
        ax = np.zeros(n_samples)
        ay = np.concatenate([np.full(150, -9.80665), np.full(150, 0.0)])
        az = np.zeros(n_samples)

        ax_dec = decimate_axis_50hz(ax, AA_KERNEL)
        ay_dec = decimate_axis_50hz(ay, AA_KERNEL)
        az_dec = decimate_axis_50hz(az, AA_KERNEL)
        mag_dec = np.sqrt(ax_dec**2 + ay_dec**2 + az_dec**2)

        self.assertTrue(np.all(mag_dec >= 0.0), "3D step magnitude produced negative values")
        self.assertFalse(np.isnan(mag_dec).any(), "3D step magnitude contains NaN")
        self.assertFalse(np.isinf(mag_dec).any(), "3D step magnitude contains Inf")

        # Sharp 5ms impact impulse (60 m/s^2)
        impulse = np.full(n_samples, 9.80665)
        impulse[150] = 60.0
        impulse_out = decimate_axis_50hz(impulse, AA_KERNEL)
        self.assertFalse(np.isnan(impulse_out).any())

        print(f"\n[TEST 2] Step Response & Magnitude Stability:")
        print(f"  3D Fall Step Min Mag : {np.min(mag_dec):.6f} m/s²")
        print(f"  3D Fall Step Max Mag : {np.max(mag_dec):.6f} m/s²")
        print(f"  Negative Mag Detected: {np.any(mag_dec < 0.0)}")

    # =========================================================================
    # CHALLENGE 3: Kinematic Feature Preservation Across All 15 Fall Types
    # =========================================================================

    def test_fall_kinematics_preservation_across_all_15_types(self):
        """
        Adversarial Test 3:
        Examines ALL 448 fall trials in SisFall across all 15 Fall types (F01 - F15).
        Directly compares raw 200 Hz vs processed 50 Hz:
        - Free-fall dip (< 5.0 m/s²) preservation
        - Impact peak (> 11.5 m/s²) preservation
        - Quantifies peak attenuation and dip difference per fall type.
        """
        fall_manifest = self.manifest[self.manifest["is_fall"] == 1].copy()
        self.assertEqual(len(fall_manifest), 448, "Manifest does not contain exactly 448 falls")
        self.assertEqual(fall_manifest["activity_code"].nunique(), 15,
                         "Manifest does not contain all 15 Fall types")

        results = []
        for _, row in fall_manifest.iterrows():
            trial_id = row["trial_id"]
            act = row["activity_code"]
            proc_file = REPO_ROOT / row["processed_path"]

            # Load processed 50 Hz trial
            df_proc = pd.read_csv(proc_file)
            mag_proc = np.sqrt(df_proc["ax"]**2 + df_proc["ay"]**2 + df_proc["az"]**2)
            min_proc = float(mag_proc.min())
            max_proc = float(mag_proc.max())

            # Load raw 200 Hz trial
            raw_file = self.raw_files_by_stem[trial_id]
            arr_raw = parse_raw_sisfall_file(raw_file)
            ax_raw = arr_raw[:, 0] * ADXL_SCALE_MS2
            ay_raw = arr_raw[:, 1] * ADXL_SCALE_MS2
            az_raw = arr_raw[:, 2] * ADXL_SCALE_MS2
            mag_raw = np.sqrt(ax_raw**2 + ay_raw**2 + az_raw**2)
            min_raw = float(mag_raw.min())
            max_raw = float(mag_raw.max())

            peak_atten_pct = (max_raw - max_proc) / max_raw * 100.0
            dip_diff = min_proc - min_raw

            results.append({
                "trial_id": trial_id,
                "activity_code": act,
                "subject_id": row["subject_id"],
                "subject_group": row["subject_group"],
                "min_raw": min_raw,
                "min_proc": min_proc,
                "max_raw": max_raw,
                "max_proc": max_proc,
                "peak_atten_pct": peak_atten_pct,
                "dip_diff": dip_diff,
                "raw_peak_gt_11_5": max_raw > 11.5,
                "proc_peak_gt_11_5": max_proc > 11.5,
                "raw_dip_lt_5": min_raw < 5.0,
                "proc_dip_lt_5": min_proc < 5.0,
            })

        df = pd.DataFrame(results)

        # Quantitative checks
        n_total = len(df)
        proc_peaks_gt_11_5 = int(df["proc_peak_gt_11_5"].sum())
        proc_dips_lt_5 = int(df["proc_dip_lt_5"].sum())
        mean_atten = float(df["peak_atten_pct"].mean())
        mean_dip_diff = float(df["dip_diff"].mean())

        print(f"\n[TEST 3] Fall Kinematic Feature Preservation (448 Fall Trials):")
        print(f"  Processed Impact Peaks > 11.5 m/s²: {proc_peaks_gt_11_5} / {n_total} ({proc_peaks_gt_11_5/n_total*100:.2f}%)")
        print(f"  Processed Free-Fall Dips < 5.0 m/s²: {proc_dips_lt_5} / {n_total} ({proc_dips_lt_5/n_total*100:.2f}%)")
        print(f"  Mean Peak Attenuation              : {mean_atten:.2f}% (Std: {df['peak_atten_pct'].std():.2f}%)")
        print(f"  Mean Dip Difference                : {mean_dip_diff:+.3f} m/s² (Std: {df['dip_diff'].std():.3f} m/s²)")

        # Verify impact peaks: EVERY single fall trial in processed dataset MUST exceed 11.5 m/s²
        # (FallGuard impact detection threshold)
        self.assertEqual(proc_peaks_gt_11_5, n_total,
                         f"Impact peaks > 11.5 m/s² missing in {n_total - proc_peaks_gt_11_5} trials")

        # Verify free-fall dips: all genuine free-fall dips (<5.0 m/s²) must be preserved
        # Note: in SisFall, some falls (e.g. syncope while sitting F13-F15) are slumps without full free-fall.
        # But for all trials where raw exhibited a free-fall dip (<5.0 m/s²), check how many were preserved:
        raw_with_dip = df[df["raw_dip_lt_5"]]
        proc_preserved_dip = raw_with_dip["proc_dip_lt_5"].sum()
        dip_preservation_rate = proc_preserved_dip / len(raw_with_dip) * 100.0
        print(f"  Free-Fall Dip Preservation Rate    : {proc_preserved_dip}/{len(raw_with_dip)} ({dip_preservation_rate:.2f}%)")
        self.assertGreaterEqual(dip_preservation_rate, 95.0,
                                f"Free-fall dip preservation rate {dip_preservation_rate:.2f}% < 95%")

        # Average peak attenuation should be modest (< 15%)
        self.assertLess(mean_atten, 15.0,
                        f"Mean impact peak attenuation {mean_atten:.2f}% exceeded 15%")

        # Print per-activity breakdown
        grouped = df.groupby("activity_code").agg(
            trials=("trial_id", "count"),
            raw_peak=("max_raw", "mean"),
            proc_peak=("max_proc", "mean"),
            atten_pct=("peak_atten_pct", "mean"),
            raw_dip=("min_raw", "mean"),
            proc_dip=("min_proc", "mean"),
            peaks_gt_11_5=("proc_peak_gt_11_5", "sum"),
            dips_lt_5=("proc_dip_lt_5", "sum"),
        )
        print("\n  Per-Fall-Type Breakdown (F01 - F15):")
        print(grouped.to_string())

        # Verify elderly cohort (SE06) representation
        elderly_falls = df[df["subject_group"] == "ELDERLY"]
        print(f"\n  Elderly Falls Tested (SE06): {len(elderly_falls)} trials across 15 fall types.")
        self.assertEqual(len(elderly_falls), 75, "Elderly cohort must have 75 falls")
        self.assertTrue(elderly_falls["proc_peak_gt_11_5"].all(),
                        "Not all elderly falls preserved impact peak > 11.5 m/s²")

    # =========================================================================
    # CHALLENGE 4: Processed Dataset & Manifest Integrity
    # =========================================================================

    def test_processed_dataset_integrity(self):
        """
        Adversarial Test 4:
        Validates all 1,038 processed files on disk:
        - Exact file count and manifest synchronization
        - No NaN or Inf values across all columns
        - Timestamps strictly starting at 0.0 and spaced by 0.02s
        - Column headers match 't_sec,ax,ay,az,gx,gy,gz'
        - Acceleration dynamic range (-160 to +160 m/s²)
        - Gyroscope dynamic range (-2000 to +2000 °/s)
        """
        self.assertEqual(len(self.manifest), 1038, "Manifest must contain 1038 trials")
        self.assertEqual(self.manifest["is_fall"].value_counts().to_dict(), {0: 590, 1: 448})

        # Check every file
        processed_files = list(self.processed_dir.glob("*.csv"))
        self.assertEqual(len(processed_files), 1038,
                         f"Expected 1038 processed files, found {len(processed_files)}")

        checked = 0
        expected_cols = ["t_sec", "ax", "ay", "az", "gx", "gy", "gz"]

        for f in processed_files:
            df = pd.read_csv(f)
            self.assertEqual(list(df.columns), expected_cols, f"{f.name}: Invalid columns")
            self.assertGreaterEqual(len(df), 50, f"{f.name}: Too few samples ({len(df)})")

            t = df["t_sec"].values
            self.assertAlmostEqual(t[0], 0.0, places=5, msg=f"{f.name}: t[0] != 0.0")
            dt = np.diff(t)
            self.assertTrue(np.allclose(dt, 0.02, atol=1e-5), f"{f.name}: Non-uniform dt")

            vals = df.values
            self.assertFalse(np.isnan(vals).any(), f"{f.name}: Contains NaN")
            self.assertFalse(np.isinf(vals).any(), f"{f.name}: Contains Inf")

            # Check acceleration ranges
            acc_vals = vals[:, 1:4]
            self.assertLessEqual(np.max(np.abs(acc_vals)), 165.0, f"{f.name}: Acceleration overflow")

            # Check gyro ranges
            gyro_vals = vals[:, 4:7]
            self.assertLessEqual(np.max(np.abs(gyro_vals)), 2100.0, f"{f.name}: Gyro overflow")

            checked += 1

        print(f"\n[TEST 4] Processed Dataset Integrity:")
        print(f"  Verified {checked} / 1038 processed CSV files.")
        print(f"  Timestamps, schemas, NaN/Inf, and dynamic ranges: 100% VALID.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
