"""
Parameter optimisation.

Because FVG size and liquidity lookback change which signals are *generated*
(not just how they are traded), they cannot be tuned with Backtest.optimize()
alone — signals must be regenerated per combination.  This module runs an
explicit grid search over:

    * min_fvg_size       (FVG size filter)
    * liquidity_lookback (equal-level search window)
    * risk_reward        (target RR)

and returns a ranked results table.
"""
from __future__ import annotations

import itertools
from dataclasses import replace
from typing import List, Optional

import pandas as pd

from config import StrategyParams, DEFAULT_PARAMS
from backtest import run_backtest


def optimize_grid(df: pd.DataFrame,
                  fvg_sizes: Optional[List[float]] = None,
                  liquidity_lookbacks: Optional[List[int]] = None,
                  risk_rewards: Optional[List[float]] = None,
                  base: StrategyParams = DEFAULT_PARAMS,
                  cash: float = 100_000,
                  commission: float = 0.0004,
                  rank_by: str = "Return [%]",
                  min_trades: int = 5) -> pd.DataFrame:
    """Run the grid and return a DataFrame sorted by `rank_by` (descending)."""
    fvg_sizes = fvg_sizes or [0.0005, 0.0008, 0.0012, 0.0020]
    liquidity_lookbacks = liquidity_lookbacks or [10, 20, 30, 40]
    risk_rewards = risk_rewards or [2.0, 3.0, 4.0, 5.0]

    rows = []
    combos = list(itertools.product(fvg_sizes, liquidity_lookbacks, risk_rewards))
    for n, (fvg, lb, rr) in enumerate(combos, 1):
        params = replace(base, min_fvg_size=fvg, liquidity_lookback=lb,
                          risk_reward=rr)
        try:
            _, _, _, metrics = run_backtest(df, params, cash=cash,
                                            commission=commission, plot=False)
        except Exception as exc:                      # noqa: BLE001
            print(f"[{n}/{len(combos)}] skipped {(fvg, lb, rr)}: {exc}")
            continue

        row = {"min_fvg_size": fvg, "liquidity_lookback": lb, "risk_reward": rr}
        row.update(metrics)
        rows.append(row)
        print(f"[{n}/{len(combos)}] fvg={fvg} lb={lb} rr={rr} "
              f"-> Return={metrics['Return [%]']:.1f}%  "
              f"Trades={metrics['Total Trades']}")

    if not rows:
        return pd.DataFrame()

    res = pd.DataFrame(rows)
    res = res[res["Total Trades"] >= min_trades]
    if res.empty:
        print(f"No combination produced >= {min_trades} trades.")
        return pd.DataFrame(rows).sort_values(rank_by, ascending=False)
    return res.sort_values(rank_by, ascending=False).reset_index(drop=True)


if __name__ == "__main__":
    import sys, os
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from data.data_loader import load_data

    data = load_data("BTCUSDT", interval="1h", start="2023-06-01", end="2024-06-01")
    table = optimize_grid(data)
    pd.set_option("display.width", 160)
    print("\nTop 10 parameter sets:")
    print(table.head(10).to_string(index=False))
