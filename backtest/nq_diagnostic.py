"""Adversarial diagnostic — why does NQ produce only 4 closed trades?

Walks every setup through the funnel and counts attrition. No filter
loosening — explanation only. Verdict at the end:

    INSUFFICIENT DATA   — sample size too low to evaluate
    VALID SAMPLE        — strategy genuinely fires this rarely
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.execution_model import NORMAL
from backtest.simulator import simulate
from backtest.tier1_montecarlo import MICRO_MAP
from data.loader import load_bars
from signals.htf_bias import compute_bias_series, htf_timeframe_for
from signals.setup import find_setups

console = Console()
log = logging.getLogger("nq_diag")


def run_funnel(sym: str, timeframe: str, days: int, source: str,
               equity: float, risk_pct: float) -> dict:
    df = load_bars(sym, timeframe, days=days, source=source)
    if df.empty:
        return {"error": "no data"}
    df_htf = load_bars(sym, htf_timeframe_for(timeframe), days=days, source=source)
    bias = compute_bias_series(df, df_htf) if not df_htf.empty else None
    setups = find_setups(df, htf_bias_series=bias)

    # Hand-categorise pre-simulator funnel
    bull = sum(1 for s in setups if s.direction == "bull")
    bear = len(setups) - bull
    with_bias_align = sum(1 for s in setups if s.bias == s.direction)

    # Session distribution (uses utils.time_utils.current_session)
    from utils.time_utils import current_session
    sess = Counter()
    for s in setups:
        try:
            sess[current_session(s.timestamp) or "NONE"] += 1
        except Exception:
            sess["NONE"] += 1

    # RR distribution
    rr = [s.rr for s in setups]

    # Run simulator with NORMAL profile, several seeds, look at outcome funnel
    funnels = []
    for seed in (42, 1, 2, 7, 99):
        sim = simulate(df=df, setups=setups,
                       starting_equity=equity,
                       instrument_symbol=MICRO_MAP.get(sym, sym),
                       risk_pct=risk_pct, min_rr=1.0,
                       execution_profile=NORMAL, random_seed=seed)
        outcomes = Counter(t.outcome for t in sim.trades)
        skip_reasons = Counter(t.skip_reason.split(":")[0].strip()
                               for t in sim.trades if t.outcome == "skipped")
        funnels.append({
            "seed": seed, "n_trades_in_sim": len(sim.trades),
            "outcomes": dict(outcomes), "skip_reasons": dict(skip_reasons),
            "limit_attempts": sim.stats["limit_attempts"],
            "limit_filled_full": sim.stats["limit_filled_full"],
            "limit_filled_partial": sim.stats["limit_filled_partial"],
            "limit_missed": sim.stats["limit_missed"],
            "n_filled": sim.stats["n_filled"],
        })

    return {
        "symbol": sym,
        "n_bars": len(df),
        "n_setups": len(setups),
        "bull": bull, "bear": bear,
        "with_htf_bias_alignment": with_bias_align,
        "sessions": dict(sess),
        "rr_min": float(min(rr)) if rr else 0,
        "rr_median": float(pd.Series(rr).median()) if rr else 0,
        "rr_max": float(max(rr)) if rr else 0,
        "funnels": funnels,
    }


def render(res: dict):
    sym = res["symbol"]
    console.rule(f"[bold]{sym} — sample-validity funnel[/bold]")

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold cyan"); summary.add_column()
    summary.add_row("bars (1h, 180d)", str(res["n_bars"]))
    summary.add_row("setups detected", str(res["n_setups"]))
    summary.add_row("bull / bear", f"{res['bull']} / {res['bear']}")
    summary.add_row("HTF bias-aligned", str(res["with_htf_bias_alignment"]))
    summary.add_row("session split", ", ".join(f"{k}={v}" for k, v in res["sessions"].items()))
    summary.add_row("RR (min / median / max)",
                    f"{res['rr_min']:.2f} / {res['rr_median']:.2f} / {res['rr_max']:.2f}")
    console.print(Panel(summary, title="Pre-simulator", border_style="cyan", title_align="left"))

    # Funnel across seeds
    tbl = Table(title="Simulator funnel — outcomes by seed", header_style="bold")
    for col in ("seed", "trade rows", "target", "stop", "voided",
                "timeout", "skipped", "limit attempts", "filled full",
                "filled partial", "missed"):
        tbl.add_column(col, justify=("right"))
    for f in res["funnels"]:
        o = f["outcomes"]
        tbl.add_row(
            str(f["seed"]), str(f["n_trades_in_sim"]),
            str(o.get("target", 0)), str(o.get("stop", 0)),
            str(o.get("voided_before_entry", 0)),
            str(o.get("timeout_unfilled", 0)),
            str(o.get("skipped", 0)),
            str(f["limit_attempts"]),
            str(f["limit_filled_full"]),
            str(f["limit_filled_partial"]),
            str(f["limit_missed"]),
        )
    console.print(tbl)

    # Skip reasons (averaged across seeds — just show seed 42)
    f0 = res["funnels"][0]
    if f0["skip_reasons"]:
        sr = Table.grid(padding=(0, 2))
        sr.add_column(style="bold"); sr.add_column()
        for k, v in f0["skip_reasons"].items():
            sr.add_row(k, str(v))
        console.print(Panel(sr, title=f"seed=42 — skip reasons",
                            border_style="yellow", title_align="left"))


def verdict(res: dict) -> str:
    """Brief's strict rule: NQ cannot be WATCHLIST unless sample is sufficient."""
    closed = []
    for f in res["funnels"]:
        closed.append(f["outcomes"].get("target", 0) + f["outcomes"].get("stop", 0))
    median_closed = sorted(closed)[len(closed) // 2]
    n_setups = res["n_setups"]
    voided = res["funnels"][0]["outcomes"].get("voided_before_entry", 0)
    missed = res["funnels"][0]["limit_missed"]
    if median_closed >= 20:
        return f"VALID SAMPLE — median {median_closed} closed across seeds"
    return (f"INSUFFICIENT DATA — median {median_closed} closed (< 20). "
            f"Attrition: {n_setups} setups → {voided} voided pre-fill, "
            f"{missed} limit fills missed, {median_closed} closed. "
            f"Verdict per the brief: cannot be promoted off INSUFFICIENT_DATA "
            f"without higher-frequency data or longer history.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="NQ,ES,CL")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--equity", type=float, default=50_000)
    parser.add_argument("--risk-pct", type=float, default=0.005)
    parser.add_argument("--source", default="yfinance")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    verdicts = {}
    for sym in syms:
        res = run_funnel(sym, args.timeframe, args.days, args.source,
                         args.equity, args.risk_pct)
        if "error" in res:
            console.print(f"[red]{sym}: {res['error']}[/red]")
            continue
        render(res)
        v = verdict(res)
        verdicts[sym] = v
        console.print(Panel(v, title=f"{sym} verdict",
                            border_style=("red" if v.startswith("INSUFFICIENT") else "green"),
                            title_align="left"))

    if len(verdicts) > 1:
        console.print()
        console.rule("[bold]Per-symbol sample-validity verdicts[/bold]")
        for sym, v in verdicts.items():
            color = "red" if v.startswith("INSUFFICIENT") else "green"
            console.print(f"  [{color}]{sym}: {v.split(' — ')[0]}[/{color}]")


if __name__ == "__main__":
    main()
