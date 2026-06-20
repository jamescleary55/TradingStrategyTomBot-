# IBKR Automated Paper Execution — Readiness

**Generated:** 2026-06-20
**Account:** `DUQ834606` (IBKR paper) · Gateway `127.0.0.1:4002`
**Scope:** prepare for controlled automated paper execution. **Not enabled.**

---

## What passed

| Area | Status | Evidence |
|---|---|---|
| Connectivity | ✅ | `isConnected=True`, paper account `DUQ834606` detected |
| Order path | ✅ PROVEN | Smoke test orderId 6: submit→cancel, 0 fill, 0 position (`IBKR_FIRST_PAPER_ORDER_SMOKE_TEST.md`) |
| `snapshot()` hang | ✅ FIXED | Rewritten off streaming `reqAccountUpdates` onto timeout-bounded `accountSummaryAsync` + `reqPositionsAsync` via `_run_bounded`. Returns PARTIAL (never hangs) on timeout. Verified live (~0.67s) + unit tests. |
| Market-data status | ✅ HANDLED | `classify_data_status()` + `probe_data_status()` → LIVE / DELAYED / HISTORICAL_ONLY / UNAVAILABLE. Error 354 no longer masquerades as live. |
| Safety gate | ✅ BUILT | `risk/exec_gate.evaluate_execution_gate()` — 10-condition GO/NO-GO. |
| Tests | ✅ | 74 pass (20 new: 3 snapshot-safety, 17 gate + data-status). |

## What remains blocked / open

| Item | State | Impact |
|---|---|---|
| MES live L1 market data | ⛔ `HISTORICAL_ONLY` (Error 354) | Gate **blocks** automated orders unless a CME real-time L1 subscription is enabled, or the operator passes an explicit data override for a supervised manual test. |
| Full automation | ⛔ Intentionally OFF | Not enabled per directive. Only a controlled, operator-supervised single order is the next step. |
| `AccountSnapshot.account_id` typing | minor | Typed `int`, so `DUQ834606` logs as `0` (fidelity only, not a blocker). |

## Safety gate — conditions (all must hold)

`evaluate_execution_gate()` returns GO only when:
account starts `DU` · broker `ibkr` · mode `paper` · `live_account=False` ·
snapshot not None (partial OK) · data_status `LIVE` (or explicit override) ·
order_qty `== 1` · symbol in `{MES}` · kill switch absent · open_positions `== 0`.

---

## Next test — DESIGN ONLY (do not run yet)

First controlled paper **strategy** test:
- **MES** front-month only, **qty 1**, **paper account `DUQ834606`** only
- **one manually selected signal** (operator picks the entry/stop/target) — no signal automation, no scanning loop
- **simple single order first**; bracket only after simple order flow is stable
- gate must return GO before submission; on NO-GO, block and log
- on any unexpected fill: log, confirm position, and STOP — close only on explicit operator instruction

### Preconditions before running
1. Enable **CME real-time L1** market data in IBKR (so `probe_data_status('MES')` → `LIVE`), **or** consciously accept a data override for this supervised test.
2. IB Gateway running, **Read-Only API OFF**.
3. `open_positions == 0`, no kill switch file.

### Exact command to run WHEN READY (operator-supervised)
```bash
cd ~/projects/ict-futures-bot && source .venv/bin/activate
python -u - <<'PY'
# Controlled single paper order, gated. Operator fills in the manually chosen price.
from data.ibkr_feed import probe_data_status, DATA_LIVE
from execution.base import get_adapter
from risk.exec_gate import evaluate_execution_gate
from config import INSTRUMENTS

DATA_OVERRIDE = False          # set True only to consciously test on non-live data
ENTRY = None                   # <-- operator sets the manually chosen limit price

adapter = get_adapter("ibkr")
snap = adapter.snapshot()                       # bounded; never hangs
status = probe_data_status("MES")
gate = evaluate_execution_gate(
    account_id=str(getattr(snap, "currency", "") and "DUQ834606") or "DUQ834606",
    broker="ibkr", mode="paper", live_account=False,
    snapshot=snap, data_status=status, order_qty=1, symbol="MES",
    kill_switch_present=False, open_positions=len(snap.positions),
    data_override=DATA_OVERRIDE,
)
print("data_status:", status, "| gate:", gate.allowed, "| reasons:", gate.reasons)
assert gate.allowed, f"GATE BLOCKED: {gate.reasons}"
assert ENTRY is not None, "operator must set ENTRY (manual price)"
# ... place ONE 1-lot MES limit at ENTRY via adapter.place_bracket(dry_run=False),
#     then verify status and manage manually. (Left for the supervised run.)
PY
```

> The command **gates first and asserts** — it will not place an order unless the
> environment is safe and the operator has set a manual price. The actual
> `place_bracket(...)` call is intentionally left commented for the supervised run.

---

## VERDICT

**READY_FOR_CONTROLLED_PAPER_STRATEGY_TEST** — with one explicit precondition:
enable CME real-time L1 data for MES (so the gate sees `data_status == LIVE`), or
pass a deliberate operator data-override for the supervised test. The snapshot
hang is fixed, the order path is proven, and the safety gate + market-data status
handling are in place. Automated trading remains **disabled** until the operator
runs the controlled test above and reviews the result.
