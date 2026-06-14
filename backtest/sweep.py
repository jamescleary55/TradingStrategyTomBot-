"""Parameter sweep across the ICT setup detector + simulator.

Iterates over every combination of configurable parameters and prints a
sorted comparison table so you can see which configs *might* have an edge
on the loaded data. Sorted by expectancy (R) descending.

Usage:
    python -m backtest.sweep --days 60 --equity 50000
    python -m backtest.sweep --symbol MNQ --metric pnl
"""
from __future__ import annotations

import argparse
import itertools
import logging
import sys
from copy import copy
from pathlib import Path

from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
from backtest.simulator import simulate
from data.loader import load_bars
from signals.setup import find_setups

logging.basicConfig(level=logging.WARNING)
console = Console()


# Default sweep grid — kept small so the run is quick.
DEFAULT_GRID = {
    "SWING_LOOKBACK":            [3, 5, 7],
    "SWEEP_TO_CHOCH_MAX_BARS":   [6, 10, 14],
    "CHOCH_TO_FVG_MAX_BARS":     [4, 6, 8],
    "SETUP_MIN_RR":              [1.0, 1.5, 2.0],
}


def _set_cfg(name: str, value):
    setattr(cfg, name, value)


def main():
    parser = argparse.ArgumentParser(description="ICT parameter sweep")
    parser.add_argument("--symbol", default=cfg.DEFAULT_SYMBOL)
    parser.add_argument("--timeframe", default=cfg.DEFAULT_TIMEFRAME)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--equity", type=float, default=50_000.0)
    parser.add_argument("--sim-symbol", default=None)
    parser.add_argument("--source", default="auto",
                        choices=["auto", "tradovate", "yfinance", "synthetic"])
    parser.add_argument("--metric", default="expectancy",
                        choices=["expectancy", "pnl", "win_rate", "filled"],
                        help="Sort key (default: expectancy)")
    parser.add_argument("--top", type=int, default=15, help="Show top N rows")
    args = parser.parse_args()

    console.rule(f"[bold]Loading {args.symbol} {args.timeframe} × {args.days}d[/bold]")
    df = load_bars(args.symbol, args.timeframe, days=args.days, source=args.source)
    sim_symbol = args.sim_symbol or ("MNQ" if args.symbol == "NQ" else args.symbol)

    combos = list(itertools.product(*DEFAULT_GRID.values()))
    keys = list(DEFAULT_GRID.keys())
    console.print(f"Sweeping [bold]{len(combos)}[/bold] combinations "
                  f"({' × '.join(str(len(v)) for v in DEFAULT_GRID.values())}) "
                  f"on {sim_symbol} @ ${args.equity:,.0f}…\n")

    # Snapshot originals so we can restore at the end
    originals = {k: getattr(cfg, k) for k in keys}

    results = []
    for combo in combos:
        for k, v in zip(keys, combo):
            _set_cfg(k, v)
        setups = find_setups(df)
        sim = simulate(
            df=df, setups=setups,
            starting_equity=args.equity,
            instrument_symbol=sim_symbol,
            risk_pct=cfg.RISK.max_risk_per_trade_pct,
            min_rr=1.0,
        )
        st = sim.stats
        results.append({
            **dict(zip(keys, combo)),
            "setups": st["n_setups"],
            "filled": st["n_filled"],
            "win_rate": st["win_rate_pct"],
            "expectancy": st["expectancy_R"],
            "pnl": st["total_pnl_usd"],
            "max_dd": st["max_drawdown_pct"],
            "return_pct": st["return_pct"],
        })

    # Restore originals
    for k, v in originals.items():
        _set_cfg(k, v)

    sort_key = {"expectancy": "expectancy", "pnl": "pnl",
                "win_rate": "win_rate", "filled": "filled"}[args.metric]
    results.sort(key=lambda r: r[sort_key], reverse=True)

    table = Table(title=f"Top {min(args.top, len(results))} by {args.metric}", header_style="bold")
    table.add_column("Swing\nLB", justify="right")
    table.add_column("Sw→CH\nbars", justify="right")
    table.add_column("CH→FVG\nbars", justify="right")
    table.add_column("Min\nRR", justify="right")
    table.add_column("Setups", justify="right")
    table.add_column("Filled", justify="right")
    table.add_column("Win %", justify="right")
    table.add_column("Avg R", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("Max DD %", justify="right")

    for r in results[:args.top]:
        rr_color = "green" if r["expectancy"] > 0 else ("yellow" if r["expectancy"] == 0 else "red")
        pnl_color = "green" if r["pnl"] > 0 else ("dim" if r["pnl"] == 0 else "red")
        table.add_row(
            str(r["SWING_LOOKBACK"]),
            str(r["SWEEP_TO_CHOCH_MAX_BARS"]),
            str(r["CHOCH_TO_FVG_MAX_BARS"]),
            f"{r['SETUP_MIN_RR']:.1f}",
            str(r["setups"]),
            str(r["filled"]),
            f"{r['win_rate']:.0f}%" if r["filled"] > 0 else "—",
            f"[{rr_color}]{r['expectancy']:+.2f}R[/{rr_color}]",
            f"[{pnl_color}]${r['pnl']:+,.0f}[/{pnl_color}]",
            f"{abs(r['max_dd']):.2f}",
        )

    console.print(table)
    console.print(f"\n[dim]Sample size note:[/dim] {len(combos)} combos × {len(df)} bars. "
                  f"Any combo with <10 filled trades is statistical noise — ignore it.")


if __name__ == "__main__":
    main()
