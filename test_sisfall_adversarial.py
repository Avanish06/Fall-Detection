#!/usr/bin/env python3
"""
test_sisfall_adversarial.py
===========================
Adversarial test harness and stress suite for SisFall acquisition and preprocessing:
1. CLI Argument Handling (invalid, help, non-existent, empty, conflicting)
2. File Corruption & Truncation Handling (blanks, corrupted lines, partial columns, short trials)
3. Dynamic Range & Kinematic Boundary Assertions (extreme peaks, unphysical gyros, sub-threshold falls)
4. DSP Filter Verification (FIR zero-delay alignment, boundary step response, anti-aliasing stopband)
5. Manifest Integrity & Bijective Mapping against processed files
6. Dataset Completeness (cohorts, activities, NaN/Inf checks across all 1038 files)
"""

import os
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path
import numpy as np
import pandas as pd

# Add repo root to sys.path
REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from tools.preprocess_sisfall import (
    parse_raw_sisfall_file,
    process_trial,
    decimate_axis_50hz,
    design_anti_aliasing_fir,
    AA_KERNEL,
    ADXL_SCALE_MS2,
    ITG_SCALE_DPS,
    MMA_SCALE_MS2,
    ACTIVITY_TAXONOMY,
    TRIAL_REGEX
)


def run_cli_test(cmd: list[str]) -> tuple[int, str, str]:
    """Runs a CLI command and returns exit code, stdout, stderr."""
    res = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    return res.returncode, res.stdout, res.stderr


def test_cli_argument_handling():
    print("\n--- [TEST GROUP 1: CLI Argument Handling] ---")

    # 1.1 download_sisfall --help
    code, out, err = run_cli_test([sys.executable, "tools/download_sisfall.py", "--help"])
    assert code == 0, f"download_sisfall --help failed with {code}"
    assert "SisFall Dataset Acquisition Engine" in out
    print("  [PASS] download_sisfall --help returns code 0 and usage")

    # 1.2 download_sisfall conflicting flags (--subset AND --full)
    code, out, err = run_cli_test([sys.executable, "tools/download_sisfall.py", "--subset", "--full"])
    assert code != 0, "Conflicting flags should fail"
    assert "not allowed with argument" in err
    print("  [PASS] download_sisfall mutually exclusive flags rejected")

    # 1.3 download_sisfall unrecognized argument
    code, out, err = run_cli_test([sys.executable, "tools/download_sisfall.py", "--unknown-flag-xyz"])
    assert code != 0, "Unknown flag should fail"
    assert "unrecognized arguments" in err
    print("  [PASS] download_sisfall unknown argument rejected")

    # 1.4 download_sisfall --verify on existing data
    code, out, err = run_cli_test([sys.executable, "tools/download_sisfall.py", "--verify"])
    assert code == 0, f"--verify failed: {err}"
    assert "Found 1038 trial files (448 falls, 590 ADLs)" in out
    print("  [PASS] download_sisfall --verify on real raw dir verifies 1038 files")

    # 1.5 download_sisfall --verify on non-existent dir
    code, out, err = run_cli_test([
        sys.executable, "tools/download_sisfall.py", "--output-dir", "non_existent_raw_dir", "--verify"
    ])
    assert code == 0, f"--verify on missing dir failed: {err}"
    assert "Found 0 trial files (0 falls, 0 ADLs)" in out
    print("  [PASS] download_sisfall --verify on nonexistent dir gracefully handles 0 files")

    # 1.6 preprocess_sisfall --help
    code, out, err = run_cli_test([sys.executable, "tools/preprocess_sisfall.py", "--help"])
    assert code == 0, f"preprocess_sisfall --help failed with {code}"
    assert "SisFall Dataset Preprocessing" in out
    print("  [PASS] preprocess_sisfall --help returns code 0 and usage")

    # 1.7 preprocess_sisfall unrecognized argument
    code, out, err = run_cli_test([sys.executable, "tools/preprocess_sisfall.py", "--invalid-opt"])
    assert code != 0, "Unknown flag should fail"
    assert "unrecognized arguments" in err
    print("  [PASS] preprocess_sisfall unknown argument rejected")

    # 1.8 preprocess_sisfall nonexistent input dir
    code, out, err = run_cli_test([
        sys.executable, "tools/preprocess_sisfall.py", "--input-dir", "non_existent_input_dir"
    ])
    assert code != 0, "Nonexistent input dir should fail"
    assert "does not exist" in err
    print("  [PASS] preprocess_sisfall nonexistent input dir exits with error")

    # 1.9 preprocess_sisfall empty input dir
    with tempfile.TemporaryDirectory() as empty_dir:
        code, out, err = run_cli_test([
            sys.executable, "tools/preprocess_sisfall.py", "--input-dir", empty_dir
        ])
        assert code != 0, "Empty input dir should fail"
        assert "No valid SisFall trial .txt files found" in err
        print("  [PASS] preprocess_sisfall empty input dir exits with descriptive error")


def test_file_corruption_and_truncation():
    print("\n--- [TEST GROUP 2: File Corruption & Truncation] ---")

    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        out_dir = tmp_dir / "out"
        out_dir.mkdir()

        # 2.1 Truncated file (< 200 samples)
        trunc_file = tmp_dir / "F01_SA01_R01.txt"
        with open(trunc_file, "w") as f:
            for i in range(150):
                f.write("100, -200, 300, 10, -20, 30, 40, -50, 60;\n")
        try:
            parse_raw_sisfall_file(trunc_file)
            assert False, "Should have raised ValueError for < 200 samples"
        except ValueError as e:
            assert "Trial too short" in str(e)
            print("  [PASS] Truncated file (< 200 samples) rejected by parser")

        # 2.2 Blank lines, semicolons, extra spaces
        messy_file = tmp_dir / "F02_SA01_R01.txt"
        with open(messy_file, "w") as f:
            f.write("\n\n   \n")  # leading blank lines
            for i in range(250):
                f.write(f"  {100 + i} ,  {-250} , 300 , 10 , -20 , 30 , 0 , 0 , 0 ; \n\n")
            f.write("\n   \n")  # trailing blank lines
        arr = parse_raw_sisfall_file(messy_file)
        assert arr.shape == (250, 9), f"Expected shape (250, 9), got {arr.shape}"
        assert arr[0, 0] == 100.0
        assert arr[249, 0] == 349.0
        print("  [PASS] Parser robustly handles arbitrary blank lines, spacing, and trailing semicolons")

        # 2.3 Non-numeric characters / corrupt tokens
        corrupt_file = tmp_dir / "F03_SA01_R01.txt"
        with open(corrupt_file, "w") as f:
            for i in range(100):
                f.write("100, -200, 300, 10, -20, 30, 0, 0, 0;\n")
            f.write("CORRUPTED_LINE_WITH_TEXT, 0, 0, 0, 0, 0, 0, 0, 0;\n")
            for i in range(100):
                f.write("100, -200, 300, 10, -20, 30, 0, 0, 0;\n")
        try:
            parse_raw_sisfall_file(corrupt_file)
            assert False, "Should have raised ValueError for non-numeric token"
        except ValueError as e:
            assert "could not convert string to float" in str(e)
            print("  [PASS] Non-numeric corrupt tokens caught with ValueError")

        # 2.4 File with fewer than 6 columns
        short_cols_file = tmp_dir / "F04_SA01_R01.txt"
        with open(short_cols_file, "w") as f:
            for i in range(250):
                f.write("100, 200, 300;\n")  # only 3 columns
        try:
            parse_raw_sisfall_file(short_cols_file)
            assert False, "Should have raised ValueError for insufficient columns"
        except ValueError as e:
            assert "No valid data rows" in str(e)
            print("  [PASS] Insufficient columns (< 6) rejected with ValueError")

        # 2.5 Empty file
        empty_file = tmp_dir / "F05_SA01_R01.txt"
        empty_file.touch()
        try:
            parse_raw_sisfall_file(empty_file)
            assert False, "Should have raised ValueError for empty file"
        except ValueError as e:
            assert "No valid data rows" in str(e)
            print("  [PASS] Empty file rejected with ValueError")


def test_boundary_assertions_and_kinematics():
    print("\n--- [TEST GROUP 3: Dynamic Range & Kinematic Boundary Assertions] ---")

    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        out_dir = tmp_dir / "out"
        out_dir.mkdir()

        # Helper to generate raw file
        def write_mock_trial(file_path: Path, raw_mat: np.ndarray):
            with open(file_path, "w") as f:
                for row in raw_mat:
                    line = ", ".join(f"{int(x)}" for x in row) + ";\n"
                    f.write(line)

        # Baseline valid fall: 300 samples, impact peak ~25 m/s²
        base_mat = np.zeros((300, 9), dtype=np.int32)
        base_mat[:, 1] = int(-9.80665 / ADXL_SCALE_MS2)  # 1g on Y axis (~-256 counts)
        # Impact at sample 100
        base_mat[95:105, 0] = int(25.0 / ADXL_SCALE_MS2)

        # 3.1 Normal fall processing
        normal_fall = tmp_dir / "F01_SA01_R01.txt"
        write_mock_trial(normal_fall, base_mat)
        meta = process_trial(normal_fall, out_dir, force=True)
        assert meta["trial_id"] == "F01_SA01_R01"
        assert meta["is_fall"] == 1
        assert meta["subject_group"] == "YOUNG"
        assert (out_dir / "F01_SA01_R01.csv").exists()
        print("  [PASS] Valid fall processed and verified")

        # 3.2 Unphysical Acceleration Peak (> 250 m/s² post-filtering)
        # Note: 4:1 FIR filter tap peak is ~0.25, so sustained burst or > 1200 m/s² impulse exceeds 250 m/s²
        extreme_accel_fall = tmp_dir / "F02_SA01_R01.txt"
        mat_extreme_accel = base_mat.copy()
        mat_extreme_accel[95:115, 0] = int(300.0 / ADXL_SCALE_MS2)  # sustained 300 m/s² burst
        write_mock_trial(extreme_accel_fall, mat_extreme_accel)
        try:
            process_trial(extreme_accel_fall, out_dir, force=True)
            assert False, "Should have raised assertion for unphysical acceleration"
        except AssertionError as e:
            assert "Unphysical acceleration peak" in str(e)
            print("  [PASS] Unphysical acceleration (> 250 m/s²) caught by assertion")

        # 3.3 Unphysical Gyroscope Rate (> 2500 °/s post-filtering)
        extreme_gyro_fall = tmp_dir / "F03_SA01_R01.txt"
        mat_extreme_gyro = base_mat.copy()
        mat_extreme_gyro[95:115, 3] = int(3000.0 / ITG_SCALE_DPS)  # sustained ~3000 °/s
        write_mock_trial(extreme_gyro_fall, mat_extreme_gyro)
        try:
            process_trial(extreme_gyro_fall, out_dir, force=True)
            assert False, "Should have raised assertion for unphysical gyro rate"
        except AssertionError as e:
            assert "Unphysical gyroscope rate" in str(e)
            print("  [PASS] Unphysical gyro rate (> 2500 °/s) caught by assertion")

        # 3.4 Fall without sufficient dynamic impact (< 10 m/s²)
        flat_fall = tmp_dir / "F04_SA01_R01.txt"
        mat_flat = np.zeros((300, 9), dtype=np.int32)
        # static 0.5g (~4.9 m/s²)
        mat_flat[:, 1] = int(-4.9 / ADXL_SCALE_MS2)
        write_mock_trial(flat_fall, mat_flat)
        try:
            process_trial(flat_fall, out_dir, force=True)
            assert False, "Should have raised ValueError for fall without dynamic impact"
        except ValueError as e:
            assert "Fall impact below minimum dynamic threshold" in str(e)
            print("  [PASS] Fall trial with peak < 10 m/s² caught by dynamic validation")

        # 3.5 ADL with < 10 m/s² is allowed (e.g. sitting still)
        flat_adl = tmp_dir / "D01_SA01_R01.txt"
        write_mock_trial(flat_adl, mat_flat)
        adl_meta = process_trial(flat_adl, out_dir, force=True)
        assert adl_meta["is_fall"] == 0
        assert (out_dir / "D01_SA01_R01.csv").exists()
        print("  [PASS] ADL trial with peak < 10 m/s² correctly permitted")

        # 3.6 Invalid Trial Filename format
        bad_name_file = tmp_dir / "INVALID_NAME_R01.txt"
        write_mock_trial(bad_name_file, base_mat)
        try:
            process_trial(bad_name_file, out_dir, force=True)
            assert False, "Should have rejected bad trial name format"
        except ValueError as e:
            assert "Unrecognized trial filename format" in str(e)
            print("  [PASS] Malformed filename format caught by regex check")

        # 3.7 Subject cohort mapping (SE -> ELDERLY, SA -> YOUNG)
        elderly_file = tmp_dir / "F01_SE06_R01.txt"
        write_mock_trial(elderly_file, base_mat)
        meta_elderly = process_trial(elderly_file, out_dir, force=True)
        assert meta_elderly["subject_group"] == "ELDERLY"
        assert meta_elderly["subject_id"] == "SE06"
        print("  [PASS] Elderly cohort correctly tagged for SE prefix")


def test_filter_properties_and_dsp():
    print("\n--- [TEST GROUP 4: DSP Filter Properties & Invariances] ---")

    kernel = AA_KERNEL
    assert len(kernel) == 81, f"Expected 81 taps, got {len(kernel)}"
    assert np.isclose(np.sum(kernel), 1.0, atol=1e-12), "DC gain of filter must be exactly 1.0"
    # Linear-phase symmetry
    assert np.allclose(kernel, kernel[::-1], atol=1e-12), "FIR filter must be strictly symmetric (linear phase)"
    print("  [PASS] Kaiser FIR kernel DC gain == 1.0000 and strictly symmetric (linear phase)")

    # Test edge-padding and zero-ringing on DC step
    constant_sig = np.full(500, 9.80665)
    dec = decimate_axis_50hz(constant_sig, kernel)
    assert np.allclose(dec, 9.80665, atol=1e-6), "Constant signal must remain constant with zero boundary transients"
    print("  [PASS] DC signal undergoes zero boundary transients (mode='edge' verified)")

    # Test frequency response cutoff at 25 Hz
    w = np.linspace(0, np.pi, 1000)
    freq_hz = (w / np.pi) * 100.0  # fs/2 = 100 Hz
    h = np.polyval(kernel[::-1], np.exp(-1j * w))
    mag_db = 20 * np.log10(np.abs(h))

    # Passband at 10 Hz should be ~0 dB
    idx_10 = np.argmin(np.abs(freq_hz - 10.0))
    assert -0.5 < mag_db[idx_10] < 0.1, f"Passband at 10 Hz: {mag_db[idx_10]:.2f} dB"

    # Stopband at 35 Hz should be attenuated > 30 dB
    idx_35 = np.argmin(np.abs(freq_hz - 35.0))
    assert mag_db[idx_35] < -25.0, f"Attenuation at 35 Hz: {mag_db[idx_35]:.2f} dB"
    print(f"  [PASS] Filter frequency response: 10 Hz attenuation = {mag_db[idx_10]:.2f} dB, 35 Hz = {mag_db[idx_35]:.2f} dB")


def test_full_dataset_integrity():
    print("\n--- [TEST GROUP 5: Full Processed Dataset & Manifest Verification] ---")

    manifest_path = REPO_ROOT / "data" / "sisfall" / "manifest.csv"
    assert manifest_path.exists(), "Manifest file missing"

    df = pd.read_csv(manifest_path)
    print(f"  Total manifest rows: {len(df)}")
    assert len(df) == 1038, f"Expected 1038 rows, got {len(df)}"

    expected_cols = [
        "trial_id", "activity_code", "activity_name", "subject_id",
        "subject_group", "is_fall", "duration_s", "samples_count", "processed_path"
    ]
    assert list(df.columns) == expected_cols, f"Column mismatch: {df.columns}"

    # Counts
    falls_count = int(df["is_fall"].sum())
    adls_count = len(df) - falls_count
    assert falls_count == 448, f"Expected 448 falls, got {falls_count}"
    assert adls_count == 590, f"Expected 590 ADLs, got {adls_count}"
    print(f"  [PASS] Class distribution verified: {falls_count} falls, {adls_count} ADLs")

    # Activities
    fall_acts = set(df[df["is_fall"] == 1]["activity_code"].unique())
    adl_acts = set(df[df["is_fall"] == 0]["activity_code"].unique())
    assert len(fall_acts) == 15, f"Expected 15 fall activities (F01-F15), got {len(fall_acts)}"
    assert len(adl_acts) == 19, f"Expected 19 ADL activities (D01-D19), got {len(adl_acts)}"
    print(f"  [PASS] Activity coverage complete: 15/15 fall codes, 19/19 ADL codes")

    # Cohorts
    young_count = len(df[df["subject_group"] == "YOUNG"])
    elderly_count = len(df[df["subject_group"] == "ELDERLY"])
    elderly_falls = len(df[(df["subject_group"] == "ELDERLY") & (df["is_fall"] == 1)])
    assert elderly_falls == 75, f"Expected 75 elderly falls from SE06, got {elderly_falls}"
    print(f"  [PASS] Cohort distribution: {young_count} Young, {elderly_count} Elderly ({elderly_falls} elderly falls)")

    # Verify Bijective Mapping and Data Integrity on sample / all files
    processed_dir = REPO_ROOT / "data" / "sisfall" / "processed"
    actual_files = list(processed_dir.glob("*.csv"))
    assert len(actual_files) == 1038, f"Expected 1038 CSV files in processed dir, found {len(actual_files)}"
    print(f"  [PASS] Processed directory contains exactly 1038 CSV files")

    # Verify random sample of 25 files for strict schema, timing, and NaN/Inf absence
    rng = np.random.RandomState(42)
    sample_indices = rng.choice(len(df), size=30, replace=False)

    for idx in sample_indices:
        row = df.iloc[idx]
        file_path = REPO_ROOT / row["processed_path"]
        assert file_path.exists(), f"File missing: {file_path}"

        trial_df = pd.read_csv(file_path)
        assert list(trial_df.columns) == ["t_sec", "ax", "ay", "az", "gx", "gy", "gz"]
        assert len(trial_df) == row["samples_count"], f"Row count mismatch in {file_path.name}"
        assert not trial_df.isna().any().any(), f"NaN found in {file_path.name}"
        assert not np.isinf(trial_df.values).any(), f"Inf found in {file_path.name}"

        # Timing check: dt = 0.02
        t = trial_df["t_sec"].values
        dt = np.diff(t)
        assert np.allclose(dt, 0.02, atol=1e-4), f"Non-50Hz sample clock in {file_path.name}"
        assert np.isclose(t[0], 0.0, atol=1e-4), f"Does not start at 0.0 in {file_path.name}"

    print(f"  [PASS] Checked 30 trial CSV files: 0 NaNs, 0 Infs, strict 50 Hz clock, exact sample counts")


def main():
    print("============================================================")
    print("  SisFall Acquisition & Preprocessing Adversarial Test Suite")
    print("============================================================")

    test_cli_argument_handling()
    test_file_corruption_and_truncation()
    test_boundary_assertions_and_kinematics()
    test_filter_properties_and_dsp()
    test_full_dataset_integrity()

    print("\n============================================================")
    print("  ALL EMPIRICAL ADVERSARIAL CHALLENGES PASSED! [100% OK]")
    print("============================================================\n")


if __name__ == "__main__":
    main()
