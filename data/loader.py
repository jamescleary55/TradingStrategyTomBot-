"""Dispatcher for OHLCV sources.

    load_bars(symbol, timeframe, days, source="auto")

``source="auto"`` picks the first source that yields rows:
``tradovate`` if creds are present, otherwise ``yfinance``, otherwise the
synthetic fallback (last resort, only for offline harness checks).
"""
from __future__ import annotations

import logging
from typing import Literal

import pandas as pd

from config import has_tradovate_credentials
from data import local_csv, tradovate_feed, yfinance_feed

log = logging.getLogger(__name__)

Source = Literal["auto", "tradovate", "yfinance", "synthetic", "local"]


def load_bars(symbol: str, timeframe: str, days: int = 30,
              source: Source = "auto") -> pd.DataFrame:
    if source == "local":
        return local_csv.get_bars(symbol, timeframe, days=days)

    if source == "auto":
        if has_tradovate_credentials():
            source = "tradovate"
        else:
            source = "yfinance"

    if source == "tradovate":
        if not has_tradovate_credentials():
            log.warning("source=tradovate requested but no creds; falling back to yfinance")
            source = "yfinance"
        else:
            return tradovate_feed.get_bars(symbol, timeframe, days=days)

    if source == "yfinance":
        try:
            df = yfinance_feed.get_bars(symbol, timeframe, days=days)
            if not df.empty:
                return df
            log.error("yfinance returned empty for %s %s — NOT falling back to synthetic", symbol, timeframe)
            return df  # empty DataFrame — caller will see and skip
        except Exception as e:
            log.error("yfinance error: %s — NOT falling back to synthetic", e)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    if source == "synthetic":
        return tradovate_feed._synthetic_bars(symbol, timeframe, days)

    raise ValueError(f"Unknown source: {source}")
