"""Rolling walk-forward out-of-sample validator.

Workflow per window:

1.  Slice the OHLCV DataFrame into an in-sample (IS) chunk of
    ``--is-days`` days and an out-of-sample (OOS) chunk of ``--oos-days``
    days immediately after it.
2.  Sweep the configurable detector parameters across the IS chunk, pick
    the config with the highest IS expectancy.
3.  Apply that *exact* config to the OOS chunk without re-tuning.
4.  Stitch every OOS chunk's trades together into a single contiguous
    equity curve — this is the *honest* simulation.

We then compare IS expectancy vs OOS expectancy per window and print an
aggregate. If OOS holds up against IS, the edge is plausible. If OOS
collapses, you've been data-mining.

Usage:
    python -m backtest.walkforward --source yfinance --timeframe 1h \
        --days 180 --equity 50000 --is-days 60 --oos-days 30
"""
from __future__ import annotations

import argparse
import itertools
import logging
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Optional

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

# Same grid as backtest.sweep so we're comparing apples to apples
SWEEP_GRID = {
    "SWING_LOOKBACK":            [3, 5, 7],
    "SWEEP_TO_CHOCH_MAX_BARS":   [6, 10, 14],
    "CHOCH_TO_FVG_MAX_BARS":     [4, 6, 8],
    "SETUP_MIN_RR":              [1.0, 1.5, 2.0],
}


@dataclass
class WindowResult:
    idx: int
    is_start: pd.Timestamp
    is_end: pd.Timestamp
    oos_start: pd.Timestamp
    oos_end: pd.Timestamp
    best_config: dict
    is_stats: dict
    oos_stats: dict
    oos_trades: list


# ---------------------------------------------------------------------------
def _apply_config(c: dict):
    for k, v in c.items():
        setattr(cfg, k, v)


def _snapshot_config() -> dict:
    return {k: getattr(cfg, k) for k in SWEEP_GRID.keys()}


def _run(df, htf_bias_series, news_events, equity, sim_symbol,
         risk_pct, require_htf_alignment, news_filter, news_pad):
    setups = find_setups(df, htf_bias_series=htf_bias_series,
                         require_htf_alignment=require_htf_alignment)
    if news_filter and news_events and setups:
        kept, _ = filter_setups_news(setups, news_events,
                                     minutes_before=news_pad,
                                     minutes_after=news_pad)
        setups = kept
    sim = simulate(
        df=df, setups=setups, starting_equity=equity,
        instrument_symbol=sim_symbol, risk_pct=risk_pct, min_rr=1.0,
    )
    return sim, setups


def _sweep_for_best(df_is, htf_bias_is, news_events, equity, sim_symbol,
                    risk_pct, require_htf_alignment, news_filter, news_pad,
                    min_filled: int = 5):
    """Walk the grid on IS data; return (best_config, best_stats, best_sim)."""
    keys = list(SWEEP_GRID.keys())
    best: Optional[tuple[dict, dict]] = None
    for combo in itertools.product(*SWEEP_GRID.values()):
        config_dict = dict(zip(keys, combo))
        _apply_config(config_dict)
        sim, _ = _run(df_is, htf_bias_is, news_events, equity, sim_symbol,
                      risk_pct, require_htf_alignment, news_filter, news_pad)
        st = sim.stats
        # Disqualify undersized samples
        if st["n_filled"] < min_filled:
            continue
        score = st["expectancy_R"]
        if best is None or score > best[1]["expectancy_R"]:
            best = (config_dict.copy(), dict(st))
    if best is None:
        # Fall back to current defaults if nothing passed the filter
        sim, _ = _run(df_is, htf_bias_is, news_events, equity, sim_symbol,
                      risk_pct, require_htf_alignment, news_filter, news_pad)
        return _snapshot_config(), dict(sim.stats), sim
    return best[0], best[1], None


# ---------------------------------------------------------------------------
def _build_windows(df: pd.DataFrame, is_days: int, oos_days: int) -> list[tuple]:
    """Generate (is_start, is_end, oos_start, oos_end) tuples covering df."""
    if df.empty:
        return []
    start = df.index[0]
    end = df.index[-1]
    windows = []
    cur_is_start = start
    while True:
        is_end = cur_is_start + timedelta(days=is_days)
        oos_end = is_end + timedelta(days=oos_days)
        if oos_end > end:
            break
        windows.append((cur_is_start, is_end, is_end, oos_end))
        cur_is_start = cur_is_start + timedelta(days=oos_days)
    return windows


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Walk-forward IS/OOS validator")
    parser.add_argument("--symbol", default=cfg.DEFAULT_SYMBOL)
    parser.add_argument("--timeframe", default=cfg.DEFAULT_TIMEFRAME)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--equity", type=float, default=50_000.0)
    parser.add_argument("--source", default="auto",
                        choices=["auto", "tradovate", "yfinance", "synthetic"])
    parser.add_argument("--sim-symbol", default=None)
    parser.add_argument("--is-days", type=int, default=60, help="In-sample window length (days)")
    parser.add_argument("--oos-days", type=int, default=30, help="Out-of-sample window length (days)")
    parser.add_argument("--risk-pct", type=float, default=0.01)
    parser.add_argument("--entry-mode", default="closer_edge",
                        choices=["mid", "closer_edge", "farther_edge"])
    parser.add_argument("--max-stop-pts", type=float, default=0.0)
    parser.add_argument("--htf", default=None)
    parser.add_argument("--no-htf", action="store_true")
    parser.add_argument("--htf-strict", action="store_true")
    parser.add_argument("--news-filter", action="store_true")
    parser.add_argument("--news-pad", type=int, default=30)
    parser.add_argument("--min-filled", type=int, default=5,
                        help="Reject IS configs with fewer filled trades than this")
    args = parser.parse_args()

    # Static config overrides for the whole run
    cfg.SETUP_ENTRY_MODE = args.entry_mode
    cfg.SETUP_MAX_STOP_POINTS = args.max_stop_pts

    console.rule(f"[bold]Walk-forward · {args.symbol} {args.timeframe} · "
                 f"{args.days}d · IS {args.is_days}d / OOS {args.oos_days}d[/bold]")

    df = load_bars(args.symbol, args.timeframe, days=args.days, source=args.source)
    if df.empty:
        console.print("[red]No bars loaded[/red]")
        sys.exit(1)

    sim_symbol = args.sim_symbol or ("MNQ" if args.symbol == "NQ" else args.symbol)

    # Pre-compute HTF bias series + news events for the whole range (reused per window)
    htf_bias_series = None
    if not args.no_htf:
        htf_tf = args.htf or htf_timeframe_for(args.timeframe)
        if htf_tf != args.timeframe:
            df_htf = load_bars(args.symbol, htf_tf, days=args.days, source=args.source)
            if not df_htf.empty:
                htf_bias_series = compute_bias_series(df, df_htf)
    news_events = []
    if args.news_filter:
        news_events = generate_events(df.index[0].to_pydatetime().replace(tzinfo=None),
                                      df.index[-1].to_pydatetime().replace(tzinfo=None))

    windows = _build_windows(df, args.is_days, args.oos_days)
    if not windows:
        console.print(f"[red]Cannot build windows: data spans {df.index[0]} → {df.index[-1]} "
                      f"(need ≥ {args.is_days + args.oos_days} days).[/red]")
        sys.exit(1)
    console.print(f"Built [bold]{len(windows)}[/bold] window(s).\n")

    # ---- Run each window -----------------------------------------------
    results: list[WindowResult] = []
    running_equity = args.equity
    oos_equity_pieces: list[pd.Series] = []
    oos_trades_all: list = []

    for i, (is_s, is_e, oos_s, oos_e) in enumerate(windows, start=1):
        is_df = df[(df.index >= is_s) & (df.index < is_e)]
        oos_df = df[(df.index >= oos_s) & (df.index < oos_e)]
        htf_is = htf_bias_series.reindex(is_df.index) if htf_bias_series is not None else None
        htf_oos = htf_bias_series.reindex(oos_df.index) if htf_bias_series is not None else None

        console.print(f"[dim]Window {i}/{len(windows)}  "
                      f"IS {is_s.strftime('%Y-%m-%d')} → {is_e.strftime('%Y-%m-%d')} "
                      f"({len(is_df)} bars) · "
                      f"OOS {oos_s.strftime('%Y-%m-%d')} → {oos_e.strftime('%Y-%m-%d')} "
                      f"({len(oos_df)} bars)[/dim]")

        best_cfg, is_stats, _ = _sweep_for_best(
            is_df, htf_is, news_events, args.equity, sim_symbol,
            args.risk_pct, args.htf_strict, args.news_filter, args.news_pad,
            min_filled=args.min_filled,
        )
        _apply_config(best_cfg)

        oos_sim, _ = _run(oos_df, htf_oos, news_events, running_equity, sim_symbol,
                          args.risk_pct, args.htf_strict, args.news_filter, args.news_pad)

        results.append(WindowResult(
            idx=i, is_start=is_s, is_end=is_e,
            oos_start=oos_s, oos_end=oos_e,
            best_config=best_cfg, is_stats=is_stats,
            oos_stats=dict(oos_sim.stats), oos_trades=list(oos_sim.trades),
        ))
        running_equity = oos_sim.stats["ending_equity"]
        oos_equity_pieces.append(oos_sim.equity_curve)
        oos_trades_all.extend(oos_sim.trades)

    # ---- Per-window comparison table -----------------------------------
    tbl = Table(title="Walk-forward windows", header_style="bold")
    tbl.add_column("#", justify="right")
    tbl.add_column("OOS period")
    tbl.add_column("Best config (Sw/Ch/F/RR)")
    tbl.add_column("IS exp", justify="right")
    tbl.add_column("IS filled", justify="right")
    tbl.add_column("OOS exp", justify="right")
    tbl.add_column("OOS filled", justify="right")
    tbl.add_column("OOS win %", justify="right")
    tbl.add_column("OOS P&L", justify="right")

    for r in results:
        is_exp = r.is_stats["expectancy_R"]
        oos_exp = r.oos_stats["expectancy_R"]
        diff_color = "green" if oos_exp >= is_exp * 0.6 and oos_exp > 0 else \
                     ("yellow" if oos_exp > 0 else "red")
        cfg_str = (f"{r.best_config['SWING_LOOKBACK']}/"
                   f"{r.best_config['SWEEP_TO_CHOCH_MAX_BARS']}/"
                   f"{r.best_config['CHOCH_TO_FVG_MAX_BARS']}/"
                   f"{r.best_config['SETUP_MIN_RR']:.1f}")
        oos_pnl = r.oos_stats["total_pnl_usd"]
        pnl_color = "green" if oos_pnl > 0 else ("red" if oos_pnl < 0 else "dim")
        tbl.add_row(
            str(r.idx),
            f"{r.oos_start.strftime('%m-%d')} → {r.oos_end.strftime('%m-%d')}",
            cfg_str,
            f"[green]{is_exp:+.2f}R[/green]" if is_exp > 0 else f"[red]{is_exp:+.2f}R[/red]",
            str(r.is_stats["n_filled"]),
            f"[{diff_color}]{oos_exp:+.2f}R[/{diff_color}]",
            str(r.oos_stats["n_filled"]),
            f"{r.oos_stats['win_rate_pct']:.0f}%" if r.oos_stats["n_filled"] > 0 else "—",
            f"[{pnl_color}]${oos_pnl:+,.0f}[/{pnl_color}]",
        )
    console.print(tbl)

    # ---- Aggregate OOS stats -------------------------------------------
    n_filled = sum(1 for t in oos_trades_all if t.outcome in ("target", "stop"))
    wins = sum(1 for t in oos_trades_all if t.outcome == "target")
    losses = sum(1 for t in oos_trades_all if t.outcome == "stop")
    total_pnl = sum(t.pnl_usd for t in oos_trades_all if t.outcome in ("target", "stop"))
    win_rate = (wins / n_filled * 100) if n_filled else 0
    avg_r = (sum(t.r_multiple for t in oos_trades_all if t.outcome in ("target", "stop")) / n_filled) if n_filled else 0
    ret_pct = (running_equity / args.equity - 1) * 100

    is_avg_exp = sum(r.is_stats["expectancy_R"] for r in results) / len(results) if results else 0
    oos_avg_exp = sum(r.oos_stats["expectancy_R"] for r in results) / len(results) if results else 0
    edge_retained = (oos_avg_exp / is_avg_exp * 100) if is_avg_exp > 0 else 0

    pnl_color = "green" if total_pnl > 0 else "red"
    avg_color = "green" if avg_r > 0 else "red"
    edge_color = "green" if edge_retained >= 60 else ("yellow" if edge_retained > 0 else "red")

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold")
    summary.add_column(justify="right")
    summary.add_row("Aggregate OOS trades",     str(len(oos_trades_all)))
    summary.add_row("Filled / Wins / Losses",   f"{n_filled} / {wins} / {losses}")
    summary.add_row("OOS win rate",             f"{win_rate:.1f}%")
    summary.add_row("OOS avg R",                f"[{avg_color}]{avg_r:+.2f}R[/{avg_color}]")
    summary.add_row("OOS total P&L",            f"[{pnl_color}]${total_pnl:+,.2f}[/{pnl_color}]")
    summary.add_row("OOS return",               f"[{pnl_color}]{ret_pct:+.2f}%[/{pnl_color}]")
    summary.add_row("Starting → ending equity", f"${args.equity:,.0f} → ${running_equity:,.2f}")
    summary.add_row("",                         "")
    summary.add_row("Mean IS expectancy",       f"{is_avg_exp:+.2f}R")
    summary.add_row("Mean OOS expectancy",      f"{oos_avg_exp:+.2f}R")
    summary.add_row("Edge retained",            f"[{edge_color}]{edge_retained:.0f}%[/{edge_color}]")
    console.print(Panel(summary, title="Aggregate (OOS only — honest)",
                        border_style="green" if total_pnl > 0 else "red",
                        title_align="left"))

    # Verdict
    if edge_retained >= 60 and oos_avg_exp > 0:
        verdict = "[green]✓  Plausible edge: OOS retained ≥60% of IS performance. Worth paper-trading.[/green]"
    elif oos_avg_exp > 0:
        verdict = "[yellow]△  Weak edge: OOS positive but well below IS. May be marginal after costs.[/yellow]"
    else:
        verdict = "[red]✗  No edge: OOS expectancy is non-positive. The IS performance was data-mining bias.[/red]"
    console.print(verdict)


if __name__ == "__main__":
    main()
