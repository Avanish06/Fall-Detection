"""
tools/baselines.py
==================
Literature Baseline Fall Detectors and Unified Evaluation Interfaces for FallGuard v2.

Implements three authoritative literature baseline algorithms and FallGuard v2 offline simulation:
1. BourkeSVMDetector: Signal Vector Magnitude Threshold (Bourke et al. 2007)
2. TwoStageThresholdDetector: Free-Fall Dip + Impact Deceleration (Noury et al. 2007, Kangas et al. 2008)
3. GyroTiltDetector: Tri-Modal Threshold + Stillness / Gyroscope (Li et al. 2009, Bagalà et al. 2012)
4. FallGuardOfflineDetector: Cycle-accurate FallGuard v2 firmware FSM (SMA + Dual-Path + Stillness + GNB)

All detectors inherit from BaseFallDetector and implement:
- reset(): Clears internal states and buffers.
- step(t_sec, ax, ay, az, gx, gy, gz): Process a single 50 Hz streaming sample.
- process_trial(t_sec, ax, ay, az, gx, gy, gz, ...): Process full trial arrays and compute latency.
- evaluate_dataframe(df, ...): Ingest a pandas DataFrame with columns [t_sec, ax, ay, az, gx, gy, gz].
"""

import abc
from dataclasses import dataclass, field
import math
from typing import Dict, Any, Tuple, Optional, List, Union
import numpy as np
import pandas as pd

# Physical Constants (NIST standard)
STANDARD_GRAVITY = 9.80665  # m/s^2 per 1.0g


# ============================================================================
# 0. Unified Data Structures & Abstract Base Class
# ============================================================================

@dataclass
class DetectionResult:
    """Standardized trial detection outcome container."""
    detector: str
    detected: int
    trigger_time: Optional[float] = None
    trigger_index: Optional[int] = None
    latency_ms: Optional[float] = None
    latency_ref: Optional[str] = None
    triggers_count: int = 0
    duration_s: float = 0.0
    samples_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

    def to_dict(self) -> Dict[str, Any]:
        """Convert container to standard dictionary."""
        return {
            "detector": self.detector,
            "detected": self.detected,
            "trigger_time": self.trigger_time,
            "trigger_index": self.trigger_index,
            "latency_ms": self.latency_ms,
            "latency_ref": self.latency_ref,
            "triggers_count": self.triggers_count,
            "duration_s": self.duration_s,
            "samples_count": self.samples_count,
            "metadata": self.metadata,
        }


class BaseFallDetector(abc.ABC):
    """Abstract base class for all streaming fall detectors."""

    def __init__(self, name: str, sample_rate_hz: int = 50):
        self.name = name
        self.sample_rate_hz = sample_rate_hz
        self.dt_sec = 1.0 / sample_rate_hz
        self.triggered = False
        self.first_trigger_time: Optional[float] = None
        self.first_trigger_index: Optional[int] = None
        self.trigger_count = 0

    @abc.abstractmethod
    def reset(self) -> None:
        """Reset internal buffers, FSM state, and detection flags."""
        self.triggered = False
        self.first_trigger_time = None
        self.first_trigger_index = None
        self.trigger_count = 0

    @abc.abstractmethod
    def step(
        self,
        t_sec: float,
        ax: float,
        ay: float,
        az: float,
        gx: float = 0.0,
        gy: float = 0.0,
        gz: float = 0.0,
    ) -> Tuple[int, Optional[float], Dict[str, Any]]:
        """
        Process a single streaming IMU sample at 50 Hz.

        Parameters:
            t_sec: Timestamp in seconds.
            ax, ay, az: Triaxial acceleration in m/s^2 (1g ≈ 9.80665).
            gx, gy, gz: Triaxial angular velocity in deg/s (or °/s).

        Returns:
            Tuple of:
                decision (int): 1 if fall is confirmed on this sample, 0 otherwise.
                trigger_timestamp (float or None): t_sec if decision == 1, else None.
                state_metadata (dict): Internal diagnostics.
        """
        pass

    def process_trial(
        self,
        t_sec: np.ndarray,
        ax: np.ndarray,
        ay: np.ndarray,
        az: np.ndarray,
        gx: Optional[np.ndarray] = None,
        gy: Optional[np.ndarray] = None,
        gz: Optional[np.ndarray] = None,
        impact_peak_time: Optional[float] = None,
        onset_time: Optional[float] = None,
    ) -> DetectionResult:
        """
        Executes sample-by-sample streaming simulation across full trial arrays.

        Parameters:
            t_sec: Array of sample timestamps (s).
            ax, ay, az: Arrays of triaxial acceleration (m/s^2).
            gx, gy, gz: Arrays of triaxial angular velocity (deg/s).
            impact_peak_time: Known or estimated timestamp of impact deceleration peak.
            onset_time: Known timestamp of movement onset (if available).

        Returns:
            DetectionResult dataclass.
        """
        self.reset()
        n_samples = len(t_sec)
        if gx is None:
            gx = np.zeros(n_samples, dtype=np.float64)
        if gy is None:
            gy = np.zeros(n_samples, dtype=np.float64)
        if gz is None:
            gz = np.zeros(n_samples, dtype=np.float64)

        # Estimate impact peak from raw acceleration magnitude if not provided
        if impact_peak_time is None and n_samples > 0:
            mags = np.sqrt(ax * ax + ay * ay + az * az)
            peak_idx = int(np.argmax(mags))
            impact_peak_time = float(t_sec[peak_idx])

        first_trig_t = None
        first_trig_idx = None
        total_triggers = 0
        last_metadata: Dict[str, Any] = {}

        for i in range(n_samples):
            dec, trig_t, meta = self.step(
                float(t_sec[i]),
                float(ax[i]),
                float(ay[i]),
                float(az[i]),
                float(gx[i]),
                float(gy[i]),
                float(gz[i]),
            )
            last_metadata = meta
            if dec == 1:
                total_triggers += 1
                if first_trig_t is None:
                    first_trig_t = trig_t if trig_t is not None else float(t_sec[i])
                    first_trig_idx = i

        detected = 1 if total_triggers > 0 else 0
        latency_ms = None
        latency_ref = None

        if detected == 1 and first_trig_t is not None:
            if onset_time is not None and onset_time > 0:
                latency_ms = round((first_trig_t - onset_time) * 1000.0, 1)
                latency_ref = "onset_marker"
            elif impact_peak_time is not None:
                latency_ms = round((first_trig_t - impact_peak_time) * 1000.0, 1)
                latency_ref = "impact_peak"

        duration_s = round(float(t_sec[-1] - t_sec[0]), 3) if n_samples > 0 else 0.0

        return DetectionResult(
            detector=self.name,
            detected=detected,
            trigger_time=first_trig_t,
            trigger_index=first_trig_idx,
            latency_ms=latency_ms,
            latency_ref=latency_ref,
            triggers_count=total_triggers,
            duration_s=duration_s,
            samples_count=n_samples,
            metadata=last_metadata,
        )

    def evaluate_dataframe(
        self,
        df: pd.DataFrame,
        impact_peak_time: Optional[float] = None,
        onset_time: Optional[float] = None,
    ) -> DetectionResult:
        """Helper to run trial evaluation on a pandas DataFrame."""
        return self.process_trial(
            t_sec=df["t_sec"].to_numpy(dtype=np.float64),
            ax=df["ax"].to_numpy(dtype=np.float64),
            ay=df["ay"].to_numpy(dtype=np.float64),
            az=df["az"].to_numpy(dtype=np.float64),
            gx=df["gx"].to_numpy(dtype=np.float64) if "gx" in df.columns else None,
            gy=df["gy"].to_numpy(dtype=np.float64) if "gy" in df.columns else None,
            gz=df["gz"].to_numpy(dtype=np.float64) if "gz" in df.columns else None,
            impact_peak_time=impact_peak_time,
            onset_time=onset_time,
        )


# ============================================================================
# 1. Baseline 1: Basic Acceleration Magnitude Threshold (Bourke et al. 2007)
# ============================================================================

class BourkeSVMDetector(BaseFallDetector):
    """
    Bourke et al. (2007) Signal Vector Magnitude (SVM) Upper Fall Threshold Detector.
    Instantaneous thresholding on Euclidean norm of acceleration with refractory period.
    Standard upper fall threshold for trunk/waist sensors is 3.0g (~29.42 m/s²).
    """

    def __init__(
        self,
        uft_g: float = 3.0,
        refractory_s: float = 2.0,
        sample_rate_hz: int = 50,
    ):
        super().__init__(name="Baseline_SVM", sample_rate_hz=sample_rate_hz)
        self.uft_g = uft_g
        self.uft_ms2 = uft_g * STANDARD_GRAVITY
        self.refractory_samples = int(round(refractory_s * sample_rate_hz))
        self.cooldown = 0

    def reset(self) -> None:
        super().reset()
        self.cooldown = 0

    def step(
        self,
        t_sec: float,
        ax: float,
        ay: float,
        az: float,
        gx: float = 0.0,
        gy: float = 0.0,
        gz: float = 0.0,
    ) -> Tuple[int, Optional[float], Dict[str, Any]]:
        if self.cooldown > 0:
            self.cooldown -= 1

        amag = math.sqrt(ax * ax + ay * ay + az * az)
        decision = 0
        trig_t = None

        if amag >= self.uft_ms2 and self.cooldown == 0:
            decision = 1
            trig_t = t_sec
            self.cooldown = self.refractory_samples
            self.trigger_count += 1
            if not self.triggered:
                self.triggered = True
                self.first_trigger_time = t_sec

        return decision, trig_t, {
            "amag": amag,
            "state": "COOLDOWN" if self.cooldown > 0 else "ARMED",
            "cooldown": self.cooldown,
        }


# ============================================================================
# 2. Baseline 2: Two-Stage Sequential Threshold (Noury 2007, Kangas 2008)
# ============================================================================

class TwoStageThresholdDetector(BaseFallDetector):
    """
    Two-Stage Sequential Threshold Fall Detector (Noury et al. 2007, Kangas et al. 2008).
    Demands a sustained free-fall weightlessness dip (< LFT for >= min_ff_ms)
    followed by an impact deceleration peak (>= UFT) within max_wait_ms.
    Nominal literature thresholds: LFT = 0.6g (~5.88 m/s²), UFT = 2.0g (~19.61 m/s²).
    """

    def __init__(
        self,
        lft_g: float = 0.60,
        uft_g: float = 2.00,
        min_ff_ms: int = 40,
        max_wait_ms: int = 1400,
        refractory_s: float = 2.0,
        sample_rate_hz: int = 50,
    ):
        super().__init__(name="Baseline_TwoStage", sample_rate_hz=sample_rate_hz)
        self.lft_ms2 = lft_g * STANDARD_GRAVITY
        self.uft_ms2 = uft_g * STANDARD_GRAVITY
        self.min_ff_samples = max(1, int(round(min_ff_ms * sample_rate_hz / 1000.0)))
        self.max_wait_samples = int(round(max_wait_ms * sample_rate_hz / 1000.0))
        self.refractory_samples = int(round(refractory_s * sample_rate_hz))

        self.state = "NORMAL"
        self.ff_count = 0
        self.wait_count = 0
        self.cooldown = 0

    def reset(self) -> None:
        super().reset()
        self.state = "NORMAL"
        self.ff_count = 0
        self.wait_count = 0
        self.cooldown = 0

    def step(
        self,
        t_sec: float,
        ax: float,
        ay: float,
        az: float,
        gx: float = 0.0,
        gy: float = 0.0,
        gz: float = 0.0,
    ) -> Tuple[int, Optional[float], Dict[str, Any]]:
        if self.cooldown > 0:
            self.cooldown -= 1

        amag = math.sqrt(ax * ax + ay * ay + az * az)
        decision = 0
        trig_t = None

        if self.state == "NORMAL":
            if amag < self.lft_ms2:
                self.state = "FREE_FALL"
                self.ff_count = 1

        elif self.state == "FREE_FALL":
            if amag < self.lft_ms2:
                self.ff_count += 1
                if self.ff_count > self.max_wait_samples:
                    self.state = "NORMAL"
            else:
                if self.ff_count >= self.min_ff_samples:
                    self.state = "IMPACT_WAIT"
                    self.wait_count = 0
                    # Handle immediate sharp rise crossing UFT on exit sample
                    if amag >= self.uft_ms2 and self.cooldown == 0:
                        decision = 1
                        trig_t = t_sec
                        self.cooldown = self.refractory_samples
                        self.state = "NORMAL"
                        self.trigger_count += 1
                        if not self.triggered:
                            self.triggered = True
                            self.first_trigger_time = t_sec
                else:
                    self.state = "NORMAL"

        elif self.state == "IMPACT_WAIT":
            self.wait_count += 1
            if amag >= self.uft_ms2 and self.cooldown == 0:
                decision = 1
                trig_t = t_sec
                self.cooldown = self.refractory_samples
                self.state = "NORMAL"
                self.trigger_count += 1
                if not self.triggered:
                    self.triggered = True
                    self.first_trigger_time = t_sec
            elif self.wait_count > self.max_wait_samples:
                self.state = "NORMAL"

        return decision, trig_t, {
            "amag": amag,
            "state": self.state,
            "ff_count": self.ff_count,
            "wait_count": self.wait_count,
            "cooldown": self.cooldown,
        }


# ============================================================================
# 3. Baseline 3: Threshold + Stillness / Gyroscope (Li 2009, Bagalà 2012)
# ============================================================================

class GyroTiltDetector(BaseFallDetector):
    """
    Tri-Modal Threshold + Stillness / Gyroscope Detector (Li et al. 2009, Bagalà et al. 2012).
    Evaluates:
    1. Dynamic trigger: Coincident acceleration impact (>= 2.0g) and angular velocity rotation
       (>= 200 °/s) within ±500 ms.
    2. Settle period (settle_s, default 1.0 s).
    3. Post-impact stillness (still_s, default 1.5 s) verifying acceleration band
       [7.5, 12.0] m/s^2 or SD <= 1.8 m/s^2.
    4. Posture tilt change: delta theta >= tilt_thresh (default 50 deg).
    """

    def __init__(
        self,
        a_thresh_g: float = 2.0,
        w_thresh_dps: float = 200.0,
        coincident_window_ms: int = 500,
        settle_s: float = 1.0,
        still_s: float = 1.5,
        tilt_thresh_deg: float = 50.0,
        still_sd_thresh: float = 1.8,
        still_min_ms2: float = 7.5,
        still_max_ms2: float = 12.0,
        post_impact_mode: str = "stillness_or_tilt",
        sample_rate_hz: int = 50,
    ):
        super().__init__(name="Baseline_GyroTilt", sample_rate_hz=sample_rate_hz)
        self.a_thresh_ms2 = a_thresh_g * STANDARD_GRAVITY
        self.w_thresh_dps = w_thresh_dps
        self.coincident_samples = max(1, int(round(coincident_window_ms * sample_rate_hz / 1000.0)))
        self.settle_samples = int(round(settle_s * sample_rate_hz))
        self.still_samples = int(round(still_s * sample_rate_hz))
        self.tilt_thresh_rad = math.radians(tilt_thresh_deg)
        self.tilt_thresh_deg = tilt_thresh_deg
        self.still_sd_thresh = still_sd_thresh
        self.still_min_ms2 = still_min_ms2
        self.still_max_ms2 = still_max_ms2
        self.post_impact_mode = post_impact_mode

        # Buffers & state
        self.pre_buf: List[Tuple[float, float, float]] = []
        self.post_buf: List[Tuple[float, float, float]] = []
        self.state = "MONITOR"
        self.timer = 0
        self.v_pre = np.array([0.0, 0.0, 1.0])
        self.impact_time: Optional[float] = None
        self.last_tilt_deg = 0.0
        self.last_still_sd = 0.0
        self.last_gyro_breach_sample = -999999
        self.sample_idx = 0

    def reset(self) -> None:
        super().reset()
        self.pre_buf.clear()
        self.post_buf.clear()
        self.state = "MONITOR"
        self.timer = 0
        self.v_pre = np.array([0.0, 0.0, 1.0])
        self.impact_time = None
        self.last_tilt_deg = 0.0
        self.last_still_sd = 0.0
        self.last_gyro_breach_sample = -999999
        self.sample_idx = 0

    def step(
        self,
        t_sec: float,
        ax: float,
        ay: float,
        az: float,
        gx: float = 0.0,
        gy: float = 0.0,
        gz: float = 0.0,
    ) -> Tuple[int, Optional[float], Dict[str, Any]]:
        self.sample_idx += 1
        amag = math.sqrt(ax * ax + ay * ay + az * az)
        wmag = math.sqrt(gx * gx + gy * gy + gz * gz)

        if wmag >= self.w_thresh_dps:
            self.last_gyro_breach_sample = self.sample_idx

        # Maintain 1.0 s sliding history of acceleration for pre-impact baseline posture
        self.pre_buf.append((ax, ay, az))
        if len(self.pre_buf) > self.sample_rate_hz:
            self.pre_buf.pop(0)

        decision = 0
        trig_t = None

        if self.state == "MONITOR":
            if amag >= self.a_thresh_ms2:
                # Check if gyro breached within recent coincident window (-500 ms)
                if (self.sample_idx - self.last_gyro_breach_sample) <= self.coincident_samples:
                    self._arm_settling(t_sec)
                else:
                    # Allow up to +500 ms for gyro rotation to occur
                    self.state = "AWAIT_GYRO"
                    self.timer = self.coincident_samples
                    self.impact_time = t_sec

        elif self.state == "AWAIT_GYRO":
            self.timer -= 1
            if wmag >= self.w_thresh_dps:
                self._arm_settling(self.impact_time or t_sec)
            elif self.timer <= 0:
                self.state = "MONITOR"

        elif self.state == "SETTLING":
            self.timer -= 1
            if self.timer <= 0:
                self.state = "STILLNESS_CHECK"
                self.timer = self.still_samples
                self.post_buf.clear()

        elif self.state == "STILLNESS_CHECK":
            self.post_buf.append((ax, ay, az))
            self.timer -= 1
            if self.timer <= 0:
                arr_post = np.array(self.post_buf)
                mags = np.linalg.norm(arr_post, axis=1)
                self.last_still_sd = float(np.std(mags))
                still_band_pass = bool(
                    np.all((mags >= self.still_min_ms2) & (mags <= self.still_max_ms2))
                )
                still_sd_pass = bool(self.last_still_sd <= self.still_sd_thresh)
                still_pass = still_band_pass or still_sd_pass

                mean_post = np.mean(arr_post, axis=0)
                norm_post = float(np.linalg.norm(mean_post))
                if norm_post > 1e-4:
                    v_post_norm = mean_post / norm_post
                else:
                    v_post_norm = np.array([0.0, 0.0, 1.0])

                cos_angle = float(np.clip(np.dot(self.v_pre, v_post_norm), -1.0, 1.0))
                tilt_rad = math.acos(cos_angle)
                self.last_tilt_deg = math.degrees(tilt_rad)
                tilt_pass = bool(tilt_rad >= self.tilt_thresh_rad)

                confirmed = False
                if self.post_impact_mode == "stillness_or_tilt":
                    confirmed = still_pass or tilt_pass
                elif self.post_impact_mode == "stillness_and_tilt":
                    confirmed = still_pass and tilt_pass
                elif self.post_impact_mode == "tilt_only":
                    confirmed = tilt_pass
                elif self.post_impact_mode == "stillness_only":
                    confirmed = still_pass

                if confirmed:
                    decision = 1
                    trig_t = t_sec
                    self.trigger_count += 1
                    if not self.triggered:
                        self.triggered = True
                        self.first_trigger_time = t_sec

                self.state = "MONITOR"
                self.post_buf.clear()

        return decision, trig_t, {
            "amag": amag,
            "wmag": wmag,
            "state": self.state,
            "tilt_deg": self.last_tilt_deg,
            "still_sd": self.last_still_sd,
        }

    def _arm_settling(self, impact_t: float) -> None:
        """Helper to snapshot baseline posture and start settling phase."""
        baseline_slice = self.pre_buf[: max(1, len(self.pre_buf) // 2)]
        arr_pre = np.array(baseline_slice)
        mean_pre = np.mean(arr_pre, axis=0)
        norm_pre = float(np.linalg.norm(mean_pre))
        if norm_pre > 1e-4:
            self.v_pre = mean_pre / norm_pre
        else:
            self.v_pre = np.array([0.0, 0.0, 1.0])

        self.impact_time = impact_t
        self.state = "SETTLING"
        self.timer = self.settle_samples


# ============================================================================
# 4. FallGuard v2 Offline Detector (Cycle-Accurate Firmware FSM Simulation)
# ============================================================================

class FallGuardOfflineDetector(BaseFallDetector):
    """
    Offline simulation of FallGuard v2 production firmware signal chain.
    Directly reflects firmware/FallGuard_v2.ino:
    - 5-point SMA smoothing on raw magnitude (initialized to 9.81 m/s²).
    - Live jerk: |smooth[k] - smooth[k-1]| / dt_sec.
    - 25-sample (500 ms) snapshot ring buffer with 150 ms rolling peak jerk.
    - Nominal calibrated thresholds: IMPACT_THRESHOLD = 11.5 m/s², JERK_THRESHOLD = 220.0 m/s³.
    - Dual-path FSM:
        Path 1: Free-fall dip (< 5.0 m/s² for >= 40 ms) + impact (>= 11.5 m/s² & jerk >= 220 m/s³).
        Path 2: Direct high-jerk (jerk >= 280.0 m/s³ & smooth >= 11.5 m/s²).
    - Adaptive stillness window Ts in [1500, 3000] ms based on windowed energy E_k.
    - Lifelong Gaussian Naive Bayes (GNB) with cold-start lockout prevention.
    """

    def __init__(
        self,
        sample_rate_hz: int = 50,
        use_path2: bool = True,
        use_adaptive: bool = True,
        use_gnb: bool = True,
        impact_threshold: float = 11.5,
        jerk_threshold: float = 220.0,
        mag_path2: float = 11.5,
        jerk_path2: float = 280.0,
        free_fall_mag: float = 5.0,
        ff_min_ms: int = 40,
        ff_window_ms: int = 1400,
        still_low: float = 7.5,
        still_high: float = 12.0,
        still_ms: int = 2200,
    ):
        super().__init__(name="FallGuard_v2", sample_rate_hz=sample_rate_hz)
        self.use_path2 = use_path2
        self.use_adaptive = use_adaptive
        self.use_gnb = use_gnb

        # Firmware constants
        self.FREE_FALL_MAG = free_fall_mag
        self.FF_MIN_MS = ff_min_ms
        self.FF_WINDOW_MS = ff_window_ms
        self.IMPACT_THRESHOLD = impact_threshold
        self.JERK_THRESHOLD = jerk_threshold
        self.MAG_PATH2 = mag_path2
        self.JERK_PATH2 = jerk_path2
        self.STILL_LOW = still_low
        self.STILL_HIGH = still_high
        self.STILL_MS = still_ms
        self.SMA_SIZE = 5
        self.WINDOW_SIZE = 25

        # State machine variables
        self.sma_buf: List[float] = [9.81] * self.SMA_SIZE
        self.sma_sum = 9.81 * self.SMA_SIZE
        self.sma_head = 0

        self.win_mag = [9.81] * self.WINDOW_SIZE
        self.win_jerk = [0.0] * self.WINDOW_SIZE
        self.win_head = 0
        self.win_full = False

        self.live_mag = 9.81
        self.live_jerk = 0.0
        self.state = "NORMAL"
        self.in_ff_dip = False
        self.ff_start_ms = 0
        self.impact_time_ms = 0
        self.confirmed_time_ms = 0
        self.impact_peak_mag = 0.0
        self.impact_peak_time_ms = 0
        self.stillness_accum_ms = 0
        self.trigger_path: Optional[str] = None
        self.E_k = 1.0
        self.theta = 0.65
        self.events: List[Tuple[int, str]] = []

        # Welford class stats (GNB)
        self.fall_count = 0
        self.non_fall_count = 0
        self.fall_mean = [0.0] * 6
        self.fall_var = [1.0] * 6
        self.non_fall_mean = [0.0] * 6
        self.non_fall_var = [1.0] * 6

    def reset(self) -> None:
        super().reset()
        self.sma_buf = [9.81] * self.SMA_SIZE
        self.sma_sum = 9.81 * self.SMA_SIZE
        self.sma_head = 0

        self.win_mag = [9.81] * self.WINDOW_SIZE
        self.win_jerk = [0.0] * self.WINDOW_SIZE
        self.win_head = 0
        self.win_full = False

        self.live_mag = 9.81
        self.live_jerk = 0.0
        self.state = "NORMAL"
        self.in_ff_dip = False
        self.ff_start_ms = 0
        self.impact_time_ms = 0
        self.confirmed_time_ms = 0
        self.impact_peak_mag = 0.0
        self.impact_peak_time_ms = 0
        self.stillness_accum_ms = 0
        self.trigger_path = None
        self.E_k = 1.0
        self.events.clear()

    def _apply_sma(self, raw_mag: float) -> float:
        """5-sample Simple Moving Average filter per FallGuard_v2.ino:250-256."""
        self.sma_sum -= self.sma_buf[self.sma_head]
        self.sma_buf[self.sma_head] = raw_mag
        self.sma_sum += raw_mag
        self.sma_head = (self.sma_head + 1) % self.SMA_SIZE
        return self.sma_sum / float(self.SMA_SIZE)

    def _push_window(self, mag: float, jerk: float) -> None:
        """Push smoothed magnitude and jerk to snapshot ring buffer."""
        self.win_mag[self.win_head] = mag
        self.win_jerk[self.win_head] = jerk
        self.win_head = (self.win_head + 1) % self.WINDOW_SIZE
        if self.win_head == 0:
            self.win_full = True

    def _get_recent_max_jerk(self, ms: int = 150) -> float:
        """Rolling peak jerk over recent ms window (7 samples at 50 Hz)."""
        count = int(ms / 20)
        avail = self.WINDOW_SIZE if self.win_full else self.win_head
        c = min(count, avail)
        max_j = 0.0
        for k in range(c):
            idx = (self.win_head - 1 - k + self.WINDOW_SIZE * 2) % self.WINDOW_SIZE
            if self.win_jerk[idx] > max_j:
                max_j = self.win_jerk[idx]
        return max_j

    def _compute_T_s(self) -> float:
        """Compute stillness duration requirement (adaptive or fixed)."""
        if self.use_adaptive:
            return 1500.0 + 1500.0 * (self.E_k / (self.E_k + 1.0))
        return float(self.STILL_MS)

    def _extract_features(self) -> List[float]:
        """Extract 6-D impact snapshot features per FallGuard_v2.ino:385-411."""
        n = self.WINDOW_SIZE if self.win_full else self.win_head
        if n < 2:
            return [0.0] * 6
        mag_slice = self.win_mag[:n]
        jerk_slice = self.win_jerk[:n]
        mean_val = float(np.mean(mag_slice))
        max_val = float(np.max(mag_slice))
        min_val = float(np.min(mag_slice))
        var_val = float(np.var(mag_slice, ddof=1)) if n > 1 else 0.0
        ratio = max_val / max(min_val, 0.1)
        max_jerk = float(np.max(jerk_slice))
        return [mean_val, max_val, min_val, var_val, ratio, max_jerk]

    def _classify_candidate(self, feats: List[float]) -> float:
        """Cold-start safe GNB classification per FallGuard_v2.ino:446-456."""
        if not self.use_gnb or (self.fall_count < 3 or self.non_fall_count < 3):
            # Cold-start immunity: untrainable devices default to 0.75
            return 0.75

        total = float(self.fall_count + self.non_fall_count + 1)
        log_fall = math.log(float(self.fall_count) / total + 1e-9)
        log_non_fall = math.log(float(self.non_fall_count) / total + 1e-9)

        for i in range(6):
            v_f = max(self.fall_var[i], 0.01)
            d_f = feats[i] - self.fall_mean[i]
            log_fall -= 0.5 * math.log(2.0 * math.pi * v_f) + (d_f * d_f) / (2.0 * v_f)

            v_nf = max(self.non_fall_var[i], 0.01)
            d_nf = feats[i] - self.non_fall_mean[i]
            log_non_fall -= 0.5 * math.log(2.0 * math.pi * v_nf) + (d_nf * d_nf) / (2.0 * v_nf)

        max_l = max(log_fall, log_non_fall)
        exp_f = math.exp(log_fall - max_l)
        exp_nf = math.exp(log_non_fall - max_l)
        return exp_f / (exp_f + exp_nf + 1e-9)

    def step(
        self,
        t_sec: float,
        ax: float,
        ay: float,
        az: float,
        gx: float = 0.0,
        gy: float = 0.0,
        gz: float = 0.0,
    ) -> Tuple[int, Optional[float], Dict[str, Any]]:
        t_ms = int(round(t_sec * 1000.0))
        raw_mag = math.sqrt(ax * ax + ay * ay + az * az)

        # 5-point SMA smoothing
        smooth = self._apply_sma(raw_mag)

        # Live sample-to-sample jerk (dt = 0.02 s)
        jerk = abs(smooth - self.live_mag) / self.dt_sec
        self.live_mag = smooth
        self.live_jerk = jerk
        self._push_window(smooth, jerk)

        # Windowed Energy update
        self.E_k = 0.98 * self.E_k + 0.02 * ((smooth - 9.81) ** 2)
        t_s = self._compute_T_s()
        is_still = (self.STILL_LOW <= smooth <= self.STILL_HIGH)

        decision = 0
        trig_t = None

        if self.state == "NORMAL":
            # Path 2: Direct High-Jerk Trigger (Slip/Trip)
            if self.use_path2 and jerk >= self.JERK_PATH2 and smooth >= self.MAG_PATH2:
                self.state = "IMPACT"
                self.impact_time_ms = t_ms
                self.impact_peak_mag = smooth
                self.impact_peak_time_ms = t_ms
                self.stillness_accum_ms = 0
                self.in_ff_dip = False
                self.trigger_path = "PATH2_DIRECT_JERK"
                self.events.append((t_ms, "IMPACT_PATH2"))

            # Path 1: Free-Fall Dip Entry
            elif smooth < self.FREE_FALL_MAG:
                if not self.in_ff_dip:
                    self.in_ff_dip = True
                    self.ff_start_ms = t_ms
                if t_ms - self.ff_start_ms >= self.FF_MIN_MS:
                    self.state = "FREE_FALL"
                    self.trigger_path = "PATH1_DIP_IMPACT"
                    self.events.append((t_ms, "FREE_FALL_ENTER"))
            else:
                self.in_ff_dip = False

        elif self.state == "FREE_FALL":
            rec_jerk = self._get_recent_max_jerk(150)
            if smooth >= self.IMPACT_THRESHOLD and (jerk >= self.JERK_THRESHOLD or rec_jerk >= self.JERK_THRESHOLD):
                self.state = "IMPACT"
                self.impact_time_ms = t_ms
                self.impact_peak_mag = smooth
                self.impact_peak_time_ms = t_ms
                self.stillness_accum_ms = 0
                self.in_ff_dip = False
                self.events.append((t_ms, "IMPACT_PATH1"))
            elif t_ms - self.ff_start_ms >= self.FF_WINDOW_MS:
                self.state = "NORMAL"
                self.in_ff_dip = False
                self.events.append((t_ms, "FF_TIMEOUT_NO_IMPACT"))

        elif self.state == "IMPACT":
            if smooth > self.impact_peak_mag:
                self.impact_peak_mag = smooth
                self.impact_peak_time_ms = t_ms

            # Stillness accumulation
            if is_still:
                self.stillness_accum_ms += 20
                if self.stillness_accum_ms >= t_s:
                    if self.use_gnb:
                        self.state = "CANDIDATE_FALL"
                    else:
                        self.state = "FALL_CONFIRMED"
                        self.confirmed_time_ms = t_ms
                        self.events.append((t_ms, "FALL_CONFIRMED"))
                        decision = 1
                        trig_t = t_sec
                        self.trigger_count += 1
                        if not self.triggered:
                            self.triggered = True
                            self.first_trigger_time = t_sec
                    self.stillness_accum_ms = 0
            else:
                self.stillness_accum_ms = 0

            # Movement failsafe: 5 s active movement post-impact resets
            if t_ms - self.impact_time_ms >= 5000 and not is_still:
                self.state = "NORMAL"
                self.events.append((t_ms, "IMPACT_TIMEOUT_MOVING"))

        elif self.state == "CANDIDATE_FALL":
            feats = self._extract_features()
            conf = self._classify_candidate(feats)
            classifier_trained = (self.fall_count >= 3 and self.non_fall_count >= 3)

            # Cold-start immunity gate
            if not classifier_trained or conf >= self.theta:
                self.state = "FALL_CONFIRMED"
                self.confirmed_time_ms = t_ms
                self.events.append((t_ms, f"FALL_CONFIRMED,conf={conf:.3f}"))
                decision = 1
                trig_t = t_sec
                self.trigger_count += 1
                if not self.triggered:
                    self.triggered = True
                    self.first_trigger_time = t_sec
            else:
                self.theta = min(0.85, self.theta + 0.02 * 0.5)
                self.state = "NORMAL"
                self.events.append((t_ms, f"CLASSIFIER_REJECTED,conf={conf:.3f}"))

        elif self.state == "FALL_CONFIRMED":
            # Once confirmed, alert is sent; stay armed or in alert
            pass

        return decision, trig_t, {
            "smooth": smooth,
            "jerk": jerk,
            "state": self.state,
            "stillness_accum_ms": self.stillness_accum_ms,
            "trigger_path": self.trigger_path,
        }


# ============================================================================
# 5. Detector Registry & Factory
# ============================================================================

DETECTOR_REGISTRY: Dict[str, Any] = {
    "Baseline_SVM": BourkeSVMDetector,
    "Baseline_TwoStage": TwoStageThresholdDetector,
    "Baseline_GyroTilt": GyroTiltDetector,
    "FallGuard_v2": FallGuardOfflineDetector,
}

DETECTOR_ALIASES: Dict[str, str] = {
    "svm": "Baseline_SVM",
    "bourke": "Baseline_SVM",
    "baseline_svm": "Baseline_SVM",
    "twostage": "Baseline_TwoStage",
    "noury": "Baseline_TwoStage",
    "kangas": "Baseline_TwoStage",
    "baseline_twostage": "Baseline_TwoStage",
    "gyrotilt": "Baseline_GyroTilt",
    "li": "Baseline_GyroTilt",
    "bagala": "Baseline_GyroTilt",
    "baseline_gyrotilt": "Baseline_GyroTilt",
    "fallguard": "FallGuard_v2",
    "fallguard_v2": "FallGuard_v2",
}


def get_detector(name: str, **kwargs) -> BaseFallDetector:
    """
    Factory function to instantiate fall detector by canonical name or alias.

    Parameters:
        name: Name or alias of detector (e.g. 'FallGuard_v2', 'svm', 'twostage', 'gyrotilt').
        **kwargs: Optional configuration parameters passed to constructor.

    Returns:
        Instance of BaseFallDetector.
    """
    if name in DETECTOR_REGISTRY:
        return DETECTOR_REGISTRY[name](**kwargs)

    lower_key = name.strip().lower()
    if lower_key in DETECTOR_ALIASES:
        canonical_name = DETECTOR_ALIASES[lower_key]
        return DETECTOR_REGISTRY[canonical_name](**kwargs)

    valid_keys = list(DETECTOR_REGISTRY.keys()) + list(DETECTOR_ALIASES.keys())
    raise ValueError(f"Unknown detector '{name}'. Choose from: {valid_keys}")
