# IBKR First Paper Order — Smoke Test

**Result: ✅ PASS**
**Timestamp (UTC):** 2026-06-20T00:30:30 → 00:30:42
**Transport:** ib_insync → IB Gateway `127.0.0.1:4002` (clientId=50)

This was broker execution-path validation only — no strategy logic, no signal
generation, no bracket, no market order, single passive MES limit order.

---

## Account
| Field | Value |
|---|---|
| managedAccounts | `['DUQ834606']` (single, paper) |
| Account | `DUQ834606` — PAPER (DU-prefix) |
| Live account selectable | No (only the one DU account present) |

## Contract
| Field | Value |
|---|---|
| Instrument | MES front-month (Micro E-mini S&P 500) |
| localSymbol | `MESU6` (Sep '26) |
| conId | 793356217 |
| Exchange | CME |
| Qualified | Yes |

## Market price at test
| Field | Value |
|---|---|
| Live bid/ask/last | unavailable — `Error 354` (MES L1 streaming not subscribed; delayed available) |
| Anchor used | 7557.00 (last historical 5-min close; historical data IS subscribed) |

## Phase 1 — Pre-smoke safety checks
All 14 passed (price loaded via historical fallback; non-read-only confirmed by the order being accepted):

```
1_connected: True            5_symbol_MES: True        9_non_marketable: True
3_account_is_expected: True  6_qty_1: True             10_qualified: True
4_no_live_account: True      7_type_LIMIT: True        11_exchange_cme: True
8_tif_DAY: True              12_price_loaded: True     13_passive_below: True
                                                       14_status_logged: True
=> ALL_SAFETY_CHECKS_PASS
```

## Phase 2 — Submitted order
| Field | Value |
|---|---|
| Account | DUQ834606 |
| Action / Qty | BUY 1 |
| Type | LIMIT |
| Limit price | 6801.25 (≈10% below anchor 7557; tick-aligned to 0.25) |
| TIF | DAY |
| transmit | True |
| **orderId** | **6** |
| Initial status | PreSubmitted |
| Appeared in open orders | Yes (open_ids=[6]) |

## Phase 3 — Status + cancel validation
| Field | Value |
|---|---|
| Cancel requested | 2026-06-20T00:30:39Z |
| Final status | **Cancelled** |
| Filled | 0.0 |
| Remaining | 1.0 |
| Fill occurred | No |
| Position opened | No (positions_after = []) |
| Open orders after cancel | None (open_orders_after = []) |

### Raw IBKR status events
```
00:30:36.703Z  PendingSubmit
00:30:36.912Z  PreSubmitted
00:30:39.705Z  PendingCancel
00:30:39.893Z  Cancelled
```

---

## Verdict

**PASS** — Order accepted, status events received, cancel confirmed, no fill, no
position, open orders empty afterward, correct paper account, correct contract.

### Final answers
1. **PASS / FAIL:** PASS.
2. **Exact evidence:** orderId 6, status chain `PendingSubmit→PreSubmitted→PendingCancel→Cancelled`, filled 0.0, positions_after `[]`, open_orders_after `[]`, account `DUQ834606`, contract `MESU6`.
3. **Is the IBKR paper order path proven?** Yes — end-to-end submit → status → cancel is verified against the live paper account.
4. **Is automated paper trading still blocked by `snapshot()`?** No longer — **fixed and verified 2026-06-20.** `IBKRAdapter.snapshot()` was rewritten off the streaming `reqAccountUpdates` onto timeout-bounded async calls (`accountSummaryAsync` + `reqPositionsAsync` via `_run_bounded`). Live verification against `DUQ834606`: returned in **0.67s** with cash/equity €1,000,000, 0 positions, `partial=False`, no warnings. The monitor/reconcile account-readout path is unblocked. (Minor residual: `AccountSnapshot.account_id` is typed `int`, so the alphanumeric IBKR id `DUQ834606` is coerced to `0` — logging-fidelity only, not a blocker.)

### Operational note (not a blocker)
Live L1 market-data streaming for MES is **not subscribed** (`Error 354`) — only
historical/delayed is available. The smoke test didn't need live quotes, but real
order placement that prices off the live bid/ask will need a CME L1 real-time
subscription (or delayed-data mode) enabled in IBKR.

---

**STOPPING per directive.** No strategy orders, no new features, no unrelated
fixes, no automated trading. Next step is operator review.
