"""Crypto generalization falsification test.

For each of BTC/ETH/SOL/BNB on Binance 2-year 1h data, run the strategy
twice:

  Pass A: with ICT session filter active (only setups during LONDON /
          NY_AM killzones are accepted).
  Pass B: without session filter (24/7 — all setups eligible).

Compare expectancy and R distribution. Interpretation:

  - If A and B both positive with similar expectancy → killzones are
    informational only, edge is structural.
  - If A >> B → killzones encode real edge (timing matters).
  - If B >> A → killzones are destroying edge (likely killzones are
    a futures-market-hours artifact, not structural).
  - If both negative → no edge on crypto. Either the futures result was
    a regime/microstructure artifact, or sweep→CHoCH→FVG doesn't
    generalize.

The report tells you which hypothesis the data supports — it does NOT
tell you the strategy "works". One run on one window with arbitrary
parameters proves nothing definitively.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
from backtest.simulator import simulate
from data.loader import load_bars
from signals.htf_bias import compute_bias_series, htf_timeframe_for
from signals.setup import find_setups
from utils.time_utils import current_session

console = Console()

CRYPTO_PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
ICT_KILLZONES = {"LONDON", "NY_AM"}


def _filter_by_session(setups: list, keep_killzones_only: bool) -> tuple[list, int]:
    """Return (kept, dropped). If keep_killzones_only is False, no-op."""
    if not keep_killzones_only:
        return setups, 0
    kept, dropped = [], 0
    for s in setups:
        try:
            sess = current_session(s.timestamp)
        except Exception:
            sess = None
        if sess in ICT_KILLZONES:
            kept.append(s)
        else:
            dropped += 1
    return kept, dropped


def _run_pass(symbol: str, df: pd.DataFrame, htf_bias_series,
              keep_killzones_only: bool, equity: float,
              risk_pct: float, sim_symbol: str) -> dict:
    setups = find_setups(df, htf_bias_series=htf_bias_series)
    kept, dropped = _filter_by_session(setups, keep_killzones_only)
    sim = simulate(
        df=df, setups=kept,
        starting_equity=equity,
        instrument_symbol=sim_symbol,
        risk_pct=risk_pct, min_rr=1.0,
    )
    return {
        "n_setups_total": len(setups),
        "n_setups_kept": len(kept),
        "n_dropped_by_session": dropped,
        "n_filled": sim.stats["n_filled"],
        "wins": sim.stats["n_wins"],
        "losses": sim.stats["n_losses"],
        "win_rate_pct": sim.stats["win_rate_pct"],
        "avg_R": sim.stats["avg_R"],
        "expectancy_R": sim.stats["expectancy_R"],
        "total_pnl_usd": sim.stats["total_pnl_usd"],
        "return_pct": sim.stats["return_pct"],
        "max_drawdown_pct": sim.stats["max_drawdown_pct"],
        "trades": sim.trades,
    }


def main():
    parser = argparse.ArgumentParser(description="Crypto generalization falsification")
    parser.add_argument("--equity", type=float, default=50_000.0)
    parser.add_argument("--risk-pct", type=float, default=0.005)
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--symbols", default=",".join(CRYPTO_PAIRS))
    parser.add_argument("--timeframe", default="1h",
                        help="LTF timeframe — must exist in ~/.ict-bot/historical/")
    parser.add_argument("--htf", default="1d", help="HTF timeframe for bias")
    args = parser.parse_args()

    pairs = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    rows: list[dict] = []
    for sym in pairs:
        df = load_bars(sym, args.timeframe, days=args.days, source="local")
        if df.empty:
            console.print(f"[red]No data for {sym} {args.timeframe} — skip[/red]")
            continue
        df_htf = load_bars(sym, args.htf, days=args.days, source="local")
        htf_bias = None
        if not df_htf.empty:
            htf_bias = compute_bias_series(df, df_htf)

        console.print(f"[bold cyan]{sym}[/bold cyan]   {len(df)} bars · "
                      f"{df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}")

        pass_with = _run_pass(sym, df, htf_bias, True,
                              args.equity, args.risk_pct, sym)
        pass_without = _run_pass(sym, df, htf_bias, False,
                                 args.equity, args.risk_pct, sym)

        rows.append({"symbol": sym, "pass": "WITH session filter", **pass_with})
        rows.append({"symbol": sym, "pass": "WITHOUT session filter", **pass_without})

    # Render
    tbl = Table(title="Crypto generalization — sweep→CHoCH→FVG · Binance 1h · 2y",
                header_style="bold")
    tbl.add_column("Symbol")
    tbl.add_column("Filter")
    tbl.add_column("Setups", justify="right")
    tbl.add_column("Filled", justify="right")
    tbl.add_column("W / L", justify="right")
    tbl.add_column("Win %", justify="right")
    tbl.add_column("Avg R", justify="right")
    tbl.add_column("P&L", justify="right")
    tbl.add_column("Max DD %", justify="right")
    last_sym = None
    for r in rows:
        avg_color = "green" if r["avg_R"] > 0 else "red"
        pnl_color = "green" if r["total_pnl_usd"] > 0 else "red"
        sym_disp = r["symbol"] if r["symbol"] != last_sym else ""
        last_sym = r["symbol"]
        tbl.add_row(
            sym_disp, r["pass"],
            str(r["n_setups_kept"]),
            str(r["n_filled"]),
            f"[green]{r['wins']}[/green] / [red]{r['losses']}[/red]",
            f"{r['win_rate_pct']:.0f}%" if r["n_filled"] else "—",
            f"[{avg_color}]{r['avg_R']:+.2f}R[/{avg_color}]",
            f"[{pnl_color}]${r['total_pnl_usd']:+,.0f}[/{pnl_color}]",
            f"{abs(r['max_drawdown_pct']):.2f}",
        )
    console.print(tbl)

    # Interpretation panel
    by_pair: dict[str, dict] = {}
    for r in rows:
        by_pair.setdefault(r["symbol"], {})[r["pass"]] = r

    lines = []
    for sym, p in by_pair.items():
        a = p.get("WITH session filter", {})
        b = p.get("WITHOUT session filter", {})
        a_r, b_r = a.get("avg_R", 0), b.get("avg_R", 0)
        if a_r > 0 and b_r > 0 and abs(a_r - b_r) < 0.2:
            v = "[green]edge plausibly structural (both passes positive, similar)[/green]"
        elif a_r > 0 > b_r:
            v = "[yellow]edge appears killzone-conditional[/yellow]"
        elif b_r > 0 > a_r:
            v = "[red]killzone filter destroys edge — likely a futures artifact[/red]"
        elif a_r < 0 and b_r < 0:
            v = "[red]NO crypto edge — futures result questionable[/red]"
        else:
            v = "[yellow]mixed / weak[/yellow]"
        lines.append(f"  {sym}:  with={a_r:+.2f}R  without={b_r:+.2f}R  →  {v}")
    console.print(Panel("\n".join(lines),
                        title="Interpretation per pair",
                        border_style="blue", title_align="left"))

    # Portfolio-level
    a_total = sum(p.get("WITH session filter", {}).get("total_pnl_usd", 0) for p in by_pair.values())
    b_total = sum(p.get("WITHOUT session filter", {}).get("total_pnl_usd", 0) for p in by_pair.values())
    a_avg = sum(p.get("WITH session filter", {}).get("avg_R", 0) * p.get("WITH session filter", {}).get("n_filled", 0)
                for p in by_pair.values())
    a_n = sum(p.get("WITH session filter", {}).get("n_filled", 0) for p in by_pair.values())
    b_avg = sum(p.get("WITHOUT session filter", {}).get("avg_R", 0) * p.get("WITHOUT session filter", {}).get("n_filled", 0)
                for p in by_pair.values())
    b_n = sum(p.get("WITHOUT session filter", {}).get("n_filled", 0) for p in by_pair.values())

    console.print(Panel(
        f"WITH session filter:    n={a_n}  avg R = {(a_avg/a_n if a_n else 0):+.2f}  P&L = ${a_total:+,.0f}\n"
        f"WITHOUT session filter: n={b_n}  avg R = {(b_avg/b_n if b_n else 0):+.2f}  P&L = ${b_total:+,.0f}",
        title="Portfolio aggregate (across 4 pairs)",
        border_style="green" if a_total > 0 and b_total > 0 else "yellow",
        title_align="left",
    ))


if __name__ == "__main__":
    main()
