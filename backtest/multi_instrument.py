"""Cross-asset edge validator.

Runs the full walk-forward pipeline (IS sweep → OOS apply, no re-tuning) on
each of NQ / ES / GC / CL using yfinance data, simulating execution on the
micro-contract for each so a $50k account can size in. Aggregates per-symbol
OOS expectancy + win rate + P&L into a single comparison table.

The point: **real edges generalize across uncorrelated assets**. If the
strategy prints +1R on NQ but ‑0.5R on Gold and Crude, you've curve-fit to
NQ's specific microstructure. If all four show positive OOS expectancy, the
edge is more likely structural.

Usage:
    python -m backtest.multi_instrument --days 180 --equity 50000
"""
from __future__ import annotations

import argparse
import itertools
import logging
import sys
from datetime import timedelta
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
from utils.news import filter_setups as filter_setups_news, generate_events

logging.basicConfig(level=logging.WARNING)
console = Console()

# (price-data symbol, sim symbol, label)
DEFAULT_UNIVERSE = [
    ("NQ",  "MNQ", "Nasdaq 100"),
    ("ES",  "MES", "S&P 500"),
    ("GC",  "MGC", "Gold"),
    ("CL",  "MCL", "Crude Oil"),
]

SWEEP_GRID = {
    "SWING_LOOKBACK":            [3, 5, 7],
    "SWEEP_TO_CHOCH_MAX_BARS":   [6, 10, 14],
    "CHOCH_TO_FVG_MAX_BARS":     [4, 6, 8],
    "SETUP_MIN_RR":              [1.0, 1.5, 2.0],
}


def _apply(d: dict):
    for k, v in d.items():
        setattr(cfg, k, v)


def _windows(df: pd.DataFrame, is_days: int, oos_days: int):
    if df.empty:
        return []
    out = []
    cur = df.index[0]
    end = df.index[-1]
    while True:
        is_end = cur + timedelta(days=is_days)
        oos_end = is_end + timedelta(days=oos_days)
        if oos_end > end:
            break
        out.append((cur, is_end, is_end, oos_end))
        cur = cur + timedelta(days=oos_days)
    return out


def _run_one(df, htf_bias, news_events, equity, sim_symbol,
             risk_pct, require_htf_alignment, news_filter, news_pad):
    setups = find_setups(df, htf_bias_series=htf_bias,
                         require_htf_alignment=require_htf_alignment)
    if news_filter and news_events and setups:
        kept, _ = filter_setups_news(setups, news_events,
                                     minutes_before=news_pad, minutes_after=news_pad)
        setups = kept
    return simulate(df=df, setups=setups, starting_equity=equity,
                    instrument_symbol=sim_symbol, risk_pct=risk_pct, min_rr=1.0)


def _sweep_in_sample(df_is, htf_is, news_events, equity, sim_symbol,
                     risk_pct, require_htf_alignment, news_filter, news_pad,
                     min_filled: int):
    keys = list(SWEEP_GRID.keys())
    best = None
    for combo in itertools.product(*SWEEP_GRID.values()):
        c = dict(zip(keys, combo))
        _apply(c)
        sim = _run_one(df_is, htf_is, news_events, equity, sim_symbol,
                       risk_pct, require_htf_alignment, news_filter, news_pad)
        st = sim.stats
        if st["n_filled"] < min_filled:
            continue
        if best is None or st["expectancy_R"] > best[1]["expectancy_R"]:
            best = (c.copy(), dict(st))
    return best


def _walk_forward_for_symbol(symbol, sim_symbol, label, args, news_events):
    """Run all WF windows for a single symbol and return aggregate OOS results."""
    df = load_bars(symbol, args.timeframe, days=args.days, source=args.source)
    if df.empty:
        return None
    htf_bias = None
    if not args.no_htf:
        htf_tf = args.htf or htf_timeframe_for(args.timeframe)
        if htf_tf != args.timeframe:
            df_htf = load_bars(symbol, htf_tf, days=args.days, source=args.source)
            if not df_htf.empty:
                htf_bias = compute_bias_series(df, df_htf)

    windows = _windows(df, args.is_days, args.oos_days)
    if not windows:
        console.print(f"[yellow]Skip {symbol}: insufficient data for windowing.[/yellow]")
        return None

    running_equity = args.equity
    oos_trades_all: list = []
    is_exps: list[float] = []

    for is_s, is_e, oos_s, oos_e in windows:
        is_df = df[(df.index >= is_s) & (df.index < is_e)]
        oos_df = df[(df.index >= oos_s) & (df.index < oos_e)]
        htf_is = htf_bias.reindex(is_df.index) if htf_bias is not None else None
        htf_oos = htf_bias.reindex(oos_df.index) if htf_bias is not None else None

        best = _sweep_in_sample(
            is_df, htf_is, news_events, args.equity, sim_symbol,
            args.risk_pct, args.htf_strict, args.news_filter, args.news_pad,
            min_filled=args.min_filled,
        )
        if best is None:
            continue
        best_cfg, is_stats = best
        is_exps.append(is_stats["expectancy_R"])
        _apply(best_cfg)

        oos_sim = _run_one(oos_df, htf_oos, news_events, running_equity, sim_symbol,
                           args.risk_pct, args.htf_strict, args.news_filter, args.news_pad)
        running_equity = oos_sim.stats["ending_equity"]
        oos_trades_all.extend(oos_sim.trades)

    filled = [t for t in oos_trades_all if t.outcome in ("target", "stop")]
    wins = [t for t in filled if t.outcome == "target"]
    losses = [t for t in filled if t.outcome == "stop"]
    avg_r = (sum(t.r_multiple for t in filled) / len(filled)) if filled else 0.0
    win_rate = (len(wins) / len(filled) * 100) if filled else 0.0
    total_pnl = sum(t.pnl_usd for t in filled)
    return_pct = (running_equity / args.equity - 1) * 100
    is_mean = (sum(is_exps) / len(is_exps)) if is_exps else 0.0

    return {
        "symbol": symbol,
        "sim_symbol": sim_symbol,
        "label": label,
        "windows": len(windows),
        "is_mean_expectancy": is_mean,
        "oos_filled": len(filled),
        "oos_wins": len(wins),
        "oos_losses": len(losses),
        "oos_win_rate": win_rate,
        "oos_avg_R": avg_r,
        "oos_total_pnl": total_pnl,
        "oos_return_pct": return_pct,
        "ending_equity": running_equity,
        "edge_retained": (avg_r / is_mean * 100) if is_mean > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Cross-asset edge validator")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--equity", type=float, default=50_000.0)
    parser.add_argument("--source", default="auto",
                        choices=["auto", "tradovate", "yfinance", "synthetic"])
    parser.add_argument("--is-days", type=int, default=60)
    parser.add_argument("--oos-days", type=int, default=30)
    parser.add_argument("--risk-pct", type=float, default=0.015)
    parser.add_argument("--entry-mode", default="closer_edge",
                        choices=["mid", "closer_edge", "farther_edge"])
    parser.add_argument("--max-stop-pts", type=float, default=0.0)
    parser.add_argument("--htf", default=None)
    parser.add_argument("--no-htf", action="store_true")
    parser.add_argument("--htf-strict", action="store_true")
    parser.add_argument("--news-filter", action="store_true")
    parser.add_argument("--news-pad", type=int, default=30)
    parser.add_argument("--min-filled", type=int, default=5)
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated subset, e.g. 'NQ,ES'. Default = NQ,ES,GC,CL.")
    args = parser.parse_args()

    cfg.SETUP_ENTRY_MODE = args.entry_mode
    cfg.SETUP_MAX_STOP_POINTS = args.max_stop_pts

    universe = DEFAULT_UNIVERSE
    if args.symbols:
        wanted = {s.strip().upper() for s in args.symbols.split(",")}
        universe = [u for u in DEFAULT_UNIVERSE if u[0] in wanted]

    console.rule(f"[bold]Multi-instrument · {args.timeframe} · "
                 f"{args.days}d · IS {args.is_days}d / OOS {args.oos_days}d · "
                 f"risk {args.risk_pct*100:.2f}%[/bold]")

    news_events = []
    if args.news_filter:
        # We don't know the data span yet so we'll regenerate per symbol below
        pass

    rows = []
    for symbol, sim_symbol, label in universe:
        console.print(f"\n[bold cyan]→ {label}  ({symbol} → {sim_symbol})[/bold cyan]")
        if args.news_filter:
            df_probe = load_bars(symbol, args.timeframe, days=args.days, source=args.source)
            news_events = generate_events(
                df_probe.index[0].to_pydatetime().replace(tzinfo=None),
                df_probe.index[-1].to_pydatetime().replace(tzinfo=None),
            ) if not df_probe.empty else []
        result = _walk_forward_for_symbol(symbol, sim_symbol, label, args, news_events)
        if result is None:
            continue
        rows.append(result)
        console.print(
            f"  windows={result['windows']}  "
            f"OOS filled={result['oos_filled']}  "
            f"win%={result['oos_win_rate']:.0f}%  "
            f"avgR={result['oos_avg_R']:+.2f}  "
            f"P&L=${result['oos_total_pnl']:+,.0f}  "
            f"retained={result['edge_retained']:.0f}%"
        )

    # ---- Comparison table ---------------------------------------------
    console.print()
    tbl = Table(title="Cross-asset OOS results (honest)", header_style="bold")
    tbl.add_column("Instrument")
    tbl.add_column("Sim", style="dim")
    tbl.add_column("Windows", justify="right")
    tbl.add_column("OOS filled", justify="right")
    tbl.add_column("Wins / Losses", justify="right")
    tbl.add_column("Win %", justify="right")
    tbl.add_column("Avg R", justify="right")
    tbl.add_column("P&L", justify="right")
    tbl.add_column("Return", justify="right")
    tbl.add_column("Edge retained", justify="right")

    positive_assets = 0
    aggregate_r: list[float] = []
    aggregate_filled = 0
    aggregate_wins = 0
    aggregate_losses = 0
    aggregate_pnl = 0.0

    for r in rows:
        avg_color = "green" if r["oos_avg_R"] > 0 else "red"
        pnl_color = "green" if r["oos_total_pnl"] > 0 else "red"
        if r["edge_retained"] >= 60 and r["oos_avg_R"] > 0:
            edge_color = "green"
        elif r["edge_retained"] > 0 and r["oos_avg_R"] > 0:
            edge_color = "yellow"
        else:
            edge_color = "red"
        if r["oos_avg_R"] > 0:
            positive_assets += 1
        for _ in range(r["oos_filled"]):
            aggregate_r.append(r["oos_avg_R"])
        aggregate_filled += r["oos_filled"]
        aggregate_wins += r["oos_wins"]
        aggregate_losses += r["oos_losses"]
        aggregate_pnl += r["oos_total_pnl"]

        tbl.add_row(
            f"{r['label']} ({r['symbol']})",
            r["sim_symbol"],
            str(r["windows"]),
            str(r["oos_filled"]),
            f"[green]{r['oos_wins']}[/green] / [red]{r['oos_losses']}[/red]",
            f"{r['oos_win_rate']:.0f}%" if r["oos_filled"] > 0 else "—",
            f"[{avg_color}]{r['oos_avg_R']:+.2f}R[/{avg_color}]",
            f"[{pnl_color}]${r['oos_total_pnl']:+,.0f}[/{pnl_color}]",
            f"[{pnl_color}]{r['oos_return_pct']:+.2f}%[/{pnl_color}]",
            f"[{edge_color}]{r['edge_retained']:.0f}%[/{edge_color}]",
        )
    console.print(tbl)

    # ---- Aggregate panel + verdict -------------------------------------
    portfolio_r = (sum(aggregate_r) / len(aggregate_r)) if aggregate_r else 0
    portfolio_win = (aggregate_wins / aggregate_filled * 100) if aggregate_filled else 0
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold")
    summary.add_column(justify="right")
    summary.add_row("Assets tested", str(len(rows)))
    summary.add_row("Assets with positive OOS expectancy", f"{positive_assets} / {len(rows)}")
    summary.add_row("Total OOS filled trades", str(aggregate_filled))
    summary.add_row("Total wins / losses", f"{aggregate_wins} / {aggregate_losses}")
    summary.add_row("Portfolio win rate", f"{portfolio_win:.1f}%")
    summary.add_row("Portfolio avg R", f"{portfolio_r:+.2f}R")
    pnl_color = "green" if aggregate_pnl > 0 else "red"
    summary.add_row("Total OOS P&L (sum across assets)",
                    f"[{pnl_color}]${aggregate_pnl:+,.2f}[/{pnl_color}]")
    console.print(Panel(summary, title="Portfolio aggregate",
                        border_style="green" if positive_assets >= 3 else "yellow",
                        title_align="left"))

    # Verdict
    if positive_assets == len(rows) and len(rows) >= 3:
        console.print("[green]✓  Edge generalizes: every asset produced positive OOS expectancy.[/green]")
        console.print("[green]   Strong signal of structural edge. Move to paper trading.[/green]")
    elif positive_assets >= max(2, len(rows) // 2 + 1):
        console.print("[yellow]△  Partial generalization: edge holds on some assets but not all.[/yellow]")
        console.print("[yellow]   Consider trading only the asset(s) where OOS edge is robust.[/yellow]")
    else:
        console.print("[red]✗  Edge doesn't generalize. The NQ-only result was likely a one-asset artifact.[/red]")


if __name__ == "__main__":
    main()
