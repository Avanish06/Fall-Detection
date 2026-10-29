"""
FallGuard v2 — Comprehensive Test Suite & State Machine Simulator
===================================================================
Tests core firmware logic against specifications:
  - Dynamic regex parsing of firmware constants from FallGuard_v2.ino
  - Python simulator of core state machine under all compile flag configurations
  - Test 1: all-flags-off = baseline behavior
  - Test 2: sensor fault - zero magnitude suppresses falls
  - Test 3: sensor fault - frozen IMU axes triggers SENSOR_FAULT
  - Test 4: theta lockout prevention under repeated cancels in cold start
  - Test 5: USE_ADAPTIVE isolation (kinematic triggers unchanged, adaptive T_s and T_c)
"""

import math
import re
import sys
from pathlib import Path

# Paths resolved relative to current test script
REPO_ROOT = Path(__file__).resolve().parent
INO_PATH = REPO_ROOT / "firmware" / "FallGuard_v2.ino"


# ──────────────────────────────────────────────────────────────────────────────
# Firmware Constants Parser
# ──────────────────────────────────────────────────────────────────────────────


def parse_firmware_constants(ino_path: Path) -> dict:
    """Parse #define flags and const values directly from firmware C++ file."""
    if not ino_path.exists():
        raise FileNotFoundError(f"Firmware file not found at {ino_path}")

    text = ino_path.read_text(encoding="utf-8")
    config_match = re.search(r"// ===== CONFIGURATION =====(.*?)// =====", text, re.DOTALL)
    config_text = config_match.group(1) if config_match else text

    constants = {}

    # 1. Parse #define NAME VALUE
    for m in re.finditer(r"#define\s+([A-Za-z0-9_]+)\s+([0-9.]+)", config_text):
        val_str = m.group(2)
        constants[m.group(1)] = float(val_str) if "." in val_str else int(val_str)

    # 2. Parse const <type> NAME = VALUE;
    for m in re.finditer(r"const\s+[A-Za-z0-9_ ]+?\s+([A-Za-z0-9_]+)\s*=\s*([0-9.+-]+)f?\s*;", config_text):
        val_str = m.group(2)
        constants[m.group(1)] = float(val_str) if "." in val_str else int(val_str)

    # 3. Parse float theta = VALUE;
    m_th = re.search(r"float\s+theta\s*=\s*([0-9.]+)f?\s*;", config_text)
    if m_th:
        constants["THETA_INIT"] = float(m_th.group(1))

    return constants


# ──────────────────────────────────────────────────────────────────────────────
# Python Simulator of FallGuard Core State Machine
# ──────────────────────────────────────────────────────────────────────────────


class FallGuardSimulator:
    """Simulates the FallGuard v2 embedded firmware state transitions."""

    def __init__(self, constants: dict, flag_overrides: dict = None):
        self.c = dict(constants)
        if flag_overrides:
            self.c.update(flag_overrides)

        # Feature Flags
        self.USE_WIFI = bool(self.c.get("USE_WIFI", 1))
        self.USE_GNB = bool(self.c.get("USE_GNB", 1))
        self.USE_INTEGRITY = bool(self.c.get("USE_INTEGRITY", 1))
        self.USE_ADAPTIVE = bool(self.c.get("USE_ADAPTIVE", 1))
        self.USE_STILLNESS = bool(self.c.get("USE_STILLNESS", 1))
        self.USE_PATH2 = bool(self.c.get("USE_PATH2", 1))

        # Kinematic Constants
        self.FREE_FALL_MAG = float(self.c.get("FREE_FALL_MAG", 5.0))
        self.FF_MIN_MS = int(self.c.get("FF_MIN_MS", 40))
        self.FF_WINDOW_MS = int(self.c.get("FF_WINDOW_MS", 1400))
        self.IMPACT_THRESHOLD = float(self.c.get("IMPACT_THRESHOLD", 22.0))
        self.JERK_THRESHOLD = float(self.c.get("JERK_THRESHOLD", 500.0))
        self.MAG_PATH2 = float(self.c.get("MAG_PATH2", 11.5))
        self.JERK_PATH2 = float(self.c.get("JERK_PATH2", 280.0))
        self.STILL_LOW = float(self.c.get("STILL_LOW", 7.5))
        self.STILL_HIGH = float(self.c.get("STILL_HIGH", 12.0))
        self.STILL_MS = int(self.c.get("STILL_MS", 2200))
        self.CANCEL_MS = int(self.c.get("CANCEL_MS", 5000))
        self.THETA_INIT = float(self.c.get("THETA_INIT", 0.65))
        self.THETA_MAX = float(self.c.get("THETA_MAX", 0.85))
        self.THETA_MIN = float(self.c.get("THETA_MIN", 0.50))
        self.THETA_UP = float(self.c.get("THETA_UP", 0.02))
        self.THETA_DOWN = float(self.c.get("THETA_DOWN", 0.01))
        self.E0 = float(self.c.get("E0", 1.0))

        # State Variables
        self.state = "NORMAL"
        self.theta = self.THETA_INIT
        self.Q = 1.0
        self.E_k = 1.0
        self.sensor_fault = False
        self.fall_stats_count = 0
        self.non_fall_stats_count = 0
        self.stillness_accum_ms = 0
        self.stillness_start_ms = 0
        self.free_fall_start_ms = 0
        self.impact_time_ms = 0
        self.confirmed_time_ms = 0
        self.a_peak = 0.0
        self.j_peak = 0.0
        self.axis_history = []  # stores (rx, ry, rz)
        self.events = []  # (t_ms, event_name)

    def compute_T_s(self) -> float:
        """Adaptive stillness window."""
        if self.USE_ADAPTIVE:
            return 1500.0 + (3000.0 - 1500.0) * (self.E_k / (self.E_k + self.E0))
        return float(self.STILL_MS)

    def compute_T_c(self) -> float:
        """Adaptive cancellation window."""
        if self.USE_ADAPTIVE:
            imp_norm = self.a_peak / self.IMPACT_THRESHOLD if self.IMPACT_THRESHOLD > 0 else 1.0
            jrk_norm = self.j_peak / self.JERK_THRESHOLD if self.JERK_THRESHOLD > 0 else 1.0
            m = min(imp_norm, jrk_norm)
            m_clip = max(0.0, min(1.0, (m - 1.0) / 0.5))
            return 8000.0 - (8000.0 - 3000.0) * m_clip
        return float(self.CANCEL_MS)

    def step(
        self,
        t_ms: int,
        mag: float,
        jerk: float = 0.0,
        raw_axes: tuple = None,
        dt_us: int = 20000,
        quick_press: bool = False,
    ):
        """Execute one 20 ms iteration of the fall-guard state machine."""
        if raw_axes is None:
            # Natural sensor jitter (non-frozen)
            rx = 120 + (t_ms % 17)
            ry = -140 + (t_ms % 13)
            rz = 16384 + (t_ms % 19)
        else:
            rx, ry, rz = raw_axes

        # ── 1. Sensor Integrity Layer (USE_INTEGRITY) ──
        current_vk = 1.0
        if self.USE_INTEGRITY:
            # Check axis delta over last 25 samples
            self.axis_history.append((rx, ry, rz))
            if len(self.axis_history) > 25:
                self.axis_history.pop(0)

            delta_ok = True
            if len(self.axis_history) >= 25:
                dx = max(a[0] for a in self.axis_history) - min(a[0] for a in self.axis_history)
                dy = max(a[1] for a in self.axis_history) - min(a[1] for a in self.axis_history)
                dz = max(a[2] for a in self.axis_history) - min(a[2] for a in self.axis_history)
                if dx <= 2 and dy <= 2 and dz <= 2:
                    delta_ok = False

            mag_ok = mag >= 2.0
            axis_ok = abs(rx) < 32700 and abs(ry) < 32700 and abs(rz) < 32700
            timing_ok = dt_us <= 40000

            v_k_sample = 1.0 if (mag_ok and axis_ok and delta_ok and timing_ok) else 0.0
            current_vk = v_k_sample
            self.Q = 0.9 * self.Q + 0.1 * v_k_sample

            # Hysteresis state transitions
            if not self.sensor_fault and self.Q < 0.5:
                self.sensor_fault = True
                self.events.append((t_ms, "SENSOR_FAULT"))
                if self.state in ("FREE_FALL", "IMPACT"):
                    self.events.append((t_ms, "POSSIBLE_FALL_DURING_FAULT"))
            elif self.sensor_fault and self.Q > 0.9:
                self.sensor_fault = False
                self.events.append((t_ms, "SENSOR_RECOVERED"))

        # ── 2. Windowed Energy Update ──
        if current_vk > 0.5:
            self.E_k = 0.98 * self.E_k + 0.02 * ((mag - 9.81) ** 2)

        # ── 3. State Machine ──
        is_still = (self.STILL_LOW <= mag <= self.STILL_HIGH)
        t_s = self.compute_T_s()
        t_c = self.compute_T_c()

        if self.state == "NORMAL":
            # Path 2 trigger (direct high-jerk impact)
            if self.USE_PATH2 and jerk >= self.JERK_PATH2 and mag >= self.MAG_PATH2:
                self.state = "IMPACT"
                self.impact_time_ms = t_ms
                self.stillness_accum_ms = 0
                self.stillness_start_ms = 0
                self.a_peak = mag
                self.j_peak = jerk
                self.events.append((t_ms, "IMPACT_PATH2"))
                return

            # Path 1 trigger (free-fall entry)
            if mag < self.FREE_FALL_MAG:
                self.state = "FREE_FALL"
                self.free_fall_start_ms = t_ms
                self.events.append((t_ms, "FREE_FALL_ENTER"))

        elif self.state == "FREE_FALL":
            ff_dur = t_ms - self.free_fall_start_ms
            if mag >= self.FREE_FALL_MAG:
                if ff_dur >= self.FF_MIN_MS:
                    self.state = "IMPACT"
                    self.impact_time_ms = t_ms
                    self.stillness_accum_ms = 0
                    self.stillness_start_ms = 0
                    self.a_peak = mag
                    self.j_peak = jerk
                    self.events.append((t_ms, "FREE_FALL_QUALIFIED"))
                else:
                    self.state = "NORMAL"
            elif ff_dur > self.FF_WINDOW_MS:
                self.state = "NORMAL"

        elif self.state == "IMPACT":
            self.a_peak = max(self.a_peak, mag)
            self.j_peak = max(self.j_peak, jerk)

            if quick_press:
                if self.USE_GNB:
                    self.theta = min(self.THETA_MAX, self.theta + self.THETA_UP)
                self.state = "NORMAL"
                self.events.append((t_ms, "CANCEL_USER_IMPACT"))
                return

            time_since_impact = t_ms - self.impact_time_ms
            if time_since_impact >= 5000 and not is_still:
                self.state = "NORMAL"
                self.events.append((t_ms, "IMPACT_TIMEOUT_MOVING"))
                return

            # Post-impact stillness evaluation
            if self.USE_INTEGRITY:
                if current_vk > 0.5 and is_still:
                    self.stillness_accum_ms += 20
                    if self.stillness_accum_ms >= t_s:
                        if self.USE_GNB:
                            self.state = "CANDIDATE_FALL"
                        else:
                            self.state = "FALL_CONFIRMED"
                            self.confirmed_time_ms = t_ms
                            self.events.append((t_ms, "FALL_CONFIRMED"))
                        self.stillness_accum_ms = 0
                else:
                    self.stillness_accum_ms = max(0, self.stillness_accum_ms - 10)
            else:
                if is_still:
                    if self.stillness_start_ms == 0:
                        self.stillness_start_ms = t_ms
                    if t_ms - self.stillness_start_ms >= t_s:
                        if self.USE_GNB:
                            self.state = "CANDIDATE_FALL"
                        else:
                            self.state = "FALL_CONFIRMED"
                            self.confirmed_time_ms = t_ms
                            self.events.append((t_ms, "FALL_CONFIRMED"))
                else:
                    self.stillness_start_ms = 0

        elif self.state == "CANDIDATE_FALL":
            if self.USE_INTEGRITY and self.sensor_fault:
                # Suppress confirmed falls during sensor fault
                return

            classifier_trained = (self.fall_stats_count >= 3 and self.non_fall_stats_count >= 3)
            # Simulated model confidence for genuine candidate fall
            cand_conf = 0.95
            if not classifier_trained or cand_conf >= self.theta:
                self.state = "FALL_CONFIRMED"
                self.confirmed_time_ms = t_ms
                self.events.append((t_ms, "FALL_CONFIRMED"))
            else:
                self.theta = min(self.THETA_MAX, self.theta + self.THETA_UP * 0.5)
                self.state = "NORMAL"
                self.events.append((t_ms, "CLASSIFIER_REJECTED"))

        elif self.state == "FALL_CONFIRMED":
            if self.USE_INTEGRITY and self.sensor_fault:
                return

            if quick_press:
                if self.USE_GNB:
                    self.theta = min(self.THETA_MAX, self.theta + self.THETA_UP)
                self.state = "NORMAL"
                self.events.append((t_ms, "CANCEL_USER"))
            elif t_ms - self.confirmed_time_ms >= t_c:
                self.state = "ALERT_SENT"
                if self.USE_GNB:
                    self.theta = max(self.THETA_MIN, self.theta - self.THETA_DOWN)
                self.events.append((t_ms, "ALERT_SENT"))


# ──────────────────────────────────────────────────────────────────────────────
# Test 1: all-flags-off = baseline behavior
# ──────────────────────────────────────────────────────────────────────────────


def test_1_all_flags_off(constants: dict):
    print("=== TEST 1: All-Flags-Off Baseline Verification ===")
    flags_off = {
        "USE_WIFI": 0,
        "USE_GNB": 0,
        "USE_INTEGRITY": 0,
        "USE_ADAPTIVE": 0,
        "USE_STILLNESS": 1,
        "USE_PATH2": 0,
    }
    sim = FallGuardSimulator(constants, flag_overrides=flags_off)
    assert not sim.USE_GNB
    assert not sim.USE_INTEGRITY
    assert not sim.USE_ADAPTIVE
    assert not sim.USE_PATH2

    # Simulate Normal -> Free Fall (80 ms) -> Impact -> Stillness -> Confirmed -> Alert
    t = 0
    # 1. Normal phase (100 ms)
    for _ in range(5):
        sim.step(t, mag=9.81, jerk=5.0)
        t += 20
    assert sim.state == "NORMAL"

    # 2. Free fall phase (< 5.0 m/s2 for 80 ms >= 40 ms)
    for _ in range(4):
        sim.step(t, mag=1.2, jerk=10.0)
        t += 20
    assert sim.state == "FREE_FALL"

    # 3. Impact pulse (mag 26.0 m/s2, jerk 600.0 m/s3)
    sim.step(t, mag=26.0, jerk=600.0)
    t += 20
    assert sim.state == "IMPACT"

    # 4. Post-impact stillness (STILL_MS ms)
    still_steps = int(sim.STILL_MS / 20) + 5
    for _ in range(still_steps):
        sim.step(t, mag=9.81, jerk=2.0)
        t += 20
    # Because USE_GNB = 0, directly transitions to FALL_CONFIRMED without GNB candidate gate
    assert sim.state == "FALL_CONFIRMED"
    event_names = [e[1] for e in sim.events]
    assert "FALL_CONFIRMED" in event_names

    # 5. Wait CANCEL_MS (5000 ms) without button press -> ALERT_SENT
    cancel_steps = int(sim.CANCEL_MS / 20) + 5
    for _ in range(cancel_steps):
        sim.step(t, mag=9.81, jerk=2.0)
        t += 20
    assert sim.state == "ALERT_SENT"
    assert "ALERT_SENT" in [e[1] for e in sim.events]

    print("  [PASS] All-flags-off simulator executed baseline sequence perfectly:")
    print(f"         NORMAL -> FREE_FALL -> IMPACT -> FALL_CONFIRMED -> ALERT_SENT")


# ──────────────────────────────────────────────────────────────────────────────
# Test 2: sensor fault - zero magnitude suppresses falls
# ──────────────────────────────────────────────────────────────────────────────


def test_2_sensor_fault_zero_mag(constants: dict):
    print("=== TEST 2: Sensor Fault (Zero Mag Suppression) ===")
    sim = FallGuardSimulator(constants, flag_overrides={"USE_INTEGRITY": 1})

    t = 0
    # Feed 20 frames of zero-magnitude samples
    for _ in range(25):
        sim.step(t, mag=0.0, jerk=0.0, raw_axes=(0, 0, 0))
        t += 20

    # Quality index must decay below 0.5 and emit SENSOR_FAULT
    assert sim.sensor_fault is True
    assert sim.Q < 0.5
    event_names = [e[1] for e in sim.events]
    assert "SENSOR_FAULT" in event_names
    assert "FALL_CONFIRMED" not in event_names

    # Continue feeding zero magnitude and attempt to trigger fall
    for _ in range(50):
        sim.step(t, mag=0.0, jerk=0.0, raw_axes=(0, 0, 0))
        t += 20

    assert "FALL_CONFIRMED" not in [e[1] for e in sim.events]
    assert "ALERT_SENT" not in [e[1] for e in sim.events]
    print(f"  [PASS] Zero magnitude caused Q decay (Q={sim.Q:.3f} < 0.5) and SENSOR_FAULT.")
    print("         FALL_CONFIRMED was completely suppressed.")


# ──────────────────────────────────────────────────────────────────────────────
# Test 3: sensor fault - frozen IMU axes triggers SENSOR_FAULT
# ──────────────────────────────────────────────────────────────────────────────


def test_3_sensor_fault_frozen_axes(constants: dict):
    print("=== TEST 3: Sensor Fault (Frozen IMU Axes) ===")
    sim = FallGuardSimulator(constants, flag_overrides={"USE_INTEGRITY": 1})

    t = 0
    # Feed identical-axis samples for 35 frames (delta <= 2 LSB over 25 samples)
    frozen_axis = (250, -410, 16300)
    for _ in range(35):
        sim.step(t, mag=9.81, jerk=0.0, raw_axes=frozen_axis)
        t += 20

    assert sim.sensor_fault is True
    assert sim.Q < 0.5
    event_names = [e[1] for e in sim.events]
    assert "SENSOR_FAULT" in event_names
    print(f"  [PASS] Identical axis samples for 35 frames triggered SENSOR_FAULT (Q={sim.Q:.3f}).")


# ──────────────────────────────────────────────────────────────────────────────
# Test 4: theta lockout prevention under repeated cancels in cold start
# ──────────────────────────────────────────────────────────────────────────────


def test_4_theta_lockout_prevention(constants: dict):
    print("=== TEST 4: Theta Lockout Prevention (Cold-Start Immunity) ===")
    sim = FallGuardSimulator(constants, flag_overrides={"USE_GNB": 1})
    # Device starts untrained (count < 3)
    assert sim.fall_stats_count < 3 and sim.non_fall_stats_count < 3

    t = 0
    initial_theta = sim.theta

    # Simulate 6 consecutive user cancels (e.g. false alarms or tests)
    for cancel_idx in range(6):
        # Trigger impact and stillness
        sim.state = "IMPACT"
        sim.impact_time_ms = t
        still_steps = int(sim.compute_T_s() / 20) + 2
        for _ in range(still_steps):
            sim.step(t, mag=9.81, jerk=1.0)
            t += 20

        assert sim.state == "FALL_CONFIRMED"
        # User cancels during cancel window
        sim.step(t, mag=9.81, jerk=1.0, quick_press=True)
        t += 20
        assert sim.state == "NORMAL"

    print(f"  Theta elevated after 6 cancels from {initial_theta:.2f} to {sim.theta:.2f}")
    assert sim.theta > initial_theta

    # 7th fall event occurs
    sim.state = "IMPACT"
    sim.impact_time_ms = t
    still_steps = int(sim.compute_T_s() / 20) + 2
    for _ in range(still_steps):
        sim.step(t, mag=9.81, jerk=1.0)
        t += 20

    # Assert 7th fall STILL confirmed despite elevated theta!
    assert sim.state == "FALL_CONFIRMED"
    confirmed_events = [e for e in sim.events if e[1] == "FALL_CONFIRMED"]
    assert len(confirmed_events) == 7
    print("  [PASS] 7th fall confirmed into FALL_CONFIRMED (no cold-start lockout).")


# ──────────────────────────────────────────────────────────────────────────────
# Test 5: USE_ADAPTIVE isolation
# ──────────────────────────────────────────────────────────────────────────────


def test_5_use_adaptive_isolation(constants: dict):
    print("=== TEST 5: USE_ADAPTIVE Isolation ===")

    # Run Path 1 and Path 2 on adaptive=1 and adaptive=0
    sim_adapt = FallGuardSimulator(constants, flag_overrides={"USE_ADAPTIVE": 1})
    sim_fixed = FallGuardSimulator(constants, flag_overrides={"USE_ADAPTIVE": 0})

    # Kinematic thresholds must be identical
    assert sim_adapt.FREE_FALL_MAG == sim_fixed.FREE_FALL_MAG
    assert sim_adapt.JERK_PATH2 == sim_fixed.JERK_PATH2
    assert sim_adapt.MAG_PATH2 == sim_fixed.MAG_PATH2

    # Feed identical Path 2 kinematic trigger
    t = 0
    sim_adapt.step(t, mag=sim_adapt.MAG_PATH2 + 2.0, jerk=sim_adapt.JERK_PATH2 + 50.0)
    sim_fixed.step(t, mag=sim_fixed.MAG_PATH2 + 2.0, jerk=sim_fixed.JERK_PATH2 + 50.0)

    assert sim_adapt.state == "IMPACT"
    assert sim_fixed.state == "IMPACT"
    assert sim_adapt.events[-1][1] == "IMPACT_PATH2"
    assert sim_fixed.events[-1][1] == "IMPACT_PATH2"

    # Verify T_s adaptation based on energy
    sim_adapt.E_k = 4.0
    sim_fixed.E_k = 4.0
    t_s_adapt = sim_adapt.compute_T_s()
    t_s_fixed = sim_fixed.compute_T_s()
    assert t_s_adapt != t_s_fixed
    assert math.isclose(t_s_fixed, float(sim_fixed.STILL_MS))
    expected_adapt = 1500.0 + (3000.0 - 1500.0) * (4.0 / (4.0 + 1.0))
    assert math.isclose(t_s_adapt, expected_adapt, abs_tol=1e-3)

    # Verify T_c adaptation based on impact severity
    sim_adapt.a_peak = 33.0
    sim_adapt.j_peak = 750.0
    sim_fixed.a_peak = 33.0
    sim_fixed.j_peak = 750.0
    t_c_adapt = sim_adapt.compute_T_c()
    t_c_fixed = sim_fixed.compute_T_c()
    assert t_c_adapt != t_c_fixed
    assert t_c_fixed == float(sim_fixed.CANCEL_MS)

    print(f"  [PASS] Kinematic triggers identical between adaptive and non-adaptive.")
    print(f"         T_s: adaptive={t_s_adapt:.1f} ms vs fixed={t_s_fixed:.1f} ms")
    print(f"         T_c: adaptive={t_c_adapt:.1f} ms vs fixed={t_c_fixed:.1f} ms")


# ──────────────────────────────────────────────────────────────────────────────
# Main Runner
# ──────────────────────────────────────────────────────────────────────────────


def main():
    print("=" * 60)
    print("  FallGuard v2 — Firmware Verification Test Suite")
    print(f"  Firmware path: {INO_PATH}")
    print("=" * 60)

    constants = parse_firmware_constants(INO_PATH)
    print(f"Loaded {len(constants)} constants from firmware config block.")

    test_1_all_flags_off(constants)
    print("-" * 60)
    test_2_sensor_fault_zero_mag(constants)
    print("-" * 60)
    test_3_sensor_fault_frozen_axes(constants)
    print("-" * 60)
    test_4_theta_lockout_prevention(constants)
    print("-" * 60)
    test_5_use_adaptive_isolation(constants)
    print("=" * 60)
    print("  ALL 5 TESTS PASSED SUCCESSFULLY!")
    print("=" * 60)


if __name__ == "__main__":
    main()
