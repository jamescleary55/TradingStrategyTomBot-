"""Tier-1 futures Monte Carlo execution test.

Pre-computes setups + HTF bias **once per symbol** (the slow step), then
runs the simulator N times per (symbol, profile, seed) — only the RNG
seed varies. With N=200, total work ≈ 1800 simulator runs across 3
symbols × 3 profiles. Runs in roughly a minute.

For each (symbol, profile) report:

- mean / median / std / 5th / 95th percentile expectancy
- P(expectancy > 0R), P(expectancy > +0.25R), P(maxDD > 3R)
- median fill rate, partial%, miss%

Designed to answer one question: **does the NORMAL-profile expectancy
confidence interval cross 0?** If yes → NOT PROVEN.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.execution_model import NORMAL, OPTIMISTIC, PROFILES, PUNITIVE
from backtest.simulator import simulate
from data.loader import load_bars
from signals.htf_bias import compute_bias_series, htf_timeframe_for
from signals.setup import find_setups

console = Console()
log = logging.getLogger("tier1_montecarlo")

MICRO_MAP = {"NQ": "MNQ", "ES": "MES", "CL": "MCL", "GC": "MGC"}
DEFAULT_SYMBOLS = ("NQ", "ES", "CL")
PROFILE_ORDER = ("OPTIMISTIC", "NORMAL", "PUNITIVE")


# ---------------------------------------------------------------------------
@dataclass
class SymbolData:
    symbol: str
    sim_symbol: str
    df: pd.DataFrame
    setups: list
    n_bars: int


def _prepare(symbols, timeframe, days, source) -> dict[str, SymbolData]:
    out = {}
    for sym in symbols:
        sim_sym = MICRO_MAP.get(sym, sym)
        df = load_bars(sym, timeframe, days=days, source=source)
        if df.empty:
            log.error("[%s] NO DATA — will skip", sym)
            continue
        df_htf = load_bars(sym, htf_timeframe_for(timeframe), days=days, source=source)
        htf_bias = compute_bias_series(df, df_htf) if not df_htf.empty else None
        setups = find_setups(df, htf_bias_series=htf_bias)
        log.info("[%s] bars=%d setups=%d", sym, len(df), len(setups))
        out[sym] = SymbolData(symbol=sym, sim_symbol=sim_sym,
                              df=df, setups=setups, n_bars=len(df))
    return out


# ---------------------------------------------------------------------------
def _run_one(sd: SymbolData, profile, seed: int,
             equity: float, risk_pct: float) -> dict:
    sim = simulate(
        df=sd.df, setups=sd.setups,
        starting_equity=equity,
        instrument_symbol=sd.sim_symbol,
        risk_pct=risk_pct, min_rr=1.0,
        execution_profile=profile,
        random_seed=seed,
    )
    s = sim.stats
    closed = [t for t in sim.trades if t.outcome in ("target", "stop")]
    return {
        "expectancy_R": s["expectancy_R"],
        "win_rate_pct": s["win_rate_pct"],
        "max_drawdown_pct": s["max_drawdown_pct"],
        "n_closed": s["n_filled"],
        "limit_fill_rate_pct": s["limit_fill_rate_pct"],
        "limit_partial": s["limit_filled_partial"],
        "limit_missed": s["limit_missed"],
        "avg_slip": s["avg_slippage_pts"],
        "median_slip": s["median_slippage_pts"],
        "biggest_winner_share_pct": s["biggest_winner_share_pct"],
        "profit_factor": _profit_factor(closed),
        "total_pnl_usd": s["total_pnl_usd"],
        "return_pct": s["return_pct"],
    }


def _profit_factor(closed) -> float:
    wins = sum(t.r_multiple for t in closed if t.outcome == "target")
    losses = abs(sum(t.r_multiple for t in closed if t.outcome == "stop"))
    if losses == 0:
        return math.inf if wins > 0 else 0.0
    return wins / losses


# ---------------------------------------------------------------------------
def monte_carlo(sd: SymbolData, profile, n_seeds: int,
                equity: float, risk_pct: float, max_dd_R_cap: float = 3.0) -> dict:
    rows = [_run_one(sd, profile, seed=s, equity=equity, risk_pct=risk_pct)
            for s in range(n_seeds)]
    exps = np.array([r["expectancy_R"] for r in rows])
    dds = np.array([abs(r["max_drawdown_pct"]) / 100 * abs(equity) for r in rows])
    dds_R = dds / max(1.0, equity * risk_pct)        # roughly drawdown in R
    n_closed = np.array([r["n_closed"] for r in rows])
    fill = np.array([r["limit_fill_rate_pct"] for r in rows])
    return {
        "profile": profile.name,
        "n_seeds": n_seeds,
        "exp_mean": float(np.mean(exps)),
        "exp_median": float(np.median(exps)),
        "exp_std": float(np.std(exps)),
        "exp_p5": float(np.percentile(exps, 5)),
        "exp_p95": float(np.percentile(exps, 95)),
        "p_pos": float(np.mean(exps > 0)) * 100,
        "p_above_025": float(np.mean(exps > 0.25)) * 100,
        "p_dd_above_3R": float(np.mean(dds_R > max_dd_R_cap)) * 100,
        "median_n_closed": float(np.median(n_closed)),
        "median_fill_rate_pct": float(np.median(fill)),
        "all_runs": rows,
    }


# ---------------------------------------------------------------------------
def render(symbol_data: dict[str, SymbolData], results: dict[str, dict[str, dict]]):
    # P1-style table: one row per (symbol, profile) at seed=42 (the "headline" run)
    tbl = Table(title="Tier-1 sensitivity (headline run, seed=42)", header_style="bold")
    for col in ("Symbol", "Profile", "Setups", "Closed", "Win %",
                "Avg R", "PF", "Fill %", "Partial%", "Missed%",
                "Avg slip", "Med slip", "Big winner %", "Max DD %"):
        tbl.add_column(col, justify=("left" if col in ("Symbol", "Profile") else "right"))
    last_sym = None
    for sym, sd in symbol_data.items():
        for pname in PROFILE_ORDER:
            profile = PROFILES[pname]
            single = _run_one(sd, profile, seed=42, equity=50_000, risk_pct=0.005)
            r_color = "green" if single["expectancy_R"] > 0 else "red"
            attempts_signal = (single["limit_partial"] + single["limit_missed"]
                               + single["n_closed"])
            partial_pct = (single["limit_partial"] / attempts_signal * 100
                           if attempts_signal else 0)
            missed_pct = (single["limit_missed"] / attempts_signal * 100
                          if attempts_signal else 0)
            pf = single["profit_factor"]
            pf_str = "∞" if pf == math.inf else f"{pf:.2f}"
            sym_show = sym if sym != last_sym else ""
            last_sym = sym
            tbl.add_row(
                sym_show, pname, str(len(sd.setups)), str(single["n_closed"]),
                f"{single['win_rate_pct']:.0f}%",
                f"[{r_color}]{single['expectancy_R']:+.2f}R[/{r_color}]",
                pf_str,
                f"{single['limit_fill_rate_pct']:.0f}%",
                f"{partial_pct:.0f}%",
                f"{missed_pct:.0f}%",
                f"{single['avg_slip']:.2f}",
                f"{single['median_slip']:.2f}",
                f"{single['biggest_winner_share_pct']:.0f}%",
                f"{abs(single['max_drawdown_pct']):.2f}",
            )
    console.print(tbl)

    # P2 — Monte Carlo summary
    tbl2 = Table(title="Monte Carlo over execution RNG", header_style="bold")
    for col in ("Symbol", "Profile", "N seeds", "Mean R", "Median R", "Std R",
                "5%ile", "95%ile", "P(>0R)", "P(>+0.25R)", "P(DD>3R)",
                "Med fill%", "Med closed"):
        tbl2.add_column(col, justify=("left" if col in ("Symbol", "Profile") else "right"))
    last_sym = None
    for sym, mc in results.items():
        for pname in PROFILE_ORDER:
            m = mc.get(pname)
            if m is None:
                continue
            sym_show = sym if sym != last_sym else ""
            last_sym = sym
            mean_color = "green" if m["exp_mean"] > 0 else "red"
            p5_color = "green" if m["exp_p5"] > 0 else "red"
            tbl2.add_row(
                sym_show, pname, str(m["n_seeds"]),
                f"[{mean_color}]{m['exp_mean']:+.2f}R[/{mean_color}]",
                f"{m['exp_median']:+.2f}R",
                f"{m['exp_std']:.2f}",
                f"[{p5_color}]{m['exp_p5']:+.2f}R[/{p5_color}]",
                f"{m['exp_p95']:+.2f}R",
                f"{m['p_pos']:.0f}%",
                f"{m['p_above_025']:.0f}%",
                f"{m['p_dd_above_3R']:.0f}%",
                f"{m['median_fill_rate_pct']:.0f}%",
                f"{m['median_n_closed']:.0f}",
            )
    console.print(tbl2)


# ---------------------------------------------------------------------------
def verdicts(results: dict[str, dict[str, dict]]) -> list[str]:
    """Apply the brief's strict rules per symbol."""
    lines = []
    for sym, mc in results.items():
        normal = mc.get("NORMAL")
        punitive = mc.get("PUNITIVE")
        if normal is None:
            lines.append(f"  {sym}: NO DATA — DISABLE")
            continue
        mean = normal["exp_mean"]
        p_pos = normal["p_pos"]
        p5 = normal["exp_p5"]
        n_closed_med = normal["median_n_closed"]
        biggest = normal["all_runs"][0].get("biggest_winner_share_pct", 0)

        # Decision rules from the brief
        if (mean > 0.25 and p_pos >= 70 and n_closed_med >= 20
                and biggest <= 25
                and (punitive is None or punitive["exp_mean"] > -0.5)):
            verdict = "[green]CONTINUE FORWARD TEST[/green]"
        elif 0 < mean <= 0.25 or n_closed_med < 20 or (punitive and punitive["exp_p5"] < -1.0):
            verdict = "[yellow]WATCHLIST[/yellow]"
        elif mean <= 0 or (punitive and punitive["exp_mean"] < -0.3):
            verdict = "[red]DISABLE[/red]"
        else:
            verdict = "[yellow]WATCHLIST[/yellow]"
        lines.append(
            f"  {sym:<3}  NORMAL mean={mean:+.2f}R  P(>0)={p_pos:.0f}%  "
            f"5%ile={p5:+.2f}R  median closed={n_closed_med:.0f}  →  {verdict}"
        )
    return lines


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Tier-1 Monte Carlo execution test")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--equity", type=float, default=50_000)
    parser.add_argument("--risk-pct", type=float, default=0.005)
    parser.add_argument("--source", default="yfinance")
    parser.add_argument("--seeds", type=int, default=200)
    parser.add_argument("--out", default=None,
                        help="Optional JSON dump of all per-seed results")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    sd = _prepare(syms, args.timeframe, args.days, args.source)
    if not sd:
        console.print("[red]No data loaded.[/red]"); sys.exit(1)

    if "NQ" not in sd:
        console.print("[bold red]NQ missing — TIER 1 INCOMPLETE.[/bold red]")

    results: dict[str, dict[str, dict]] = {}
    for sym, data in sd.items():
        results[sym] = {}
        for pname in PROFILE_ORDER:
            profile = PROFILES[pname]
            log.info("[%s/%s] running %d seeds…", sym, pname, args.seeds)
            results[sym][pname] = monte_carlo(
                data, profile, args.seeds,
                equity=args.equity, risk_pct=args.risk_pct,
            )

    render(sd, results)

    console.print()
    console.print(Panel("\n".join(verdicts(results)),
                        title="Per-symbol verdict (strict rules)",
                        border_style="blue", title_align="left"))

    if args.out:
        # Strip the raw all_runs lists for compactness
        dump = {sym: {p: {k: v for k, v in r.items() if k != "all_runs"}
                      for p, r in profs.items()}
                for sym, profs in results.items()}
        Path(args.out).write_text(json.dumps(dump, indent=2, default=str))
        console.print(f"\n[dim]Wrote {args.out}[/dim]")

    # Quick truth check for the NORMAL-confidence-interval guardrail
    not_proven = []
    for sym, mc in results.items():
        n = mc.get("NORMAL")
        if n and n["exp_p5"] < 0:
            not_proven.append(f"{sym} (5%ile {n['exp_p5']:+.2f}R)")
    if not_proven:
        console.print(Panel(
            "NORMAL-profile expectancy CI crosses 0 on: " + ", ".join(not_proven)
            + "\n→ Strategy NOT PROVEN on these symbols.",
            title="Guardrail", border_style="red", title_align="left",
        ))


if __name__ == "__main__":
    main()
