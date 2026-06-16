"""
Fair Value Gap (FVG) detection.

A FVG is a 3-candle imbalance.  For candles (a, b, c) at indices (i-2, i-1, i):
    * Bullish FVG : a.High < c.Low      -> gap = [a.High, c.Low]
    * Bearish FVG : a.Low  > c.High     -> gap = [c.High, a.Low]

The gap is recorded at index i (the bar that completes it).  Its 50%
retracement (midpoint) is the ICT entry reference.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def fair_value_gaps(df: pd.DataFrame, min_size: float = 0.0008) -> pd.DataFrame:
    """
    Adds columns:
        FVG        -> +1 bullish, -1 bearish, 0 none (at the completing bar)
        FVGTop     -> upper bound of the gap
        FVGBottom  -> lower bound of the gap
        FVGMid     -> 50% retracement level (entry reference)
    `min_size` is the minimum gap height expressed as a fraction of price.
    """
    out = df.copy()
    highs = out["High"].to_numpy(dtype=float)
    lows = out["Low"].to_numpy(dtype=float)
    n = len(out)

    fvg = np.zeros(n, dtype=int)
    top = np.full(n, np.nan)
    bottom = np.full(n, np.nan)
    mid = np.full(n, np.nan)

    for i in range(2, n):
        a_high, a_low = highs[i - 2], lows[i - 2]
        c_high, c_low = highs[i], lows[i]
        price = out["Close"].iat[i]

        # Bullish imbalance.
        if a_high < c_low:
            size = (c_low - a_high) / price
            if size >= min_size:
                fvg[i] = 1
                top[i] = c_low
                bottom[i] = a_high
                mid[i] = (c_low + a_high) / 2.0
                continue

        # Bearish imbalance.
        if a_low > c_high:
            size = (a_low - c_high) / price
            if size >= min_size:
                fvg[i] = -1
                top[i] = a_low
                bottom[i] = c_high
                mid[i] = (a_low + c_high) / 2.0

    out["FVG"] = fvg
    out["FVGTop"] = top
    out["FVGBottom"] = bottom
    out["FVGMid"] = mid
    return out
