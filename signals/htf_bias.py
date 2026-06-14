"""Higher-Time-Frame bias inference.

Two complementary signals are blended:

1. **EMA slope** — bias is bullish while ``close > EMA(close, period)`` AND
   the EMA itself is rising; bearish when both flip; otherwise neutral.
2. **HTF market structure** — direction of the most recent BOS event on
   the HTF series (uses :mod:`engine.market_structure`).

The two signals must *agree* for a non-neutral bias to be returned. If they
disagree we fall back to neutral so we don't take counter-trend setups
without confluence.

The output of :func:`compute_bias_series` is a pandas Series indexed by the
LTF DataFrame with values in ``{"bull", "bear", None}``. Each LTF timestamp
gets the bias value from the *most recent* HTF bar that closed at or before
it (no look-ahead).
"""
from __future__ import annotations

from typing import Literal, Optional

import numpy as np
import pandas as pd

from engine.market_structure import find_bos, find_swings


Direction = Literal["bull", "bear"]
HTF_EMA_PERIOD = 20
HTF_SWING_LOOKBACK = 3   # smaller, since HTF bars are scarcer


def _ema_bias(df_htf: pd.DataFrame, period: int = HTF_EMA_PERIOD) -> pd.Series:
    ema = df_htf["close"].ewm(span=period, adjust=False).mean()
    slope = ema.diff()
    out = pd.Series(index=df_htf.index, dtype="object")
    above = df_htf["close"] > ema
    rising = slope > 0
    out[above & rising] = "bull"
    out[(~above) & (~rising)] = "bear"
    return out


def _structure_bias(df_htf: pd.DataFrame, lookback: int = HTF_SWING_LOOKBACK) -> pd.Series:
    swings = find_swings(df_htf, lookback=lookback)
    bos = find_bos(df_htf, swings)
    out = pd.Series(index=df_htf.index, dtype="object")
    last_dir: Optional[Direction] = None
    bos_iter = iter(bos)
    next_ev = next(bos_iter, None)
    for i, ts in enumerate(df_htf.index):
        while next_ev is not None and next_ev.idx <= i:
            last_dir = next_ev.direction
            next_ev = next(bos_iter, None)
        out.iloc[i] = last_dir
    return out


def compute_bias_series(df_ltf: pd.DataFrame, df_htf: pd.DataFrame,
                        require_agreement: bool = True) -> pd.Series:
    """Per-LTF-bar HTF bias as a ``{"bull","bear",None}`` Series.

    The HTF value applied to each LTF bar is the latest HTF bar whose
    timestamp is ``<=`` the LTF bar's timestamp (no look-ahead).
    """
    if df_htf.empty:
        return pd.Series([None] * len(df_ltf), index=df_ltf.index, dtype="object")

    ema = _ema_bias(df_htf)
    struct = _structure_bias(df_htf)

    if require_agreement:
        agreed = pd.Series(index=df_htf.index, dtype="object")
        mask = ema.notna() & struct.notna() & (ema == struct)
        agreed[mask] = ema[mask]
        htf_bias = agreed
    else:
        htf_bias = ema.where(ema.notna(), struct)

    # Align to LTF: forward-fill the last known HTF value
    mapped = htf_bias.reindex(df_ltf.index, method="ffill")
    return mapped


def htf_timeframe_for(ltf: str) -> str:
    """Default HTF for a given LTF (one level up)."""
    return {
        "1m":  "15m",
        "5m":  "1h",
        "15m": "4h",
        "30m": "4h",
        "1h":  "1d",
        "4h":  "1d",
        "1d":  "1d",
    }.get(ltf, "1d")
