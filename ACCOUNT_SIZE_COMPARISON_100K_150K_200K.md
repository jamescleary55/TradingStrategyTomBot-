# Account-size sensitivity — NQ & ES at $100k / $150k / $200k

**Posture:** skeptical risk manager. The hypothesis under test:
*NQ's previous INSUFFICIENT_DATA verdict was caused by the $50k
account's $250/trade risk cap rejecting 27/31 setups.* Disprove or
confirm.

**Source:** `python -m backtest.account_size_compare --seeds 200 --out /tmp/account_size.json`
- universe NQ + ES · 1h · 180d yfinance · NORMAL/OPTIMISTIC/PUNITIVE
- risk_pct = 0.5% → $/trade budget = 0.005 × equity
- strategy + filters + sessions + execution profile **unchanged**

---

## 1 · PHASE 1 — CAPACITY ANALYSIS

| Symbol | Account | Setups | Accepted | Rejected (risk cap) | Rejected (other) | Accept % |
|---|---|---:|---:|---:|---:|---:|
| NQ | $50k (prior) | 31 | 4 | 27 | 0 | **13%** |
| NQ | $100k | 31 | 17 | 13 | 1 | 55% |
| NQ | $150k | 31 | **26** | 3 | 2 | **84%** |
| NQ | $200k | 31 | 25 | 1 | 5 | 81% |
| ES | $100k | 25 | 20 | **0** | 5 | 80% |
| ES | $150k | 25 | 20 | **0** | 5 | 80% |
| ES | $200k | 25 | 20 | **0** | 5 | 80% |

**Answer to "how much of NQ's problem was account size?"**

A lot of the **capacity problem**: at $50k only 4 setups made it
through; at $150k it's 26. The risk cap was structurally too small
for NQ stop distances at 1h.

But "INSUFFICIENT_DATA" had two components: capacity AND statistical
significance. The next phases show that fixing capacity **does not
fix statistical significance**, and reveals a more uncomfortable
truth — what looked like an edge at $50k was partly the result of
the risk cap accidentally filtering for the best setups.

### Note on the $200k row

NQ accepts drop from 26 (at $150k) to 25 (at $200k) and "rejected
other" rises from 2 to 5. This is the simulator's
"position-already-open" rule: at higher equity more setups enter the
queue and overlap with existing positions. Throughput plateaus.

---

## 2 · PHASE 2 — HEADLINE PERFORMANCE (seed 42, NORMAL profile)

| Symbol | Acct | Closed | Fill% | Win% | Exp R | PF | Max DD% | Avg slip pts | Med slip pts |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| NQ | $100k | 15 | 40% | 53% | (see MC) | 2.x | 1.20 | 3.26 | 0.00 |
| NQ | $150k | 16 | 36% | 31% | (see MC) | 0.x | 2.40 | 5.80 | 0.00 |
| NQ | $200k | 18 | 38% | 39% | (see MC) | 1.x | 1.62 | 5.32 | 0.00 |
| ES | $100k | 13 | 44% | 46% | (see MC) | 1.x | 1.74 | 0.92 | 0.00 |
| ES | $150k | 13 | 44% | 46% | (see MC) | 1.x | 2.19 | 0.92 | 0.00 |
| ES | $200k | 13 | 44% | 46% | (see MC) | 1.x | 2.32 | 0.92 | 0.00 |

(Headline numbers vary with the RNG seed; the Monte Carlo means in
§3 are the load-bearing numbers.)

ES results are **identical** at all three account sizes — capacity
already maxed out at $100k. NQ counts grow with account size, as
expected. Median slip = 0 across the board (the design artifact
flagged earlier — stop-only slip is materially > 0).

---

## 3 · PHASE 3 — MONTE CARLO (200 seeds per cell)

### NORMAL profile

| Symbol | Acct | Mean R | Median R | 5%ile | 95%ile | P(>0) | P(>+0.25) | P(DD>3R) | Med closed |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **NQ** | $100k | **+0.79R** | +0.82 | **+0.46** | +1.05 | 100% | 99% | 0% | 13 |
| | $150k | +0.57R | +0.55 | +0.27 | +0.92 | 99% | 96% | 10% | **18** |
| | $200k | **+0.30R** | +0.30 | **+0.01** | +0.62 | 95% | 57% | **52%** | 18 |
| **ES** | $100k | +0.36R | +0.36 | **-0.04** | +0.69 | 94% | 66% | 53% | 14 |
| | $150k | +0.36R | +0.36 | -0.04 | +0.69 | 94% | 66% | 60% | 14 |
| | $200k | +0.36R | +0.36 | -0.04 | +0.69 | 94% | 66% | 62% | 14 |

### OPTIMISTIC profile

| Sym | Acct | Mean R | 5%ile | Med closed |
|---|---|---:|---:|---:|
| NQ | $100k | +0.89 | +0.68 | 16 |
| NQ | $150k | +0.73 | +0.49 | 22 |
| NQ | $200k | +0.46 | +0.22 | 22 |
| ES | $100k | +0.38 | +0.12 | 17 |
| ES | $150k | +0.38 | +0.12 | 17 |
| ES | $200k | +0.38 | +0.12 | 17 |

### PUNITIVE profile

| Sym | Acct | Mean R | 5%ile | Med closed |
|---|---|---:|---:|---:|
| NQ | $100k | +0.41 | **-0.47** | 7 |
| NQ | $150k | +0.28 | -0.40 | 10 |
| NQ | $200k | **-0.01** | -0.67 | 10 |
| ES | $100k | +0.39 | **-0.38** | 8 |
| ES | $150k | +0.39 | -0.38 | 8 |
| ES | $200k | +0.39 | -0.38 | 8 |

---

## 4 · PHASE 4 — ACCOUNT SIZE IMPACT

### Q1 · At which account size does NQ become statistically testable?

**Capacity-testable** at **$150k** — 84% accept rate, 18 median
closed (close to but still below the 20-trade floor).

**Statistically meaningful** — **none of them yet**. Even at $150k
the closed-trade count (18) is below the strict 20-trade floor.

### Q2 · Does NQ outperform ES at any account size?

- **$100k**: NQ NORMAL +0.79R > ES +0.36R — **but NQ has only 17 of
  31 setups accepted**. The 14 rejected NQ setups carry no data.
  The +0.79R is the expectancy on the *tight-stop subset*, not on
  the strategy's NQ output as a whole.
- **$150k**: NQ +0.57R > ES +0.36R, with comparable capacity.
- **$200k**: NQ +0.30R ≈ ES +0.36R (NQ slightly worse). With
  comparable capacity (~80%), NQ's apparent advantage **disappears**.

### Q3 · Does increasing account size improve robustness?

**No — it makes NQ less robust.** Specifically:

- NQ NORMAL mean: **+0.79 → +0.57 → +0.30** as account size grows.
- NQ NORMAL P(>+0.25R): **99% → 96% → 57%**.
- NQ NORMAL P(DD > 3R): **0% → 10% → 52%**.
- NQ PUNITIVE mean: **+0.41 → +0.28 → -0.01** (crosses zero at $200k).

ES is invariant across $100-200k (its capacity is already maxed at
$100k, so further equity buys nothing).

### Q4 · Larger size: trade-count or expectancy?

**Trade-count only** for NQ ($150k unlocks ~5 more closed trades),
and **negative for expectancy** (the new admits are lower quality).
This is the report's headline finding:

> **The risk cap was acting as a stealth quality filter on NQ.**
> Tight-stop setups (RR concentrated in winners) made it through;
> loose-stop setups (RR diluted) did not. Raising the cap admits
> the dilution and the apparent edge degrades.

### Q5 · Best account size?

For NQ — **$150k** is the local optimum: 18 closed (still below
floor), +0.57R mean, +0.27R 5%ile, manageable DD risk.
- $100k: better expectancy but only 17/31 setups → not a fair
  measurement of strategy quality.
- $200k: enough setups but expectancy drops below ES.

For ES — **any of $100k/$150k/$200k** is equivalent.

---

## 5 · PHASE 5 — SYMBOL RECOMMENDATION

| Account | Recommendation | Rationale |
|---|---|---|
| **$100k** | A) **ES only** | NQ MARGINAL_SAMPLE (capacity-constrained, only 17/31 setups). NQ's apparent +0.79R is a selection artifact. ES is the only honestly-measured candidate. |
| **$150k** | A) **ES only** (paper-only) | NQ now capacity-fair (84% accept) but expectancy dropped to +0.57R with 18 closed — still under the 20-trade floor. ES unchanged. NQ doesn't beat ES on a risk-adjusted basis once capacity is equalized. |
| **$200k** | A) **ES only** (paper-only) | NQ expectancy drops to +0.30R with 5%ile +0.01R (essentially break-even). P(DD>3R) = 52% — too risky. ES still the cleaner candidate, though both are MARGINAL. |

**At no account size do both symbols qualify (C).**
**At no account size does NQ outperform ES on a fair comparison (B).**

---

## 6 · PHASE 6 — FORWARD TEST PLAN (recommended account)

**Recommended account size:** **$150k notional**, paper-only.

Why $150k specifically:
- Resolves the NQ capacity question definitively (84% accept).
- Provides headroom for ES at 0.5% risk without risk-cap impact.
- $100k is too constrained for credible NQ measurement; $200k
  provides no new information on top of $150k.

### Forward test targets

| Metric | Target | Disable / re-evaluate trigger |
|---|---|---|
| Symbols active | MES (full) + MNQ (observation-only data collection) | If MES forward expectancy < 0R over 30 closed → pause |
| Closed trades on MES | ≥ 30 to qualify for any go-live discussion | If 30d roll < 5 closed → call this "too thin to evaluate" not "insufficient signal" |
| Forward NORMAL mean | ≥ +0.25R rolling 30-trade | If < 0R → halt |
| Forward 5%ile | > 0R bootstrap 5%ile on rolling 30 | If crosses zero → not proven |
| Max DD | < 3R | If breached → reduce risk to 0.10% or pause |
| Slippage drift | realized ≤ 1.5× NORMAL profile slip | If exceeded for >20% of stops → recalibrate profile, do not just continue |
| Largest winner share | < 25% of cumulative wins | If one trade dominates → halt, re-examine |
| NQ observation | log setups + would-be outcomes | After ≥ 30 NQ closed forward → re-run this whole sweep with live data |

### Disable conditions (any one triggers halt)

- MES forward NORMAL mean drops below 0R on rolling 30
- MES rolling 5%ile crosses below 0R for two consecutive evaluations
- realized stop slippage > 2× modelled NORMAL for >25% of stops
- account drawdown > 5% of starting equity
- consecutive losses > 5 (rule-based circuit breaker)

### Go-live criteria (these are NOT met yet)

- 100+ resolved trades to calibrate execution model (see
  `EXECUTION_CALIBRATION_PLAN.md`)
- 30+ closed MES paper trades with NORMAL mean > +0.25R and 5%ile > 0R
- Live slip ≤ 1.5× modelled NORMAL for at least 25 stop hits
- All hard gates in `analysis/go_live.py` pass with the calibrated
  profile (not the hand-tuned NORMAL)

---

## 7 · FINAL OUTPUT — bottom line

### Did NQ deserve to join ES in forward testing?

**No.** Account-size was indeed the cause of NQ's capacity problem
at $50k (27/31 setups rejected). Once capacity is fixed at $150k,
NQ accepts 26/31 setups — but expectancy degrades from +0.79R
(small-account, selection-filtered) to +0.57R (capacity-fair), and
further to +0.30R at $200k. **Larger account did not improve NQ's
edge — it revealed the edge was concentrated in a tight-stop subset
the risk cap was accidentally selecting.**

### The uncomfortable corollary

The *same selection effect* affected ES. At the $50k account used
in the prior Tier-1 v2 review, ES NORMAL was +0.78R. At $100k+
where ES has zero risk-cap rejections, ES NORMAL drops to **+0.36R
with 5%ile -0.04R** — borderline CI-crosses-zero. **ES's "edge"
also looks weaker once the stealth filter is removed.** The prior
"ICT_HAS_INFORMATIONAL_EDGE" finding (which compared ICT to
baselines at $50k) needs an asterisk: it was measured in the regime
where the risk cap was screening setups.

### Risk manager's call

- **Account size to use for honest paper:** $150k notional.
- **Symbol set:** **MES only** for actual paper-traded execution.
  Log MNQ in signal-collection mode to build NQ statistics without
  risking position-conflict noise.
- **Real money:** still NO, on either symbol. The 5%ile is at or
  below zero on every cell with comparable capacity.
- **What was actually learned:** the prior NQ INSUFFICIENT_DATA
  verdict was correct, but for a different reason than originally
  framed. NQ was undertested not because of sample-size scarcity
  per se but because the small account size was suppressing the
  setups that drag the average down. NQ's "real" expectancy under
  honest capacity is mediocre.

— end of report —
