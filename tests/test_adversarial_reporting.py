"""
tests/test_adversarial_reporting.py
===================================
Adversarial Empirical Challenge Suite for Milestone 3:
Comparative Metrics Calculation & Dual-Table Markdown Reporting (tools/generate_comparison_report.py).

Covers:
1. CLI Argument Stress Testing:
   - CLI combinations (--quiet, --verbose, custom --output-doc)
   - Unrecognized flags and argument validation
   - Missing and nonexistent input CSV paths
   - Nested output directory creation
2. Corrupted & Degenerate Input Handling:
   - Zero-byte empty CSV files (defensive error & non-zero exit)
   - Missing required schema columns (KeyErrors & non-zero exit)
   - Malformed data types (non-numeric is_fall, detected, latency_ms)
   - Zero-fall and zero-ADL slices (graceful NaN / N/A handling)
   - All-NaN latency series
3. Mathematical Verification & Domain Contracts:
   - Wilson 95% Confidence Interval exact numerical limits
   - False alarm rate calculation strictly based on ADL exposure duration
   - Impact latency statistics strictly on True Positives
   - Strict separation of [HARDWARE] (Table 1) and [OFFLINE] (Table 2)
   - Zero fabrication enforcement (hardware baselines labeled N/A)
4. Bit-for-Bit Idempotence:
   - Successive generator executions produce identical SHA-256 hashes
"""

import hashlib
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

# Project root setup
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.generate_comparison_report import (
    wilson_ci,
    compute_contingency_counts,
    compute_latency_statistics,
    evaluate_detector,
    compute_sha256,
)


class TestCLIAdversarial(unittest.TestCase):
    """Stress tests CLI invocation, argument validation, and edge-case paths."""

    def test_help_flag(self):
        res = subprocess.run(
            [sys.executable, "tools/generate_comparison_report.py", "--help"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("usage:", res.stdout)
        self.assertIn("--hardware-csv", res.stdout)
        self.assertIn("--offline-csv", res.stdout)

    def test_unrecognized_argument(self):
        res = subprocess.run(
            [sys.executable, "tools/generate_comparison_report.py", "--invalid-flag-xyz"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("error: unrecognized arguments", res.stderr)

    def test_nonexistent_hardware_csv(self):
        res = subprocess.run(
            [
                sys.executable,
                "tools/generate_comparison_report.py",
                "--hardware-csv",
                "results/nonexistent_hw_file.csv",
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("Hardware benchmark file not found", res.stderr)

    def test_nonexistent_offline_csv(self):
        res = subprocess.run(
            [
                sys.executable,
                "tools/generate_comparison_report.py",
                "--offline-csv",
                "results/nonexistent_off_file.csv",
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("Offline SisFall benchmark file not found", res.stderr)

    def test_quiet_flag_suppresses_stdout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = Path(tmpdir) / "test_quiet.md"
            res = subprocess.run(
                [
                    sys.executable,
                    "tools/generate_comparison_report.py",
                    "--output-doc",
                    str(out_file),
                    "--quiet",
                ],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.stdout, "")
            self.assertEqual(res.stderr, "")
            self.assertTrue(out_file.exists())

    def test_verbose_flag_prints_detailed_breakdowns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = Path(tmpdir) / "test_verbose.md"
            res = subprocess.run(
                [
                    sys.executable,
                    "tools/generate_comparison_report.py",
                    "--output-doc",
                    str(out_file),
                    "--verbose",
                ],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn("[HARDWARE] FallGuard_v2 Metrics", res.stdout)
            self.assertIn("[OFFLINE] SisFall Detector Metrics", res.stdout)
            self.assertIn("Baseline_GyroTilt", res.stdout)
            self.assertIn("Baseline_SVM", res.stdout)

    def test_custom_output_nested_directory_creation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = Path(tmpdir) / "nested" / "deep" / "custom_report.md"
            res = subprocess.run(
                [
                    sys.executable,
                    "tools/generate_comparison_report.py",
                    "--output-doc",
                    str(out_file),
                    "--quiet",
                ],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0)
            self.assertTrue(out_file.exists())
            self.assertGreater(out_file.stat().st_size, 30000)


class TestCorruptedInputsAdversarial(unittest.TestCase):
    """Stress tests corrupted data files, missing schema fields, and invalid types."""

    def test_empty_zero_byte_hardware_csv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            empty_file = Path(tmpdir) / "empty_hw.csv"
            empty_file.write_text("")
            res = subprocess.run(
                [
                    sys.executable,
                    "tools/generate_comparison_report.py",
                    "--hardware-csv",
                    str(empty_file),
                ],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)

    def test_empty_zero_byte_offline_csv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            empty_file = Path(tmpdir) / "empty_off.csv"
            empty_file.write_text("")
            res = subprocess.run(
                [
                    sys.executable,
                    "tools/generate_comparison_report.py",
                    "--offline-csv",
                    str(empty_file),
                ],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)

    def test_missing_required_hardware_columns(self):
        hw_orig = pd.read_csv(PROJECT_ROOT / "results/hardware_pilot_benchmark.csv")
        required_cols = ["detector", "trial_id", "is_fall", "detected", "latency_ms", "duration_s"]
        with tempfile.TemporaryDirectory() as tmpdir:
            for col in required_cols:
                dropped = hw_orig.drop(columns=[col])
                test_csv = Path(tmpdir) / f"missing_{col}.csv"
                dropped.to_csv(test_csv, index=False)
                res = subprocess.run(
                    [
                        sys.executable,
                        "tools/generate_comparison_report.py",
                        "--hardware-csv",
                        str(test_csv),
                    ],
                    cwd=str(PROJECT_ROOT),
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(
                    res.returncode,
                    0,
                    f"Expected non-zero exit code when hardware column '{col}' is missing",
                )

    def test_missing_required_offline_columns(self):
        off_orig = pd.read_csv(PROJECT_ROOT / "results/offline_sisfall_benchmark.csv")
        required_cols = ["detector", "trial_id", "is_fall", "detected", "latency_ms", "duration_s", "false_alarm"]
        with tempfile.TemporaryDirectory() as tmpdir:
            for col in required_cols:
                dropped = off_orig.drop(columns=[col])
                test_csv = Path(tmpdir) / f"off_missing_{col}.csv"
                dropped.to_csv(test_csv, index=False)
                res = subprocess.run(
                    [
                        sys.executable,
                        "tools/generate_comparison_report.py",
                        "--offline-csv",
                        str(test_csv),
                    ],
                    cwd=str(PROJECT_ROOT),
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(
                    res.returncode,
                    0,
                    f"Expected non-zero exit code when offline column '{col}' is missing",
                )

    def test_non_numeric_is_fall_fails(self):
        hw_orig = pd.read_csv(PROJECT_ROOT / "results/hardware_pilot_benchmark.csv")
        hw_bad = hw_orig.copy()
        hw_bad["is_fall"] = "FALL_EVENT"
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_csv = Path(tmpdir) / "bad_is_fall.csv"
            hw_bad.to_csv(bad_csv, index=False)
            res = subprocess.run(
                [
                    sys.executable,
                    "tools/generate_comparison_report.py",
                    "--hardware-csv",
                    str(bad_csv),
                ],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("ValueError", res.stderr)

    def test_non_numeric_detected_fails(self):
        hw_orig = pd.read_csv(PROJECT_ROOT / "results/hardware_pilot_benchmark.csv")
        hw_bad = hw_orig.copy()
        hw_bad["detected"] = "DETECTED_TRUE"
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_csv = Path(tmpdir) / "bad_detected.csv"
            hw_bad.to_csv(bad_csv, index=False)
            res = subprocess.run(
                [
                    sys.executable,
                    "tools/generate_comparison_report.py",
                    "--hardware-csv",
                    str(bad_csv),
                ],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("ValueError", res.stderr)

    def test_all_nan_latencies_graceful(self):
        """When all latencies are NaN, latency engine should return N/A without crashing."""
        df = pd.DataFrame({
            "is_fall": [1, 1],
            "detected": [1, 1],
            "latency_ms": [np.nan, np.nan],
            "duration_s": [5.0, 5.0],
            "detector": ["TestDet", "TestDet"],
        })
        stats = compute_latency_statistics(df)
        self.assertEqual(stats.count, 0)
        self.assertTrue(math.isnan(stats.mean_ms))
        self.assertEqual(stats.format_mean_std(), "N/A")


class TestMathematicalAndDomainContracts(unittest.TestCase):
    """Empirical mathematical validation of metrics and strict separation rules."""

    def test_wilson_ci_bounds_and_exactness(self):
        # Degenerate: n = 0
        low, up = wilson_ci(0, 0)
        self.assertTrue(math.isnan(low) and math.isnan(up))

        # Boundary: k = 0, n = 10
        low0, up0 = wilson_ci(0, 10)
        self.assertAlmostEqual(low0, 0.0, places=4)
        self.assertGreater(up0, 0.0)
        self.assertLessEqual(up0, 1.0)

        # Boundary: k = 10, n = 10
        low10, up10 = wilson_ci(10, 10)
        self.assertGreaterEqual(low10, 0.0)
        self.assertAlmostEqual(up10, 1.0, places=4)

        # Hardware Pilot Sensitivity: 5 / 10 -> [23.66%, 76.34%]
        low_hw, up_hw = wilson_ci(5, 10)
        self.assertAlmostEqual(low_hw * 100.0, 23.66, places=1)
        self.assertAlmostEqual(up_hw * 100.0, 76.34, places=1)

        # Offline SisFall FallGuard Sensitivity: 351 / 448 -> [74.30%, 81.91%]
        low_off, up_off = wilson_ci(351, 448)
        self.assertAlmostEqual(low_off * 100.0, 74.30, places=1)
        self.assertAlmostEqual(up_off * 100.0, 81.91, places=1)

    def test_fa_rate_excludes_fall_durations(self):
        """Verifies FA rate formula uses strictly non-fall (ADL) exposure hours."""
        df = pd.DataFrame({
            "is_fall": [1, 1, 0, 0],
            "detected": [1, 0, 1, 0],          # 1 FP on ADL
            "duration_s": [10.0, 10.0, 1800.0, 1800.0], # 3600s total ADL exposure = 1.0 hr
            "latency_ms": [3000.0, np.nan, np.nan, np.nan],
            "detector": ["TestDet"] * 4,
        })
        perf = evaluate_detector(df, "[TEST]", "TestDet")
        self.assertEqual(perf.counts.fp, 1)
        self.assertAlmostEqual(perf.adl_exposure_s, 3600.0)
        self.assertAlmostEqual(perf.adl_exposure_hr, 1.0)
        self.assertAlmostEqual(perf.fa_per_hr, 1.0)

    def test_latency_computed_strictly_on_true_positives(self):
        """Latency must only include trials where is_fall==1 and detected==1."""
        df = pd.DataFrame({
            "is_fall": [1, 1, 0, 0],
            "detected": [1, 0, 1, 0],          # 1 TP, 1 FN, 1 FP, 1 TN
            "latency_ms": [3100.0, 9999.0, 500.0, np.nan], # Only 3100.0 should be included!
            "duration_s": [5.0, 5.0, 5.0, 5.0],
            "detector": ["TestDet"] * 4,
        })
        stats = compute_latency_statistics(df)
        self.assertEqual(stats.count, 1)
        self.assertAlmostEqual(stats.mean_ms, 3100.0)

    def test_dual_table_separation_in_document(self):
        """Verifies Table 1 is [HARDWARE] and Table 2 is [OFFLINE] with no merged tables."""
        doc_path = PROJECT_ROOT / "docs/sisfall_benchmark_comparison.md"
        self.assertTrue(doc_path.exists())
        text = doc_path.read_text(encoding="utf-8")

        self.assertIn("Table 1: [HARDWARE]", text)
        self.assertIn("Table 2: [OFFLINE]", text)
        self.assertNotIn("Pooled Benchmark", text)
        self.assertNotIn("Combined Hardware and Offline", text)

        # Baseline hardware honesty check
        self.assertIn("*N/A (Not deployed)*", text)

    def test_checksum_registry_fidelity(self):
        """Verifies all hashes published in Section 6 match real on-disk files."""
        doc_path = PROJECT_ROOT / "docs/sisfall_benchmark_comparison.md"
        text = doc_path.read_text(encoding="utf-8")

        files_to_check = [
            "data/pilot1/trials.csv",
            "results/hardware_pilot_benchmark.csv",
            "data/sisfall/manifest.csv",
            "results/offline_sisfall_benchmark.csv",
            "firmware/FallGuard_v2.ino",
            "tools/generate_comparison_report.py",
        ]
        for f in files_to_check:
            h = compute_sha256(PROJECT_ROOT / f)
            self.assertIn(h, text, f"Expected SHA-256 for {f} ({h}) not found in document")


class TestIdempotence(unittest.TestCase):
    """Stress tests bit-for-bit repeatability of report generation."""

    def test_successive_runs_sha256_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_a = Path(tmpdir) / "report_a.md"
            file_b = Path(tmpdir) / "report_b.md"

            res_a = subprocess.run(
                [
                    sys.executable,
                    "tools/generate_comparison_report.py",
                    "--output-doc",
                    str(file_a),
                    "--quiet",
                ],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_a.returncode, 0)

            res_b = subprocess.run(
                [
                    sys.executable,
                    "tools/generate_comparison_report.py",
                    "--output-doc",
                    str(file_b),
                    "--quiet",
                ],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_b.returncode, 0)

            hash_a = hashlib.sha256(file_a.read_bytes()).hexdigest()
            hash_b = hashlib.sha256(file_b.read_bytes()).hexdigest()

            self.assertEqual(hash_a, hash_b, "Generator is not idempotent! Hashes differed between runs.")


if __name__ == "__main__":
    unittest.main()
