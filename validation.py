"""
Walk-forward & regime validation for the (long-only) index model.

One 60/40 split tells you almost nothing — it can't reveal whether an edge
survives a bear market.  This module measures the strategy across many
out-of-sample windows:

  * by_year()       - run each calendar year independently (regime x-ray:
                      see 2018 / 2020 crash / 2022 bear vs the bull years).
  * walk_forward()  - rolling out-of-sample windows with indicator warmup;
                      optionally grid-optimise on each train block first
                      (true walk-forward optimisation).

Both measure TEST-period trades only (entries inside the window), with a
leading warmup buffer so indicators/HTF EMA are fully seeded -> no look-ahead,
no cold-start distortion.

Usage
-----
    python validation.py --symbol NQ --csv NQ_yfinance_1d_long.csv --mode year
    python validation.py --symbol ES --csv ES_yfinance_1d_long.csv --mode wfo \
        --train 756 --test 252 --step 252
"""
from __future__ import annotations

import argparse
import os
import warnings

import numpy as np
import pandas as pd

from config import StrategyParams, DEFAULT_PARAMS
from backtest import run_backtest, _count_short_trades

warnings.filterwarnings("ignore")
MARKET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_data")

# bars of leading context so signals/HTF-EMA are warm before the test window
WARMUP_BARS = 80


# --------------------------------------------------------------------------- #
#  Test-window metrics (computed from the trades that ENTERED in the window)
# --------------------------------------------------------------------------- #
def _metrics(trades: pd.DataFrame, cash: float) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "shorts": 0, "ret%": 0.0, "win%": float("nan"),
                "PF": float("nan"), "maxDD%": 0.0}
    pnl = trades["PnL"].to_numpy(dtype=float)
    wins = pnl[pnl > 0].sum()
    losses = -pnl[pnl <= 0].sum()
    eq = cash + np.cumsum(pnl)                       # window equity path
    dd = (eq / np.maximum.accumulate(eq) - 1.0).min() * 100
    return {
        "trades": n,
        "shorts": int((trades["Size"] < 0).sum()),
        "ret%": pnl.sum() / cash * 100,
        "win%": (pnl > 0).mean() * 100,
        "PF": (wins / losses) if losses > 0 else float("inf"),
        "maxDD%": dd,
    }


def _run_window(df: pd.DataFrame, params: StrategyParams, cash: float,
                test_start, test_end) -> dict:
    """Backtest a slice (warmup + test) and keep only test-period entries."""
    stats, _, _, _ = run_backtest(df, params, cash=cash)
    trades = stats.get("_trades")
    if trades is None or len(trades) == 0:
        return _metrics(pd.DataFrame(columns=["PnL", "Size"]), cash)
    et = pd.to_datetime(trades["EntryTime"])
    mask = (et >= test_start) & (et <= test_end)
    return _metrics(trades.loc[mask], cash)


# --------------------------------------------------------------------------- #
#  Regime x-ray: one row per calendar year
# --------------------------------------------------------------------------- #
def by_year(df: pd.DataFrame, params: StrategyParams = DEFAULT_PARAMS,
            cash: float = 5_000_000) -> pd.DataFrame:
    rows = []
    for yr in sorted({d.year for d in df.index}):
        ts, te = pd.Timestamp(yr, 1, 1), pd.Timestamp(yr, 12, 31, 23, 59)
        # include warmup bars before Jan 1 so indicators are seeded
        start_pos = max(0, df.index.searchsorted(ts) - WARMUP_BARS)
        sl = df.iloc[start_pos:df.index.searchsorted(te) + 1]
        if len(sl) < WARMUP_BARS + 5:
            continue
        m = _run_window(sl, params, cash, ts, te)
        rows.append({"year": yr, **m})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
#  Rolling walk-forward (optionally optimised per train block)
# --------------------------------------------------------------------------- #
def walk_forward(df: pd.DataFrame, params: StrategyParams = DEFAULT_PARAMS,
                 train: int = 756, test: int = 252, step: int = 252,
                 cash: float = 5_000_000, optimize: bool = False) -> pd.DataFrame:
    """
    Roll [train | test] forward by `step` bars.  With optimize=True, grid-search
    params on each train block and apply the winner to the following test block
    (true WFO); otherwise use fixed `params` (robustness across regimes).
    """
    rows = []
    n = len(df)
    start = 0
    while start + train + test <= n:
        tr0, te0, te1 = start, start + train, start + train + test
        test_start = df.index[te0]
        test_end = df.index[te1 - 1]
        used = params
        if optimize:
            from optimize import optimize_grid
            train_df = df.iloc[tr0:te0]
            grid = optimize_grid(train_df, base=params, cash=cash)
            if len(grid):
                top = grid.iloc[0]
                from dataclasses import replace
                used = replace(params, min_fvg_size=float(top["min_fvg_size"]),
                               liquidity_lookback=int(top["liquidity_lookback"]),
                               risk_reward=float(top["risk_reward"]))
        # test slice with warmup pulled from the tail of the train block
        sl = df.iloc[max(0, te0 - WARMUP_BARS):te1]
        m = _run_window(sl, used, cash, test_start, test_end)
        rows.append({"test_start": test_start.date(), "test_end": test_end.date(), **m})
        start += step
    return pd.DataFrame(rows)


def summarize_windows(table: pd.DataFrame, label: str):
    if table.empty:
        print(f"\n{label}: no windows produced."); return
    valid = table[table["trades"] > 0]
    pct_profit = (table["ret%"] > 0).mean() * 100
    print(f"\n{'='*64}\n  {label}\n{'='*64}")
    cols = [c for c in ("year", "test_start", "test_end") if c in table.columns]
    show = table.copy()
    for c in ("ret%", "win%", "PF", "maxDD%"):
        show[c] = show[c].round(2)
    print(show.to_string(index=False))
    print(f"\n  windows: {len(table)} | profitable: {pct_profit:.0f}% | "
          f"total shorts executed: {int(table['shorts'].sum())} (must be 0) | "
          f"avg ret/window: {table['ret%'].mean():.2f}% | "
          f"median: {table['ret%'].median():.2f}%")


def main():
    ap = argparse.ArgumentParser(description="Walk-forward / regime validation")
    ap.add_argument("--symbol", default="NQ")
    ap.add_argument("--csv", default="NQ_yfinance_1d_long.csv")
    ap.add_argument("--mode", choices=["year", "wfo"], default="year")
    ap.add_argument("--train", type=int, default=756)
    ap.add_argument("--test", type=int, default=252)
    ap.add_argument("--step", type=int, default=252)
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--cash", type=float, default=5_000_000)
    args = ap.parse_args()

    df = pd.read_csv(os.path.join(MARKET_DIR, args.csv), index_col=0, parse_dates=True)
    if args.mode == "year":
        t = by_year(df, cash=args.cash)
        summarize_windows(t, f"{args.symbol} - per-year regime x-ray (long-only)")
    else:
        t = walk_forward(df, train=args.train, test=args.test, step=args.step,
                         cash=args.cash, optimize=args.optimize)
        tag = "WFO (optimised)" if args.optimize else "walk-forward (fixed)"
        summarize_windows(t, f"{args.symbol} - {tag}")


if __name__ == "__main__":
    main()
