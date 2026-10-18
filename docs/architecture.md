# FallGuard v2 — System Architecture

> **Document version:** 1.0  
> **Target hardware:** ESP32 (Xtensa LX6 dual-core, 240 MHz, 320 KB SRAM) + MPU6050 IMU

---

## 1. System Overview

FallGuard v2 is a fully self-contained fall detection system that runs entirely on the ESP32 microcontroller. No cloud connectivity or external DSP hardware is required. The device:

1. Samples the MPU6050 IMU at **50 Hz** (every 20 ms) over I²C.
2. Applies a **Simple Moving Average (SMA) filter** to reduce sensor noise.
3. Computes **derived features** (jerk, sliding-window statistics) continuously.
4. Passes features through a **3-phase rule engine** to gate candidate fall events.
5. Scores gated events using an **Online Gaussian Naïve Bayes (GNB) classifier**.
6. Activates a **buzzer alarm** on high-confidence falls and waits for button feedback.
7. Uses button feedback to **adapt the confidence threshold** $\theta$ online.
8. Streams all telemetry over **USB serial** at 115200 baud for PC-side logging.

The design philosophy prioritises **false-positive suppression** (specificity) over raw sensitivity because nuisance alarms in wearable devices cause users to disable them entirely — a well-documented problem in clinical literature.

---

## 2. Signal Processing Pipeline

```
MPU6050 (I²C, 50 Hz)
        │
        ▼
  ┌─────────────┐
  │  SMA Filter │  N=5 tap moving average per axis (ax, ay, az)
  └──────┬──────┘
         │  filtered (ax, ay, az)
         ▼
  ┌──────────────────┐
  │  Magnitude calc  │  mag = sqrt(ax² + ay² + az²)
  └────────┬─────────┘
           │  scalar mag(t)
           ▼
  ┌──────────────────┐
  │   Jerk calc      │  jerk(t) = |mag(t) - mag(t-1)| / Δt
  └────────┬─────────┘
           │
           ▼
  ┌─────────────────────┐
  │  Sliding window     │  W = 40 samples (800 ms)
  │  Feature extraction │  → 6 scalar features
  └────────┬────────────┘
           │
           ▼
  ┌──────────────────────┐
  │  3-Phase Rule Engine │  Gate non-fall events before classifier
  └────────┬─────────────┘
           │  (if pattern matches)
           ▼
  ┌───────────────────────────┐
  │  Online Gaussian NB       │  Posterior P(FALL | features)
  └────────────┬──────────────┘
               │  conf ≥ θ ?
        ┌──────┴──────┐
       YES             NO
        │               │
  Alarm + log      Return to NORMAL
```

---

## 3. Feature Definitions

All features are computed over a **sliding window of $W = 40$ samples** (800 ms at 50 Hz). Let $\mathbf{m} = [m_1, m_2, \ldots, m_W]$ be the vector of magnitude values within the window.

| Feature | Symbol | Formula |
|---------|--------|---------|
| Mean magnitude | $\bar{m}$ | $\bar{m} = \dfrac{1}{W}\sum_{i=1}^{W} m_i$ |
| Maximum magnitude | $m_{\max}$ | $m_{\max} = \max(\mathbf{m})$ |
| Minimum magnitude | $m_{\min}$ | $m_{\min} = \min(\mathbf{m})$ |
| Variance of magnitude | $\sigma^2_m$ | $\sigma^2_m = \dfrac{1}{W}\sum_{i=1}^{W}(m_i - \bar{m})^2$ |
| Peak-to-valley | $P2V$ | $P2V = m_{\max} - m_{\min}$ |
| Maximum jerk | $J_{\max}$ | $J_{\max} = \max\!\left(\left\{\dfrac{|m_i - m_{i-1}|}{\Delta t}\right\}_{i=2}^{W}\right)$ |

These six features were selected because:
- $\bar{m}$ and $\sigma^2_m$ characterise the *average* and *spread* of the fall signature.
- $m_{\min}$ captures the free-fall trough (values near 0 g during weightlessness).
- $m_{\max}$ captures the impact peak (values often exceeding 3–5 g).
- $P2V$ provides a combined discriminator between free-fall and impact in a single scalar.
- $J_{\max}$ separates sharp-onset impacts (falls, jumps) from gradual transitions.

---

## 4. Three-Phase Rule Engine

The rule engine acts as a **deterministic pre-filter** before the probabilistic classifier. It models the temporal structure of a human fall:

```
NORMAL ──[free-fall trigger]──► PRE_IMPACT ──[impact trigger]──► IMPACT ──[classifier]──► POST_IMPACT
  ▲                                  │                                │                        │
  │                               timeout                          no match                  button / timeout
  └──────────────────────────────────┘───────────────────────────────┘◄───────────────────────┘
```

### State Descriptions

**NORMAL**
- The system continuously samples, filters, and logs telemetry.
- Enters `PRE_IMPACT` when $m(t) < \theta_{\text{ff}}$ (free-fall threshold, default 0.5 g) for $n_{\text{ff}} \geq 3$ consecutive samples.

**PRE_IMPACT**
- Duration limit: 500 ms. If no impact is detected within this window the system returns to `NORMAL` (avoiding false transitions from slow bending).
- The jerk discriminator is active: if $J_{\max}$ exceeds a short-spike threshold without the preceding low-$m$ signature, the event is rejected.
- Enters `IMPACT` when $m(t) > \theta_{\text{imp}}$ (impact threshold, default 2.0 g).

**IMPACT**
- Feature extraction runs over the buffered 40-sample window.
- The GNB classifier computes $P(\text{FALL} \mid \mathbf{x})$.
- If $P \geq \theta$: enter `POST_IMPACT` (alarm).
- If $P < \theta$: return to `NORMAL`.

**POST_IMPACT**
- Buzzer sounds on `GPIO 5`.
- A 10-second countdown begins.
- If the button on `GPIO 2` is pressed → `ALARM_CANCELLED` logged, return to `NORMAL`.
- If no press within 10 s → `FALL_CONFIRMED` logged (future: trigger caregiver SMS/BLE).

### Timing Parameters

| Parameter | Symbol | Default value |
|-----------|--------|---------------|
| Free-fall magnitude threshold | $\theta_{\text{ff}}$ | 0.5 g |
| Free-fall minimum consecutive samples | $n_{\text{ff}}$ | 3 (60 ms) |
| PRE_IMPACT timeout | $T_{\text{pi}}$ | 500 ms |
| Impact magnitude threshold | $\theta_{\text{imp}}$ | 2.0 g |
| POST_IMPACT alarm window | $T_{\text{alarm}}$ | 10 s |

---

## 5. Why Sitting and Jumping Do Not Trigger Alarms

### Sitting down abruptly

When a person sits down quickly the acceleration magnitude rises briefly (chair impact) but there is **no preceding free-fall trough**. The rule engine requires $m < 0.5\,\text{g}$ for at least 3 consecutive samples before an impact qualifies as a fall candidate. Sitting never produces near-zero-g readings, so the `PRE_IMPACT` gate is never crossed.

### Jumping

A jump does produce a brief free-fall phase (airborne) followed by a landing impact. However the jerk discriminator distinguishes falls from jumps by analysing the **temporal shape** of the jerk signal:

- **Fall:** extended low-$m$ phase (50–200 ms) → sharp sustained jerk spike at landing.
- **Jump:** very short low-$m$ phase (< 40 ms, typically 2 samples at 50 Hz) because the person pushes off the ground with muscular force, shortening apparent weightlessness.

If the duration of the sub-threshold $m$ segment is less than $n_{\text{ff}} = 3$ samples, the `PRE_IMPACT` condition is never satisfied and the event is discarded before the classifier is invoked.

---

## 6. Online Gaussian Naïve Bayes with Welford's Algorithm

### Model

The GNB classifier assumes that each feature $x_j$ given class $c \in \{\text{FALL}, \text{NORMAL}\}$ is Gaussian:

$$
P(x_j \mid c) = \frac{1}{\sqrt{2\pi\sigma_{c,j}^2}} \exp\!\left(-\frac{(x_j - \mu_{c,j})^2}{2\sigma_{c,j}^2}\right)
$$

The posterior (log-sum form to avoid underflow):

$$
\log P(c \mid \mathbf{x}) = \log P(c) + \sum_{j=1}^{6} \log P(x_j \mid c)
$$

The class with the higher log-posterior wins. Confidence is reported as the softmax of the two log-posteriors.

### Welford's Online Algorithm

Rather than storing all historical samples, the classifier maintains only three accumulators per feature per class:

- $n_{c,j}$ — count of samples seen for class $c$, feature $j$
- $\mu_{c,j}$ — running mean
- $M_{c,j}$ — running sum of squared deviations (Welford's M)

**Update rule** (called once per confirmed event, with the feature vector of that event labelled by class $c$):

$$
\delta = x_j - \mu_{c,j}
$$
$$
n_{c,j} \leftarrow n_{c,j} + 1
$$
$$
\mu_{c,j} \leftarrow \mu_{c,j} + \frac{\delta}{n_{c,j}}
$$
$$
M_{c,j} \leftarrow M_{c,j} + \delta \cdot (x_j - \mu_{c,j})
$$
$$
\sigma^2_{c,j} = \frac{M_{c,j}}{n_{c,j}}
$$

**Why Welford's?** The naïve two-pass formula $\sigma^2 = \overline{x^2} - \bar{x}^2$ suffers from catastrophic cancellation when $\sigma^2 \ll \bar{x}^2$ (common with near-1-g magnitude data). Welford's algorithm is numerically stable and requires no stored history — only the three accumulators above.

**Why GNB?** Gaussian Naïve Bayes requires only $2 \times 6 \times 3 = 36$ floats for the classifier state (2 classes × 6 features × {count, mean, M}). At 4 bytes per float that is **144 bytes** — trivial for the ESP32's 320 KB SRAM. It also supports incremental updates, making true online learning possible without a training/deployment split.

---

## 7. Adaptive Confidence Threshold θ

The initial threshold is $\theta_0 = 0.70$.

After each confirmed event (button pressed or timeout):

$$
\theta \leftarrow \theta + \alpha \cdot (y - \theta)
$$

where:
- $\alpha = 0.1$ is the exponential learning rate.
- $y = 1.0$ if the event was a true fall (button not pressed, or labelled via PC logger).
- $y = 0.0$ if the event was a false alarm (button pressed within alarm window).

This is an **exponential moving average** of the per-event label. If the wearer generates many false positives (e.g. an active manual worker), $\theta$ gradually rises to demand higher classifier confidence before alarming. Conversely, for a sedentary or elderly wearer with few high-jerk activities, $\theta$ may decrease slightly to improve sensitivity.

The threshold is bounded to $[0.55,\, 0.95]$ to prevent degenerate collapse.

---

## 8. Activity Inference

The system infers the wearer's current activity by monitoring a **5-second rolling window** of mean magnitude $\bar{m}_{5s}$ and standard deviation $\sigma_{5s}$:

| Condition | Inferred Activity |
|-----------|-------------------|
| $\bar{m}_{5s} < 1.05\,\text{g}$ and $\sigma_{5s} < 0.05\,\text{g}$ | SITTING |
| $\bar{m}_{5s} \in [1.05, 1.20]\,\text{g}$ or $\sigma_{5s} \in [0.05, 0.15]\,\text{g}$ | WALKING |
| $\bar{m}_{5s} > 1.20\,\text{g}$ or $\sigma_{5s} > 0.15\,\text{g}$ | RUNNING |

Activity inference affects the free-fall threshold $\theta_{\text{ff}}$ via a small correction:

$$
\theta_{\text{ff}}^{\text{eff}} = \theta_{\text{ff}} \cdot (1 + 0.1 \cdot k_{\text{act}})
$$

where $k_{\text{act}} \in \{0, 1, 2\}$ for SITTING / WALKING / RUNNING respectively. This makes the pre-impact gate slightly looser for more active wearers, preventing missed detections during dynamic gait.

---

## 9. Memory Footprint Analysis

| Data structure | Size |
|----------------|------|
| SMA filter buffers (5 × 3 axes × 4 B) | 60 B |
| Sliding window buffer (40 samples × 4 B) | 160 B |
| GNB classifier state (36 floats × 4 B) | 144 B |
| Rolling 5-s activity window (250 samples × 4 B) | 1 000 B |
| Serial TX ring buffer (Arduino default) | 256 B |
| Program stack + misc | ~2 000 B |
| **Total (estimated)** | **~3.6 KB** |

FallGuard v2 therefore uses approximately **1.1%** of the ESP32's 320 KB SRAM, leaving ample headroom for future features (BLE stack, SD card buffering, OTA).

---

## 10. Comparison with Related Work

### Threshold-based systems (e.g. SisFall dataset benchmarks)

Traditional fall detectors (Le Boutillier et al., Bourke et al.) use fixed acceleration and angle thresholds derived from population studies. They are:

- **Fast** to implement and run.
- **Brittle** — a single global threshold cannot handle inter-subject variability (body mass, gait, fall style).
- **Unable to learn** — require manual re-tuning per deployment.

### FallGuard v2 advantages

| Property | Fixed-threshold | FallGuard v2 |
|----------|----------------|--------------|
| Per-subject adaptation | ✗ | ✓ (online GNB + adaptive θ) |
| Activity context | ✗ | ✓ (3-class inference) |
| Jerk discrimination | Partial | ✓ (temporal gating) |
| Training data required | Population dataset | None (cold start) |
| Inference RAM | < 100 B | ~3.6 KB |
| Incremental update cost | O(1) | O(F) where F=6 features |
| False positive suppression | Moderate | High (3-phase gate) |

The primary trade-off is **cold-start performance**: with zero prior falls recorded, the GNB reverts to its class prior (50/50) and the 3-phase engine carries the full classification burden. Performance improves after the first 5–10 labelled events as Welford's statistics converge.
