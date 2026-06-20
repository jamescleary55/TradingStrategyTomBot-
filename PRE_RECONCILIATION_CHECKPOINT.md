# Pre-Reconciliation Checkpoint

**Date:** 2026-06-20 · **Branch:** `ibkr-migration` · **Tests:** 97 passing

Snapshot of the codebase immediately before the trade-reconciliation layer is
built. Everything below the reconciliation boundary is complete and verified;
metrics are NOT yet trustworthy because reconciliation does not exist.

## Current status — COMPLETE

- IBKR connectivity verified (IB Gateway `127.0.0.1:4002`, paper `DUQ834606`)
- Snapshot hang fixed; returns < 1s; live-verified
- `account_id` refactor: string end-to-end, no int coercion (live-verified `"DUQ834606"`)
- Execution safety gates (10 mandatory conditions), non-short-circuit, wired into monitor
- Kill switch (configured path + common flag names), fail-safe
- Emergency flatten (`scripts/flatten_account.py`, dry-run default), live-verified
- `auto_paper_safe` mode (MES only, qty 1, one position, paper-only)
- Submit/cancel paper order path proven (earlier smoke test, orderId 6)
- 97 tests passing
- Verdict: READY_FOR_AUTO_PAPER_TEST

## Files changed in this checkpoint

**New modules**
- `risk/exec_gate.py` — pure 10-condition execution gate
- `risk/kill_switch.py` — fail-safe operator halt
- `live/exec_guard.py` — live preflight choke point (snapshot + orders + gate)
- `scripts/flatten_account.py` — emergency flatten CLI (manual, dry-run default)
- `scripts/go_no_go.py` — readiness reader (criteria from logs)

**Modified**
- `execution/base.py` — `AccountSnapshot.account_id: str`; `list_open_orders` default
- `execution/ibkr_orders.py` — string account id, `_safe_int` removed, `list_open_orders`, `flatten_and_cancel_all`
- `execution/tradovate_orders.py`, `execution/topstepx_orders.py` — stringify account id at boundary
- `live/forward_log.py` — `log_event` + `events.jsonl` observability taxonomy
- `live/monitor.py` — mandatory gate wiring, `auto_paper_safe` mode, `--data-override`, data-status probe
- `data/ibkr_feed.py` — market-data status classifier (pre-existing, included)

**Docs**
- `ACCOUNT_ID_REFACTOR.md`, `EXECUTION_SAFETY_GATES.md`, `AUTO_PAPER_SAFE.md`,
  `FIRST_AUTOMATED_PAPER_TEST.md`, and prior IBKR readiness reports.

**Tests** (8 files, 97 tests)
- `tests/test_safety.py` (new), `tests/test_exec_gate.py`, `tests/test_ibkr.py`,
  `tests/test_ibkr_snapshot.py`, `tests/test_reconcile.py` (legacy), others.

## Known issues / limitations (the reason reconciliation is next)

1. **No trustworthy closed-trade statistics.** The legacy `live/reconcile.py` is a
   heuristic forward-test joiner (first-fill = entry, last-opposing = exit,
   distance-based stop/target label). It does **not** handle: partial fills,
   net-position-to-zero closing, executionId dedup, multiple executions per leg,
   or out-of-order events. Expectancy / profit factor / drawdown / win rate built
   on it cannot be trusted. **This is the next mission.**
2. **Live L1 market data not subscribed** for MES (`Error 354`) → the gate blocks
   on data-status unless `--data-override` is used. Acceptable for plumbing
   validation; a CME L1 subscription is needed for price-accurate runs.
3. `ExecutionEvent` currently lacks `perm_id` / `account` (added as part of the
   reconciliation work).
4. First automated paper test is designed but not yet run (operator-gated).

## Next

Trade reconciliation: Broker Events → Reconciled Trades → Trustworthy Statistics.
