"""Market structure detection.

- ``find_swings(df, lookback)`` — pivot highs / lows where the bar is the
  strict extreme over ``lookback`` bars on each side (default 5).
- ``find_bos(df, swings)`` — Break Of Structure: the first close beyond the
  most recent same-side swing in the prevailing direction.
- ``find_choch(df, bos_events)`` — Change Of Character: the first BOS that
  opposes the previous BOS direction.

Each event carries its bar index, timestamp, price and direction. The whole
module is pure functions over a price DataFrame indexed by timestamp with
``open / high / low / close`` columns.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

import config


Side = Literal["high", "low"]
Direction = Literal["bull", "bear"]


@dataclass
class Swing:
    idx: int
    timestamp: pd.Timestamp
    price: float
    side: Side          # "high" or "low"


@dataclass
class StructureEvent:
    idx: int
    timestamp: pd.Timestamp
    price: float
    direction: Direction          # "bull" = broke a high; "bear" = broke a low
    broken_swing: Swing
    kind: Literal["BOS", "CHoCH"]


# ---------------------------------------------------------------------------
def find_swings(df: pd.DataFrame, lookback: int | None = None) -> list[Swing]:
    if lookback is None:
        lookback = config.SWING_LOOKBACK
    """Return the list of confirmed swing highs and lows.

    A swing high at bar i requires ``high[i]`` to be the strict maximum over
    bars ``[i-lookback, i+lookback]``. Same logic mirrored for swing lows.
    """
    swings: list[Swing] = []
    highs = df["high"].values
    lows = df["low"].values
    ts = df.index
    n = len(df)
    for i in range(lookback, n - lookback):
        window_h = highs[i - lookback: i + lookback + 1]
        window_l = lows[i - lookback: i + lookback + 1]
        if highs[i] == window_h.max() and (window_h == highs[i]).sum() == 1:
            swings.append(Swing(i, ts[i], float(highs[i]), "high"))
        if lows[i] == window_l.min() and (window_l == lows[i]).sum() == 1:
            swings.append(Swing(i, ts[i], float(lows[i]), "low"))
    swings.sort(key=lambda s: s.idx)
    return swings


# ---------------------------------------------------------------------------
def find_bos(df: pd.DataFrame, swings: list[Swing]) -> list[StructureEvent]:
    """Detect Break-Of-Structure events.

    Walk forward bar-by-bar. Track the most recent swing high and swing low.
    A bullish BOS fires when a close exceeds the last swing high; a bearish
    BOS fires when a close prints below the last swing low. After a BOS the
    "broken" swing is consumed (next BOS requires a fresher swing of that
    side that formed after the break).
    """
    if not swings:
        return []

    closes = df["close"].values
    ts = df.index
    n = len(df)

    last_high: Swing | None = None
    last_low: Swing | None = None
    swing_iter = iter(swings)
    next_swing: Swing | None = next(swing_iter, None)

    events: list[StructureEvent] = []
    for i in range(n):
        # Promote any swings whose confirmation index has passed
        while next_swing is not None and next_swing.idx <= i:
            if next_swing.side == "high":
                last_high = next_swing
            else:
                last_low = next_swing
            next_swing = next(swing_iter, None)

        c = closes[i]
        if last_high is not None and c > last_high.price and i > last_high.idx:
            events.append(StructureEvent(i, ts[i], float(c), "bull", last_high, "BOS"))
            last_high = None  # consumed; await next swing high to re-arm
        if last_low is not None and c < last_low.price and i > last_low.idx:
            events.append(StructureEvent(i, ts[i], float(c), "bear", last_low, "BOS"))
            last_low = None
    return events


# ---------------------------------------------------------------------------
def find_choch(bos_events: list[StructureEvent]) -> list[StructureEvent]:
    """Change-Of-Character = first BOS that opposes the previous BOS direction.

    Returns a list of StructureEvents (copies) with ``kind="CHoCH"``.
    """
    choch: list[StructureEvent] = []
    prev_dir: Direction | None = None
    for ev in bos_events:
        if prev_dir is not None and ev.direction != prev_dir:
            choch.append(StructureEvent(
                ev.idx, ev.timestamp, ev.price, ev.direction, ev.broken_swing, "CHoCH"
            ))
        prev_dir = ev.direction
    return choch


# ---------------------------------------------------------------------------
def summary(df: pd.DataFrame, lookback: int | None = None) -> dict:
    """One-shot run of all detectors with counts — handy for the backtest report."""
    swings = find_swings(df, lookback)
    bos = find_bos(df, swings)
    choch = find_choch(bos)
    return {
        "swings": swings,
        "bos": bos,
        "choch": choch,
        "counts": {
            "swing_high": sum(1 for s in swings if s.side == "high"),
            "swing_low":  sum(1 for s in swings if s.side == "low"),
            "bos_bull":   sum(1 for e in bos if e.direction == "bull"),
            "bos_bear":   sum(1 for e in bos if e.direction == "bear"),
            "choch":      len(choch),
        },
    }
