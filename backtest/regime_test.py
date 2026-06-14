"""Adverse-period regime test.

Runs the multi-instrument walk-forward validator on several named historical
slices, including known bearish/choppy periods, to check whether the edge
survives regime change.

Why this matters:
    A strategy that prints +1R on a trending bull doesn't necessarily work
    when the market sells off or chops. The most common failure mode of
    "promising" backtests is regime overfitting — the parameters that
    worked in Dec–Jun would have been disastrous in Jul–Sep of the prior
    year. This script forces the same parameter-selection process onto
    multiple regimes and compares.

Constraints:
    yfinance caps 1h data at ~730 days from today. That means we cannot
    reach 2022 directly — but the August 2024 yen-carry-trade unwind and
    the Q1 2025 tariff selloff are both inside the window and genuinely
    bearish/volatile. Daily timeframe is unlimited; passing ``--timeframe
    1d`` will let you reach as far back as you like, at the cost of fewer
    intraday signals.

Usage:
    python -m backtest.regime_test --equity 50000 --risk-pct 0.015
"""
from __future__ import annotations

import argparse
import itertools
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
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


# --- Named regimes inside the yfinance 730-day window ---------------------
@dataclass
class Regime:
    key: str
    label: str
    start: Optional[str]   # "YYYY-MM-DD" or None for "from data start"
    end:   Optional[str]   # "YYYY-MM-DD" or None for "to data end"
    note:  str = ""


REGIMES: list[Regime] = [
    Regime("aug24-shock",
           "Aug 2024 carry-trade unwind",
           "2024-07-15", "2024-09-30",
           "NQ dropped ~13% in 3 weeks on JPY carry unwind. Real bear-like move."),
    Regime("q1-25-selloff",
           "Q1 2025 tariff selloff",
           "2025-02-01", "2025-05-15",
           "NQ down ~15% on tariff fears. Adverse for momentum longs."),
    Regime("mid-25-chop",
           "Mid-2025 chop",
           "2025-06-01", "2025-09-30",
           "Sideways volatility, fewer clean trends."),
    Regime("recent-bull",
           "Dec 2025 → Jun 2026 bull",
           "2025-12-01", None,
           "The original sample. Strong bull trend."),
    Regime("full-window",
           "Full 24-month range",
           None, None,
           "Everything yfinance gives us at this timeframe. Best statistical power."),
]


UNIVERSE = [
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


# ---------------------------------------------------------------------------
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
        cur += timedelta(days=oos_days)
    return out


def _run_one(df, htf_bias, news_events, equity, sim_symbol,
             risk_pct, news_filter, news_pad):
    setups = find_setups(df, htf_bias_series=htf_bias)
    if news_filter and news_events and setups:
        kept, _ = filter_setups_news(setups, news_events,
                                     minutes_before=news_pad, minutes_after=news_pad)
        setups = kept
    return simulate(df=df, setups=setups, starting_equity=equity,
                    instrument_symbol=sim_symbol, risk_pct=risk_pct, min_rr=1.0)


def _sweep_in_sample(df_is, htf_is, news_events, equity, sim_symbol,
                     risk_pct, news_filter, news_pad, min_filled: int):
    keys = list(SWEEP_GRID.keys())
    best = None
    for combo in itertools.product(*SWEEP_GRID.values()):
        _apply(dict(zip(keys, combo)))
        sim = _run_one(df_is, htf_is, news_events, equity, sim_symbol,
                       risk_pct, news_filter, news_pad)
        st = sim.stats
        if st["n_filled"] < min_filled:
            continue
        if best is None or st["expectancy_R"] > best[1]["expectancy_R"]:
            best = (dict(zip(keys, combo)), dict(st))
    return best


# ---------------------------------------------------------------------------
def walk_forward_slice(df, htf_bias, news_events, equity, sim_symbol,
                       is_days, oos_days, risk_pct, news_filter, news_pad,
                       min_filled):
    """Returns aggregate OOS stats over all windows in ``df``."""
    windows = _windows(df, is_days, oos_days)
    if not windows:
        return None
    running_equity = equity
    oos_trades = []
    is_exps: list[float] = []
    for is_s, is_e, oos_s, oos_e in windows:
        is_df = df[(df.index >= is_s) & (df.index < is_e)]
        oos_df = df[(df.index >= oos_s) & (df.index < oos_e)]
        htf_is = htf_bias.reindex(is_df.index) if htf_bias is not None else None
        htf_oos = htf_bias.reindex(oos_df.index) if htf_bias is not None else None
        best = _sweep_in_sample(is_df, htf_is, news_events, equity, sim_symbol,
                                risk_pct, news_filter, news_pad, min_filled)
        if best is None:
            continue
        _apply(best[0])
        is_exps.append(best[1]["expectancy_R"])
        sim = _run_one(oos_df, htf_oos, news_events, running_equity, sim_symbol,
                       risk_pct, news_filter, news_pad)
        running_equity = sim.stats["ending_equity"]
        oos_trades.extend(sim.trades)

    filled = [t for t in oos_trades if t.outcome in ("target", "stop")]
    wins = [t for t in filled if t.outcome == "target"]
    losses = [t for t in filled if t.outcome == "stop"]
    avg_r = (sum(t.r_multiple for t in filled) / len(filled)) if filled else 0.0
    win_rate = (len(wins) / len(filled) * 100) if filled else 0.0
    total_pnl = sum(t.pnl_usd for t in filled)
    is_mean = (sum(is_exps) / len(is_exps)) if is_exps else 0.0
    return {
        "windows": len(windows),
        "oos_filled": len(filled),
        "oos_wins": len(wins),
        "oos_losses": len(losses),
        "oos_win_rate": win_rate,
        "oos_avg_R": avg_r,
        "oos_total_pnl": total_pnl,
        "is_mean_expectancy": is_mean,
        "ending_equity": running_equity,
    }


# ---------------------------------------------------------------------------
def _slice(df: pd.DataFrame, start: Optional[str], end: Optional[str]) -> pd.DataFrame:
    s = pd.Timestamp(start, tz="UTC") if start else df.index[0]
    e = pd.Timestamp(end, tz="UTC") if end else df.index[-1]
    return df[(df.index >= s) & (df.index <= e)]


def main():
    parser = argparse.ArgumentParser(description="Adverse-period regime test")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--equity", type=float, default=50_000.0)
    parser.add_argument("--source", default="auto",
                        choices=["auto", "tradovate", "yfinance", "synthetic"])
    parser.add_argument("--is-days", type=int, default=60)
    parser.add_argument("--oos-days", type=int, default=30)
    parser.add_argument("--risk-pct", type=float, default=0.015)
    parser.add_argument("--entry-mode", default="closer_edge",
                        choices=["mid", "closer_edge", "farther_edge"])
    parser.add_argument("--max-stop-pts", type=float, default=0.0)
    parser.add_argument("--no-htf", action="store_true")
    parser.add_argument("--news-filter", action="store_true")
    parser.add_argument("--news-pad", type=int, default=30)
    parser.add_argument("--min-filled", type=int, default=5)
    parser.add_argument("--symbols", default=None)
    parser.add_argument("--regimes", default=None,
                        help="Comma-separated subset of regime keys.")
    args = parser.parse_args()

    cfg.SETUP_ENTRY_MODE = args.entry_mode
    cfg.SETUP_MAX_STOP_POINTS = args.max_stop_pts

    universe = UNIVERSE
    if args.symbols:
        wanted = {s.strip().upper() for s in args.symbols.split(",")}
        universe = [u for u in UNIVERSE if u[0] in wanted]

    regimes = REGIMES
    if args.regimes:
        wanted_r = {r.strip() for r in args.regimes.split(",")}
        regimes = [r for r in REGIMES if r.key in wanted_r]

    console.rule(f"[bold]Adverse-period regime test · {args.timeframe} · "
                 f"IS {args.is_days}d / OOS {args.oos_days}d · risk {args.risk_pct*100:.2f}%[/bold]")
    console.print(f"Testing {len(regimes)} regime(s) on {len(universe)} asset(s).\n")

    # Fetch the max available history per symbol once (then slice per regime)
    full_data: dict[str, tuple[pd.DataFrame, Optional[pd.Series]]] = {}
    htf_tf_for_ltf = htf_timeframe_for(args.timeframe)
    for symbol, sim_symbol, label in universe:
        console.print(f"[dim]Fetching {symbol} {args.timeframe} max history…[/dim]")
        df_full = load_bars(symbol, args.timeframe, days=730, source=args.source)
        if df_full.empty:
            console.print(f"[red]No data for {symbol}; skipping.[/red]")
            continue
        htf_bias = None
        if not args.no_htf and htf_tf_for_ltf != args.timeframe:
            df_htf = load_bars(symbol, htf_tf_for_ltf, days=730, source=args.source)
            if not df_htf.empty:
                htf_bias = compute_bias_series(df_full, df_htf)
        full_data[symbol] = (df_full, htf_bias)
        console.print(f"  {symbol}: {len(df_full)} bars  "
                      f"{df_full.index[0].strftime('%Y-%m-%d')} → "
                      f"{df_full.index[-1].strftime('%Y-%m-%d')}")

    # ---- Run per regime × per symbol -----------------------------------
    all_results: list[dict] = []
    for regime in regimes:
        console.print(f"\n[bold cyan]Regime: {regime.label}[/bold cyan]")
        console.print(f"[dim]{regime.note}[/dim]")
        regime_rows = []
        for symbol, sim_symbol, label in universe:
            if symbol not in full_data:
                continue
            df_full, htf_bias_full = full_data[symbol]
            df = _slice(df_full, regime.start, regime.end)
            span_days = (df.index[-1] - df.index[0]).days if not df.empty else 0
            if span_days < args.is_days + args.oos_days:
                console.print(f"  [dim]{symbol}: only {span_days} days in window, need "
                              f"{args.is_days + args.oos_days}; skip[/dim]")
                continue
            htf_bias = htf_bias_full.reindex(df.index) if htf_bias_full is not None else None
            news_events = []
            if args.news_filter:
                news_events = generate_events(
                    df.index[0].to_pydatetime().replace(tzinfo=None),
                    df.index[-1].to_pydatetime().replace(tzinfo=None),
                )
            r = walk_forward_slice(
                df, htf_bias, news_events, args.equity, sim_symbol,
                args.is_days, args.oos_days, args.risk_pct,
                args.news_filter, args.news_pad, args.min_filled,
            )
            if r is None:
                console.print(f"  [dim]{symbol}: no windows fit, skip[/dim]")
                continue
            r["regime_key"] = regime.key
            r["regime_label"] = regime.label
            r["symbol"] = symbol
            r["label"] = label
            regime_rows.append(r)
            console.print(
                f"  {symbol:<3}  windows={r['windows']:>2}  "
                f"filled={r['oos_filled']:>3}  "
                f"win={r['oos_win_rate']:>5.1f}%  "
                f"avgR={r['oos_avg_R']:+.2f}  "
                f"P&L=${r['oos_total_pnl']:+,.0f}"
            )
        all_results.extend(regime_rows)

    # ---- Cross-regime comparison table ---------------------------------
    console.print()
    tbl = Table(title="Regime × Asset OOS results", header_style="bold")
    tbl.add_column("Regime")
    tbl.add_column("Asset")
    tbl.add_column("Filled", justify="right")
    tbl.add_column("Win %", justify="right")
    tbl.add_column("Avg R", justify="right")
    tbl.add_column("P&L", justify="right")
    tbl.add_column("Mean IS exp", justify="right")
    tbl.add_column("Retained", justify="right")
    last_regime = None
    for r in all_results:
        avg_color = "green" if r["oos_avg_R"] > 0 else "red"
        pnl_color = "green" if r["oos_total_pnl"] > 0 else "red"
        retained = (r["oos_avg_R"] / r["is_mean_expectancy"] * 100) if r["is_mean_expectancy"] > 0 else 0
        ret_color = "green" if retained >= 60 and r["oos_avg_R"] > 0 else \
                    ("yellow" if r["oos_avg_R"] > 0 else "red")
        regime_str = r["regime_label"] if r["regime_label"] != last_regime else ""
        last_regime = r["regime_label"]
        tbl.add_row(
            regime_str,
            r["symbol"],
            str(r["oos_filled"]),
            f"{r['oos_win_rate']:.0f}%" if r["oos_filled"] > 0 else "—",
            f"[{avg_color}]{r['oos_avg_R']:+.2f}R[/{avg_color}]",
            f"[{pnl_color}]${r['oos_total_pnl']:+,.0f}[/{pnl_color}]",
            f"{r['is_mean_expectancy']:+.2f}R",
            f"[{ret_color}]{retained:.0f}%[/{ret_color}]",
        )
    console.print(tbl)

    # ---- Per-regime portfolio aggregate --------------------------------
    summary = Table(title="Per-regime portfolio aggregate", header_style="bold")
    summary.add_column("Regime")
    summary.add_column("Assets +R", justify="right")
    summary.add_column("Total filled", justify="right")
    summary.add_column("Portfolio win %", justify="right")
    summary.add_column("Portfolio avg R", justify="right")
    summary.add_column("Total P&L", justify="right")

    regime_verdicts: dict[str, dict] = {}
    for regime in regimes:
        rows = [r for r in all_results if r["regime_key"] == regime.key]
        if not rows:
            continue
        positive = sum(1 for r in rows if r["oos_avg_R"] > 0)
        total_filled = sum(r["oos_filled"] for r in rows)
        wins = sum(r["oos_wins"] for r in rows)
        avg_r = (sum(r["oos_avg_R"] * r["oos_filled"] for r in rows) / total_filled) if total_filled else 0
        win_rate = (wins / total_filled * 100) if total_filled else 0
        pnl = sum(r["oos_total_pnl"] for r in rows)
        regime_verdicts[regime.key] = {
            "positive_assets": positive,
            "total_assets": len(rows),
            "total_filled": total_filled,
            "win_rate": win_rate,
            "avg_r": avg_r,
            "pnl": pnl,
        }
        avg_color = "green" if avg_r > 0 else "red"
        pnl_color = "green" if pnl > 0 else "red"
        summary.add_row(
            regime.label,
            f"{positive} / {len(rows)}",
            str(total_filled),
            f"{win_rate:.1f}%" if total_filled else "—",
            f"[{avg_color}]{avg_r:+.2f}R[/{avg_color}]",
            f"[{pnl_color}]${pnl:+,.0f}[/{pnl_color}]",
        )
    console.print(summary)

    # ---- Verdict --------------------------------------------------------
    if not regime_verdicts:
        console.print("[red]No regime produced any results.[/red]")
        return
    n_positive_regimes = sum(1 for v in regime_verdicts.values() if v["avg_r"] > 0)
    n_total = len(regime_verdicts)
    if n_positive_regimes == n_total and n_total >= 3:
        verdict_color = "green"
        verdict = ("✓  Edge survives every regime tested. "
                   "Strong evidence the strategy isn't a one-regime artifact.")
    elif n_positive_regimes >= max(2, n_total // 2 + 1):
        verdict_color = "yellow"
        verdict = ("△  Edge holds on most regimes but fails on at least one. "
                   "Likely workable with regime filtering (e.g. skip during chop).")
    else:
        verdict_color = "red"
        verdict = ("✗  Edge fails on most regimes. The earlier results were regime-specific. "
                   "Do NOT trade this live.")
    console.print(Panel(f"[{verdict_color}]{verdict}[/{verdict_color}]",
                        title="Regime-robustness verdict",
                        border_style=verdict_color, title_align="left"))


if __name__ == "__main__":
    main()
