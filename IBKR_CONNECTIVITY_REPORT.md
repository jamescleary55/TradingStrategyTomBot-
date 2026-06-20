# IBKR Connectivity Report

**Generated:** 2026-06-20
**Transport:** ib_insync → IB Gateway, `127.0.0.1:4002` (Gateway paper)
**Trigger:** Read-Only API mode reported disabled — verifying order capability.

---

## PHASE 1 — Connectivity validation

| Check | Result |
|---|---|
| IB Gateway running | ✅ yes (socket open on 4002) |
| Host/port configured | ✅ `IB_HOST=127.0.0.1`, `IB_PORT=4002` (`.env`) |
| Authentication / connect | ✅ `isConnected=True` |
| Account information loads | ✅ see below |
| Positions load | ✅ 0 open |
| Open orders load | ✅ 0 open |

### Account detected
| Field | Value |
|---|---|
| Account ID | `DUQ834606` |
| Type | **PAPER** (DU-prefix) · INDIVIDUAL |
| NetLiquidation | 1,000,000.00 EUR |
| TotalCashValue | 1,000,000.00 EUR |
| AvailableFunds | 1,000,000.00 EUR |
| BuyingPower | 6,666,666.67 EUR |

### Endpoints exercised
- `managedAccounts()` ✅
- `accountSummary()` ✅
- `positions()` ✅ (0)
- `reqAllOpenOrders()` ✅ (0)
- `qualifyContracts(ContFuture MES/CME/USD)` ✅ → `MESU6`, conId `793356217`
- `whatIfOrder()` ✅ (order validation — see Phase 2)

### Errors encountered
- `Error 10349: Order TIF was set to DAY based on order preset.` — **informational**, not a rejection. IBKR auto-sets TIF=DAY for the future per account preset. Setting `order.tif='DAY'` explicitly removes the message.
- **Known tooling gap:** the bundled `scripts/probe_broker.py --broker ibkr` **hangs** (the `IBKRAdapter.snapshot()` path stalls on `reqAccountUpdates`/`accountValues`). Direct `ib_insync` calls return instantly, so this is a probe/adapter-snapshot bug, **not** a Gateway/connectivity problem. Order submission uses a different code path and is unaffected (see readiness report).

---

## PHASE 2 — Order capability check (verified, not assumed)

Validated via non-destructive `whatIfOrder` (no order placed):

| Field | Value |
|---|---|
| Contract | MES (MESU6) |
| Order | MarketOrder BUY 1, TIF=DAY |
| status | `PreSubmitted` |
| commission | 0.62 |
| initMarginChange | 2,964.92 |
| maintMarginChange | 2,177.63 |
| warningText | (none) |
| **ORDER_ACCEPTED** | **True** |

Interpretation: the paper account has trading permissions for CME index futures, no blocking restrictions, and the API accepts order submission. Margin and commission are computed, confirming the order would route.

**Verdict for connectivity + order capability: PASS.**
