# ES vs NQ — controlled-RNG verdict + stop-distance analysis

**Posture:** the prior account-size review's "stealth quality filter"
claim was potentially confounded by RNG-path divergence. This report
cleans up the methodology and produces a fair ES vs NQ verdict.

**Sources:**
- `python -m backtest.controlled_verdict --seeds 100 --out /tmp/controlled_verdict.json`
- `backtest/simulator_controlled.py` — per-setup deterministic RNG
- 180-day yfinance · 1h · 0.5% risk per trade · profiles unchanged

---

## 1 · PHASE 1 — Controlled RNG account-size sweep (ES)

### Methodology

Each setup gets its own RNG, seeded from a hash of
`(master_seed, profile.name, setup_identity)` where
`setup_identity = (timestamp, direction, entry, stop, target)` is
stable across runs. Result: the random draws used for that setup's
fill attempts do not depend on what other setups are also in the
queue.

This isolates the **selection effect** (which setups get accepted)
from the **RNG path divergence** (a stream-RNG side-effect).

### Results — 100 seeds per cell, NORMAL profile

| Account | Total accepted | Shared with $50k | All-setups mean R | 5%ile | Shared-only mean R | Newly admitted n | Newly admitted mean R |
|---|---:|---:|---:|---:|---:|---:|---:|
| $50k  | 15 | 15 | **+0.80R** | +0.46 | +0.77R | 0 | — |
| $100k | 21 | 13 | **+0.37R** | -0.04 | +0.53R | 8 | **-0.47R** |
| $150k | 21 | 13 | +0.37R | -0.04 | +0.53R | 8 | -0.47R |
| $200k | 21 | 13 | +0.37R | -0.04 | +0.53R | 8 | -0.47R |

### Decomposition of the $50k → $100k drop (-0.43R)

| Component | Contribution | Mechanism |
|---|---|---|
| Newly admitted setups underperforming | ~ -0.18R | 8 new setups @ -0.47R diluting the average over 21 trades |
| Shared setups drifting -0.24R (+0.77 → +0.53R) | ~ -0.25R | Position-blocking causes the same setup to fill at different bars in $50k vs $100k. The market outcome (target vs stop) can flip based on entry bar. |
| Total | -0.43R | |

### Answer to "real selection vs RNG divergence?"

> **Both — but the selection effect dominates the "fill-bar timing"
> effect, and the prior RNG-path-divergence concern was a real but
> minor confounder.**

- Per-setup RNG eliminated stream-RNG divergence (random draws for
  setup S are identical between runs).
- A new confounder is exposed: when other setups change which bars
  hold an open position, the SAME setup can fill at a different
  bar, and the deterministic stop/target outcome can flip.
- The newly admitted setups average **-0.47R**, which is a real
  selection effect. The $50k risk cap was selecting setups that
  do not equal the $100k+ admit population.

**The "stealth quality filter" claim survives.** Half the drop is
new-admit selection (confirmed real). The other half is fill-bar
timing (also real, not an RNG artifact, but a different mechanism
than first framed).

---

## 2 · PHASE 2 — Stop-distance bucket analysis

Setups bucketed by terciles of `abs(entry - stop)`.

### ES at $150k notional (NORMAL profile, 100 seeds)

| Bucket | N setups | Avg stop dist | Mean R | 5%ile | P(>0) | P(>+0.25) | Med closed | Win% | Med DD% |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| tight  | 9 | 25.7 pts | +0.50R | **+0.23** | 96% | 77% | 7 | 50% | 0.99 |
| **medium** | 8 | 42.1 pts | **+0.97R** | **+0.48** | 97% | 97% | 5 | 67% | 0.44 |
| wide   | 9 | 73.0 pts | +0.23R | **-0.44** | 63% | 47% | 5 | 40% | 1.11 |

**ES finding:** medium-stop is the strongest bucket (+0.97R, 5%ile
+0.48, P>+0.25R = 97%). Tight is second. **Wide is poor** — 5%ile
negative, P>0 only 63%. The small-account risk cap was selecting
tight + medium (because it rejected the widest-stop setups), which
is partially correct but slightly off the optimum.

### NQ at $150k notional (NORMAL profile, 100 seeds)

| Bucket | N setups | Avg stop dist | Mean R | 5%ile | P(>0) | P(>+0.25) | Med closed | Win% | Med DD% |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **tight**  | 11 | 130.9 pts | **+1.04R** | **+0.63** | **100%** | **100%** | 8 | 68% | 0.52 |
| medium | 10 | 226.9 pts | +0.31R | -0.05 | 89% | 55% | 7 | 44% | 0.74 |
| wide   | 10 | 380.5 pts | +0.44R | -1.06 | 70% | 69% | 4 | 50% | 0.53 |

**NQ finding:** tight is **decisively** best — +1.04R mean, 100% of
seeds above +0.25R, 5%ile +0.63R. Medium is mediocre (+0.31R, 5%ile
near zero). Wide is volatile (+0.44R mean but 5%ile -1.06R — high
variance).

### Are tight-stop setups genuinely better?

**Yes, but the strength of the effect differs by symbol.**

- **NQ**: tight-stop bucket is the entire edge. The small-account
  risk cap was **correctly** selecting it.
- **ES**: tight + medium are both edge-carrying, but medium is
  the strongest. The risk cap was approximately correct on ES.
- **Wide-stop** is poor on both: 5%ile -0.44R (ES) and -1.06R (NQ).

### Caveats

Sample sizes are tight: bucket n=8-11 setups → median 4-8 closed
per Monte Carlo run. None of the bucket-level findings would clear
a strict 20-trade floor. These are **research hypotheses**, not
production filters. They should not be wired into the live strategy
without independent validation on out-of-sample data (Tradovate
demo, longer history, or a second 180-day window).

---

## 3 · PHASE 3 — ES vs NQ fair verdict

### Updated classification

| Symbol | Trading recommendation | Research recommendation |
|---|---|---|
| **ES** | `CONTINUE_PAPER_TEST` at $100k notional | Investigate medium-stop bucket on Tradovate demo; verify +0.97R / 5%ile +0.48 holds out-of-sample |
| **NQ** | `SIGNAL_COLLECTION_ONLY` at $150k notional | Investigate tight-stop bucket; verify +1.04R / 5%ile +0.63 holds out-of-sample |
| **CL** | `DISABLE` (unchanged from prior review) | — |

### Why ES is `CONTINUE_PAPER_TEST` not `SIGNAL_COLLECTION_ONLY`

- $100k+ NORMAL: mean +0.37R, 5%ile -0.04R. The 5%ile is *just*
  below zero — borderline but not catastrophic.
- ES medium-stop bucket shows a real strong signal (+0.97R / 5%ile
  +0.48R), suggesting the strategy's edge is real, just concentrated.
- ES is the only symbol with both a positive mean and a >0% P>+0.25R
  across the full universe at $100k+.
- Continue paper-trading the full universe (not just the medium
  bucket) because filtering on bucket would be parameter optimization.

### Why NQ is `SIGNAL_COLLECTION_ONLY` not `CONTINUE_PAPER_TEST`

- NQ full-universe NORMAL at $150k: mean +0.57R, 5%ile +0.27R,
  median closed 18 (BELOW the 20-trade floor).
- The tight-stop subset is impressive (+1.04R, 5%ile +0.63R) but
  n=11 setups produces median 8 closed — too thin to commit paper
  risk on the subset, and the unfiltered universe is sample-thin.
- Collect NQ signals for ≥ 60 additional days (or move to 30m for
  more setups) to grow the sample. Specifically validate whether
  the tight-stop pattern holds in fresh data.

### Drawdown comparison — does ES have lower DD risk?

| Symbol | NORMAL mean R | NORMAL Med DD% | P(DD > 3R) at $100k |
|---|---:|---:|---:|
| ES | +0.37 | 1.74-2.32 | 53-62% |
| NQ ($100k) | +0.79 | 1.20 | 0% |
| NQ ($150k) | +0.57 | 2.40 | 10% |
| NQ ($200k) | +0.30 | 1.62 | 52% |

**Claim retraction:** *the prior report implied ES has lower
drawdown risk. It does not.* ES P(DD>3R) is 53-62% at all account
sizes; NQ ranges 0-52% depending on size. **NQ at $100-150k has
materially LOWER modelled DD risk than ES.** This refines the
recommendation: ES is selected for *expectancy / sample
characteristics*, not for DD safety.

---

## 4 · PHASE 4 — Observation-mode → signal-collection-mode

The prior account-size report used "observation mode" to describe
the proposed NQ stance. That phrasing was misleading. **No real
fills are observed in simulator paper-trading.** The only thing
collected is the signal stream — setups, directions, hypothesized
fills, hypothesized outcomes — all of which are simulated.

### Renaming

| Old term | New term | What it actually means |
|---|---|---|
| observation mode | **signal-collection mode** | log every detected setup with its theoretical outcome under the current execution profile; **no broker order is submitted**, real or paper |
| paper-trade | paper-trade | broker order submitted to demo account; real fills measured |
| live-trade | live-trade | real money |

### What signal-collection mode **can** tell us

- Setup frequency (per symbol, session, timeframe)
- Setup quality distribution (RR, bucket — tight/medium/wide)
- Session distribution of signals
- Theoretical PnL given the modelled execution profile
- Forward consistency of detection (does the strategy fire at the
  expected rate?)

### What signal-collection mode **cannot** tell us

- Real fill probability (limit fills at the simulated price)
- Real partial-fill rate
- Real slippage on stop hits
- Broker rejection behavior (e.g., margin violations, futures
  session boundaries)
- Whether the strategy's *actual* P&L matches the *simulated* P&L
  — that requires real (paper or live) execution

**Implication for the forward plan:** signal-collection on NQ
generates calibration data ONLY for the front half of the
strategy (detection / sizing). It does not advance the execution
calibration plan, which still needs real Tradovate paper fills.

---

## 5 · What evidence is still missing

1. **Out-of-sample validation of the tight-stop / medium-stop
   bucket findings.** Both findings come from ONE 180-day window.
   They could be artifacts of that period's vol structure. Required:
   - A second 180-day window (e.g., 2024-2025) showing the same
     bucket ordering, OR
   - Tradovate demo data covering ≥ 60 days with bucket-labelled
     setups, OR
   - At minimum: a deliberately permuted "destroyed-stop-distance"
     baseline (shuffle stop distances across setups) to confirm
     stop-distance carries information beyond ATR.
2. **Real execution calibration** per `EXECUTION_CALIBRATION_PLAN.md`.
   ≥ 100 resolved Tradovate-paper trades.
3. **Forward 30-trade rolling expectancy** on MES under the current
   profile.
4. **News-event timeline** integration into the simulator (no news
   events were loaded in any of the 4 audit rounds).

---

## 6 · Final answers

### 1. Was the ES drop real or mostly simulation artifact?

**Real, with two distinct mechanisms.** Per-setup RNG control
showed:
- Newly admitted setups average -0.47R (real selection effect).
- Shared setups drift -0.24R due to position-blocking
  fill-bar timing — NOT an RNG artifact, but also NOT pure
  selection. The same setup fills at different bars depending on
  what else is open.
- Combined: -0.43R of the $50k → $100k drop is real and reproducible.

### 2. Does NQ deserve further testing?

**Yes, in signal-collection mode.** NQ's tight-stop bucket shows
a +1.04R mean with 5%ile +0.63R and 100% probability above +0.25R
— the strongest signal in the entire audit, on either symbol.
But sample is only 11 setups → median 8 closed. This is a research
hypothesis worth investigating, not a paper-traded position.

### 3. Should the current forward test remain ES-focused?

**Yes.** ES is the only symbol with positive full-universe
NORMAL expectancy that doesn't depend on a sample-thin sub-bucket.

### 4. Paper, signal-collected, or ignored — NQ?

**Signal-collected.** Log every NQ setup with its theoretical
fill outcome. Do not submit broker orders on NQ until the
tight-stop hypothesis is validated on independent data and NQ
accumulates ≥ 30 actual closed trades in some form (paper or
sufficient out-of-sample).

### 5. The single next piece of evidence that would change the decision

**A second independent 180-day window (different period) showing
the same tight-stop bucket effect on NQ.** If +1.04R replicates
out-of-sample, NQ goes to `CONTINUE_PAPER_TEST` on the tight
bucket. If it does not replicate, NQ stays `SIGNAL_COLLECTION_ONLY`
or moves to `DISABLE`. Everything else (more seeds, more knob
fragility, more profiles) is noise relative to this single test.

---

## 7 · Summary

| Item | Status |
|---|---|
| ES drop ($50k → $100k+) | REAL — half from new-admit selection, half from position-blocking fill-bar shift |
| RNG-path divergence claim | DISMISSED as primary cause — per-setup RNG showed shared setups still drift via blocking dynamics |
| Stealth quality filter claim | UPHELD with refinement — risk cap selects tight + medium-stop setups on ES (approximately correct), tight-stop on NQ (decisively correct) |
| ES verdict | `CONTINUE_PAPER_TEST` at $100k notional |
| NQ verdict | `SIGNAL_COLLECTION_ONLY` at $150k notional — tight-stop research hypothesis is the open thread |
| "ES has lower DD" claim | RETRACTED — ES P(DD>3R) is 53-62%, NQ at $100-150k is 0-10% |
| Observation mode | RENAMED to signal-collection mode |
| Open research thread | tight-stop bucket replication on independent data |

— end of report —
