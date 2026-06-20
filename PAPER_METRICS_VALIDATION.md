# Paper Metrics Validation (Phases 5 & 7)

**Date:** 2026-06-20 · **Status:** ✅ reconciliation + metrics implemented &
tested · **Verdict:** READY_FOR_RECONCILIATION_TESTING

## Phase 5 — metrics engine

`reconciliation/metrics.py :: compute_metrics(trades)` consumes a list of
`ReconciledTrade` and returns statistics **computed only from `status == CLOSED`
trades with a non-null `net_pnl`**. OPEN / PARTIAL / CANCELLED / REJECTED trades
are excluded and counted separately for transparency.

| Metric | Definition |
|---|---|
| expectancy | mean net P&L per closed trade |
| expectancy_R / avg_R | mean realized_R per closed trade |
| profit_factor | gross_profit ÷ gross_loss (None if no losers yet) |
| win_rate | wins ÷ closed |
| avg_winner / avg_loser | mean net P&L of winners / losers |
| max_drawdown | max peak-to-trough on the cumulative net-P&L curve (ordered by exit time) |
| recovery_factor | total net P&L ÷ max_drawdown |
| avg_slippage | mean adverse entry slippage (points) |
| avg_commission / total_commission | per-trade and summed |

**It is structurally impossible to compute these from raw orders** — the engine
only emits a CLOSED trade when the broker's net position returns to zero, and
metrics read `net_pnl`, which is only set on CLOSED trades.

## Validation evidence

- **Unit tests (19):** all 10 required reconciliation cases + position flip,
  short side, commission, instrument point-value, and 4 metrics tests. 116
  total tests pass.
- **End-to-end scripted run** (partial-fill entry + bracket with OCA stop
  auto-cancel + duplicate event + rejected order + one winner + one loser),
  MES @ $5/pt:

  | Trade | net_pnl | R | reason | hand-check |
  |---|---|---|---|---|
  | A long 2 | **95.64** | 1.625 | target | (6810−6800.25)·2·5 − 1.86 ✓ |
  | B long 1 | **−36.24** | −1.0 | stop | (6805−6812)·1·5 − 1.24 ✓ |

  Metrics: win_rate 0.5 · total 59.40 · expectancy 29.70 · profit_factor 2.639 ·
  avg_R 0.3125 · max_drawdown 36.24 · recovery_factor 1.639 · rejected 1. Every
  figure matches manual calculation.

## Phase 7 — can the system now generate trustworthy statistics?

**Expectancy, profit factor, drawdown, win rate — YES, the machinery is now
trustworthy:**

- Derived only from broker-confirmed CLOSED round-trips (net flat), not orders.
- Robust to partial fills, multiple executions, brackets, stops/targets,
  position flips, duplicate broker events, and out-of-order delivery.
- P&L uses actual fill VWAPs and contract point values; commissions included.
- Drawdown/recovery use the realized equity curve; profit factor and win rate
  use realized winners/losers.

**What remains before the NUMBERS (not the machinery) are meaningful:**

1. **No data yet.** 0 executions collected — the first automated paper test has
   not run. Trustworthy *machinery* ≠ trustworthy *sample*. Stats need volume
   (the go/no-go thresholds: 100+ signals, 50+ trades, 25+ fills).
2. **Execution capture must be wired to the live run.** The engine reads
   `~/.ict-bot/live_executions.jsonl`; the poller/`live.reconcile` writes it from
   `adapter.list_executions()`. That path exists and the IBKR adapter now
   populates `execId`/`permId`/`account`, but it has not yet run against live
   fills end-to-end (no fills have occurred).
3. **Price-accuracy caveat (unchanged):** MES live L1 isn't subscribed, so the
   first runs price off historical/delayed data with `--data-override`. Fill
   prices in paper are still real broker fills, but slippage figures should be
   read with that context until a CME L1 subscription is added.
4. **Realized-R depends on order intentions** being logged (they are, via
   `live_trades.jsonl`). P&L and win rate do not depend on this; R and slippage
   do.

None of these are reconciliation defects — they are data-collection steps that
come next.

## Verdict

**READY_FOR_RECONCILIATION_TESTING** — the reconciliation layer and metrics
engine are implemented, unit-tested, and end-to-end-verified against
hand-computed results. Begin collecting paper executions; statistics will be
trustworthy as the sample grows. Do NOT start automated paper trading beyond the
controlled validation run, and make no strategy-performance claims until a
sufficient sample of reconciled CLOSED trades exists.
