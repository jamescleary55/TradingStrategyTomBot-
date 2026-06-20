# First Paper Order — Readiness

**Generated:** 2026-06-20
**Account:** `DUQ834606` (PAPER, IBKR, €1,000,000 sim funds)
**Gateway:** `127.0.0.1:4002`, Read-Only API **disabled** (confirmed)

---

## PHASE 3 — Smoke test plan (smallest possible)

A single resting limit order that **cannot fill**, then cancel — proves submission
+ status flow + cancel with zero fill/risk. Paper account only, MES, qty 1, no
strategy logic, manual run.

| Field | Value |
|---|---|
| Account | DUQ834606 (paper) |
| Contract | MES (Micro E-mini S&P 500), MESU6 |
| Side / Qty | BUY 1 |
| Order type | LIMIT, far **below** market (e.g. ~10% under last) so it rests unfilled |
| TIF | DAY |
| Expected API status flow | `PendingSubmit` → `PreSubmitted`/`Submitted` (resting) |
| Then | Cancel → `Cancelled` |
| Expected fill | none (intentionally unmarketable) |

### Exact command to run next (manual)
```bash
cd ~/projects/ict-futures-bot && source .venv/bin/activate
python -u - <<'PY'
from ib_insync import IB, ContFuture, LimitOrder
ib = IB(); ib.connect('127.0.0.1', 4002, clientId=40, timeout=15)
c = ContFuture('MES','CME','USD'); ib.qualifyContracts(c)
last = ib.reqTickers(c)[0].marketPrice() or 6000
o = LimitOrder('BUY', 1, round(last*0.90, 2)); o.tif='DAY'   # ~10% below market: rests, won't fill
trade = ib.placeOrder(c, o)
ib.sleep(2); print("status:", trade.orderStatus.status, "id:", trade.order.orderId)
ib.cancelOrder(o); ib.sleep(2); print("after cancel:", trade.orderStatus.status)
ib.disconnect()
PY
```
Expect: `status: PreSubmitted` (or `Submitted`) with a non-zero order id, then
`after cancel: Cancelled`. That proves end-to-end paper order submission.

> The bot's own path (`execution.IBKRAdapter.place_bracket`) uses a separate
> connection from the hanging `snapshot()` and should submit fine, but is not
> exercised here to keep the test to the smallest possible manual action.

---

## PHASE 4 — Execution logging review

Existing logging (sufficient for the smoke test):
- **Order id + submission** — `IBKRAdapter.place_bracket` returns `PlacedOrder(order_id, raw_response)`; the live monitor's auto-execute path writes `live/forward_log.py` → `live_trades.jsonl` (order_id, broker_response, outcome, timestamp).
- **Status changes / fills / executions** — `IBKRAdapter.list_executions()` maps IBKR fills → `ExecutionEvent` (execId, orderId, qty, price, commission, timestamp); `live/reconcile.py` resolves entry/exit.
- **Rejections** — `place_bracket` raises `IBKROrderError` if the parent goes `Inactive/Cancelled/ApiCancelled`; IBKR error text surfaces via ib_insync error events.

Gaps (do not block the manual smoke test):
- `IBKRAdapter.snapshot()` hangs (probe), so account/position logging via the bot is currently unusable — direct API works. Flag for a later fix; not needed to submit an order.
- For a quick manual limit-order test, status transitions are observed live via
  `trade.orderStatus.status`; persistent logging only kicks in through the monitor.

---

## PHASE 5 — GO / NO-GO

1. **Can we connect?** ✅ Yes (`isConnected=True`, port 4002).
2. **Can we read account data?** ✅ Yes (DUQ834606 paper, €1M, positions/orders load).
3. **Can we submit paper orders?** ✅ Yes — `whatIfOrder` accepted (PreSubmitted, margin+commission computed). Read-Only is off.
4. **What is still blocking the first paper trade?** Nothing on the IBKR side. The only open item is a **bot tooling bug** (`probe_broker`/`snapshot()` hang) which does not affect order submission. The actual first order is a **manual run** by the operator (per directive).
5. **Exact command to run next:** the Phase 3 snippet above.

---

## VERDICT

**READY_FOR_PAPER_ORDER**

Connectivity, account access, and order acceptance are all verified against the
live paper account. Run the Phase 3 command to submit (and cancel) the first
real paper order. The `snapshot()` hang is a separate, non-blocking tooling bug.
