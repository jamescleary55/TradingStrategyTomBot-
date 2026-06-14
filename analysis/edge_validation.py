"""Edge-validation metrics.

Answers six adversarial questions on forward + paper data:

    1. Is there an edge at all?           → expectancy, profit factor, Sharpe
    2. Is the edge stable in time?         → rolling R, drawdown durations
    3. Is the edge tradeable?              → fill rate, avg slippage, time-in-trade
    4. Is it robust across symbols?        → per-symbol expectancy + sample sizes
    5. Is it robust across sessions?       → per-session expectancy + sample sizes
    6. Is it robust across regimes?        → split forward window into halves

Strictly read-only. No mutating, no opinions baked into the math.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import median, pstdev
from typing import Any, Optional

import pandas as pd


# ---------------------------------------------------------------------------
@dataclass
class CoreMetrics:
    n: int
    wins: int
    losses: int
    win_rate: float
    avg_R: float
    median_R: float
    avg_win_R: float
    avg_loss_R: float
    total_R: float
    profit_factor: float          # sum(wins) / |sum(losses)|
    expectancy_R: float           # same as avg_R
    payoff_ratio: float           # avg_win / |avg_loss|
    sharpe_R: float               # mean(R) / stdev(R)  (R-based, not annualised)
    max_drawdown_R: float
    max_drawdown_trades: int
    recovery_factor: float        # total_R / |max_drawdown_R|
    avg_time_in_trade_h: float
    r_distribution: dict[str, int]   # bucketed (<-1, -1..-0.5, -0.5..0, 0..0.5, ...)


def _closed(trades: list[dict]) -> list[dict]:
    return [t for t in trades if t.get("outcome") in ("target", "stop")
            and "r_realised" in t]


def _time_in_trade_hours(trades: list[dict]) -> float:
    durations = []
    for t in trades:
        a = t.get("timestamp") or t.get("ts_logged")
        b = t.get("exit_ts")
        if not (a and b):
            continue
        try:
            durations.append((pd.Timestamp(b) - pd.Timestamp(a)).total_seconds() / 3600)
        except Exception:
            pass
    return (sum(durations) / len(durations)) if durations else 0.0


def _r_buckets(rs: list[float]) -> dict[str, int]:
    edges = [-99, -2, -1.5, -1.0, -0.5, 0, 0.5, 1.0, 1.5, 2.0, 99]
    labels = ["<-2R", "-2..-1.5", "-1.5..-1", "-1..-0.5", "-0.5..0",
              "0..0.5", "0.5..1", "1..1.5", "1.5..2", "2R+"]
    out = {k: 0 for k in labels}
    for r in rs:
        for i in range(len(edges) - 1):
            if edges[i] < r <= edges[i + 1]:
                out[labels[i]] += 1
                break
    return out


# ---------------------------------------------------------------------------
def compute_metrics(trades: list[dict]) -> CoreMetrics:
    closed = _closed(trades)
    if not closed:
        return CoreMetrics(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                           0.0, 0.0, 0.0, 0, 0.0, 0.0, {})

    rs = [float(t["r_realised"]) for t in closed]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]

    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0

    cum = 0.0
    peak = 0.0
    peak_idx = -1
    max_dd = 0.0
    max_dd_trades = 0
    cur_dd_trades = 0
    for i, r in enumerate(rs):
        cum += r
        if cum > peak:
            peak = cum
            peak_idx = i
            cur_dd_trades = 0
        else:
            cur_dd_trades += 1
            dd = cum - peak
            if dd < max_dd:
                max_dd = dd
                max_dd_trades = cur_dd_trades

    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf") if wins else 0.0
    payoff = (avg_win / abs(avg_loss)) if avg_loss != 0 else 0.0
    sharpe = (sum(rs) / len(rs) / pstdev(rs)) if len(rs) > 1 and pstdev(rs) > 0 else 0.0
    recov = (cum / abs(max_dd)) if max_dd != 0 else float("inf") if cum > 0 else 0.0

    return CoreMetrics(
        n=len(rs),
        wins=len(wins), losses=len(losses),
        win_rate=len(wins) / len(rs) * 100,
        avg_R=sum(rs) / len(rs),
        median_R=median(rs),
        avg_win_R=avg_win, avg_loss_R=avg_loss,
        total_R=cum,
        profit_factor=pf,
        expectancy_R=sum(rs) / len(rs),
        payoff_ratio=payoff,
        sharpe_R=sharpe,
        max_drawdown_R=max_dd,
        max_drawdown_trades=max_dd_trades,
        recovery_factor=recov,
        avg_time_in_trade_h=_time_in_trade_hours(closed),
        r_distribution=_r_buckets(rs),
    )


# ---------------------------------------------------------------------------
@dataclass
class StabilityReport:
    by_symbol: dict[str, CoreMetrics] = field(default_factory=dict)
    by_session: dict[str, CoreMetrics] = field(default_factory=dict)
    by_setup_subtype: dict[str, CoreMetrics] = field(default_factory=dict)
    by_htf_bias: dict[str, CoreMetrics] = field(default_factory=dict)
    halves: dict[str, CoreMetrics] = field(default_factory=dict)   # first_half / second_half
    rolling_R: list[dict] = field(default_factory=list)            # [{idx,total_R}, ...]
    answers: dict[str, str] = field(default_factory=dict)


def _slice_by(trades: list[dict], field_name: str) -> dict[str, CoreMetrics]:
    by: dict[str, list[dict]] = {}
    for t in trades:
        k = str(t.get(field_name) or "?")
        by.setdefault(k, []).append(t)
    return {k: compute_metrics(v) for k, v in by.items()}


def _halves(trades: list[dict]) -> dict[str, CoreMetrics]:
    closed = _closed(trades)
    if len(closed) < 4:
        return {}
    mid = len(closed) // 2
    return {
        "first_half": compute_metrics(closed[:mid]),
        "second_half": compute_metrics(closed[mid:]),
    }


def _rolling_total(trades: list[dict]) -> list[dict]:
    closed = _closed(trades)
    out = []
    total = 0.0
    for i, t in enumerate(closed):
        total += float(t["r_realised"])
        out.append({"idx": i, "total_R": total})
    return out


def evaluate_stability(trades: list[dict]) -> StabilityReport:
    sym = _slice_by(trades, "symbol")
    ses = _slice_by(trades, "session")
    sub = _slice_by(trades, "setup_subtype")
    bias = _slice_by(trades, "htf_bias")
    halves = _halves(trades)
    rolling = _rolling_total(trades)

    # --- adversarial verdicts (string answers, evidence-based) ---
    overall = compute_metrics(trades)
    answers: dict[str, str] = {}

    answers["edge_exists"] = (
        f"YES (n={overall.n}, expectancy {overall.expectancy_R:+.2f}R, PF {overall.profit_factor:.2f})"
        if overall.n >= 30 and overall.expectancy_R > 0.1 and overall.profit_factor > 1.2
        else f"INSUFFICIENT EVIDENCE (n={overall.n}, expectancy {overall.expectancy_R:+.2f}R)"
    )

    if halves:
        f, s = halves["first_half"], halves["second_half"]
        gap = abs(f.expectancy_R - s.expectancy_R)
        answers["edge_stable"] = (
            f"YES (1st-half exp {f.expectancy_R:+.2f}R, 2nd-half {s.expectancy_R:+.2f}R, gap {gap:.2f}R)"
            if gap < 0.3 and f.expectancy_R > 0 and s.expectancy_R > 0
            else f"NO (1st={f.expectancy_R:+.2f}R vs 2nd={s.expectancy_R:+.2f}R, gap {gap:.2f}R)"
        )
    else:
        answers["edge_stable"] = "INSUFFICIENT EVIDENCE (need ≥4 closed trades to split)"

    # Tradeable = honest fill rate + slippage measured
    n_attempts = len([t for t in trades if t.get("outcome") in ("submitted", "filled", "target", "stop", "rejected", "failed")])
    n_closed = overall.n
    fill_rate = (n_closed / n_attempts * 100) if n_attempts else 0
    slips = [abs(float(t.get("slippage_pts") or 0)) for t in trades if t.get("slippage_pts") is not None]
    n_with_slip = len([s for s in slips if s > 0])
    answers["edge_tradeable"] = (
        f"YES (fill rate {fill_rate:.0f}%, {n_with_slip} trades with measured slippage)"
        if fill_rate >= 50 and n_with_slip >= 10
        else f"INSUFFICIENT EVIDENCE (fill rate {fill_rate:.0f}%, only {n_with_slip} measured slippage rows)"
    )

    positive_syms = [k for k, m in sym.items() if m.n >= 5 and m.expectancy_R > 0]
    answers["robust_across_symbols"] = (
        f"YES ({len(positive_syms)} of {len(sym)} symbols positive on ≥5 trades)"
        if len(positive_syms) >= 2
        else f"NO ({len(positive_syms)} of {len(sym)} symbols positive on ≥5 trades)"
    )

    positive_sess = [k for k, m in ses.items() if m.n >= 5 and m.expectancy_R > 0]
    answers["robust_across_sessions"] = (
        f"YES ({len(positive_sess)} of {len(ses)} sessions positive on ≥5 trades)"
        if len(positive_sess) >= 2
        else f"NO ({len(positive_sess)} of {len(ses)} sessions positive on ≥5 trades)"
    )

    # Regime robustness — placeholder until we tag trades with regime
    answers["robust_across_regimes"] = (
        "REQUIRES regime tagging on trades (todo). Use halves test above as proxy."
    )

    return StabilityReport(
        by_symbol=sym, by_session=ses, by_setup_subtype=sub,
        by_htf_bias=bias, halves=halves, rolling_R=rolling, answers=answers,
    )
