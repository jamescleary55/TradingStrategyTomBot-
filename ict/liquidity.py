"""
Liquidity detection: equal highs / equal lows and liquidity sweeps.

* Equal highs/lows : two (or more) swing pivots within `tolerance` of each
                     other, marking a pool of resting liquidity (stops).
* Liquidity sweep  : a candle that wicks BEYOND such a level (or a prior swing
                     extreme) but CLOSES back inside it — a classic stop hunt.
                       - sweep low  (+1): wick below support, close back above
                       - sweep high (-1): wick above resistance, close back below
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def equal_levels(df: pd.DataFrame, lookback: int = 20, tolerance: float = 0.0010):
    """
    Detect equal-high and equal-low pools from confirmed swing pivots.

    Requires the SwingHigh / SwingLow columns from `market_structure`.
    Adds columns EqualHigh, EqualLow holding the pooled price level (NaN else).
    """
    out = df.copy()
    sh = out["SwingHigh"].to_numpy(dtype=float)
    sl = out["SwingLow"].to_numpy(dtype=float)
    n = len(out)

    eq_high = np.full(n, np.nan)
    eq_low = np.full(n, np.nan)

    high_idx = [i for i in range(n) if not np.isnan(sh[i])]
    low_idx = [i for i in range(n) if not np.isnan(sl[i])]

    # Equal highs: compare each swing high with earlier swing highs in window.
    for k, i in enumerate(high_idx):
        for j in high_idx[max(0, k - lookback):k]:
            if abs(sh[i] - sh[j]) / sh[i] <= tolerance:
                level = (sh[i] + sh[j]) / 2.0
                eq_high[i] = level
                eq_low[j] = eq_low[j]  # no-op, keep symmetry readable
                break

    for k, i in enumerate(low_idx):
        for j in low_idx[max(0, k - lookback):k]:
            if abs(sl[i] - sl[j]) / sl[i] <= tolerance:
                eq_low[i] = (sl[i] + sl[j]) / 2.0
                break

    out["EqualHigh"] = eq_high
    out["EqualLow"] = eq_low
    return out


def liquidity_sweeps(df: pd.DataFrame, right: int = 3, sweep_lookback: int = 30):
    """
    Mark liquidity sweeps of recent swing extremes / equal levels.

    Adds columns:
        Sweep       -> +1 bullish (sell-side swept), -1 bearish (buy-side), 0 none
        SweepLevel  -> the liquidity level that was swept
        SweepExtreme-> the wick extreme of the sweeping candle (used for stops)
    """
    out = df.copy()
    highs = out["High"].to_numpy(dtype=float)
    lows = out["Low"].to_numpy(dtype=float)
    closes = out["Close"].to_numpy(dtype=float)
    opens = out["Open"].to_numpy(dtype=float)
    sh = out["SwingHigh"].to_numpy(dtype=float)
    sl = out["SwingLow"].to_numpy(dtype=float)
    n = len(out)

    sweep = np.zeros(n, dtype=int)
    sweep_level = np.full(n, np.nan)
    sweep_extreme = np.full(n, np.nan)

    for i in range(n):
        # Gather confirmed swing levels available before bar i.
        lo_levels = []
        hi_levels = []
        start = max(0, i - sweep_lookback)
        for j in range(start, i):
            conf_ok = j <= i - right       # pivot confirmed by now
            if conf_ok and not np.isnan(sl[j]):
                lo_levels.append(sl[j])
            if conf_ok and not np.isnan(sh[j]):
                hi_levels.append(sh[j])

        body_top = max(opens[i], closes[i])
        body_bottom = min(opens[i], closes[i])

        # Bullish sweep: wick takes out a recent low, body closes back above it.
        for lvl in lo_levels:
            if lows[i] < lvl and body_bottom > lvl:
                sweep[i] = 1
                sweep_level[i] = lvl
                sweep_extreme[i] = lows[i]
                break

        if sweep[i] == 0:
            # Bearish sweep: wick takes out a recent high, body closes below it.
            for lvl in hi_levels:
                if highs[i] > lvl and body_top < lvl:
                    sweep[i] = -1
                    sweep_level[i] = lvl
                    sweep_extreme[i] = highs[i]
                    break

    out["Sweep"] = sweep
    out["SweepLevel"] = sweep_level
    out["SweepExtreme"] = sweep_extreme
    return out
