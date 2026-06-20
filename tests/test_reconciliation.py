"""Trade-reconciliation engine + metrics — Phase 6 (10 required cases).

Pure unit tests: ExecutionEvent objects in, ReconciledTrade objects out. No
broker, no files. point_value is overridden to 1.0 so P&L equals price points
unless a test specifically checks instrument resolution.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from execution.base import ExecutionEvent
from reconciliation import (
    CANCELLED, CLOSED, OPEN, PARTIAL, REJECTED, compute_metrics, reconcile,
)

PV1 = lambda _sym: 1.0   # 1 point = 1 unit of P&L, for clean arithmetic


def _ev(exec_id, order_id, side, qty, price, *, ts, kind="fill", parent=None,
        perm=None, account="DUQ834606", symbol="MESU6", commission=0.0):
    return ExecutionEvent(
        execution_id=exec_id, order_id=order_id, parent_order_id=parent,
        timestamp=ts, symbol=symbol, side=side, qty=qty, price=price,
        commission=commission, kind=kind, perm_id=perm, account=account)


def _only(trades, status=CLOSED):
    xs = [t for t in trades if t.status == status]
    assert len(xs) == 1, f"expected 1 {status}, got {[t.status for t in trades]}"
    return xs[0]


# 1 ─ simple entry + target -------------------------------------------------
def test_case1_simple_entry_target():
    evs = [
        _ev("EX1", "O1", "Buy", 1, 6800.0, ts="2026-06-20T14:00:00Z"),
        _ev("EX2", "O2", "Sell", 1, 6810.0, ts="2026-06-20T14:30:00Z", parent="O1"),
    ]
    meta = {"O1": {"intended_entry": 6800.0, "intended_stop": 6790.0, "intended_target": 6810.0}}
    t = _only(reconcile(evs, order_meta=meta, point_value=PV1))
    assert t.status == CLOSED and t.side == "Buy"
    assert t.entry_price == 6800.0 and t.exit_price == 6810.0
    assert t.entry_qty == 1 and t.exit_qty == 1
    assert t.gross_pnl == 10.0 and t.net_pnl == 10.0
    assert t.realized_R == 1.0 and t.exit_reason == "target"
    assert t.execution_ids == ["EX1", "EX2"]


# 2 ─ simple entry + stop ---------------------------------------------------
def test_case2_simple_entry_stop():
    evs = [
        _ev("EX1", "O1", "Buy", 1, 6800.0, ts="2026-06-20T14:00:00Z"),
        _ev("EX2", "O3", "Sell", 1, 6790.0, ts="2026-06-20T14:10:00Z", parent="O1"),
    ]
    meta = {"O1": {"intended_entry": 6800.0, "intended_stop": 6790.0, "intended_target": 6820.0}}
    t = _only(reconcile(evs, order_meta=meta, point_value=PV1))
    assert t.net_pnl == -10.0 and t.realized_R == -1.0 and t.exit_reason == "stop"


# 3 ─ partial fill entry (scale-in over two executions) ---------------------
def test_case3_partial_fill_entry():
    evs = [
        _ev("EX1", "O1", "Buy", 1, 6800.0, ts="2026-06-20T14:00:00Z"),
        _ev("EX2", "O1", "Buy", 1, 6802.0, ts="2026-06-20T14:00:05Z"),
        _ev("EX3", "O2", "Sell", 2, 6810.0, ts="2026-06-20T14:30:00Z", parent="O1"),
    ]
    t = _only(reconcile(evs, point_value=PV1))
    assert t.entry_qty == 2 and t.exit_qty == 2
    assert t.entry_price == 6801.0          # VWAP (6800+6802)/2
    assert t.gross_pnl == 18.0              # (6810-6801)*2


# 4 ─ partial fill exit (scale-out) + intermediate PARTIAL state ------------
def test_case4_partial_fill_exit():
    evs = [
        _ev("EX1", "O1", "Buy", 2, 6800.0, ts="2026-06-20T14:00:00Z"),
        _ev("EX2", "O2", "Sell", 1, 6810.0, ts="2026-06-20T14:20:00Z", parent="O1"),
        _ev("EX3", "O2", "Sell", 1, 6812.0, ts="2026-06-20T14:25:00Z", parent="O1"),
    ]
    t = _only(reconcile(evs, point_value=PV1))
    assert t.entry_qty == 2 and t.exit_qty == 2 and t.exit_price == 6811.0
    assert t.gross_pnl == 22.0              # (6811-6800)*2

    # Stop after only one exit fill → PARTIAL, no realised P&L.
    part = _only(reconcile(evs[:2], point_value=PV1), status=PARTIAL)
    assert part.exit_qty == 1 and part.net_pnl is None


# 5 ─ multiple executions on both legs --------------------------------------
def test_case5_multiple_executions():
    evs = [
        _ev("EX1", "O1", "Buy", 1, 6800.0, ts="2026-06-20T14:00:00Z"),
        _ev("EX2", "O1", "Buy", 1, 6801.0, ts="2026-06-20T14:00:01Z"),
        _ev("EX3", "O1", "Buy", 1, 6802.0, ts="2026-06-20T14:00:02Z"),
        _ev("EX4", "O2", "Sell", 1, 6810.0, ts="2026-06-20T14:30:00Z"),
        _ev("EX5", "O2", "Sell", 1, 6811.0, ts="2026-06-20T14:30:01Z"),
        _ev("EX6", "O2", "Sell", 1, 6812.0, ts="2026-06-20T14:30:02Z"),
    ]
    t = _only(reconcile(evs, point_value=PV1))
    assert t.entry_qty == 3 and t.exit_qty == 3
    assert t.entry_price == 6801.0 and t.exit_price == 6811.0
    assert t.gross_pnl == 30.0


# 6 ─ cancelled order (zero fills) ------------------------------------------
def test_case6_cancelled_order():
    evs = [_ev("", "O9", "Buy", 0, 0.0, ts="2026-06-20T14:00:00Z", kind="cancel")]
    t = _only(reconcile(evs, point_value=PV1), status=CANCELLED)
    assert t.entry_order_id == "O9" and t.net_pnl is None


def test_case6b_cancel_after_fill_is_not_a_separate_trade():
    """A cancel of the remainder of a partially-filled order is not a new trade."""
    evs = [
        _ev("EX1", "O1", "Buy", 1, 6800.0, ts="2026-06-20T14:00:00Z"),
        _ev("EX2", "O2", "Sell", 1, 6810.0, ts="2026-06-20T14:30:00Z"),
        _ev("", "O1", "Buy", 0, 0.0, ts="2026-06-20T14:31:00Z", kind="cancel"),
    ]
    trades = reconcile(evs, point_value=PV1)
    assert not any(t.status == CANCELLED for t in trades)
    assert sum(1 for t in trades if t.status == CLOSED) == 1


# 7 ─ rejected order --------------------------------------------------------
def test_case7_rejected_order():
    evs = [_ev("", "O7", "Buy", 0, 0.0, ts="2026-06-20T14:00:00Z", kind="reject")]
    t = _only(reconcile(evs, point_value=PV1), status=REJECTED)
    assert t.entry_order_id == "O7"


# 8 ─ bracket order: entry + target fill, stop sibling auto-cancels ----------
def test_case8_bracket_order():
    evs = [
        _ev("EX1", "O1", "Buy", 1, 6800.0, ts="2026-06-20T14:00:00Z"),               # parent entry
        _ev("EX2", "O2", "Sell", 1, 6810.0, ts="2026-06-20T14:30:00Z", parent="O1"),  # target fill
        _ev("", "O3", "Sell", 0, 0.0, ts="2026-06-20T14:30:01Z", kind="cancel", parent="O1"),  # OCA stop cancel
    ]
    trades = reconcile(evs, point_value=PV1)
    # Exactly one CLOSED trade; the OCA stop cancel is NOT a separate cancelled trade.
    assert [t.status for t in trades] == [CLOSED]
    assert trades[0].gross_pnl == 10.0


# 9 ─ duplicate broker events (same execution_id) ---------------------------
def test_case9_duplicate_events_deduped():
    evs = [
        _ev("EX1", "O1", "Buy", 1, 6800.0, ts="2026-06-20T14:00:00Z"),
        _ev("EX1", "O1", "Buy", 1, 6800.0, ts="2026-06-20T14:00:00Z"),   # exact duplicate
        _ev("EX2", "O2", "Sell", 1, 6810.0, ts="2026-06-20T14:30:00Z"),
    ]
    t = _only(reconcile(evs, point_value=PV1))
    assert t.entry_qty == 1 and t.exit_qty == 1   # dup ignored, not doubled
    assert t.gross_pnl == 10.0


# 10 ─ out-of-order events ---------------------------------------------------
def test_case10_out_of_order_events():
    # Exit appears BEFORE entry in the list, but timestamps are correct.
    evs = [
        _ev("EX2", "O2", "Sell", 1, 6810.0, ts="2026-06-20T14:30:00Z"),
        _ev("EX1", "O1", "Buy", 1, 6800.0, ts="2026-06-20T14:00:00Z"),
    ]
    t = _only(reconcile(evs, point_value=PV1))
    assert t.side == "Buy" and t.entry_price == 6800.0 and t.exit_price == 6810.0
    assert t.gross_pnl == 10.0


# ── extras: short side, commission, instrument point value, flip-through-zero
def test_short_side_round_trip():
    evs = [
        _ev("EX1", "O1", "Sell", 1, 6810.0, ts="2026-06-20T14:00:00Z"),   # short entry
        _ev("EX2", "O2", "Buy", 1, 6800.0, ts="2026-06-20T14:30:00Z"),    # cover lower → win
    ]
    t = _only(reconcile(evs, point_value=PV1))
    assert t.side == "Sell" and t.gross_pnl == 10.0


def test_commission_reduces_net_pnl():
    evs = [
        _ev("EX1", "O1", "Buy", 1, 6800.0, ts="2026-06-20T14:00:00Z", commission=2.0),
        _ev("EX2", "O2", "Sell", 1, 6810.0, ts="2026-06-20T14:30:00Z", commission=2.0),
    ]
    t = _only(reconcile(evs, point_value=PV1))
    assert t.commission == 4.0 and t.gross_pnl == 10.0 and t.net_pnl == 6.0


def test_point_value_resolves_mes():
    """Without a point_value override, MES resolves to 5.0 USD/point."""
    evs = [
        _ev("EX1", "O1", "Buy", 1, 6800.0, ts="2026-06-20T14:00:00Z", symbol="MESU6"),
        _ev("EX2", "O2", "Sell", 1, 6810.0, ts="2026-06-20T14:30:00Z", symbol="MESU6"),
    ]
    t = _only(reconcile(evs))
    assert t.point_value == 5.0 and t.gross_pnl == 50.0   # 10 pts * 5.0


def test_flip_through_zero_splits_into_two_trades():
    """Long 1, then Sell 2 → closes the long AND opens a short 1."""
    evs = [
        _ev("EX1", "O1", "Buy", 1, 6800.0, ts="2026-06-20T14:00:00Z"),
        _ev("EX2", "O2", "Sell", 2, 6810.0, ts="2026-06-20T14:30:00Z"),
        _ev("EX3", "O3", "Buy", 1, 6805.0, ts="2026-06-20T15:00:00Z"),   # cover the short
    ]
    trades = reconcile(evs, point_value=PV1)
    closed = [t for t in trades if t.status == CLOSED]
    assert len(closed) == 2
    longt = next(t for t in closed if t.side == "Buy")
    shortt = next(t for t in closed if t.side == "Sell")
    assert longt.gross_pnl == 10.0       # 6800 → 6810
    assert shortt.gross_pnl == 5.0       # short 6810 → cover 6805


# ── metrics engine (closed trades only) -------------------------------------
def _closed(net, r=None, exit_time="2026-06-20T15:00:00Z", slippage=0.0):
    from reconciliation.model import ReconciledTrade
    return ReconciledTrade(
        trade_id=f"t{net}", account_id="DUQ834606", symbol="MESU6", status=CLOSED,
        net_pnl=net, gross_pnl=net, realized_R=r, exit_time=exit_time,
        slippage=slippage, commission=1.0)


def test_metrics_basic():
    trades = [
        _closed(10.0, r=1.0, exit_time="2026-06-20T15:00:00Z"),
        _closed(20.0, r=2.0, exit_time="2026-06-20T16:00:00Z"),
        _closed(-5.0, r=-0.5, exit_time="2026-06-20T17:00:00Z"),
    ]
    m = compute_metrics(trades)
    assert m["n_closed"] == 3 and m["n_wins"] == 2 and m["n_losses"] == 1
    assert m["gross_profit"] == 30.0 and m["gross_loss"] == 5.0
    assert m["profit_factor"] == 6.0
    assert round(m["expectancy"], 3) == round(25 / 3, 3)
    assert round(m["win_rate"], 3) == 0.667
    assert m["avg_R"] == round((1.0 + 2.0 - 0.5) / 3, 6)
    assert m["max_drawdown"] == 5.0       # +10,+30,25 → peak 30, dd 5
    assert m["recovery_factor"] == 5.0    # total 25 / dd 5


def test_metrics_exclude_non_closed():
    from reconciliation.model import ReconciledTrade
    trades = [
        _closed(10.0),
        ReconciledTrade("o", "DUQ834606", "MESU6", OPEN),
        ReconciledTrade("p", "DUQ834606", "MESU6", PARTIAL),
        ReconciledTrade("c", "DUQ834606", "MESU6", CANCELLED),
        ReconciledTrade("r", "DUQ834606", "MESU6", REJECTED),
    ]
    m = compute_metrics(trades)
    assert m["n_closed"] == 1
    assert m["excluded_open"] == 1 and m["excluded_partial"] == 1
    assert m["cancelled"] == 1 and m["rejected"] == 1


def test_metrics_profit_factor_none_without_losers():
    m = compute_metrics([_closed(10.0), _closed(5.0)])
    assert m["profit_factor"] is None and m["n_losses"] == 0


def test_metrics_empty():
    m = compute_metrics([])
    assert m["n_closed"] == 0 and m["expectancy"] is None and m["profit_factor"] is None
