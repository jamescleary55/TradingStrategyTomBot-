"""Safe automated paper-execution gate.

A single, conservative pre-flight check that must pass before ANY automated
paper order is submitted. It is deliberately strict and additive to the existing
``risk/controls.RiskGate`` — RiskGate decides whether a *setup* is tradeable;
this gate decides whether the *execution environment* is safe to act in at all.

Automated paper execution is allowed ONLY when ALL TEN conditions hold
(Phase 2 mandatory execution gates):
   1. broker == "ibkr"
   2. account id starts with "DU"  (IBKR paper)
   3. mode == "paper"  (paper account confirmed; live_account is False)
   4. snapshot did not hard-fail (a PARTIAL snapshot is acceptable; None is not)
   5. market-data status is "LIVE" (or explicit operator override)
   6. kill switch absent
   7. open positions == 0
   8. no duplicate signal (this setup was not already executed)
   9. no pending order already exists
  10. order qty <= 1 (and >= 1)

If ANY fail, the gate returns NO-GO with the exact blocking reason(s). The
caller MUST block the order and log the reason. Nothing here submits or enables
anything; it only returns a GO/NO-GO.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MAX_ORDER_QTY = 1
ALLOWED_SYMBOLS_INITIAL = {"MES"}
LIVE_DATA_STATUS = "LIVE"


@dataclass
class GateResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)  # blocking reasons (empty if allowed)

    def __bool__(self) -> bool:
        return self.allowed


def evaluate_execution_gate(
    *,
    account_id: str,
    broker: str,
    mode: str,
    live_account: bool,
    snapshot,                 # AccountSnapshot or None (None = hard failure)
    data_status: str,
    order_qty: int,
    symbol: str,
    kill_switch_present: bool,
    open_positions: int,
    duplicate_signal: bool = False,
    pending_order_exists: bool = False,
    data_override: bool = False,
) -> GateResult:
    """Return GateResult(allowed, reasons). Allowed only if reasons is empty."""
    reasons: list[str] = []

    # 1. broker
    if broker != "ibkr":
        reasons.append(f"broker != ibkr (got {broker!r})")
    # 2. paper (DU) account
    if not (account_id or "").startswith("DU"):
        reasons.append(f"account {account_id!r} is not a paper (DU) account")
    # 3. paper mode + not a live account
    if mode != "paper":
        reasons.append(f"mode != paper (got {mode!r})")
    if live_account:
        reasons.append("live_account is True")
    # 4. snapshot succeeded (partial is OK; None is a hard failure)
    if snapshot is None:
        reasons.append("snapshot hard-failed (None)")
    # 5. market data valid
    if data_status != LIVE_DATA_STATUS and not data_override:
        reasons.append(f"market-data status {data_status!r} (need LIVE or explicit override)")
    # 6. kill switch
    if kill_switch_present:
        reasons.append("kill switch present")
    # 7. flat
    if open_positions != 0:
        reasons.append(f"open_positions {open_positions} != 0")
    # 8. no duplicate signal
    if duplicate_signal:
        reasons.append("duplicate signal (already executed)")
    # 9. no pending order already resting
    if pending_order_exists:
        reasons.append("a pending order already exists")
    # 10. qty bound (<= 1, and a real order so >= 1)
    if order_qty < 1 or order_qty > MAX_ORDER_QTY:
        reasons.append(f"order_qty {order_qty} outside [1, {MAX_ORDER_QTY}]")
    # symbol allowlist (MES only for the initial controlled run)
    if (symbol or "").upper() not in ALLOWED_SYMBOLS_INITIAL:
        reasons.append(f"symbol {symbol!r} not in initial allowlist {sorted(ALLOWED_SYMBOLS_INITIAL)}")

    return GateResult(allowed=not reasons, reasons=reasons)
