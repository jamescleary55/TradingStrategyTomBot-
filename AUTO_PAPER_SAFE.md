# AUTO_PAPER_SAFE — controlled paper validation mode (Phase 6)

**Date:** 2026-06-20 · **Status:** ✅ implemented + tested

A new monitor mode whose only purpose is **operational validation** of the
signal → order → fill → stop/target → logging path. It is NOT for profitability
and NOT a live deployment.

## Hard restrictions (enforced in code, not by convention)

| Restriction | Where enforced |
|---|---|
| MES only (no NQ, no ES, no others) | `main()` drops non-MES symbols up front; the per-order path re-checks and blocks non-MES; gate allowlist = `{MES}` |
| Qty = 1 | `plan.contracts` forced to `1` in safe mode; gate condition #10 (`qty in [1,1]`) |
| One position maximum | gate condition #7 (open positions == 0) |
| One signal at a time | duplicate-signature guard + pending-order guard (gate #8/#9) |
| Paper account only | gate #2 (`DU` prefix) + #3 (mode==paper) |
| No live account | gate (`live_account` must be false) |

Every restriction is a **gate condition** — if any is violated the order is
blocked and logged, never sent.

## Running it

```bash
cd ~/projects/ict-futures-bot && source .venv/bin/activate
BROKER=ibkr python -m live.monitor \
    --symbols ES --timeframe 15m --source ibkr \
    --mode auto_paper_safe --auto-execute \
    [--data-override]
```

- `--source ibkr` is required so signals run on live IBKR bars.
- At startup the monitor probes IBKR market-data status once per symbol. If live
  L1 is not subscribed (the smoke test saw MES `Error 354` → `HISTORICAL_ONLY`),
  the gate blocks on condition #5 **unless** you pass `--data-override`, which is
  acceptable for plumbing validation (we are validating order flow, not price
  accuracy).
- `ES` resolves to the `MES` micro contract for sizing/execution.

## Halting

```bash
touch ~/.ict-bot/KILL_SWITCH          # next tick refuses all new orders
python scripts/flatten_account.py     # dry-run; add --execute to force flat
```

## What it proves vs. doesn't

✅ Proves: account routing, the ten gate conditions, kill switch, order
submission, event logging, duplicate/pending protection.
❌ Does not prove: edge, expectancy, or profitability — out of scope by design.
