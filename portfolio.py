"""
Multi-asset portfolio backtester.

Single-asset backtests miss the biggest robust lever we found: combining
uncorrelated positive-expectancy streams cuts drawdown without giving up much
return (BTC+ETH OOS: maxDD -9.6% vs -13%/-17% standalone, because their
returns are only ~0.19 correlated).

This module runs the ICT strategy on several assets, then combines their
equity curves into one portfolio with configurable weighting and periodic
rebalancing, and reports portfolio-level + per-asset metrics.

Usage
-----
    python portfolio.py                          # BTC+ETH equal-weight, monthly rebal
    python portfolio.py --weighting invvol       # inverse-volatility weights
    python portfolio.py --assets BTCUSDT_binance_1h.csv ETHUSDT_binance_1h.csv \
                        --oos 0.4                 # only backtest the last 40%
"""
from __future__ import annotations

import argparse
import os
import warnings

import numpy as np
import pandas as pd

# Benign backtesting.py notices (same-bar SL/TP, margin) — quiet for clean CLI.
warnings.filterwarnings("ignore")

from config import StrategyParams, DEFAULT_PARAMS
from backtest import run_backtest

MARKET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_data")


# --------------------------------------------------------------------------- #
#  Per-asset equity curves
# --------------------------------------------------------------------------- #
def asset_equity(df: pd.DataFrame, params: StrategyParams, cash: float,
                 oos: float = 0.0) -> pd.Series:
    """Run the strategy on one asset and return its normalized equity curve."""
    if oos > 0:
        df = df[df.index >= df.index[int(len(df) * (1 - oos))]]
    stats, _, _, _ = run_backtest(df, params, cash=cash)
    eq = stats["_equity_curve"]["Equity"].copy()
    eq.index = df.index[:len(eq)]
    return eq / eq.iloc[0]


def _daily_returns(curve: pd.Series) -> pd.Series:
    return curve.resample("1D").last().dropna().pct_change().dropna()


# --------------------------------------------------------------------------- #
#  Portfolio construction
# --------------------------------------------------------------------------- #
def combine(curves: dict, weighting: str = "equal", rebalance: str = "1ME",
            vol_lookback: int = 30) -> tuple:
    """
    Combine per-asset normalized equity curves into one portfolio curve.

    weighting : "equal"  -> 1/N each
                "invvol" -> inversely proportional to trailing volatility
    rebalance : pandas offset alias for how often weights reset ("1ME"=monthly,
                "1W"=weekly, "none"=never, weights drift after the start)
    Returns (portfolio_curve, weights_history_df).
    """
    # daily returns matrix, aligned on common dates
    rets = pd.DataFrame({name: _daily_returns(c) for name, c in curves.items()}).dropna()
    if rets.empty:
        raise RuntimeError("No overlapping dates across assets.")
    names = list(rets.columns)

    # rebalance period labels per day
    if rebalance == "none":
        periods = pd.Series(0, index=rets.index)
    else:
        periods = rets.index.to_period(_period_alias(rebalance))
        periods = pd.Series(periods, index=rets.index)

    weights_hist = {}
    port_ret = pd.Series(0.0, index=rets.index)
    for _, idx in rets.groupby(periods).groups.items():
        block = rets.loc[idx]
        if weighting == "invvol":
            # trailing vol up to the start of this block (fall back to in-block)
            start = block.index[0]
            hist = rets.loc[:start].tail(vol_lookback)
            vol = hist.std().replace(0, np.nan)
            if vol.isna().all():
                w = pd.Series(1.0 / len(names), index=names)
            else:
                inv = (1.0 / vol).fillna(0.0)
                w = inv / inv.sum()
        else:  # equal
            w = pd.Series(1.0 / len(names), index=names)
        weights_hist[block.index[0]] = w
        port_ret.loc[block.index] = (block[names] * w).sum(axis=1)

    curve = (1.0 + port_ret).cumprod()
    return curve, pd.DataFrame(weights_hist).T


def _period_alias(rebalance: str) -> str:
    return {"1ME": "M", "1M": "M", "M": "M", "1W": "W", "W": "W",
            "1Q": "Q", "Q": "Q"}.get(rebalance, "M")


# --------------------------------------------------------------------------- #
#  Metrics
# --------------------------------------------------------------------------- #
def curve_metrics(curve: pd.Series, periods_per_year: int = 365) -> dict:
    ret = curve.iloc[-1] / curve.iloc[0] - 1.0
    daily = curve.pct_change().dropna() if curve.index.freq else _daily_returns(curve)
    daily = curve.resample("1D").last().dropna().pct_change().dropna()
    sharpe = (daily.mean() / daily.std() * np.sqrt(periods_per_year)
              if daily.std() > 0 else float("nan"))
    dd = (curve / curve.cummax() - 1.0).min()
    days = max((curve.index[-1] - curve.index[0]).days, 1)
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (365.0 / days) - 1.0
    return {"Return [%]": ret * 100, "CAGR [%]": cagr * 100,
            "Sharpe": sharpe, "Max Drawdown [%]": dd * 100}


def run_portfolio(assets: dict, params: StrategyParams = DEFAULT_PARAMS,
                  cash: float = 5_000_000, weighting: str = "equal",
                  rebalance: str = "1ME", oos: float = 0.0,
                  periods_per_year: int = 365) -> dict:
    """
    assets : {display_name: dataframe}.  Returns a dict with the portfolio
    curve, weights history, portfolio metrics, per-asset metrics, and the
    correlation matrix of daily returns.
    """
    curves = {name: asset_equity(df, params, cash, oos=oos)
              for name, df in assets.items()}
    port_curve, weights = combine(curves, weighting=weighting, rebalance=rebalance)

    per_asset = {name: curve_metrics(c, periods_per_year) for name, c in curves.items()}
    rets = pd.DataFrame({n: _daily_returns(c) for n, c in curves.items()}).dropna()
    return {
        "portfolio_curve": port_curve,
        "weights": weights,
        "portfolio": curve_metrics(port_curve, periods_per_year),
        "per_asset": per_asset,
        "correlation": rets.corr(),
    }


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def _print_report(res: dict, weighting: str, rebalance: str, oos: float):
    span = f"OOS last {int(oos*100)}%" if oos > 0 else "full period"
    print(f"\n{'='*60}\n  PORTFOLIO BACKTEST  ({weighting} weights, {rebalance} "
          f"rebalance, {span})\n{'='*60}")
    print(f"\n  {'asset':<20}{'ret%':>8}{'CAGR%':>8}{'Sharpe':>8}{'maxDD%':>9}")
    for name, m in res["per_asset"].items():
        print(f"  {name:<20}{m['Return [%]']:>8.1f}{m['CAGR [%]']:>8.1f}"
              f"{m['Sharpe']:>8.2f}{m['Max Drawdown [%]']:>9.1f}")
    p = res["portfolio"]
    print(f"  {'-'*52}")
    print(f"  {'PORTFOLIO':<20}{p['Return [%]']:>8.1f}{p['CAGR [%]']:>8.1f}"
          f"{p['Sharpe']:>8.2f}{p['Max Drawdown [%]']:>9.1f}")
    print(f"\n  Diversification: portfolio maxDD {p['Max Drawdown [%]']:.1f}% vs "
          f"worst single {min(m['Max Drawdown [%]'] for m in res['per_asset'].values()):.1f}%")
    print(f"\n  Daily-return correlation matrix:")
    print(res["correlation"].round(2).to_string().replace("\n", "\n  "))


def main():
    ap = argparse.ArgumentParser(description="Multi-asset portfolio backtester")
    ap.add_argument("--assets", nargs="+",
                    default=["BTCUSDT_binance_1h.csv", "ETHUSDT_binance_1h.csv"],
                    help="CSV filenames in market_data/")
    ap.add_argument("--weighting", choices=["equal", "invvol"], default="equal")
    ap.add_argument("--rebalance", default="1ME", help="1ME | 1W | 1Q | none")
    ap.add_argument("--oos", type=float, default=0.0,
                    help="fraction to hold out (e.g. 0.4 = last 40%%)")
    ap.add_argument("--cash", type=float, default=5_000_000)
    args = ap.parse_args()

    assets = {}
    for f in args.assets:
        name = f.replace("_yfinance", "").replace("_binance", "").replace(".csv", "")
        assets[name] = pd.read_csv(os.path.join(MARKET_DIR, f),
                                   index_col=0, parse_dates=True)

    res = run_portfolio(assets, cash=args.cash, weighting=args.weighting,
                        rebalance=args.rebalance, oos=args.oos)
    _print_report(res, args.weighting, args.rebalance, args.oos)

    out = os.path.join(MARKET_DIR, "portfolio_equity.csv")
    res["portfolio_curve"].to_frame("Equity").to_csv(out)
    print(f"\n  Portfolio equity curve -> {out}\n")


if __name__ == "__main__":
    main()
