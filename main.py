"""
Command-line entry point for the ICT trading system.

Examples
--------
    # Single backtest on BTC 1h with the analytical chart
    python main.py --symbol BTCUSDT --interval 1h \
        --start 2023-06-01 --end 2024-06-01 --plot

    # Forex via Yahoo Finance
    python main.py --symbol EURUSD --source yfinance --interval 1h

    # Grid optimisation (FVG size / liquidity lookback / risk-reward)
    python main.py --symbol ETHUSDT --optimize
"""
from __future__ import annotations

import argparse
import os
from dataclasses import replace

import pandas as pd

from config import DEFAULT_PARAMS, DEFAULT_SOURCE
from data.data_loader import load_data
from ict.signals import generate_signals
from backtest import run_backtest, print_metrics, report_diagnostics
from optimize import optimize_grid
from plotting import plot_ict

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def parse_args():
    p = argparse.ArgumentParser(description="ICT trading system (Backtesting.py)")
    p.add_argument("--symbol", default="BTCUSDT",
                   help="BTCUSDT | ETHUSDT | EURUSD | SPY (or any ticker)")
    p.add_argument("--source", default=None,
                   choices=[None, "binance", "yfinance", "fmp", "ibkr"],
                   help="data source (default: auto by symbol)")
    p.add_argument("--interval", default="1h")
    p.add_argument("--start", default="2023-06-01")
    p.add_argument("--end", default=None)
    p.add_argument("--cash", type=float, default=100_000)
    p.add_argument("--commission", type=float, default=0.0004)
    p.add_argument("--risk", type=float, default=DEFAULT_PARAMS.risk_per_trade)
    p.add_argument("--rr", type=float, default=DEFAULT_PARAMS.risk_reward)
    p.add_argument("--trail", action="store_true", help="enable trailing stop")
    p.add_argument("--plot", action="store_true", help="write analytical + bokeh charts")
    p.add_argument("--optimize", action="store_true", help="run grid optimisation")
    return p.parse_args()


def main():
    args = parse_args()
    os.makedirs(RESULTS_DIR, exist_ok=True)
    source = args.source or DEFAULT_SOURCE.get(args.symbol.upper(), "yfinance")

    print(f"Loading {args.symbol} [{source}] {args.interval} "
          f"{args.start} -> {args.end or 'now'} ...")
    data = load_data(args.symbol, source=source, interval=args.interval,
                     start=args.start, end=args.end)
    print(f"Loaded {len(data)} bars.\n")

    params = replace(DEFAULT_PARAMS, risk_per_trade=args.risk,
                     risk_reward=args.rr, use_trailing_stop=args.trail)

    if args.optimize:
        table = optimize_grid(data, base=params, cash=args.cash,
                              commission=args.commission)
        out_csv = os.path.join(RESULTS_DIR, f"optimize_{args.symbol}.csv")
        table.to_csv(out_csv, index=False)
        pd.set_option("display.width", 160)
        print("\nTop parameter sets:")
        print(table.head(10).to_string(index=False))
        print(f"\nFull table -> {out_csv}")
        return

    # --- single run ------------------------------------------------------- #
    signals = generate_signals(data, params)
    n_sig = (signals["Signal"] != "NONE").sum()
    print(f"Generated {n_sig} raw signals "
          f"({(signals['Signal'] == 'BUY').sum()} BUY / "
          f"{(signals['Signal'] == 'SELL').sum()} SELL).")

    sig_csv = os.path.join(RESULTS_DIR, f"signals_{args.symbol}.csv")
    signals[["Open", "High", "Low", "Close", "Signal",
             "Entry", "StopLoss", "TakeProfit", "RiskReward"]].to_csv(sig_csv)
    print(f"Signal table -> {sig_csv}")

    bokeh_path = os.path.join(RESULTS_DIR, f"backtest_{args.symbol}.html") if args.plot else None
    stats, bt, prepared, metrics = run_backtest(
        data, params, cash=args.cash, commission=args.commission,
        plot=args.plot, plot_path=bokeh_path,
    )
    print_metrics(metrics, f"{args.symbol} {args.interval}")
    report_diagnostics(stats, prepared,
                       title=f"{args.symbol} LONG-ONLY DIAGNOSTICS")
    if metrics["Short Trades"] == 0:
        print("  [OK] verified: zero short trades executed.\n")
    else:
        print(f"  [WARN] {metrics['Short Trades']} short trades executed!\n")
    print(stats)

    if args.plot:
        html = os.path.join(RESULTS_DIR, f"ict_{args.symbol}.html")
        plot_ict(prepared, title=f"{args.symbol} {args.interval} — ICT", save_html=html)
        print(f"Backtest chart -> {bokeh_path}")


if __name__ == "__main__":
    main()
