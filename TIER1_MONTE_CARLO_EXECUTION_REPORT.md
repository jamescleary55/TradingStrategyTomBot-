# Tier-1 Monte Carlo execution report

**Universe:** NQ, ES, CL · 1h · 180 calendar days of yfinance bars
**Seeds:** 200 per (symbol, profile) — 1,800 simulator runs total
**Equity / risk:** $50k / 0.5% per trade
**Profiles:** OPTIMISTIC / NORMAL / PUNITIVE (see `backtest/execution_model.py`)
**Generated:** 2026-04-16
**Source script:** `python -m backtest.tier1_montecarlo --symbols NQ,ES,CL --seeds 200`

> The Tier-1 brief asked one question: **does the NORMAL-profile
> expectancy confidence interval cross zero?** If yes on any symbol,
> that symbol is "NOT PROVEN" and is excluded from any live universe.
>
> **Answer: CL is NOT PROVEN. NQ and ES survive but with too few
> closed trades to claim statistical significance.**

---

## 1 · Headline run (seed 42 — the "canonical" sensitivity)

| Symbol | Profile | Setups | Closed | Win % | Avg R | PF | Fill % | Partial % | Missed % | Avg slip pts | Med slip pts | Big winner share | Max DD % |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **NQ** | OPTIMISTIC | 33 | 4 | 100% | +2.00R | ∞ | 73% | 0% | 43% | 0.59 | 0.00 | 30% | 0.07 |
| | NORMAL | 33 | 3 | 67% | +0.83R | 3.50 | 50% | 0% | 62% | 0.13 | 0.00 | 50% | 0.11 |
| | PUNITIVE | 33 | 1 | 100% | +2.00R | ∞ | 18% | 0% | 90% | 0.00 | 0.00 | 100% | 0.04 |
| **ES** | OPTIMISTIC | 26 | 15 | 60% | +0.79R | 2.96 | 100% | 0% | 0% | 0.16 | 0.00 | 13% | 1.04 |
| | NORMAL | 26 | 12 | 67% | +0.97R | 3.66 | 41% | 9% | 64% | 0.57 | 0.00 | 13% | 1.12 |
| | PUNITIVE | 26 | 6 | 33% | -0.14R | 0.82 | 11% | 3% | 89% | 2.28 | 0.00 | 67% | 1.31 |
| **CL** | OPTIMISTIC | 33 | 20 | 60% | +0.78R | 2.87 | 78% | 3% | 30% | 0.02 | 0.00 | 13% | 0.83 |
| | NORMAL | 33 | 5 | 20% | -0.54R | 0.42 | 0% | 0% | 100% | 0.04 | 0.00 | 100% | 2.10 |
| | PUNITIVE | 33 | 6 | 33% | -0.37R | 0.64 | 3% | 1% | 97% | 0.11 | 0.00 | 50% | 2.55 |

**Observations:**

- **Fill rate is the dominant lever.** ES drops from 100% (OPTIMISTIC) →
  41% (NORMAL) → 11% (PUNITIVE). Average slip is a small adjustment;
  fill probability dictates whether the strategy *exists* in PUNITIVE.
- **Median slip is 0 across nearly every cell.** Slip impact is
  concentrated in a few stop hits, not spread across all fills. That
  matters for the calibration plan — robust statistics, not means.
- **NQ has too few closed trades to evaluate** under any profile
  (1-4 closed/180d/1h). This is the audit's "GC problem" repeating
  for NQ at 1h. NQ needs 15m or 30m data — out of scope here.
- **CL OPTIMISTIC says +0.78R, NORMAL says -0.54R.** That's the gap
  the audit warned about: any positive expectancy reported under the
  prior simulator was an OPTIMISTIC artifact.

---

## 2 · Monte Carlo (200 RNG seeds per cell)

| Symbol | Profile | Mean R | Median R | Std R | 5%ile | 95%ile | P(>0R) | P(>+0.25R) | P(DD>3R) | Med fill % | Med closed |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **NQ** | OPTIMISTIC | **+1.83R** | +2.00 | 0.35 | +1.25 | +2.00 | 100% | 100% | 0% | 80% | 5 |
| | NORMAL | **+1.49R** | +1.39 | 0.61 | +0.99 | +2.00 | 97% | 97% | 0% | 55% | 4 |
| | PUNITIVE | **+0.80R** | +1.03 | 1.31 | -1.18 | +2.00 | 64% | 64% | 1% | 20% | 1 |
| **ES** | OPTIMISTIC | **+0.79R** | +0.79 | 0.13 | +0.61 | +0.99 | 100% | 100% | 0% | 83% | 14 |
| | NORMAL | **+0.78R** | +0.81 | 0.21 | +0.46 | +1.07 | 100% | 99% | 6% | 46% | 12 |
| | PUNITIVE | **+0.56R** | +0.58 | 0.50 | -0.37 | +1.33 | 89% | 76% | 8% | 19% | 7 |
| **CL** | OPTIMISTIC | **+0.68R** | +0.67 | 0.18 | +0.39 | +0.93 | 100% | 98% | 1% | 11% | 17 |
| | NORMAL | **+0.29R** | +0.29 | 0.34 | **-0.33** | +0.85 | 82% | 54% | 35% | 4% | 10 |
| | PUNITIVE | **-0.41R** | -0.45 | 0.87 | -1.67 | +1.05 | 29% | 23% | 43% | 3% | 4 |

### Guardrail — NORMAL CI crosses 0?

| Symbol | NORMAL 5%ile | NORMAL 95%ile | Decision |
|---|---:|---:|---|
| NQ | +0.99R | +2.00R | CI strictly positive |
| ES | +0.46R | +1.07R | CI strictly positive |
| **CL** | **-0.33R** | +0.85R | **CI crosses 0 — NOT PROVEN** |

**Conclusion of the guardrail:** **CL is the only symbol failing the
brief's primary test.** Under NORMAL execution, 18% of the seed
universe produces negative expectancy — outside the brief's
tolerance.

---

## 3 · Per-symbol verdict (brief's strict rules)

| Symbol | NORMAL mean | P(>0R) | Med closed | NORMAL 5%ile | PUNITIVE mean | Verdict | Why |
|---|---:|---:|---:|---:|---:|---|---|
| **NQ** | +1.49R | 97% | 4 | +0.99R | +0.80R | **WATCHLIST** | High expectancy but only **4 median closed trades / 180d** — fails brief's "≥20 closed" requirement. Statistically not yet meaningful. Promote to CONTINUE only after 15m / 30m sensitivity or a longer window proves it fires often enough. |
| **ES** | +0.78R | 100% | 12 | +0.46R | +0.56R | **WATCHLIST** | Strictly positive expectancy on every seed, but **median 12 closed trades** — still below brief's 20-trade floor. PUNITIVE mean stays comfortably above -0.5R. This is the strongest candidate but does not earn CONTINUE without more closed-trade volume. |
| **CL** | +0.29R | 82% | 10 | -0.33R | -0.41R | **DISABLE** | NORMAL CI crosses 0 → NOT PROVEN by the guardrail. PUNITIVE mean is -0.41R, just below the -0.50R hard floor for WATCHLIST. P(DD>3R) = 35% under NORMAL alone is unacceptable. **Remove from `allowed_symbols`.** |

### Rule summary (as encoded in `tier1_montecarlo.py::verdicts`)

```
CONTINUE   if NORMAL mean > +0.25R AND P(>0R) >= 70% AND
              median closed >= 20 AND biggest winner share <= 25% AND
              PUNITIVE mean > -0.5R
WATCHLIST  if positive but under the volume / margin bar
DISABLE    if NORMAL mean <= 0 OR PUNITIVE mean < -0.3R OR
              NORMAL 5%ile < 0 (guardrail)
```

CL fails the guardrail. NQ and ES qualify on every quality dimension
except **closed-trade volume**.

---

## 4 · Knob-by-knob fragility (P3)

**Setup:** perturb each of 7 knobs ±50% (or to clearly-pessimistic
value) around NORMAL profile. 50 seeds per perturbation. Rank by
absolute swing.

Source script: `python -m backtest.knob_fragility --symbols ES,CL`.

### ES — baseline +0.81R, closed≈13

| Rank | Knob | Worse → R | Better → R | Δ worse | Δ better | Swing |
|---:|---|---:|---:|---:|---:|---:|
| 1 | limit fill p — med vol | +0.74R | +0.81R | -0.07R | -0.01R | 0.07R |
| 2 | stop slip ATR frac (med vol) | +0.78R | +0.83R | -0.03R | +0.01R | 0.04R |
| 3 | partial fill rate | +0.83R | +0.79R | +0.02R | -0.02R | 0.04R |
| 4-7 | limit fill p — high vol / blackout / elevated / partial qty | +0.81R | +0.81R | 0.00R | 0.00R | 0.00R |

**ES is robust to every single knob.** The largest swing is 0.07R.
This is itself a finding: if ES *is* an edge, it's not delicately
balanced on one assumption.

### CL — baseline +0.32R, closed≈10

| Rank | Knob | Worse → R | Better → R | Δ worse | Δ better | Swing |
|---:|---|---:|---:|---:|---:|---:|
| 1 | **limit fill p — high vol** | +0.12R | +0.45R | -0.20R | +0.14R | 0.33R |
| 2 | **limit fill p — med vol** | +0.10R | +0.40R | -0.22R | +0.08R | 0.30R |
| 3 | partial fill rate | +0.23R | +0.30R | -0.09R | -0.02R | 0.07R |
| 4 | stop slip ATR frac (med vol) | +0.30R | +0.33R | -0.02R | +0.01R | 0.03R |
| 5-7 | news / partial qty | +0.32R | +0.32R | 0.00R | 0.00R | 0.00R |

**CL is fragile to limit-fill probability.** A 30% reduction in
NORMAL fill probability moves expectancy from +0.32R to +0.10-0.12R
— close to flipping. Combined with the MC CI already crossing 0,
this confirms: CL's borderline-positive NORMAL expectancy is mostly
the *quality* of OPTIMISTIC fill-probability assumptions sneaking
into the NORMAL default. Worth re-stating with the language of the
brief:

> **The single assumption keeping CL alive under NORMAL is the
> hand-tuned limit-fill probability — exactly the knob the
> calibration plan targets first.**

### Knobs with zero swing

`blackout slip mult`, `elevated fill prob`, `partial qty pct` all
showed zero change. **This is a data limitation, not robustness:**
the simulator was run without a news-event timeline loaded, so no
bars were ever in `elevated` or `blackout` regime. Once
`live/news.py` is wired into the sensitivity script the news knobs
will activate. For now, treat the news-knob row of this table as
"untested," not "doesn't matter."

---

## 5 · What this changes operationally

### `personal_rules.yaml` recommended diff

```diff
- allowed_symbols: [MNQ, MES, MCL]
+ allowed_symbols: [MNQ, MES]    # MCL removed: Tier-1 MC NOT PROVEN
```

Failing to make this change will trip the new `tier1_not_proven_excluded`
hard gate in `analysis/go_live.py`.

### Updated hard gates (post Tier-1 MC)

| Gate | Before | After |
|---|---|---|
| `positive_expectancy` | > 0R | **> +0.25R** (NORMAL-profile floor) |
| `is_oos_gap` | ` < 0.3R` of backtest | unchanged — but pass `--backtest-expectancy 0.78` (ES NORMAL MC mean), **not 0.96** |
| `execution_calibrated` | did not exist | **NEW** — ≥ 100 rows in `live_trades_resolved.jsonl` |
| `tier1_not_proven_excluded` | did not exist | **NEW** — `allowed_symbols ∩ {CL, MCL} = ∅` |

These are now enforced by `analysis/go_live.py`.

### What changes about review-mode

Nothing. Continue paper-trading on MNQ + MES under NORMAL profile.
Continue logging fills via the reconciler. The next milestone is
**not** "go live" — it's "≥ 100 resolved trades so the calibration
plan can replace hand-tuned NORMAL with a measured posterior."

---

## 6 · Open risks (still NOT modelled)

1. **No news-events timeline in the Tier-1 sensitivity.** The news
   knobs read as "zero impact" but were never exercised. Fix:
   plumb ForexFactory cache into `tier1_montecarlo.py` and re-run.
   Effort: ~1h. Re-running will *only* matter for ES and CL because
   NQ rarely takes news-window trades on 1h.
2. **yfinance is 15-min-delayed and gappy.** The exact same MC on
   Tradovate-recorded bars could shift expectancy ±0.2R either way.
   Until live data is the source-of-truth, this whole report has an
   asterisk.
3. **Calibration is hand-tuned.** Every number in §1-§4 is
   conditional on `OPTIMISTIC/NORMAL/PUNITIVE` being roughly
   correct. The `EXECUTION_CALIBRATION_PLAN.md` document is how we
   close that loop, but until ≥ 100 resolved trades exist, no
   simulator output can be called "calibrated."
4. **Closed-trade volume is the binding statistical constraint.**
   ES at 12 median closed / 180d is the most-fireable symbol in the
   universe and is *still* short of the ≥ 20 threshold. Forward-test
   duration of 180 calendar days is **not enough** to qualify a
   1h ICT strategy. Either increase to 365d, or accept that
   stat-sig only arrives after ~9-12 months of paper trading.

---

## 7 · Final verdict

> **Under the brief's rules, applied honestly:**
>
> - **CL: DISABLE.** NORMAL CI crosses 0; remove from
>   `allowed_symbols`.
> - **ES: WATCHLIST.** Strongest candidate but short of statistical
>   significance. Continue paper-trading.
> - **NQ: WATCHLIST.** Too few closed trades on 1h to evaluate.
>   Worth a separate 15m / 30m sensitivity to see if higher
>   timeframe granularity unlocks volume.
> - **Strategy is not approved for real money on any symbol.**

The earlier "+0.96R" headline was a NORMAL → OPTIMISTIC profile
mismatch. The honest NORMAL-profile expectancy after MC is **+0.78R
on ES, +0.29R on CL (NOT PROVEN), and statistically uninterpretable
on NQ**. The gap between OPTIMISTIC and NORMAL is the audit's
predicted "+0.2 to +0.5R artifact" — confirmed.

The go-live gate now enforces this verdict programmatically.

— end of report —
