"""
Market structure detection: swing pivots, Break of Structure (BOS) and
Change of Character (CHoCH).

Definitions used
----------------
* Swing high : a candle whose high is the maximum of the window
               [i-left, i+right]; confirmed only `right` bars later.
* Swing low  : symmetric.
* BOS  : trend continuation — price closes beyond the most recent swing in
         the SAME direction as the prevailing trend.
* CHoCH: trend reversal — price closes beyond the most recent opposing swing,
         flipping the trend (the first shift after a BOS sequence).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def swing_points(df: pd.DataFrame, left: int = 3, right: int = 3):
    """
    Return two float arrays (swing_high, swing_low) the length of `df`.
    Non-pivot bars are NaN.  A pivot at index i is only *known* at i+right.
    """
    highs = df["High"].to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)
    n = len(df)
    swing_high = np.full(n, np.nan)
    swing_low = np.full(n, np.nan)

    for i in range(left, n - right):
        win_h = highs[i - left:i + right + 1]
        if highs[i] == win_h.max() and np.argmax(win_h) == left:
            swing_high[i] = highs[i]
        win_l = lows[i - left:i + right + 1]
        if lows[i] == win_l.min() and np.argmin(win_l) == left:
            swing_low[i] = lows[i]

    return swing_high, swing_low


def market_structure(df: pd.DataFrame, left: int = 3, right: int = 3) -> pd.DataFrame:
    """
    Annotate the dataframe with structure events.

    Adds columns:
        SwingHigh, SwingLow        -> pivot levels (NaN elsewhere)
        Trend                      -> +1 bullish / -1 bearish / 0 unknown
        BOS                        -> +1 bullish BOS, -1 bearish BOS, 0 none
        CHoCH                      -> +1 bullish CHoCH, -1 bearish CHoCH, 0 none
        BrokenLevel                -> the structural level that was broken
    """
    out = df.copy()
    sh, sl = swing_points(df, left, right)
    out["SwingHigh"] = sh
    out["SwingLow"] = sl

    closes = df["Close"].to_numpy(dtype=float)
    n = len(df)

    bos = np.zeros(n, dtype=int)
    choch = np.zeros(n, dtype=int)
    trend = np.zeros(n, dtype=int)
    broken = np.full(n, np.nan)

    last_sh = np.nan       # most recent CONFIRMED swing high level
    last_sl = np.nan       # most recent CONFIRMED swing low level
    cur_trend = 0

    for i in range(n):
        # A pivot located `right` bars ago is now confirmed and usable.
        conf = i - right
        if conf >= 0:
            if not np.isnan(sh[conf]):
                last_sh = sh[conf]
            if not np.isnan(sl[conf]):
                last_sl = sl[conf]

        c = closes[i]

        # Bullish break: close above last confirmed swing high.
        if not np.isnan(last_sh) and c > last_sh:
            if cur_trend <= 0:
                choch[i] = 1          # reversal to bullish
            else:
                bos[i] = 1            # continuation
            broken[i] = last_sh
            cur_trend = 1
            last_sh = np.nan          # consume the level

        # Bearish break: close below last confirmed swing low.
        elif not np.isnan(last_sl) and c < last_sl:
            if cur_trend >= 0:
                choch[i] = -1         # reversal to bearish
            else:
                bos[i] = -1           # continuation
            broken[i] = last_sl
            cur_trend = -1
            last_sl = np.nan

        trend[i] = cur_trend

    out["Trend"] = trend
    out["BOS"] = bos
    out["CHoCH"] = choch
    out["BrokenLevel"] = broken
    return out
