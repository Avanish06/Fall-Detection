# Project: FallGuard SisFall Benchmark & Comparative Evaluation

## Architecture
The system extends FallGuard v2 with an offline benchmark and comparative evaluation framework. It ingests both real embedded hardware pilot trial logs (`data/pilot1/trials.csv`) and public clinical motion datasets (SisFall), running FallGuard and baseline detectors to produce rigorous, non-fabricated comparative performance reports.

```
                    +------------------------------------------------+
                    |  SisFall Dataset Source                        |
                    |  (GitHub Mirror / Public Direct Zip)          |
                    +------------------------------------------------+
                                           |
                                           v
+-----------------------+     +------------------------------------------------+
|  Hardware Pilot Logs  |     |  M1: Acquisition & Preprocessing Engine       |
|  (data/pilot1/        |     |  - Download / Subset Caching                   |
|   trials.csv)         |     |  - Sensor Scaling (ADXL345 -> m/s², ITG3200)   |
|  [HARDWARE]           |     |  - Resample 200 Hz -> 50 Hz (MPU6050 match)    |
|  (ESP32, Pocket, P01) |     |  - Structured Storage (data/sisfall/)          |
+-----------------------+     +------------------------------------------------+
            |                                      |
            |                                      v
            |                 +------------------------------------------------+
            |                 |  M2: Algorithms & Evaluation Engine            |
            |                 |  - FallGuard Offline Runner (SMA+FSM+GNB)      |
            |                 |  - Baseline 1: SVM Threshold (Bourke 2007)     |
            |                 |  - Baseline 2: 2-Stage Drop+Impact (Noury 2007)|
            |                 |  - Baseline 3: Threshold+Gyro/Tilt (Li 2009)   |
            |                 +------------------------------------------------+
            |                                      |
            +-------------------+                  |
                                |                  |
                                v                  v
                    +------------------------------------------------+
                    |  M3: Comparative Metrics & Reporting Engine    |
                    |  - Sensitivity, Specificity, Precision, F1     |
                    |  - False Alarms / Hour, Detection Latency      |
                    |  - Wilson 95% Confidence Intervals             |
                    |  - Table 1: [HARDWARE] Pocket (1 Subject)      |
                    |  - Table 2: [OFFLINE] Waist (Multi-Subject)    |
                    |  - Strict Zero-Fabrication Enforcement         |
                    +------------------------------------------------+
```

## Code Layout
- `firmware/FallGuard_v2.ino`: Unmodified production firmware (50 Hz, MPU6050, ESP32).
- `test_fallguard.py`: Unmodified repository regression test suite.
- `tools/download_sisfall.py`: SisFall downloader supporting full dataset and fast representative subset.
- `tools/preprocess_sisfall.py`: Sensor scaling (ADXL345 to m/s², ITG3200 to °/s) and 200 Hz to 50 Hz polyphase decimation.
- `tools/baselines.py`: Implementation of literature baseline detectors (SVM, 2-Stage, Threshold+Gyro).
- `tools/run_benchmark.py`: Benchmark runner executing FallGuard simulator and baselines on SisFall and pilot data.
- `tools/generate_comparison_report.py`: Comparative metrics generator formatting separate `[HARDWARE]` and `[OFFLINE]` markdown tables.
- `data/sisfall/`: Structured preprocessed 50 Hz SisFall records and manifest.
- `docs/sisfall_benchmark_comparison.md`: Authoritative markdown comparison report.
- `tests/e2e/`: Opaque-box test suite created by E2E Testing Track.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| F1 | SisFall Dataset Acquisition | Download full or balanced representative subset from public source without login | M1 | ORIGINAL_REQUEST §R1 |
| F2 | Sensor Scale Conversion | Convert raw ADXL345 (col 1-3) to m/s² and ITG3200 (col 4-6) to °/s using exact author formulas | M1 | SisFall Readme / R1 |
| F3 | 200 Hz to 50 Hz Resampling | Anti-aliased decimation/resampling from 200 Hz to 50 Hz matching MPU6050 20 ms rate | M1 | ORIGINAL_REQUEST §R1 |
| F4 | Structured Preprocessed Storage | Clean file layout in `data/sisfall/` with metadata manifest cataloging subject, activity, type | M1 | ORIGINAL_REQUEST §AC1 |
| F5 | Literature Baseline Detectors | Bourke SVM threshold, Noury 2-stage drop+impact, and Li tri-modal threshold+gyro | M2 | ORIGINAL_REQUEST §R2 |
| F6 | Offline FallGuard Evaluation Engine | Faithful Python execution of FallGuard signal chain (5-point SMA, jerk, dual-path FSM, stillness) | M2 | ORIGINAL_REQUEST §R2 |
| F7 | Comparative Metrics Engine | Compute Sensitivity, Specificity, Precision, F1, False Alarms/hr, Latency, Wilson 95% CIs | M3 | ORIGINAL_REQUEST §R3 |
| F8 | Hardware Pilot Evaluation | Evaluate and report `data/pilot1/trials.csv` labeled strictly as `[HARDWARE]` | M3 | ORIGINAL_REQUEST §R4 |
| F9 | Offline SisFall Benchmark Evaluation | Evaluate and report resampled SisFall data labeled strictly as `[OFFLINE]` | M3 | ORIGINAL_REQUEST §R4 |
| F10 | Dual-Table Markdown Reporting | Generate comparison report with strictly separated `[HARDWARE]` and `[OFFLINE]` tables | M3 | ORIGINAL_REQUEST §R4 |
| F11 | Data Integrity Enforcement | Zero fabrication; all metrics originate directly from executed code on real files | M3, M4 | ORIGINAL_REQUEST §R5 |
| F12 | Regression Prevention | Existing test suite (`test_fallguard.py`) and hardware stubs pass without errors | M4 | ORIGINAL_REQUEST §AC4 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | SisFall Acquisition & Preprocessing | `tools/download_sisfall.py`, `tools/preprocess_sisfall.py`, `data/sisfall/` | none | DONE |
| M2 | Baseline Detectors & Evaluation Engine | `tools/baselines.py`, `tools/run_benchmark.py` offline runner | M1 | DONE |
| M3 | Comparative Metrics & Dual-Table Report | `tools/generate_comparison_report.py`, `docs/sisfall_benchmark_comparison.md` | M1, M2 | DONE |
| M4 | Final E2E Verification & Hardening | Pass 100% of E2E tests, run full regression checks, adversarial verification | M1, M2, M3, E2E Track | DONE |

## Interface Contracts

### M1 (Preprocessing) -> M2 (Evaluation Engine)
- Preprocessed SisFall files stored at: `data/sisfall/processed/<filename>.csv` or `.parquet` / `.npz`
- Manifest file: `data/sisfall/manifest.csv` containing columns:
  - `trial_id`: String (e.g. `F01_SA01_R01`)
  - `activity_code`: String (`F01` to `F15`, `D01` to `D19`)
  - `subject_id`: String (`SA01` to `SA23`, `SE01` to `SE15`)
  - `subject_group`: String (`YOUNG`, `ELDERLY`)
  - `is_fall`: Integer (1 for falls, 0 for ADLs)
  - `duration_s`: Float
  - `samples_count`: Integer (sampled at 50 Hz)
  - `processed_path`: Relative path to resampled file
- Signal columns in processed trial files:
  - `t_sec`: Time in seconds ($0.0, 0.02, 0.04, \dots$)
  - `ax`, `ay`, `az`: Accelerometer values in $\text{m/s}^2$ ($1g \approx 9.80665$)
  - `gx`, `gy`, `gz`: Gyroscope values in $^\circ/\text{s}$

### M2 (Detectors) -> M3 (Metrics & Reporting)
- Benchmark execution outputs structured results CSV: `results/offline_sisfall_benchmark.csv` and `results/hardware_pilot_benchmark.csv`
- Result schema:
  - `dataset`: String (`[HARDWARE]` or `[OFFLINE]`)
  - `detector`: String (`FallGuard_v2`, `Baseline_SVM`, `Baseline_TwoStage`, `Baseline_GyroTilt`)
  - `trial_id`: String
  - `is_fall`: Integer (0 or 1)
  - `detected`: Integer (0 or 1)
  - `latency_ms`: Float or null
  - `duration_s`: Float
  - `false_alarm`: Integer (0 or 1)

### M3 (Metrics) -> Output Report
- Report file: `docs/sisfall_benchmark_comparison.md`
- Markdown document containing:
  - Section 1: Executive Summary
  - Section 2: Methodology & Sensor Placement Comparison (Pocket vs Waist)
  - Section 3: `[HARDWARE]` ESP32 Pilot Trial Benchmark Table (Participant P01, Pocket, 50 Hz)
  - Section 4: `[OFFLINE]` SisFall Benchmark Table (Multi-Subject, Waist, 50 Hz)
  - Section 5: Comparative Analysis & Kinematic Discussion
  - Section 6: Data Integrity & Provenance Attestation
