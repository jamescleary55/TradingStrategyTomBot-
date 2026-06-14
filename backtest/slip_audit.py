"""Slippage measurement audit.

Hypothesis tested: the simulator pools two heterogeneous slip
populations — **stop-fill adverse points** (always positive, sized by
ATR) and **limit-fill queue slip** (zero 70% of the time by design).
The headline `median_slippage_pts` is dominated by limit-fill zeros
and conveys "slippage rarely happens" when the truth is "we never
isolated stop slippage in the metric."

Audit method: instrument-free re-run that splits the sample by
exit_type and reports stop / target distributions independently.

Verdict categories:
    REAL_SPARSITY      — both populations show small slip
    SIM_DESIGN_ARTIFACT — pooled median is misleading by construction
    MEASUREMENT_BUG    — samples missing rows or wrong sign
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.execution_model import (
    NORMAL, apply_stop_fill, apply_target_fill, attempt_limit_fill,
)
from backtest.tier1_montecarlo import MICRO_MAP
from config import COMMISSION_PER_CONTRACT_USD, INSTRUMENTS
from data.loader import load_bars
from risk.sizing import plan_trade
from signals.htf_bias import compute_bias_series, htf_timeframe_for
from signals.setup import find_setups

console = Console()
log = logging.getLogger("slip_audit")


def _audit_one(sym: str, timeframe: str, days: int, source: str,
               equity: float, risk_pct: float, seed: int) -> dict:
    """Re-implement the walk-forward and capture slip samples by exit_type."""
    df = load_bars(sym, timeframe, days=days, source=source)
    if df.empty:
        return {"error": "no data"}
    df_htf = load_bars(sym, htf_timeframe_for(timeframe), days=days, source=source)
    bias = compute_bias_series(df, df_htf) if not df_htf.empty else None
    setups = find_setups(df, htf_bias_series=bias)
    instrument = INSTRUMENTS[MICRO_MAP.get(sym, sym)]
    profile = NORMAL
    rng = np.random.default_rng(seed)

    entry_slip = []         # queue-slip on entry fills
    target_slip = []        # queue-slip on target fills
    stop_slip = []          # adverse pts on stop fills
    fill_attempt_outcome = Counter()

    # We use the simulator's logic by-hand but only record slip — no PnL.
    setup_queue = sorted(setups, key=lambda s: s.choch.idx)
    waiting = []
    open_pos: Optional[dict] = None
    next_iter = iter(setup_queue)
    pending = next(next_iter, None)
    timeout_bars = 24

    for i in range(len(df)):
        while pending is not None and pending.choch.idx <= i:
            if open_pos is None:
                plan = plan_trade(
                    equity=equity, entry=pending.entry, stop=pending.stop,
                    target=pending.target, instrument=instrument,
                    risk_pct=risk_pct, min_rr=1.0,
                )
                if plan.approved:
                    waiting.append({"setup": pending, "plan": plan,
                                    "added_idx": i})
            pending = next(next_iter, None)

        bar = df.iloc[i]
        h, l = float(bar["high"]), float(bar["low"])

        # close open
        if open_pos is not None:
            s = open_pos["setup"]
            stop_hit = (l <= s.stop) if s.direction == "bull" else (h >= s.stop)
            target_hit = (h >= s.target) if s.direction == "bull" else (l <= s.target)
            if stop_hit:
                fr = apply_stop_fill(intended_price=s.stop, bar=bar,
                                     direction=s.direction, df=df, idx=i,
                                     instrument=instrument, profile=profile,
                                     news_events=[])
                stop_slip.append(float(fr.slippage_pts))
                fill_attempt_outcome["stop"] += 1
                open_pos = None
            elif target_hit:
                fr = apply_target_fill(intended_price=s.target, bar=bar,
                                       direction=s.direction, df=df, idx=i,
                                       instrument=instrument, profile=profile,
                                       news_events=[], rng=rng)
                if fr.filled:
                    target_slip.append(float(fr.slippage_pts))
                    fill_attempt_outcome["target"] += 1
                    open_pos = None
                else:
                    fill_attempt_outcome["target_missed"] += 1

        # entries
        still = []
        for w in waiting:
            s = w["setup"]
            bars_since = i - s.choch.idx
            voided = (l <= s.stop) if s.direction == "bull" else (h >= s.stop)
            entry_touched = (l <= s.entry <= h)
            if voided and not entry_touched:
                continue
            if entry_touched and open_pos is None:
                fr = attempt_limit_fill(intended_price=s.entry, bar=bar,
                                        direction=s.direction, df=df, idx=i,
                                        instrument=instrument, profile=profile,
                                        news_events=[], rng=rng)
                if fr.filled:
                    entry_slip.append(float(fr.slippage_pts))
                    fill_attempt_outcome["entry"] += 1
                    open_pos = w
                    continue
                else:
                    fill_attempt_outcome["entry_missed"] += 1
            if bars_since >= timeout_bars:
                continue
            still.append(w)
        waiting = still

    # Aggregate distribution stats
    def _dist(arr, label):
        if not arr:
            return {"label": label, "n": 0}
        a = np.array(arr)
        return {
            "label": label, "n": len(a),
            "min": float(a.min()), "p25": float(np.percentile(a, 25)),
            "median": float(np.median(a)),
            "p75": float(np.percentile(a, 75)),
            "p95": float(np.percentile(a, 95)),
            "max": float(a.max()), "mean": float(a.mean()),
            "zero_pct": float((a == 0).mean() * 100),
        }

    pooled = entry_slip + target_slip + stop_slip
    return {
        "symbol": sym,
        "n_setups": len(setups),
        "outcomes": dict(fill_attempt_outcome),
        "entry": _dist(entry_slip, "entry"),
        "target": _dist(target_slip, "target"),
        "stop": _dist(stop_slip, "stop"),
        "pooled": _dist(pooled, "pooled (all)"),
        "entry_raw": entry_slip,
        "target_raw": target_slip,
        "stop_raw": stop_slip,
    }


def render(res: dict):
    sym = res["symbol"]
    console.rule(f"[bold]{sym} — slip distribution by exit type[/bold]")

    tbl = Table(header_style="bold")
    for col in ("Population", "N", "min", "p25", "median", "p75", "p95",
                "max", "mean", "% exactly 0"):
        tbl.add_column(col, justify=("left" if col == "Population" else "right"))
    for key in ("entry", "target", "stop", "pooled"):
        d = res[key]
        if d["n"] == 0:
            tbl.add_row(d["label"], "0", "—", "—", "—", "—", "—", "—", "—", "—")
            continue
        tbl.add_row(
            d["label"], str(d["n"]),
            f"{d['min']:.4f}", f"{d['p25']:.4f}", f"{d['median']:.4f}",
            f"{d['p75']:.4f}", f"{d['p95']:.4f}", f"{d['max']:.4f}",
            f"{d['mean']:.4f}", f"{d['zero_pct']:.0f}%",
        )
    console.print(tbl)

    # Outcomes
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold"); summary.add_column()
    for k, v in res["outcomes"].items():
        summary.add_row(k, str(v))
    console.print(Panel(summary, title=f"{sym} — fill outcomes",
                        border_style="cyan", title_align="left"))


def verdict(res: dict) -> str:
    """Decision rules for the 3 categories."""
    e_zero = res["entry"].get("zero_pct", 0)
    t_zero = res["target"].get("zero_pct", 0)
    s_med = res["stop"].get("median", 0)
    pooled_med = res["pooled"].get("median", 0)
    stop_n = res["stop"].get("n", 0)
    if stop_n < 5:
        return ("INSUFFICIENT_DATA — only %d stop hits to evaluate stop-slip "
                "distribution. Cannot diagnose." % stop_n)

    # SIM_DESIGN_ARTIFACT: pooled median is 0 but stop median is materially > 0
    if pooled_med == 0 and s_med > 0:
        return (f"SIM_DESIGN_ARTIFACT — pooled median = 0 because limit-fill "
                f"slip is zero in {e_zero:.0f}% of entries and {t_zero:.0f}% "
                f"of targets (queue_slip = 1 tick 30% of the time, 0 "
                f"otherwise). Stop-only median is {s_med:.2f} pts. Use "
                f"`median_stop_slippage_pts` as the honest stat; deprecate the "
                f"pooled metric.")
    # REAL_SPARSITY
    if s_med < 0.05 and e_zero > 50:
        return ("REAL_SPARSITY — even stop-only median is essentially zero, "
                "which is plausible only if the bar series rarely had a high-vol "
                "stop event. Sanity-check against ATR.")
    return "MIXED — manual review."


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="ES,CL")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--equity", type=float, default=50_000)
    parser.add_argument("--risk-pct", type=float, default=0.005)
    parser.add_argument("--source", default="yfinance")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    verdicts = {}
    for sym in syms:
        r = _audit_one(sym, args.timeframe, args.days, args.source,
                       args.equity, args.risk_pct, args.seed)
        if "error" in r:
            console.print(f"[red]{sym}: {r['error']}[/red]"); continue
        render(r)
        v = verdict(r)
        verdicts[sym] = v
        color = "red" if v.startswith("SIM_DESIGN") else "yellow"
        console.print(Panel(v, title=f"{sym} verdict",
                            border_style=color, title_align="left"))


if __name__ == "__main__":
    main()
