"""Fair Value Gap (3-candle imbalance) detector.

Definition:
- **Bullish FVG**: bar3.low > bar1.high. The gap range is (bar1.high, bar3.low).
- **Bearish FVG**: bar3.high < bar1.low. The gap range is (bar3.high, bar1.low).

A gap is considered *filled* (mitigated) when a later bar trades back into
the range with its wick. We scan the full series in one pass and update the
fill flag against subsequent bars.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

import pandas as pd


Direction = Literal["bull", "bear"]


@dataclass
class FVG:
    idx: int                       # index of the 3rd bar (where the gap is "completed")
    timestamp: pd.Timestamp        # timestamp of bar3
    direction: Direction
    top: float                     # upper bound of the gap (price)
    bottom: float                  # lower bound of the gap
    size: float                    # top - bottom
    filled: bool = False
    filled_idx: Optional[int] = None
    filled_timestamp: Optional[pd.Timestamp] = None


# ---------------------------------------------------------------------------
def find_fvgs(df: pd.DataFrame) -> list[FVG]:
    """Return every FVG present in ``df``, with fill state updated through
    the most recent bar.
    """
    if len(df) < 3:
        return []

    highs = df["high"].values
    lows = df["low"].values
    ts = df.index
    n = len(df)
    fvgs: list[FVG] = []

    # Pass 1 — find gaps
    for i in range(2, n):
        h1, l1 = highs[i - 2], lows[i - 2]
        h3, l3 = highs[i], lows[i]
        if l3 > h1:  # bullish FVG
            fvgs.append(FVG(
                idx=i, timestamp=ts[i], direction="bull",
                top=float(l3), bottom=float(h1), size=float(l3 - h1),
            ))
        elif h3 < l1:  # bearish FVG
            fvgs.append(FVG(
                idx=i, timestamp=ts[i], direction="bear",
                top=float(l1), bottom=float(h3), size=float(l1 - h3),
            ))

    # Pass 2 — mitigation check (any later bar trades into the range)
    for g in fvgs:
        for j in range(g.idx + 1, n):
            if lows[j] <= g.top and highs[j] >= g.bottom:
                g.filled = True
                g.filled_idx = j
                g.filled_timestamp = ts[j]
                break

    return fvgs


# ---------------------------------------------------------------------------
def summary(df: pd.DataFrame) -> dict:
    """Aggregate counts of bullish/bearish gaps and how many are still open."""
    fvgs = find_fvgs(df)
    bull = [g for g in fvgs if g.direction == "bull"]
    bear = [g for g in fvgs if g.direction == "bear"]
    return {
        "fvgs": fvgs,
        "counts": {
            "total": len(fvgs),
            "bull":  len(bull),
            "bear":  len(bear),
            "filled":  sum(1 for g in fvgs if g.filled),
            "open":    sum(1 for g in fvgs if not g.filled),
        },
    }
