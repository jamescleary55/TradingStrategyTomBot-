# Trade Reconciliation Design

**Date:** 2026-06-20 · **Status:** ✅ implemented + tested (19 reconciliation
tests, 116 total) · **Module:** `reconciliation/` (`model.py`, `engine.py`,
`metrics.py`) · **CLI:** `scripts/reconcile_report.py`

## Goal

```
Broker Events  →  Reconciled Trades  →  Trustworthy Statistics
```

Convert raw broker execution events into closed round-trips whose P&L is broker
truth, then compute performance metrics **only** from those closed trades. No
metric is ever derived from raw orders or intentions.

## Why the legacy reconciler is insufficient

`live/reconcile.py` (kept for the RiskGate forward-test path) is heuristic:
"first fill = entry, last opposing fill = exit", a distance-based stop/target
label, and no dedup. It cannot handle partial fills, scale-in/out,
net-position-to-zero closing, duplicate broker events, or out-of-order input.
The new engine is a separate, pure, production layer; the legacy one is left
untouched.

## Identity over timestamps

Matching is driven by **identifiers and the running net position**, never by
time proximity. Identifier priority:

1. **executionId** — primary dedup key. Duplicate broker events (same execId)
   collapse to one.
2. **permId** — broker-permanent order id; part of the composite fallback key
   when an execId is missing.
3. **parentOrderId** — links bracket children to the parent family (used to
   suppress OCA-sibling auto-cancels).
4. **account** — first grouping dimension (positions are per account).
5. **contract** — second grouping dimension.
6. **side** — determines entry vs. exit within a position book.

Timestamps are used only to **sequence** the position walk (sorted
deterministically), so out-of-order input yields identical results.

## Algorithm

1. **Split** fill events from order-level `cancel`/`reject` events.
2. **Dedup** fills by `execution_id` (else a stable composite of
   permId|orderId|ts|side|qty|price).
3. **Group** fills by `(account, contract)`; sort each group by
   `(timestamp, executionId, orderId)`.
4. **Walk** each group maintaining a signed net position:
   - net `0 → ≠0` opens a trade; first opening fill sets the side.
   - same-direction fills **scale in** (entry leg, VWAP accumulated).
   - opposite-direction fills **reduce** the position (exit leg).
   - a fill that would cross zero is **split**: the flattening part closes the
     current trade; the remainder opens the next (handles position flips).
   - net `→ 0` ⇒ **CLOSED**.
5. **Leftover** open position at end of stream ⇒ `OPEN` (no exits) or `PARTIAL`
   (some exits, not yet flat).
6. **Zero-fill** cancel/reject orders ⇒ `CANCELLED`/`REJECTED`, except OCA
   siblings of a filled bracket family (suppressed).

## Lifecycle (Phase 3)

```
Entry Order → [partial fill]* → Position Open → Target Fill  ─┐
                                              → Stop Fill    ─┤→ net=0 → CLOSED
```

Handled: partial fills · multiple executions per leg · bracket orders (parent +
OCA children) · stop fills · target fills · manual/OCA cancels · rejected
orders · position flips · duplicate events · out-of-order events. **A trade is
CLOSED only when net position returns to zero.**

## P&L (broker truth)

- `entry_price` / `exit_price` = VWAP of the respective legs.
- `gross_pnl = (exit_vwap − entry_vwap) · direction · qty · point_value`
  (`point_value` resolved from `config.INSTRUMENTS`, longest-root-prefix match;
  e.g. `MESU6 → MES → 5.0`).
- `net_pnl = gross_pnl − commission` (commission summed across fills,
  proportionally split when a fill is split across trades).

Order intentions (`order_meta`) feed **only** derived labels, never P&L:
`slippage = (entry_vwap − intended_entry)·direction`,
`realized_R = price_move / |intended_entry − intended_stop|`, and the
target/stop `exit_reason`.

## Data model (Phase 4)

`ReconciledTrade`: `trade_id, account_id, symbol, status, entry_time, exit_time,
entry_price, exit_price, entry_qty, exit_qty, side, gross_pnl, net_pnl,
commission, slippage, realized_R, entry_order_id, exit_order_id, entry_perm_id,
exit_perm_id, execution_ids, exit_reason, point_value`.

Statuses: `OPEN · PARTIAL · CLOSED · CANCELLED · REJECTED`.

To support the identifier priority, `ExecutionEvent` gained `perm_id` and
`account` (the IBKR adapter now populates them from `execId`/`permId`/
`acctNumber`).

## Verification

19 unit tests cover all 10 required cases + position-flip, short side,
commission, instrument point value, and the metrics engine. A scripted
end-to-end run (partial entry + bracket + duplicate + reject + winner + loser)
reproduces hand-computed P&L, R, drawdown, and profit factor exactly.
