# Execution-model calibration plan

**Status:** model is currently calibrated by hand. The OPTIMISTIC /
NORMAL / PUNITIVE profiles are *adversarial intuitions*, not empirical
measurements. Every conclusion in `EXECUTION_AUDIT.md` and the Tier-1
Monte Carlo report rests on that. Until calibrated, the strategy
**cannot** be trusted with capital — even a modest miscalibration
(say, NORMAL is actually 20% optimistic on CL fills) flips the
posterior.

This plan describes how to replace each hand-tuned knob with a number
that came from live execution data, with proper uncertainty quantified.

---

## 1 · What we are calibrating

| Knob | Profile defaults (low/med/high vol) | Calibration target |
|---|---|---|
| `limit_fill_prob_*` | 0.55..0.90 | Beta-Binomial posterior P(fill ∣ touch, regime) |
| `partial_fill_prob` | 0.05..0.35 | Beta-Binomial posterior P(partial ∣ fill, regime) |
| `partial_fill_qty_pct` | 0.30..0.75 | Empirical Bayes mean of filled-fraction distribution |
| `stop_slip_atr_frac_*` | 0.02..0.60 | Robust percentile (p50, p75, p95) of (realized-slip / ATR) |
| `stop_slip_min_ticks_*` | 0.5..5.0 | Empirical p10 of realized-slip (in ticks) |
| `stop_slip_elevated_mult` / `_blackout_mult` | 1.2..4.0 | Ratio of slip percentile in news window vs out-of-news |
| `session_slip_mult` | 1.0..1.7 | Ratio of slip per session vs LONDON baseline |
| `session_fill_mult` | 0.5..1.0 | Ratio of fill rate per session vs LONDON baseline |

Each knob will be one of:

- a **Beta-Binomial posterior** (probabilities)
- a **percentile of a robust distribution** (slippage magnitudes)
- a **ratio of two of the above** (regime multipliers)

---

## 2 · Bayesian framework for fill probability

Limit fills are binary trials with regime-dependent success rate. Beta
is the natural conjugate.

```
prior:   p ~ Beta(α₀, β₀)
data:    k fills out of n attempts in a given (symbol, vol, news, session) cell
post:    p | data ~ Beta(α₀ + k, β₀ + n − k)
```

### Priors

- **Symbol-pooled prior** per profile is the current hand-tuned value
  treated as a weak Beta(2, 2) → Beta(3.6, 1.6) for OPTIMISTIC 0.69
  fill rate, etc. **Effective sample size of the prior = 5 trials.**
  This lets observed data dominate after ~30-50 real fills.
- Cell-specific prior is the symbol-pooled posterior. This is a
  classic hierarchical empirical-Bayes setup.

### Cells

The cell grid is **symbol × vol regime × news regime × session** —
4 × 3 × 3 × 4 = **144 cells**. Most will be empty for months. The
hierarchical prior is what keeps cells with n=0..3 reasonable.

### Estimate to report

Use the **5th percentile of the Beta posterior** as the calibrated
fill probability, not the mean. That's an explicitly pessimistic
choice consistent with the strategy's "prove no edge" posture.

```python
p_calibrated = scipy.stats.beta.ppf(0.05, alpha_post, beta_post)
```

### When to update the model

| Trigger | Action |
|---|---|
| Cell has ≥ 30 trials | Replace symbol-pooled prior with cell-specific posterior |
| Symbol has ≥ 100 trials across all cells | Replace global prior with symbol posterior |
| Posterior shifts > 0.10 from current profile | Re-run Monte Carlo, alert operator |

---

## 3 · Robust percentile estimation for slippage

Slippage is unbounded above (rare gap-throughs) and bounded below by 0
or a small adverse fill. The **mean is the wrong statistic** — it's
dominated by tail events. We need percentiles.

### Estimator

- p50 (median): primary "typical" slip
- p75: NORMAL profile's calibrated value
- p95: PUNITIVE profile's calibrated value
- p99: dimensional check — if p99 / p50 > 10 the model is too
  optimistic in shape, not just magnitude

### Implementation

```python
def calibrate_slip_pts(realized_slip_pts: pd.Series,
                       atr_pts: pd.Series) -> dict[str, float]:
    """Return calibrated slip percentiles per ATR unit.

    Why per-ATR: makes the number portable across vol regimes.
    """
    ratio = (realized_slip_pts / atr_pts).dropna()
    if len(ratio) < 10:
        return {"insufficient_data": True, "n": len(ratio)}
    return {
        "p50": float(ratio.quantile(0.50)),
        "p75": float(ratio.quantile(0.75)),
        "p95": float(ratio.quantile(0.95)),
        "p99": float(ratio.quantile(0.99)),
        "n": len(ratio),
        # Bootstrap CI on p75 to know the uncertainty around the NORMAL value
        "p75_ci_low":  float(_bootstrap_ci(ratio, q=0.75, ci=0.05)[0]),
        "p75_ci_high": float(_bootstrap_ci(ratio, q=0.75, ci=0.05)[1]),
    }
```

### Bootstrap CI

500 resamples × `np.quantile`. Used to flag knobs whose CI is wide
enough that the simulator's expectancy is sensitive to the choice.

### When to update

| Trigger | Action |
|---|---|
| Cell has ≥ 20 stop hits | Replace `stop_slip_atr_frac_*` with measured p75 |
| Cell has ≥ 50 stop hits | Tighten PUNITIVE to p95 of cell |
| p95 grows > 1.5× over a 30d rolling window | Alert: market regime shifted |

---

## 4 · Per-symbol / per-session decomposition

The current profile pretends a single number works for ES NY_AM low vol
*and* CL OVERNIGHT high vol. That's the audit's biggest hand-wave.

### Plan

Calibrate **per (symbol, session)** for the high-value cells:

- ES × NY_AM (the prime trading window — high data volume)
- CL × NY_AM
- NQ × NY_AM
- (and LONDON for the same three)

Lower-volume cells fall back to the symbol-pooled posterior.

### Output

`config/execution_calibration.yaml` — auto-generated by
`backtest.calibrate` (to be built):

```yaml
schema_version: 1
generated_at: 2026-04-16T17:42:00Z
generated_from:
  source: live_trades_resolved.jsonl
  date_range: [2026-02-01, 2026-04-16]
  n_total_trades: 217

ES:
  NY_AM:
    n_attempts: 84
    n_fills: 51
    fill_prob:
      posterior_mean: 0.607
      posterior_p5: 0.527
      posterior_p95: 0.683
    stop_slip_atr_frac:
      n: 19
      p50: 0.118
      p75: 0.184
      p95: 0.342
      p75_ci: [0.142, 0.231]
  LONDON:
    n_attempts: 12
    fill_prob:
      posterior_mean: 0.594   # pulled to symbol prior — too few
      posterior_p5:   0.420
      posterior_p95:  0.755
```

### Consumed by

`backtest.execution_model.ExecutionProfile.from_calibration(
    sym, session, vol, news, profile_name="NORMAL"
)`

A `CALIBRATED` profile is added alongside OPTIMISTIC / NORMAL /
PUNITIVE. It reads YAML, falls back to the matching profile when a
cell has insufficient data.

---

## 5 · Data pipeline (where the trial counts come from)

Source: `~/.ict-bot/logs/live_trades_resolved.jsonl` — the
reconciler's output that joins paper-trade orders with their realized
fills.

For each row we need:

| Field | Source | Notes |
|---|---|---|
| `attempted_at` | order log | already exists |
| `intended_price` | order log | already exists |
| `realized_price` | broker fill | already exists |
| `bar_high`, `bar_low` at attempted_at | candle around attempt | NEW — must join bar context |
| `atr_pts` at attempted_at | computed | NEW — derive in calibrator |
| `vol_regime` | derived | derive from `atr / price` per profile thresholds |
| `news_regime` | derived | reuse `classify_news` against ForexFactory cache |
| `session` | derived | reuse `classify_session` |
| `outcome` | broker | filled / partial / missed / cancelled |

`backtest.calibrate` joins these and emits the YAML in §4.

---

## 6 · Diagnostic gates ("is the model actually calibrated yet?")

Before declaring the model "calibrated" enough to trust:

| Gate | Threshold | Why |
|---|---|---|
| Total resolved trades | ≥ 100 | Below this, posterior is still prior |
| Per-symbol resolved trades | ≥ 30 each on ES, NQ, CL | Per-symbol estimate needs data |
| Per-session-per-symbol | ≥ 15 on the **two** highest-volume sessions | Lower-volume sessions can fall back |
| Days of live data | ≥ 30 calendar | Capture at least one news cycle, one CPI, one FOMC |
| Calibration shifts profile expectancy by | < 0.25R from current NORMAL | If it shifts more, re-run Tier-1 MC |

All five must pass. Until then the simulator's profile must be the
**worse** of (NORMAL, calibrated). Asymmetric prudence by design.

---

## 7 · Continuous re-calibration

Once calibrated, **the model drifts**. Markets change. Re-fit:

| Cadence | What |
|---|---|
| Daily | Roll the resolved-trades window, recompute posteriors |
| Weekly | Recompute slip percentiles, refresh `execution_calibration.yaml` |
| Monthly | Re-run Tier-1 Monte Carlo with the calibrated profile, verify expectancy hasn't crossed below the go-live threshold |
| Per-event | After FOMC / CPI / NFP, audit fills explicitly — flag rows where realized slip > p95 cached value |

A drift alarm fires if any of:

- new posterior mean shifts > 0.10 absolute from prior week
- p95 slip grows > 1.5× over rolling 30d
- cell-fill-rate measurement N drops below half its previous value
  (data collection broken)

---

## 8 · What this plan deliberately does NOT do

- **No volatility-modeling of the underlying**. ATR is enough. GARCH
  / stochastic vol is over-engineering for a one-operator paper test.
- **No per-broker calibration**. The plan assumes one broker
  (Tradovate paper). When we add a second broker, fork the YAML by
  broker, do not pool.
- **No reinforcement-learning correction**. The model is calibrated,
  not learned. The strategy's edge — if any — must survive a fixed,
  honestly-measured execution model, not one that adapts to make the
  strategy look better.

---

## 9 · Implementation footprint

This plan, when implemented:

- **+1 module** `backtest/calibrate.py` (~250 lines)
- **+1 module** `backtest/execution_model.py::CalibratedProfile`
  (~80 lines)
- **+1 config** `~/.ict-bot/execution_calibration.yaml`
  (auto-generated, do not hand-edit)
- **+1 daemon-cron** in `live/scheduler.py` to refresh weekly
- **+5 tests** in `tests/test_calibration.py`

Estimated effort: 1.5 days. **Blocking constraint:** need the live
trade log to have ≥ 100 trades — which is itself the gate for any
go-live consideration.

---

## 10 · Bottom line

The current Tier-1 verdict — "WATCHLIST except CL which is NOT
PROVEN" — is contingent on the hand-tuned NORMAL profile being
roughly accurate. This plan is how we replace the word "roughly"
with a number that has a confidence interval. Until then no
allocation decision can claim to be informed by the model.
