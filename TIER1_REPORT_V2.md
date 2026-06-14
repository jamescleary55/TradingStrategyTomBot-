# Tier-1 v2 — adversarial second-pass review

**Posture:** risk-manager. The job is to stop deployment. Every
inconvenient finding is promoted; convenient ones are stress-tested
twice.

**Sources for this report:**
- `python -m backtest.regime_montecarlo --symbols NQ,ES,CL --days 180 --block-resamples 60`
- `python -m backtest.nq_diagnostic --symbols NQ,ES,CL`
- `python -m backtest.slip_audit --symbols NQ,ES,CL`
- `python -m backtest.interaction_fragility --symbols ES,CL --seeds 50`

JSON snapshots at `/tmp/regime_mc.json`, `/tmp/interaction_fragility.json`.

---

## 1 · Phase 1 — REGIME MONTE CARLO

Varies the *price history*, holding execution as NORMAL profile.
Four methods, all preserving local microstructure.

### Headline matrix — does the strategy survive market-regime variation?

| Symbol | Method | Runs | Mean R | Median R | 5%ile | 95%ile | P(>0) | P(>+0.25) | P(DD>3R) | Med closed |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **NQ** | rolling 60d/10d | 6 | +0.82 | +0.46 | +0.00 | +2.00 | 67% | 67% | 0% | 1 |
| | block bootstrap (15d×12) | 60 | +0.75 | +0.94 | **-0.60** | +2.00 | 85% | 80% | 3% | 5 |
| | quarters | 3 | +1.33 | +2.00 | +0.20 | +2.00 | 67% | 67% | 0% | 1 |
| | vol_low blocks | 60 | +1.02 | +1.01 | +0.14 | +1.66 | 97% | 90% | 3% | 9 |
| | vol_high blocks | 60 | **-0.01** | +0.01 | **-1.19** | +2.00 | 50% | 38% | 27% | 4 |
| **ES** | rolling 60d/10d | 6 | +0.67 | +0.59 | **-0.36** | +1.84 | 50% | 50% | 33% | 4 |
| | block bootstrap | 60 | +0.60 | +0.56 | **+0.15** | +1.18 | 95% | 85% | 33% | 17 |
| | quarters | 3 | +1.23 | +2.00 | **-0.08** | +2.00 | 67% | 67% | 0% | 3 |
| | vol_low blocks | 60 | +0.35 | +0.35 | **-0.19** | +0.80 | 90% | 68% | 57% | 19 |
| | vol_high blocks | 60 | **-0.02** | -0.06 | **-0.76** | +0.98 | 45% | 25% | 55% | 9 |
| **CL** | rolling 60d/10d | 6 | -0.11 | +0.11 | **-0.99** | +0.44 | 67% | 33% | 17% | 4 |
| | block bootstrap | 60 | **-0.00** | -0.04 | -0.55 | +0.85 | 43% | 20% | 65% | 12 |
| | quarters | 3 | +0.19 | +0.16 | +0.02 | +0.39 | 67% | 33% | 0% | 2 |
| | vol_low blocks | 60 | +0.06 | +0.07 | -0.36 | +0.51 | 58% | 25% | 85% | 22 |
| | vol_high blocks | 60 | +0.20 | +0.19 | -0.69 | +1.07 | 62% | 47% | 30% | 8 |

### Methods with 5%ile > 0R

| Symbol | passes 5%ile>0 | passing methods |
|---|---|---|
| NQ | **2/5** | quarters (n=3), vol_low (60) |
| ES | **1/5** | block_bootstrap only |
| CL | **1/5** | quarters (n=3, trivial) |

### Quarter-by-quarter breakdown (the most adversarial cut)

| Symbol | 2025-Q4 | 2026-Q1 | 2026-Q2 |
|---|---|---|---|
| NQ | +2.00R (1 closed) | **+0.00R (0 closed)** | +2.00R (1 closed) |
| ES | +2.00R (2 closed) | +2.00R (3 closed) | **-0.31R (4 closed)** |
| CL | +0.00R (0 closed) | +0.16R (5) | +0.42R (2) |

**Three observations the operator should not move past:**

1. **ES — the strongest symbol — went negative in the most recent
   quarter.** This is the only quarter with 4+ closed trades. Older
   quarters with +2R have 2-3 closed each, each pinned at the +2R
   target ceiling. The strategy may simply be running out of edge in
   the regime that exists *right now*.

2. **High-volatility blocks collapse expectancy on every symbol.**
   NQ -0.01R, ES -0.02R, CL +0.20R with 5%ile -0.69R. The strategy
   appears engineered for low-vol environments. Any forward period
   that resembles 2025 spring vol would invalidate the edge.

3. **Only ES passes 5%ile > 0 under block bootstrap** — the most
   honest "alternative-history" test. NQ block bootstrap 5%ile is
   -0.60R; CL is -0.55R. Quarters and rolling windows pass because
   n is too small to falsify (3-6 runs).

---

## 2 · Phase 2 — NQ SAMPLE VALIDITY

The brief disallows NQ as WATCHLIST unless sample is sufficient.
Diagnostic counted attrition through every funnel stage.

### Pre-simulator (setup-detector output)

| Symbol | Bars | Setups | Bull/Bear | HTF-aligned | Session split |
|---|---:|---:|---:|---:|---|
| NQ | 2825 | 31 | 14/17 | 14 | NY_AM=11, NONE=13, NY_PM=4, ASIA=2, LONDON=1 |
| ES | 2824 | 26 | 13/13 | 7 | NY_AM=12, NONE=8, NY_PM=3, ASIA=2, LONDON=1 |
| CL | 2806 | 33 | 15/18 | 9 | NY_AM=13, NONE=13, LONDON=3, ASIA=3, NY_PM=1 |

### Simulator funnel — seed 42 (representative)

| Symbol | Setups | Closed | Voided | Timed out | Skipped | Limit attempts | Filled | Missed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| NQ | 31 | 2 | 0 | 2 | **27** | 9 | 4 | 5 |
| ES | 26 | 12 | 1 | 3 | 10 | 49 | 20 | 29 |
| CL | 32 | 5 | 0 | 9 | 18 | 1690 | 7 | 1683 |

### Skip reasons (the real killer)

| Symbol | Reason | Count |
|---|---|---:|
| NQ | **risk-per-contract exceeds per-trade cap** | **27** |
| ES | risk-per-contract exceeds per-trade cap | 7 |
| ES | position already open | 3 |
| CL | position already open | 18 |

### Diagnosis

| Symbol | Verdict | Root cause |
|---|---|---|
| NQ | **INSUFFICIENT DATA** | The detector finds 31 valid setups, but the 0.5% risk cap on $50k ($250 / trade) is too small for NQ's stop distances at 1h. **27 of 31 setups never enter the queue.** This is a sizing problem, not a strategy problem. To evaluate NQ at all, either raise risk pct (changes the test), use micro NQ with smaller $/point (would let larger stops through), or move to 15m/30m where stop distances shrink. Currently the strategy on NQ at 1h has no funded sample. |
| ES | **INSUFFICIENT DATA** | 13 median closed across seeds < 20-trade floor. Median fill rate ~40%. ES is the closest to qualifying but the sample is still thin. Needs ≥ 365d at 1h or move to 30m. |
| CL | **INSUFFICIENT DATA** | 7 median closed. 18 setups skipped because the prior trade is still open — CL setups cluster. The 1690 "limit attempts" reflect retry-on-every-bar through the 24-bar timeout window; real distinct fill opportunities are ~33 (one per setup). |

The brief's strict rule applies cleanly: **none of NQ, ES, CL has
enough closed trades to qualify as VALID SAMPLE under this profile,
period, and timeframe.**

---

## 3 · Phase 3 — SLIPPAGE AUDIT

The earlier headline `median_slippage_pts ≈ 0` is now confirmed as
**SIM_DESIGN_ARTIFACT**.

### Slip by exit type (seed 42)

| Symbol | Population | N | min | p25 | median | p75 | p95 | max | mean | % exactly 0 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| NQ | entry | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 100% |
| | target | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 100% |
| | **stop** | 1 | 26.55 | 26.55 | **26.55** | 26.55 | 26.55 | 26.55 | 26.55 | 0% |
| ES | entry | 11 | 0 | 0 | 0 | 0 | 0.25 | 0.25 | 0.05 | 82% |
| | target | 7 | 0 | 0 | 0 | 0.12 | 0.25 | 0.25 | 0.07 | 71% |
| | **stop** | 4 | 0.55 | 0.60 | **2.10** | 3.88 | 4.60 | 4.78 | 2.39 | 0% |
| CL | entry | 9 | 0 | 0 | 0 | 0 | 0.006 | 0.013 | 0.001 | 89% |
| | target | 3 | 0 | 0 | 0 | 0 | 0.009 | 0.011 | 0.003 | 67% |
| | **stop** | 5 | 0.04 | 0.04 | **0.05** | 0.14 | 0.15 | 0.15 | 0.09 | 0% |

### Findings

- **Stop slip is real and material.** ES median 2.10 pts, p95 4.60
  pts. On a typical ES 1R = 8-12 pts, that's 18-25% R drag *per
  stopped trade*. NQ single stop hit 26.55 pts — call this anecdotal
  but representative of an unbounded right tail.
- **Limit slip is 67-100% exactly zero by design** —
  `attempt_limit_fill` sets `queue_slip = 1 tick` with probability
  30%, else 0. The headline median is dominated by these zeros.
- **The simulator's `median_slippage_pts` is misleading.** Replace
  with two metrics: `median_stop_slippage_pts` (the honest stop slip)
  and `limit_queue_slip_rate_pct`. The current pooled stat should be
  deprecated.

**Verdict per category:** `SIM_DESIGN_ARTIFACT` confirmed on CL by
the auditor's heuristic. NQ and ES too few stops to formally
classify, but the structural cause is the same.

---

## 4 · Phase 4 — 2-WAY INTERACTION FRAGILITY

Single-knob fragility found ES robust (max swing 0.07R). Hypothesis
under test: does ES robustness survive when two knobs move together?

### ES — baseline +0.81R

| Pair | Both worse | Both better | A worse / B better | A better / B worse | Δ both-worse | Closed | Win% |
|---|---:|---:|---:|---:|---:|---:|---:|
| fill_p_med × partial_rate | **+0.69R** | +0.82R | +0.73R | +0.82R | -0.12R | 11 | 57% |
| fill_p_med × stop_slip_atr | +0.71R | +0.82R | +0.75R | +0.78R | -0.10R | 11 | 59% |
| fill_p_high × fill_p_med | +0.74R | +0.81R | +0.81R | +0.74R | -0.07R | 11 | 59% |
| stop_slip × news_blackout_mult | +0.78R | +0.83R | +0.78R | +0.83R | -0.03R | 13 | 61% |
| partial_rate × partial_qty | +0.83R | +0.79R | +0.83R | +0.79R | +0.02R | 12 | 62% |

**ES verdict: ROBUST.** Even both-worse pairs stay above +0.69R. No
pair flips expectancy negative. Caveat: news_blackout_mult shows
near-zero impact because no news timeline is loaded — that pair is
under-tested.

### CL — baseline +0.32R

| Pair | Both worse | Both better | A worse / B better | A better / B worse | Δ both-worse | Closed | Win% |
|---|---:|---:|---:|---:|---:|---:|---:|
| **fill_p_high × fill_p_med** | **-0.14R** | +0.53R | +0.25R | +0.28R | **-0.46R** | 7 | 32% |
| fill_p_med × partial_rate | +0.07R | +0.34R | +0.03R | +0.36R | -0.25R | 8 | 38% |
| fill_p_med × stop_slip_atr | +0.08R | +0.41R | +0.11R | +0.38R | -0.24R | 9 | 39% |
| partial_rate × partial_qty | +0.23R | +0.30R | +0.23R | +0.30R | -0.09R | 10 | 43% |
| stop_slip × news_blackout_mult | +0.30R | +0.33R | +0.30R | +0.33R | -0.02R | 10 | 46% |

**CL verdict: FRAGILE.** Just moving two fill-probability knobs
worse flips CL to **-0.14R**. The single-knob test understated this
because the two knobs (`fill_p_high`, `fill_p_med`) drive overlapping
populations of trades — perturbing them together has a multiplicative
effect.

---

## 5 · UPDATED SYMBOL CLASSIFICATIONS

| Symbol | Tier-1 v1 verdict | Tier-1 v2 verdict | Why changed |
|---|---|---|---|
| NQ | WATCHLIST | **INSUFFICIENT_DATA** | Brief's volume rule + sizing exclusion (27/31 setups blocked by per-trade cap). The strategy on NQ 1h is functionally unfunded under the operator's risk profile. Cannot be reclassified WATCHLIST without a different timeframe or larger account. |
| ES | WATCHLIST | **INSUFFICIENT_DATA / WATCHLIST** | Quantitatively borderline. ES is the *only* symbol passing block-bootstrap 5%ile>0 (+0.15R) and remains ROBUST to 2-way perturbation. But: median closed is 13 < 20 floor, and the most-recent calendar quarter went **-0.31R**. The brief's strict rule wins: classify INSUFFICIENT_DATA. **If forced to choose a tradable** symbol it would still be ES — but the brief is explicit that ES cannot earn WATCHLIST without more closed trades. |
| CL | DISABLE | **DISABLE** | Reconfirmed. Block-bootstrap mean -0.00R, 5%ile -0.55R. FRAGILE under 2-way perturbation. NORMAL CI still crosses 0 from the previous test. Operator must remove `MCL` from `personal_rules.yaml` — the `tier1_not_proven_excluded` gate is failing in the live go-live evaluator. |

### Operational implication

```diff
- allowed_symbols: [MNQ, MES, MCL]
+ allowed_symbols: [MES]            # MNQ INSUFFICIENT_DATA (sizing); MCL DISABLED
```

But: even MES does not qualify as WATCHLIST until either (a)
365-day yfinance window gives ≥ 20 closed median, or (b) Tradovate
forward-paper accumulates ≥ 30 closed trades to calibrate the model
honestly. **The strategy has no symbol that earns even WATCHLIST
under v2.**

---

## 6 · FINAL QUESTION — what is most likely to invalidate the edge?

Risk manager ranking, **most likely to be fatal** first:

### #1 · REGIME UNCERTAINTY (most dangerous)

**Evidence:**
- ES most-recent quarter -0.31R after two +2.00R quarters.
- All three symbols collapse under vol_high block bootstrap
  (NQ -0.01R, ES -0.02R, CL +0.20R w/ 5%ile -0.69R).
- ES survives block bootstrap by a single percentile (5%ile = +0.15R).

**Why fatal:** the strategy has been validated on a window that
already represents one regime. The regime MC says the next 6 months
of trading could land in a vol_high regime (the test's worst case)
and expectancy collapses to break-even or negative. Unlike sample
size, **this is structural, not solvable by waiting**. Adding more
of the same kind of data does not protect against regime shift.

### #2 · SAMPLE-SIZE UNCERTAINTY

**Evidence:**
- ES: 12 closed / 180d. NQ: 1-5 / 180d. CL: 5-10 / 180d.
- Brief's volume floor: 20 closed. **Nobody hits it.**
- 27/31 NQ setups blocked by sizing alone.

**Why fatal:** with median 13 closed on ES, the difference between
"real +0.78R edge" and "lucky 13-trade run" is below the noise
floor of any honest statistical test. Confidence intervals are
wide enough that a 1R deterioration would still look plausible.

**Why partially solvable:** wait 365d or move to a faster timeframe.
But: faster timeframes change execution model parameters; switching
TF restarts the calibration loop.

### #3 · STRATEGY FRAGILITY (single-strategy survivorship)

**Evidence:**
- Only one strategy (`sweep_choch_fvg`) has been tested.
- CL is fragile to a 2-way perturbation that flips it to -0.14R.
- ES passes only because the perturbation magnitudes are below the
  edge's noise floor.

**Why fatal:** if the strategy *is* the edge, and the edge proves
to be a low-vol-only artifact, there's nothing to fall back on. No
ensemble, no backup logic.

**Why partially solvable:** add a second strategy (e.g., mean-
reversion in NY_PM) and require both to fire on independent
populations. Out of scope of this review.

### #4 · EXECUTION UNCERTAINTY

**Evidence:**
- Profile knobs are hand-tuned, not calibrated.
- `EXECUTION_CALIBRATION_PLAN.md` exists but requires ≥ 100 resolved
  live trades to activate (currently 0).
- Slip metric is mis-aggregated — operator could chase a phantom.

**Why fatal:** if NORMAL is actually 20% optimistic, ES's +0.15R
5%ile becomes -0.05R and ES joins CL on the DISABLE list. But this
is the **least uncertain** unknown because the calibration mechanism
is well-defined.

### #5 · DATA-QUALITY UNCERTAINTY

**Evidence:**
- yfinance is 15-min delayed continuous-futures.
- Same MC on Tradovate could shift expectancy ±0.2R either way.

**Why fatal:** could be ±0.2R, which in this context is meaningful
but bounded. Lowest leverage of the five.

---

## 7 · RISK MANAGER'S BOTTOM LINE

> **Recommendation: do not deploy. Continue paper-only on MES at
> existing 0.25R/trade. Disable MCL immediately. Do not attempt MNQ
> at 1h until sizing is resolved.**

The strategy clears no Tier-1 v2 gate. The strongest symbol (ES)
went negative in the most recent quarter and has too few closed
trades for any positive-expectancy claim to be statistically
distinguishable from noise. Regime variation is the dominant
unknown: the strategy appears engineered for the regime that
produced the test data, and degrades sharply outside it.

The single biggest unknown is **regime uncertainty**. Sample size
is a close second only because more data of the *current* regime
will not protect against the regime change that the bootstrap
already shows hurts the strategy.

— end of report —
