# Pre-Flight Report — First Automated Paper Trade

**Generated:** 2026-06-20 05:54 UTC (Saturday) · **Account:** `DUQ834606` (paper)
**Result:** Infrastructure GO (11/12) · **Trading blocked: market closed (weekend)**

## Pre-flight checks

| # | Check | Result | Detail |
|---|---|---|---|
| 1 | IB Gateway connected | ✅ PASS | snapshot returned, `127.0.0.1:4002` |
| 2 | Paper account (DU prefix) | ✅ PASS | `DUQ834606` |
| 3 | Snapshot succeeds | ✅ PASS | cash 1,000,000 EUR, partial=False, <1s |
| 4 | No open positions | ✅ PASS | 0 open |
| 5 | No resting orders | ✅ PASS | 0 open |
| 6 | Kill switch absent | ✅ PASS | no sentinel file |
| 7 | Flatten command available | ✅ PASS | `scripts/flatten_account.py` + adapter method |
| 8 | Reconciliation engine enabled | ✅ PASS | `reconciliation.reconcile` |
| 9 | Metrics engine enabled | ✅ PASS | `reconciliation.compute_metrics` |
| 10 | Safety gate passes (with LIVE data) | ✅ PASS | all 10 conditions satisfied |
| 11 | auto_paper_safe mode enabled | ✅ PASS | monitor mode available |
| 12 | Market-data status | ❌ NOT LIVE | `HISTORICAL_ONLY` |

## The blocker — market is closed

**It is Saturday 05:54 UTC.** CME equity-index futures (ES/MES) trade
Sun 17:00 ET → Fri 16:00 ET with a daily 16:00–17:00 ET halt. The market is
**closed for the weekend** and reopens **Sunday ≈ 21:00 UTC (17:00 ET)**, about
39 hours from this report.

A bracket **limit entry cannot fill while the market is closed**, so the mission
target — one reconciled CLOSED trade — is physically unobtainable until the
market reopens and price trades through the entry during a setup. No amount of
engineering changes this; it is a market-hours constraint.

Separately, **live L1 market data for MES is not subscribed** (`Error 354`;
delayed data is available). When the market reopens, the execution gate will
block on data-status condition #5 unless the run is started with
`--data-override` (acceptable for plumbing validation — fills are still real
broker fills; only the slippage reference is delayed/historical).

## Decision: not launching an unattended run

Per the supervised, gated philosophy of this phase, I am **not** starting a
multi-day unattended auto-execute process over the weekend. Doing so would leave
an order-placing bot running ~39h without supervision into market open — exactly
what the kill-switch/flatten scaffolding exists to avoid relying on. The first
automated run should be **operator-attended**.

## Runbook — when the market is open (Sun ≥ 21:00 UTC / weekday session)

```bash
cd ~/projects/ict-futures-bot && source .venv/bin/activate

# 0. Re-run pre-flight; confirm market-data status is LIVE (or accept --data-override)
python scripts/probe_broker.py --broker ibkr

# 1. Start the controlled run (attended). MES only, qty 1, full gate.
BROKER=ibkr python -m live.monitor --symbols ES --timeframe 15m \
    --source ibkr --mode auto_paper_safe --auto-execute [--data-override]

# 2. In a second shell, run the attended watcher — it captures fills,
#    reconciles, detects the first CLOSED trade, and auto-halts on failure.
python scripts/first_trade_watch.py --poll 30                 # stop at 1 CLOSED (milestone 1)
python scripts/first_trade_watch.py --poll 30 --max-closed 10 # then continue to 10

# Halt instantly at any time:
touch ~/.ict-bot/KILL_SWITCH
python scripts/flatten_account.py --execute  # force flat
```

The watcher writes `FIRST_CLOSED_TRADE_VALIDATION.md` at milestone 1 and
`FIRST_10_TRADES_REPORT.md` at 10. On any Phase-5 failure (orphan position,
reconciliation error, broker disconnect, duplicate execId) it trips the kill
switch and writes `FAILURE_REPORT.md`, preserving all logs.

## Verdict

**PRE-FLIGHT GO — EXECUTION BLOCKED UNTIL MARKET OPEN.** All bot infrastructure
is verified ready. The first closed trade cannot be obtained this session; it
requires a live CME session (Sun ≥ 21:00 UTC). Recommend an attended run at the
next session open.
