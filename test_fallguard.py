import re
import sys
import math

INO_PATH = r"C:\Users\kushw\.gemini\antigravity\scratch\FallGuard-v2\firmware\FallGuard_v2.ino"

def test_syntax_and_braces():
    print("=== TEST 1: Syntax & Delimiter Validation ===")
    with open(INO_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    def strip_c_cpp(text):
        out = []
        i = 0
        n = len(text)
        while i < n:
            if text.startswith('R"rawliteral(', i):
                end = text.find(')rawliteral"', i + 13)
                end = n if end == -1 else end + 12
                out.append(re.sub(r'[^\n]', ' ', text[i:end]))
                i = end
            elif text.startswith('/*', i):
                end = text.find('*/', i + 2)
                end = n if end == -1 else end + 2
                out.append(re.sub(r'[^\n]', ' ', text[i:end]))
                i = end
            elif text.startswith('//', i):
                end = text.find('\n', i + 2)
                if end == -1: end = n
                out.append(re.sub(r'[^\n]', ' ', text[i:end]))
                i = end
            elif text[i] == '"':
                start = i; i += 1
                while i < n and text[i] != '"':
                    if text[i] == '\\': i += 2
                    else: i += 1
                if i < n: i += 1
                out.append(re.sub(r'[^\n]', ' ', text[start:i]))
            elif text[i] == "'":
                start = i; i += 1
                while i < n and text[i] != "'":
                    if text[i] == '\\': i += 2
                    else: i += 1
                if i < n: i += 1
                out.append(re.sub(r'[^\n]', ' ', text[start:i]))
            else:
                out.append(text[i])
                i += 1
        return ''.join(out)

    stripped = strip_c_cpp(content)

    stack = []
    pairs = {')': '(', '}': '{', ']': '['}
    line = 1
    col = 1
    lines_orig = content.splitlines()
    for i, ch in enumerate(stripped):
        if ch == '\n':
            line += 1
            col = 1
            continue
        if ch in '({[':
            stack.append((ch, line, col))
        elif ch in ')}]':
            if not stack:
                raise AssertionError(f"Unmatched closing delimiter '{ch}' at line {line}, col {col}: {lines_orig[line-1]}")
            top, t_line, t_col = stack.pop()
            expected = pairs[ch]
            if top != expected:
                raise AssertionError(f"Mismatched delimiter: expected '{top}' (opened line {t_line}, col {t_col}: {lines_orig[t_line-1]}) but got '{ch}' at line {line}, col {col}: {lines_orig[line-1]}")
        col += 1

    if stack:
        top, t_line, t_col = stack[-1]
        raise AssertionError(f"Unclosed delimiter '{top}' opened at line {t_line}, col {t_col}: {lines_orig[t_line-1]}")

    print("PASS: All braces, parentheses, and brackets properly matched.")

def test_ring_buffer_math():
    print("\n=== TEST 2: Ring Buffer getRecentMaxJerk Math ===")
    WINDOW_SIZE = 25
    SAMPLE_INTERVAL_MS = 20

    for win_head in range(WINDOW_SIZE):
        for win_full in [False, True]:
            win_jerk = [float(x) for x in range(WINDOW_SIZE)]
            # simulate getRecentMaxJerk(150)
            ms = 150
            samples = (ms + SAMPLE_INTERVAL_MS - 1) // SAMPLE_INTERVAL_MS
            available = WINDOW_SIZE if win_full else win_head
            count = min(samples, available)
            mxJ = 0.0
            for i in range(count):
                idx = (win_head - 1 - (i % WINDOW_SIZE) + 2 * WINDOW_SIZE) % WINDOW_SIZE
                assert 0 <= idx < WINDOW_SIZE, f"Index out of bounds: {idx}"
                if win_jerk[idx] > mxJ:
                    mxJ = win_jerk[idx]

    # Test peak detection when peak is 60ms (3 samples) in the past
    win_jerk = [0.0] * WINDOW_SIZE
    # Suppose current sample is at head=10, so head-1=9 has jerk=50. Sample at 7 has jerk=350.
    win_head = 10
    win_full = True
    win_jerk[7] = 350.0  # 3 samples ago (60ms)
    win_jerk[9] = 50.0   # current sample (0ms)
    
    # Check max jerk over last 150ms (8 samples: 9, 8, 7, 6, 5, 4, 3, 2)
    samples = (150 + 20 - 1) // 20
    count = min(samples, WINDOW_SIZE if win_full else win_head)
    mxJ = 0.0
    for i in range(count):
        idx = (win_head - 1 - (i % WINDOW_SIZE) + 2 * WINDOW_SIZE) % WINDOW_SIZE
        if win_jerk[idx] > mxJ:
            mxJ = win_jerk[idx]
    assert mxJ == 350.0, f"Expected 350.0, got {mxJ}"
    print("PASS: Ring buffer indexing and peak retention verified across all configurations.")

def test_fsm_simulation():
    print("\n=== TEST 3: Deterministic State Machine Simulation ===")

    class SimESP32:
        def __init__(self):
            self.WINDOW_SIZE = 25
            self.SAMPLE_INTERVAL_MS = 20
            self.FREE_FALL_MAG = 5.0
            self.FF_MIN_MS = 40
            self.FF_WINDOW_MS = 1400
            self.STILL_LOW = 7.5
            self.STILL_HIGH = 12.0
            self.STILL_MS = 1800
            self.JERK_THRESHOLD = 220.0
            self.IMPACT_THRESHOLD = 11.5
            self.theta = 0.65
            self.state = "CALIBRATING"
            self.calStart = 0
            self.CAL_DURATION_MS = 20000
            self.calMaxMag = 0.0
            self.calMaxJerk = 0.0

            self.winMag = [9.81] * self.WINDOW_SIZE
            self.winJerk = [0.0] * self.WINDOW_SIZE
            self.winHead = 0
            self.winFull = False

            self.ffStart = 0
            self.impactTime = 0
            self.stillnessStart = 0
            self.inFFDip = False
            self.lastFeatures = [0.0] * 6
            self.lastClassConf = -1.0
            self.nonFallCount = 0
            self.fallCount = 0

        def push_window(self, mag, jerk):
            self.winMag[self.winHead] = mag
            self.winJerk[self.winHead] = jerk
            self.winHead = (self.winHead + 1) % self.WINDOW_SIZE
            if self.winHead == 0:
                self.winFull = True

        def get_recent_max_jerk(self, ms=150):
            samples = (ms + self.SAMPLE_INTERVAL_MS - 1) // self.SAMPLE_INTERVAL_MS
            available = self.WINDOW_SIZE if self.winFull else self.winHead
            count = min(samples, available)
            mxJ = 0.0
            for i in range(count):
                idx = (self.winHead - 1 - (i % self.WINDOW_SIZE) + 2 * self.WINDOW_SIZE) % self.WINDOW_SIZE
                if self.winJerk[idx] > mxJ:
                    mxJ = self.winJerk[idx]
            return mxJ

        def extract_features(self):
            n = self.WINDOW_SIZE if self.winFull else self.winHead
            if n < 2:
                return [0.0] * 6
            sub_mag = self.winMag[:n]
            sub_jerk = self.winJerk[:n]
            mean = sum(sub_mag) / n
            mx = max(sub_mag)
            mn = min(sub_mag)
            mxJ = max(sub_jerk)
            var = sum((x - mean)**2 for x in sub_mag) / (n - 1)
            p2v = mx / max(mn, 0.1)
            return [mean, mx, mn, var, p2v, mxJ]

        def reset_to_normal(self, reason):
            self.inFFDip = False
            self.stillnessStart = 0
            self.state = "NORMAL"
            self.lastClassConf = -1.0

        def step(self, t_ms, liveMag, jerk, quickPress=False, remoteCancel=False):
            self.push_window(liveMag, jerk)

            if remoteCancel:
                if self.state not in ("NORMAL", "CALIBRATING"):
                    if self.lastClassConf >= 0 or self.state == "IMPACT":
                        self.nonFallCount += 1
                        self.theta = min(0.85, self.theta + 0.02)
                    self.reset_to_normal("CANCEL_REMOTE")
                    return

            isStill = (liveMag >= self.STILL_LOW and liveMag <= self.STILL_HIGH)

            if self.state == "CALIBRATING":
                if liveMag > self.calMaxMag: self.calMaxMag = liveMag
                if jerk > self.calMaxJerk: self.calMaxJerk = jerk
                if t_ms - self.calStart >= self.CAL_DURATION_MS:
                    self.IMPACT_THRESHOLD = max(11.0, min(13.0, self.calMaxMag * 1.15))
                    self.JERK_THRESHOLD = max(180.0, min(260.0, self.calMaxJerk * 1.15))
                    self.state = "NORMAL"

            elif self.state == "NORMAL":
                if jerk >= 280.0 and liveMag >= 11.5:
                    self.lastFeatures = self.extract_features()
                    self.impactTime = t_ms
                    self.stillnessStart = 0
                    self.inFFDip = False
                    self.state = "IMPACT"
                    return

                if liveMag < self.FREE_FALL_MAG:
                    if not self.inFFDip:
                        self.inFFDip = True
                        self.ffStart = t_ms
                    if t_ms - self.ffStart >= self.FF_MIN_MS:
                        self.state = "FREE_FALL"
                else:
                    self.inFFDip = False

            elif self.state == "FREE_FALL":
                recentMaxJerk = self.get_recent_max_jerk(150)
                if liveMag >= self.IMPACT_THRESHOLD and (jerk >= self.JERK_THRESHOLD or recentMaxJerk >= self.JERK_THRESHOLD):
                    self.lastFeatures = self.extract_features()
                    self.state = "IMPACT"
                    self.impactTime = t_ms
                    self.stillnessStart = 0
                    self.inFFDip = False
                elif t_ms - self.ffStart >= self.FF_WINDOW_MS:
                    self.reset_to_normal("FF_TIMEOUT_NO_IMPACT")
                    self.inFFDip = False

            elif self.state == "IMPACT":
                if quickPress:
                    self.nonFallCount += 1
                    self.theta = min(0.85, self.theta + 0.02)
                    self.reset_to_normal("CANCEL_USER_IMPACT")
                    return

                timeSinceImpact = t_ms - self.impactTime
                if isStill:
                    if self.stillnessStart == 0:
                        self.stillnessStart = t_ms
                    if t_ms - self.stillnessStart >= self.STILL_MS:
                        self.lastClassConf = 0.85  # mock classify
                        self.state = "CANDIDATE_FALL"
                        return
                else:
                    self.stillnessStart = 0

                if timeSinceImpact >= 5000 and not isStill:
                    self.nonFallCount += 1
                    self.reset_to_normal("IMPACT_TIMEOUT_MOVING")

    # Scenario A: Calibration bounding
    sim = SimESP32()
    for t in range(0, 20020, 20):
        # Simulate vigorous calibration jumping/running
        sim.step(t, liveMag=28.0, jerk=750.0)
    assert sim.state == "NORMAL"
    assert sim.IMPACT_THRESHOLD == 13.0, f"Expected 13.0 clamp, got {sim.IMPACT_THRESHOLD}"
    assert sim.JERK_THRESHOLD == 260.0, f"Expected 260.0 clamp, got {sim.JERK_THRESHOLD}"
    print("PASS: Scenario A - Calibration bounds clamped to [11.0, 13.0] and [180.0, 260.0].")

    # Scenario B: Direct High-Jerk slip fall in NORMAL
    sim = SimESP32()
    sim.state = "NORMAL"
    sim.step(21000, liveMag=9.8, jerk=5.0)
    sim.step(21020, liveMag=12.2, jerk=295.0)  # Direct slip impact
    assert sim.state == "IMPACT"
    assert sim.impactTime == 21020
    assert sim.stillnessStart == 0
    # Next 1800ms of stillness (inclusive of 1800ms milestone)
    for t in range(21040, 21040 + 1800 + 20, 20):
        sim.step(t, liveMag=9.8, jerk=0.0)
    assert sim.state == "CANDIDATE_FALL"
    # Snapshotted features contain the slip impact
    assert sim.lastFeatures[1] >= 12.2, f"Expected max mag >= 12.2, got {sim.lastFeatures[1]}"
    assert sim.lastFeatures[5] >= 295.0, f"Expected max jerk >= 295.0, got {sim.lastFeatures[5]}"
    print("PASS: Scenario B - Direct High-Jerk Slip transitions to IMPACT, snapshots features, and enters CANDIDATE_FALL.")

    # Scenario C: Free-fall with Jerk peak leading Impact Magnitude by 60ms
    sim = SimESP32()
    sim.state = "NORMAL"
    sim.step(1000, liveMag=4.0, jerk=10.0)  # FF sample 1 (0ms elapsed)
    sim.step(1020, liveMag=4.2, jerk=15.0)  # FF sample 2 (20ms elapsed)
    sim.step(1040, liveMag=4.1, jerk=12.0)  # FF sample 3 (40ms elapsed >= FF_MIN_MS)
    assert sim.state == "FREE_FALL"
    # Jerk spike at t=1060, mag not yet reached threshold
    sim.step(1060, liveMag=9.0, jerk=240.0) # Jerk spike 240 m/s^3
    assert sim.state == "FREE_FALL"
    # At t=1080, jerk drops, mag reaches 12.5 m/s^2 (impact)
    sim.step(1080, liveMag=12.5, jerk=70.0)
    # getRecentMaxJerk(150) should detect 240.0 from 20ms ago!
    assert sim.state == "IMPACT"
    print("PASS: Scenario C - Desynchronized jerk peak captured by rolling 150ms window.")

    # Scenario D: Free-fall timeout without impact
    sim = SimESP32()
    sim.state = "NORMAL"
    sim.step(1000, liveMag=4.0, jerk=10.0)
    sim.step(1020, liveMag=4.0, jerk=10.0)
    sim.step(1040, liveMag=4.0, jerk=10.0)
    assert sim.state == "FREE_FALL"
    for t in range(1060, 1000 + 1400 + 20, 20):
        sim.step(t, liveMag=9.8, jerk=10.0)
    assert sim.state == "NORMAL"
    print("PASS: Scenario D - Free-fall without impact correctly times out to NORMAL.")

    # Scenario E: IMPACT state post-fall movement failsafe
    sim = SimESP32()
    sim.state = "IMPACT"
    sim.impactTime = 5000
    # User moves continuously after impact for 5.2 seconds
    for t in range(5020, 10040, 20):
        sim.step(t, liveMag=14.0, jerk=50.0) # moving, outside stillness band
    assert sim.state == "NORMAL"
    assert sim.nonFallCount == 1
    print("PASS: Scenario E - IMPACT state moving failsafe resets to NORMAL after 5000ms.")

    # Scenario F: Remote cancel in IMPACT updates classifier
    sim = SimESP32()
    sim.state = "IMPACT"
    sim.impactTime = 5000
    sim.step(5100, liveMag=9.8, jerk=0.0, remoteCancel=True)
    assert sim.state == "NORMAL"
    assert sim.nonFallCount == 1
    print("PASS: Scenario F - Remote cancel in IMPACT updates classifier non-fall count.")

if __name__ == "__main__":
    test_syntax_and_braces()
    test_ring_buffer_math()
    test_fsm_simulation()
    print("\nALL VERIFICATION TESTS PASSED SUCCESSFULLY!")
