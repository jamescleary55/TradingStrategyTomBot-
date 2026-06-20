"""Execution preflight — the single choke point before any automated order.

This wires the pure :func:`risk.exec_gate.evaluate_execution_gate` to the live
broker: it gathers the real-world inputs (account snapshot, open positions,
resting orders, kill-switch state, duplicate-signal check) and returns a
GO/NO-GO. Every decision and every failure is logged to ``events.jsonl``
(Phase 5 observability).

The monitor calls :func:`preflight` immediately before placing an order. On a
NO-GO it must NOT submit. The guard itself never submits anything.

Fail-safe posture: if the snapshot or any broker read errors, the gate is fed
a hard-failure (snapshot=None) so the order is blocked — never the reverse.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from execution.base import AccountSnapshot, BrokerAdapter
from live.forward_log import EV_SYSTEM, log_event
from risk import kill_switch as ks
from risk.exec_gate import GateResult, evaluate_execution_gate

log = logging.getLogger("live.exec_guard")


@dataclass
class PreflightResult:
    gate: GateResult
    snapshot: Optional[AccountSnapshot]

    @property
    def allowed(self) -> bool:
        return self.gate.allowed

    def __bool__(self) -> bool:
        return self.gate.allowed


def signal_signature(symbol: str, direction: str, timestamp) -> str:
    """Stable identity for a setup, used for duplicate-execution detection."""
    ts = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)
    return f"{(symbol or '').upper()}|{(direction or '').lower()}|{ts}"


def _safe_snapshot(adapter: BrokerAdapter, symbol: str) -> Optional[AccountSnapshot]:
    """Snapshot the account, converting any failure into None (a hard block)."""
    try:
        snap = adapter.snapshot()
        if snap is not None and snap.partial:
            log_event(EV_SYSTEM, "snapshot_partial", symbol=symbol,
                      severity="warning", detail="; ".join(snap.warnings))
        return snap
    except Exception as e:
        log_event(EV_SYSTEM, "snapshot_timeout", symbol=symbol, severity="error",
                  detail=f"{e.__class__.__name__}: {e}")
        log.warning("snapshot failed during preflight: %s", e)
        return None


def _safe_open_orders(adapter: BrokerAdapter, account_id: Optional[str],
                      symbol: str) -> tuple[bool, bool]:
    """Return (pending_exists, read_ok). A failed read is fail-safe (pending=True)."""
    try:
        orders = adapter.list_open_orders(account_id=account_id)
        return (bool(orders), True)
    except NotImplementedError:
        return (False, True)               # adapter can't tell — don't over-block
    except Exception as e:
        log_event(EV_SYSTEM, "open_orders_read_failed", symbol=symbol,
                  severity="error", detail=f"{e.__class__.__name__}: {e}")
        return (True, False)               # fail safe: assume an order may rest


def preflight(
    *,
    adapter: BrokerAdapter,
    mode: str,
    allow_live: bool,
    symbol: str,
    order_qty: int,
    setup_signature: str,
    executed_signatures: set[str],
    data_status: str,
    data_override: bool = False,
    kill_switch_path: Optional[str] = None,
) -> PreflightResult:
    """Run the full execution preflight and return a GO/NO-GO + the snapshot."""
    # Kill switch FIRST — cheapest, and an operator halt must win immediately.
    kstate = ks.check(kill_switch_path)
    if kstate.present:
        log_event(EV_SYSTEM, "kill_switch", symbol=symbol, severity="warning",
                  detail=f"halt file present: {kstate.path}")

    snap = _safe_snapshot(adapter, symbol)
    account_id = snap.account_id if snap is not None else ""
    open_positions = len(snap.positions) if snap is not None else 1  # unknown ⇒ block
    pending_exists, _orders_ok = _safe_open_orders(adapter, account_id or None, symbol)
    duplicate = setup_signature in executed_signatures

    gate = evaluate_execution_gate(
        account_id=account_id,
        broker=adapter.name,
        mode=mode,
        live_account=bool(allow_live),
        snapshot=snap,
        data_status=data_status,
        order_qty=order_qty,
        symbol=symbol,
        kill_switch_present=kstate.present,
        open_positions=open_positions,
        duplicate_signal=duplicate,
        pending_order_exists=pending_exists,
        data_override=data_override,
    )

    if not gate.allowed:
        log_event(EV_SYSTEM, "gate_block", symbol=symbol, severity="warning",
                  detail="; ".join(gate.reasons), reasons=gate.reasons,
                  account_id=account_id)
    return PreflightResult(gate=gate, snapshot=snap)
