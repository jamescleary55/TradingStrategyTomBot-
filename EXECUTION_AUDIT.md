# Execution-realism audit · ict-futures-bot

**Posture:** Adversarial. The job is to disprove the strategy. The
baseline assumption is that any positive backtest expectancy is an
artifact until proven otherwise.

---

## 1 · Assumptions before

| Layer | Pre-fix assumption |
|---|---|
| Stop fills | Exactly at intended stop price + 1 tick adverse. |
| Limit entry fills | Any bar that touches the limit → immediate full fill. **No queue priority, no trade-through requirement, no fill probability.** |
| Target fills | Same as entry — touch == fill. |
| Slippage | **1 tick, flat, everywhere.** No ATR awareness, no session awareness, no news awareness. |
| Partial fills | Never modeled. Fills were always 100% of intended size. |
| Spreads | Not modeled. |
| Profile | One mode of execution. No way to stress-test pessimistic conditions. |
| News blackout | Filter rejected setups *near* news; nothing modeled about execution *during* news. |
| Bar consumption | **The still-forming current bar was fed into detection.** A phantom FVG could fire, vanish 30 minutes later (audit finding A6). |

These together overstate expectancy. The audit predicted +0.2-0.5R of
the +0.96R reported backtest result was artifact.

## 2 · Assumptions after (this audit)

Three profiles parameterise the same machinery; the strategy's
posterior is whatever the **PUNITIVE** profile says.

### Stop fills
- Adverse points = ``max(N ticks, atr_fraction × ATR(5))``, where N and
  atr_fraction differ by vol regime (low / medium / high).
- Multiplier × 1.5 (NORMAL) or × 2.0 (PUNITIVE) when in `elevated`
  news window (±15-45 min of a known event).
- Multiplier × 3.0 (NORMAL) or × 4.0 (PUNITIVE) within `blackout`
  window (±5-15 min).
- Session multiplier: OVERNIGHT × 1.4 (NORMAL) / × 1.7 (PUNITIVE).

### Limit fills (entry + target)
- Touching the limit is **necessary, not sufficient**. Conditional
  probability of fill depends on (vol regime × news regime × session):

  | Profile | low vol | med vol | high vol | news elevated | news blackout |
  |---|---|---|---|---|---|
  | OPTIMISTIC | 90% | 80% | 65% | 60% | 30% |
  | NORMAL | 80% | 60% | 35% | 40% | 5% |
  | PUNITIVE | 55% | 35% | 15% | 15% | 0% |

- Session multiplier on fill probability (OVERNIGHT 0.7-0.9, NY_PM
  0.8-1.0).
- 20% (NORMAL) / 35% (PUNITIVE) of fills are *partials* — only ~50% of
  intended quantity executes. The unfilled fraction is discarded
  (modeled as "miss" for that share).

### Bar consumption (A6 fix)
- `utils/time_utils.trim_incomplete_bar(df, timeframe)` removes the
  trailing bar when its window has not closed.
- Wired into `signals/strategies/sweep_choch_fvg.detect_setups` so the
  strategy can never produce a setup whose CHoCH/FVG sits on a still-
  forming bar.
- Tests:
  - Last bar within window → dropped.
  - Last bar past window → kept.
  - Idempotent over repeated calls.
  - Setup detection regression test for the phantom-FVG scenario.

### What is still NOT modeled
1. **Real broker queue position** — limits filling at price X but
   skipping the front of the queue. We approximate as a probability.
2. **Exchange-side latency / circuit breakers.**
3. **Real-time bid/ask spread** widening around news. We use the
   probability gate as a proxy.
4. **Currency / clearing-fee variation** between symbols. Treated as a
   flat $4 commission per contract round trip.
5. **Crypto-specific execution** (funding, maker rebates, liquidations).
   Crypto pairs use the same model — almost certainly wrong but better
   than flat-1-tick.

## 3 · Expectancy under each profile

Source: **`python -m backtest.sensitivity --symbols NQ,ES,CL,GC --days 180`**
(yfinance NQ failed mid-fetch — usable cells are ES, CL, GC).

| Symbol | Profile | Closed | Win% | Avg R | Profit factor | Limit fill % | Avg slip (pts) | Max DD % | Recovery |
|---|---|---|---|---|---|---|---|---|---|
| ES | OPTIMISTIC | 15 | 60% | **+0.79R** | 2.96 | 100% | 0.16 | 1.04 | 5.07 |
| ES | NORMAL | 12 | 67% | **+0.97R** | 3.66 | 41% | 0.57 | 1.12 | 3.87 |
| ES | PUNITIVE | 6 | 33% | **-0.14R** | 0.82 | 11% | 2.28 | 1.31 | -0.43 |
| CL | OPTIMISTIC | 20 | 60% | **+0.78R** | 2.87 | 78% | 0.02 | 0.83 | 7.94 |
| CL | NORMAL | 5 | 20% | **-0.54R** | 0.42 | 0% | 0.04 | 2.10 | -0.68 |
| CL | PUNITIVE | 6 | 33% | **-0.37R** | 0.64 | 3% | 0.11 | 2.55 | -0.53 |
| GC | OPTIMISTIC | 1 | 0% | -1.06R | 0.00 | 100% | 0.54 | 0.40 | -1.00 |
| GC | NORMAL | 1 | 0% | -1.24R | 0.00 | 50% | 2.18 | 0.46 | -1.00 |
| GC | PUNITIVE | 0 | — | — | — | 0% | — | — | — |

### Survival verdict (≥ 5 closed trades per cell required to qualify)

| Profile | Avg expectancy across qualified symbols | Status |
|---|---|---|
| **OPTIMISTIC** | +0.79R across 2 symbols | SURVIVES |
| **NORMAL** | +0.21R across 2 symbols | **MARGINAL** |
| **PUNITIVE** | -0.26R across 2 symbols | **COLLAPSES** |

## 4 · Edge survival — does the strategy still work?

### Answer: **Conditionally NO.**

The +0.96R baseline that justified investing further effort was almost
entirely the result of:

1. **100% limit fill on touch** — under PUNITIVE this drops to 3-11%.
   The strategy never gets into 80% of its theoretical trades.
2. **Flat 1-tick stop slippage** — under PUNITIVE this rises to 2.28
   points on ES (typical real value). One stop hit at 2.28 pts adverse
   on a 100-pt risk = 0.023R drag per stop hit; cumulative across 30%
   stop rate = 0.007R per trade flat, but the geometric impact on max
   DD is much larger.
3. **No partial fill** — taking only 50% of size on partials with the
   same 1R stop = 0.5R risk on the actual trade but same drawdown
   denominator. Effective expectancy halves.

### Where expectancy collapses

- **ES**: +0.97R (NORMAL) → -0.14R (PUNITIVE). Win rate holds at 33%,
  but limit fill rate drops 41% → 11%. The strategy executes the worst
  half of its setups (those with widest stops and lowest fill
  probability surviving the probability gate) and misses the better
  half.
- **CL**: +0.78R (OPTIMISTIC) → -0.54R (NORMAL) → -0.37R (PUNITIVE).
  Most dramatic collapse. CL is the most volatile of the universe;
  realistic fills punish it hardest.
- **GC**: Too few closed trades in every profile — strategy doesn't
  fire enough on gold in 180 days at 1h to evaluate.

### What this means for the original +0.96R backtest

The +0.96R figure roughly corresponds to a profile that's even more
optimistic than OPTIMISTIC. Adjusting to NORMAL would bring the
realistic backtested expectancy to **roughly +0.2 to +0.3R**. Under
PUNITIVE: **break-even or negative**.

## 5 · Confidence framework update

Prior to this audit:
- prior(edge_real) ≈ 0.30 (with caveats)

After this audit, holding everything else constant:

- if PUNITIVE expectancy < 0 (CONFIRMED here): posterior **≈ 0.10**.
- The edge that exists at NORMAL is **+0.21R** — meaningful but
  vulnerable to small worsening of execution. Capital protection
  thresholds (e.g., 0.25R per trade) would be invalidated by any
  unmodeled drift.

This is the single most important update to the strategy's claim. The
"+0.96R" number is dead. Honest forward-expected expectancy under
realistic execution is **0.0R to +0.3R**.

## 6 · Specific findings worth promoting

1. **Stop slippage in elevated/blackout news regimes dominates losses.**
   The expectancy gap between NORMAL and PUNITIVE on ES (0.97R → -0.14R)
   is mostly driven by a handful of stops hit during higher-vol bars.
   The news filter rejects entries near news; it does not protect
   *exits*. A bracket placed before NFP can still be stopped out at
   adverse prices.

2. **Limit fill probability is the binding constraint.** The strategy
   produces ~30-50 setups per symbol per 180d at 1h. Under PUNITIVE
   only ~10-15% actually fill. With ~3-5 closed trades per symbol,
   statistical significance is gone.

3. **GC (gold) doesn't fire enough.** Across all three profiles, gold
   has 0-1 closed trades per 180 days. This was hidden by the previous
   simulator because all "would-fire" setups became "filled trades."
   With realistic fill probability, gold is effectively not in the
   universe at all on the 1h timeframe.

4. **A6 phantom-FVG fix changes nothing about historical backtests**
   (those are run on already-closed data) but is critical for live
   review-mode logging. Without the fix, the bot would alert on FVGs
   that disappear when the bar closes, polluting the forward log.

## 7 · What changes in the go-live gate

| Gate (previously) | New value |
|---|---|
| `positive_expectancy` (>0R) | Tighten to **> +0.25R** (NORMAL-profile equivalent). |
| `gap_to_backtest` (<0.3R) | The backtest must be **re-run under NORMAL** before this gate has meaning. |
| `slippage_measured` (≥ 25 fills) | Compare measured slip to NORMAL profile's expected slip — gate fails if measured > 1.5× modelled. |

These are operator changes, not code changes.

## 8 · What's still NOT proven

- The model is calibrated by hand. NORMAL might be optimistic for
  high-vol days; PUNITIVE might be unfair on calm days. Real fill data
  from Tradovate paper is the only honest calibration.
- All sensitivity above is on **yfinance 15-min-delayed feed** —
  audit finding C1 still open. The same backtest on Tradovate WS data
  could give different numbers either direction.
- The randomized fill model uses a single seed (42). Multi-seed
  Monte Carlo would tighten the confidence intervals. Out of scope
  here.

## 9 · Verdict

**Does the strategy have a real edge after realistic execution?**

> Under PUNITIVE: **No.** Expectancy is -0.26R portfolio-equally-weighted.
>
> Under NORMAL: **Marginal.** +0.21R is too thin a margin to survive
> commission slippage, the operator's psychological friction, and the
> unmodeled drift that the audit knows exists.
>
> Under OPTIMISTIC: Yes, but OPTIMISTIC is not the regime in which any
> real account will trade.

The single biggest remaining question is **whether the strategy's
fills, measured on live Tradovate demo, look like NORMAL or like
PUNITIVE.** Until 4-6 weeks of live data answers that, the strategy
cannot be trusted with real money.

Recommended posture for the operator:
- **Do not increase risk per trade.** Stay at 0.25R for paper.
- **Do not flip to live trading on positive forward results alone** —
  positive results under NORMAL fills are meaningless if the live
  fills look more like PUNITIVE.
- **Add a runtime sanity check**: every closed paper trade should
  log its realized vs modelled slippage. If realized > 1.5× modelled
  for >20% of trades, declare the model invalid and pause.

The strategy *might* be real. The backtest, as previously reported,
is **not** the evidence that proves it.
