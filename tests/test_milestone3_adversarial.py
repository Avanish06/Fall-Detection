#!/usr/bin/env python3
"""
tests/test_milestone3_adversarial.py
====================================
Comprehensive Adversarial Challenge Suite for Milestone 3 Deliverables:
1. Strict separation of [HARDWARE] vs [OFFLINE] domains (zero pooling, zero combined tables).
2. Strict Table 1 [HARDWARE] and Table 2 [OFFLINE] labeling.
3. Verification of Zero-Fabrication: hardware baselines strictly N/A.
4. Explicit labeling across all summaries, tables, and artifacts.
5. Machine-precision mathematical verification of Wilson CIs, FA/hr, and latencies.
6. SHA-256 cryptographic provenance verification.
7. CLI robustness and parameter validation for report generator.
"""

import hashlib
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def wilson_ci_ref(k: int, n: int, z: float = 1.959963984540054):
    if n <= 0:
        return (float("nan"), float("nan"))
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    spread = (z / denom) * math.sqrt((p * (1.0 - p) / n) + (z2 / (4.0 * (n ** 2))))
    return (max(0.0, center - spread), min(1.0, center + spread))


class TestMilestone3Adversarial(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.hw_csv_path = PROJECT_ROOT / "results" / "hardware_pilot_benchmark.csv"
        cls.off_csv_path = PROJECT_ROOT / "results" / "offline_sisfall_benchmark.csv"
        cls.pilot_path = PROJECT_ROOT / "data" / "pilot1" / "trials.csv"
        cls.manifest_path = PROJECT_ROOT / "data" / "sisfall" / "manifest.csv"
        cls.doc_path = PROJECT_ROOT / "docs" / "sisfall_benchmark_comparison.md"
        cls.generator_script = PROJECT_ROOT / "tools" / "generate_comparison_report.py"
        cls.firmware_path = PROJECT_ROOT / "firmware" / "FallGuard_v2.ino"

        cls.assertTrue(cls.hw_csv_path.exists(), f"Missing {cls.hw_csv_path}")
        cls.assertTrue(cls.off_csv_path.exists(), f"Missing {cls.off_csv_path}")
        cls.assertTrue(cls.doc_path.exists(), f"Missing {cls.doc_path}")

        cls.hw_df = pd.read_csv(cls.hw_csv_path, keep_default_na=False)
        cls.off_df = pd.read_csv(cls.off_csv_path, keep_default_na=False)
        cls.doc_text = cls.doc_path.read_text(encoding="utf-8")

    # ========================================================================
    # 1. STRICT DOMAIN SEPARATION & ZERO POOLING
    # ========================================================================

    def test_hardware_benchmark_domain_purity(self):
        """Verify hardware benchmark CSV contains ONLY [HARDWARE] records for FallGuard_v2."""
        self.assertEqual(len(self.hw_df), 20, "Hardware trial count must be exactly 20")
        datasets = set(self.hw_df["dataset"].unique())
        self.assertEqual(datasets, {"[HARDWARE]"}, "All records must be labeled [HARDWARE]")
        detectors = set(self.hw_df["detector"].unique())
        self.assertEqual(detectors, {"FallGuard_v2"}, "Only FallGuard_v2 was deployed to hardware")

    def test_offline_benchmark_domain_purity(self):
        """Verify offline benchmark CSV contains ONLY [OFFLINE] records across 4 detectors."""
        self.assertEqual(len(self.off_df), 4152, "Offline benchmark must have 1038 * 4 = 4152 records")
        datasets = set(self.off_df["dataset"].unique())
        self.assertEqual(datasets, {"[OFFLINE]"}, "All records must be labeled [OFFLINE]")
        detectors = set(self.off_df["detector"].unique())
        expected = {"FallGuard_v2", "Baseline_SVM", "Baseline_TwoStage", "Baseline_GyroTilt"}
        self.assertEqual(detectors, expected)

    def test_zero_cross_domain_trial_leakage(self):
        """Verify no trial IDs are shared or leaked between hardware and offline benchmarks."""
        hw_ids = set(self.hw_df["trial_id"])
        off_ids = set(self.off_df["trial_id"])
        overlap = hw_ids.intersection(off_ids)
        self.assertEqual(len(overlap), 0, f"Trial ID overlap detected: {overlap}")

    def test_no_cross_domain_row_pooling_or_combined_tables(self):
        """Verify there are no pooled/merged tables or aggregated numbers across domains."""
        # Ensure there is no combined table mixing hardware and offline rows
        forbidden_phrases = [
            "combined sensitivity",
            "pooled sensitivity",
            "overall sensitivity across both",
            "aggregate sensitivity",
            "average sensitivity of hardware and offline",
            "combined f1",
            "pooled f1",
            "combined accuracy",
            "pooled accuracy",
        ]
        doc_lower = self.doc_text.lower()
        for phrase in forbidden_phrases:
            self.assertNotIn(phrase, doc_lower, f"Found forbidden cross-domain pooling phrase: '{phrase}'")

    # ========================================================================
    # 2. TABLE 1 AND TABLE 2 STRICT LABELING AUDIT
    # ========================================================================

    def test_table_1_and_table_2_headers_and_labels(self):
        """Verify Table 1 is strictly [HARDWARE] and Table 2 is strictly [OFFLINE]."""
        self.assertIn("### Table 1: [HARDWARE] ESP32 Pocket Pilot Trial Results", self.doc_text)
        self.assertIn("### Table 2: [OFFLINE] SisFall Benchmark Results", self.doc_text)

        # Verify Table 1 execution environment row
        self.assertIn("| **Execution Environment** | **[HARDWARE] Embedded ESP32** |", self.doc_text)

        # Verify Table 2 execution environment row
        self.assertIn("| **Execution Environment** | **[OFFLINE] Replay Simulator** |", self.doc_text)

    def test_explicit_labeling_across_all_sections(self):
        """Verify Section 1, 2, 3, 4, Quick Reference Cards have explicit domain labeling."""
        self.assertIn("QUICK REFERENCE CARD: [HARDWARE]", self.doc_text)
        self.assertIn("QUICK REFERENCE CARD: [OFFLINE]", self.doc_text)
        self.assertIn("## Section 3: `[HARDWARE]` Table & Analysis", self.doc_text)
        self.assertIn("## Section 4: `[OFFLINE]` Table & Analysis", self.doc_text)
        self.assertIn("### 2.1 `[HARDWARE]` Experimental Protocol", self.doc_text)
        self.assertIn("### 2.2 `[OFFLINE]` SisFall Benchmark Dataset", self.doc_text)

    # ========================================================================
    # 3. ZERO-FABRICATION: HARDWARE BASELINES N/A AUDIT
    # ========================================================================

    def test_hardware_baselines_strictly_na(self):
        """Verify all baseline metrics in Table 1 are honestly marked N/A (Zero Fabrication)."""
        t1_start = self.doc_text.find("### Table 1: [HARDWARE]")
        t1_end = self.doc_text.find("### 3.2", t1_start)
        self.assertNotEqual(t1_start, -1)
        self.assertNotEqual(t1_end, -1)
        t1_text = self.doc_text[t1_start:t1_end]

        metrics_checked = [
            "True Positives (TP)",
            "False Negatives (FN)",
            "True Negatives (TN)",
            "False Positives (FP)",
            "Sensitivity (Recall)",
            "Specificity",
            "Precision (PPV)",
            "F1-Score",
            "Overall Accuracy",
            "False Alarm Rate",
            "Mean Latency (Impact)",
            "Median Latency (Impact)",
            "Latency Range [Min, Max]",
        ]

        for metric in metrics_checked:
            found = False
            for line in t1_text.splitlines():
                if metric in line:
                    found = True
                    parts = [p.strip() for p in line.split("|")[1:-1]]
                    # parts: [Metric, FallGuard_v2, Baseline_SVM, Baseline_TwoStage, Baseline_GyroTilt]
                    self.assertEqual(len(parts), 5, f"Malformed table row: {line}")
                    # Baseline columns: index 2, 3, 4
                    self.assertIn("N/A", parts[2], f"Baseline_SVM {metric} must be N/A, got: {parts[2]}")
                    self.assertIn("N/A", parts[3], f"Baseline_TwoStage {metric} must be N/A, got: {parts[3]}")
                    self.assertIn("N/A", parts[4], f"Baseline_GyroTilt {metric} must be N/A, got: {parts[4]}")
            self.assertTrue(found, f"Metric '{metric}' not found in Table 1")

    # ========================================================================
    # 4. MATHEMATICAL RIGOR & MACHINE-PRECISION RECOMPUTATION
    # ========================================================================

    def test_hardware_metrics_mathematical_precision(self):
        """Independently recompute Hardware Pilot metrics from raw data and verify match."""
        is_fall = self.hw_df["is_fall"].astype(int)
        detected = self.hw_df["detected"].astype(int)

        tp = int(((is_fall == 1) & (detected == 1)).sum())
        fn = int(((is_fall == 1) & (detected == 0)).sum())
        tn = int(((is_fall == 0) & (detected == 0)).sum())
        fp = int(((is_fall == 0) & (detected == 1)).sum())

        self.assertEqual(tp, 5)
        self.assertEqual(fn, 5)
        self.assertEqual(tn, 9)
        self.assertEqual(fp, 1)

        sens = tp / (tp + fn)
        sens_ci = wilson_ci_ref(tp, tp + fn)
        self.assertAlmostEqual(sens, 0.50, places=4)
        self.assertAlmostEqual(sens_ci[0], 0.2365928, places=4)
        self.assertAlmostEqual(sens_ci[1], 0.7634072, places=4)
        self.assertIn("50.00% [23.66%, 76.34%]", self.doc_text)

        spec = tn / (tn + fp)
        spec_ci = wilson_ci_ref(tn, tn + fp)
        self.assertAlmostEqual(spec, 0.90, places=4)
        self.assertIn("90.00% [59.58%, 98.21%]", self.doc_text)

        prec = tp / (tp + fp)
        self.assertAlmostEqual(prec, 5.0 / 6.0, places=4)
        self.assertIn("83.33% [43.65%, 96.99%]", self.doc_text)

        f1 = 2 * tp / (2 * tp + fp + fn)
        self.assertAlmostEqual(f1, 0.6250, places=4)
        self.assertIn("0.6250", self.doc_text)

        # ADL exposure & FA/hr
        adl_durations = self.hw_df[self.hw_df["is_fall"].astype(int) == 0]["duration_s"].astype(float)
        exp_s = adl_durations.sum()
        self.assertAlmostEqual(exp_s, 73.0, places=1)
        fa_hr = fp / (exp_s / 3600.0)
        self.assertAlmostEqual(fa_hr, 49.315, places=2)
        self.assertIn("49.32 FA/hour", self.doc_text)

        # Latency on True Positives
        tp_mask = (is_fall == 1) & (detected == 1)
        lats = self.hw_df.loc[tp_mask, "latency_ms"].astype(float).to_numpy()
        self.assertEqual(len(lats), 5)
        self.assertAlmostEqual(float(np.mean(lats)), 3314.6, places=1)
        self.assertAlmostEqual(float(np.std(lats, ddof=1)), 332.5, places=1)
        self.assertAlmostEqual(float(np.median(lats)), 3185.0, places=1)
        self.assertIn("3314.6 ± 332.5 ms", self.doc_text)
        self.assertIn("3185.0 ms", self.doc_text)

    def test_offline_metrics_mathematical_precision(self):
        """Independently recompute SisFall metrics for all 4 detectors and verify."""
        expected_results = {
            "FallGuard_v2": {"tp": 351, "fn": 97, "tn": 507, "fp": 83, "sens": "78.35% [74.30%, 81.91%]", "spec": "85.93% [82.89%, 88.51%]", "f1": "0.7959", "fa": "25.79 FA/hour"},
            "Baseline_SVM": {"tp": 396, "fn": 52, "tn": 462, "fp": 128, "sens": "88.39% [85.10%, 91.04%]", "spec": "78.31% [74.80%, 81.44%]", "f1": "0.8148", "fa": "39.77 FA/hour"},
            "Baseline_TwoStage": {"tp": 339, "fn": 109, "tn": 431, "fp": 159, "sens": "75.67% [71.49%, 79.41%]", "spec": "73.05% [69.33%, 76.47%]", "f1": "0.7167", "fa": "49.40 FA/hour"},
            "Baseline_GyroTilt": {"tp": 411, "fn": 37, "tn": 541, "fp": 49, "sens": "91.74% [88.82%, 93.95%]", "spec": "91.69% [89.19%, 93.66%]", "f1": "0.9053", "fa": "15.22 FA/hour"},
        }

        for det, exp in expected_results.items():
            df_slice = self.off_df[self.off_df["detector"] == det]
            is_f = df_slice["is_fall"].astype(int)
            det_val = df_slice["detected"].astype(int)
            tp = int(((is_f == 1) & (det_val == 1)).sum())
            fn = int(((is_f == 1) & (det_val == 0)).sum())
            tn = int(((is_f == 0) & (det_val == 0)).sum())
            fp = int(((is_f == 0) & (det_val == 1)).sum())

            self.assertEqual(tp, exp["tp"], f"{det} TP mismatch")
            self.assertEqual(fn, exp["fn"], f"{det} FN mismatch")
            self.assertEqual(tn, exp["tn"], f"{det} TN mismatch")
            self.assertEqual(fp, exp["fp"], f"{det} FP mismatch")

            self.assertIn(exp["sens"], self.doc_text, f"{det} Sensitivity not in report")
            self.assertIn(exp["spec"], self.doc_text, f"{det} Specificity not in report")
            self.assertIn(exp["f1"], self.doc_text, f"{det} F1 not in report")
            self.assertIn(exp["fa"], self.doc_text, f"{det} FA rate not in report")

    def test_activity_false_positives_cross_tabulation(self):
        """Verify the D01-D19 cross-tabulation sums match total FPs for each detector."""
        adls_df = self.off_df[self.off_df["is_fall"].astype(int) == 0].copy()
        for det, expected_fp in [("Baseline_GyroTilt", 49), ("Baseline_SVM", 128), ("Baseline_TwoStage", 159), ("FallGuard_v2", 83)]:
            det_slice = adls_df[adls_df["detector"] == det]
            total_fp = int(det_slice["false_alarm"].astype(int).sum())
            self.assertEqual(total_fp, expected_fp, f"{det} total ADL FPs mismatch")

    # ========================================================================
    # 5. CRYPTOGRAPHIC PROVENANCE REGISTRY VERIFICATION
    # ========================================================================

    def test_sha256_hash_registry_fidelity(self):
        """Verify all SHA-256 hashes listed in Section 6.2 match real on-disk files."""
        files_to_check = {
            "data/pilot1/trials.csv": self.pilot_path,
            "results/hardware_pilot_benchmark.csv": self.hw_csv_path,
            "data/sisfall/manifest.csv": self.manifest_path,
            "results/offline_sisfall_benchmark.csv": self.off_csv_path,
            "firmware/FallGuard_v2.ino": self.firmware_path,
            "tools/generate_comparison_report.py": self.generator_script,
        }

        for rel_path, abs_path in files_to_check.items():
            self.assertTrue(abs_path.exists(), f"File does not exist: {abs_path}")
            expected_hash = hashlib.sha256(abs_path.read_bytes()).hexdigest()
            # Check if this exact hash appears in Section 6.2
            self.assertIn(expected_hash, self.doc_text, f"Hash for {rel_path} ({expected_hash}) not found in report registry!")

    # ========================================================================
    # 6. SCRIPT REPRODUCIBILITY & CLI ROBUSTNESS
    # ========================================================================

    def test_report_generator_reproducibility(self):
        """Re-running the report generator must produce byte-for-byte or text-identical output."""
        res = subprocess.run(
            [sys.executable, str(self.generator_script), "--verbose"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Generator script failed: {res.stderr}")
        reloaded_text = self.doc_path.read_text(encoding="utf-8")
        self.assertEqual(self.doc_text, reloaded_text, "Report content changed upon regeneration")

    def test_report_generator_cli_missing_args(self):
        """Generator script must return non-zero when nonexistent paths are supplied."""
        res = subprocess.run(
            [sys.executable, str(self.generator_script), "--hardware-csv", "nonexistent.csv"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(res.returncode, 0, "Generator must fail on missing hardware CSV")


if __name__ == "__main__":
    unittest.main()
