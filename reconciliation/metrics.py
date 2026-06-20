"""Metrics engine — statistics derived ONLY from reconciled CLOSED trades.

Nothing here ever looks at raw orders, submissions, or open positions. If a
trade is not CLOSED (net position back to zero) it does not contribute to any
metric. This is the whole point of the reconciliation layer: performance claims
are made from broker-confirmed round-trips, not intentions.
"""
from __future__ import annotations

from statistics import mean
from typing import Optional

from reconciliation.model import CLOSED, ReconciledTrade


def _safe_div(a: float, b: float) -> Optional[float]:
    return (a / b) if b else None


def compute_metrics(trades: list[ReconciledTrade]) -> dict:
    """Return a metrics dict computed strictly from CLOSED trades.

    Keys: n_closed, n_wins, n_losses, n_scratch, gross_profit, gross_loss,
    total_net_pnl, expectancy, profit_factor, win_rate, avg_R, expectancy_R,
    avg_winner, avg_loser, max_drawdown, recovery_factor, avg_slippage,
    avg_commission, total_commission. Plus counts of non-closed trades for
    transparency (excluded_open, excluded_partial, cancelled, rejected).
    """
    closed = [t for t in trades if t.status == CLOSED and t.net_pnl is not None]

    excluded = {
        "excluded_open": sum(1 for t in trades if t.status == "OPEN"),
        "excluded_partial": sum(1 for t in trades if t.status == "PARTIAL"),
        "cancelled": sum(1 for t in trades if t.status == "CANCELLED"),
        "rejected": sum(1 for t in trades if t.status == "REJECTED"),
    }

    if not closed:
        return {
            "n_closed": 0, "n_wins": 0, "n_losses": 0, "n_scratch": 0,
            "gross_profit": 0.0, "gross_loss": 0.0, "total_net_pnl": 0.0,
            "expectancy": None, "profit_factor": None, "win_rate": None,
            "avg_R": None, "expectancy_R": None, "avg_winner": None,
            "avg_loser": None, "max_drawdown": 0.0, "recovery_factor": None,
            "avg_slippage": None, "avg_commission": None, "total_commission": 0.0,
            **excluded,
        }

    pnls = [t.net_pnl for t in closed]
    wins = [t for t in closed if t.net_pnl > 0]
    losses = [t for t in closed if t.net_pnl < 0]
    scratch = [t for t in closed if t.net_pnl == 0]

    gross_profit = sum(t.net_pnl for t in wins)
    gross_loss = -sum(t.net_pnl for t in losses)         # reported positive
    total = sum(pnls)
    n = len(closed)

    # Max drawdown on the cumulative net-P&L curve, ordered by exit time.
    ordered = sorted(closed, key=lambda t: (t.exit_time or "", t.trade_id))
    equity = peak = max_dd = 0.0
    for t in ordered:
        equity += t.net_pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    rs = [t.realized_R for t in closed if t.realized_R is not None]
    slips = [t.slippage for t in closed if t.slippage is not None]

    return {
        "n_closed": n,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "n_scratch": len(scratch),
        "gross_profit": round(gross_profit, 6),
        "gross_loss": round(gross_loss, 6),
        "total_net_pnl": round(total, 6),
        "expectancy": round(total / n, 6),                       # avg net P&L per trade
        "profit_factor": (round(gross_profit / gross_loss, 6)
                          if gross_loss > 0 else None),          # None = no losers yet
        "win_rate": round(len(wins) / n, 6),
        "avg_R": round(mean(rs), 6) if rs else None,
        "expectancy_R": round(mean(rs), 6) if rs else None,      # avg R per trade
        "avg_winner": round(mean([t.net_pnl for t in wins]), 6) if wins else None,
        "avg_loser": round(mean([t.net_pnl for t in losses]), 6) if losses else None,
        "max_drawdown": round(max_dd, 6),
        "recovery_factor": (round(total / max_dd, 6) if max_dd > 0 else None),
        "avg_slippage": round(mean(slips), 6) if slips else None,
        "avg_commission": round(mean([t.commission for t in closed]), 6),
        "total_commission": round(sum(t.commission for t in closed), 6),
        **excluded,
    }
