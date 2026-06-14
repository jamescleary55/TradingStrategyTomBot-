"""Binance spot klines downloader.

Public endpoint, no API key required. Paginates the 1000-bar-per-request
limit so we can pull years of history. Returns the same
``open / high / low / close / volume`` DataFrame indexed by UTC
timestamp that the rest of the bot expects.

API ref: https://binance-docs.github.io/apidocs/spot/en/#kline-candlestick-data
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests

log = logging.getLogger(__name__)

REST = "https://api.binance.com/api/v3/klines"

INTERVAL_MS = {
    "1m":  60_000,
    "5m":  300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h":  3_600_000,
    "4h":  14_400_000,
    "1d":  86_400_000,
}


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def get_klines(symbol: str, interval: str = "1h",
               start_ms: Optional[int] = None,
               end_ms: Optional[int] = None,
               max_bars: int = 1_000_000) -> pd.DataFrame:
    """Pull klines for ``symbol`` (e.g. ``BTCUSDT``).

    Paginates 1000 at a time until either ``end_ms`` is reached or
    ``max_bars`` are collected. Slow polite delay between requests to
    stay under Binance's weight limits.
    """
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval {interval!r}; choose {list(INTERVAL_MS)}")
    step_ms = INTERVAL_MS[interval]
    if end_ms is None:
        end_ms = _now_ms()
    if start_ms is None:
        start_ms = end_ms - step_ms * 1000

    all_rows: list[list] = []
    cur = start_ms
    while cur < end_ms and len(all_rows) < max_bars:
        try:
            r = requests.get(REST, params={
                "symbol": symbol.upper(),
                "interval": interval,
                "startTime": cur,
                "endTime": end_ms,
                "limit": 1000,
            }, timeout=15)
            r.raise_for_status()
            chunk = r.json()
        except Exception as e:
            log.warning("Binance %s @ %s failed: %s; retrying in 2s", symbol, cur, e)
            time.sleep(2)
            continue
        if not chunk:
            break
        all_rows.extend(chunk)
        last_open = int(chunk[-1][0])
        if last_open + step_ms <= cur:
            # Defensive: API returned the same bar twice
            break
        cur = last_open + step_ms
        # Politeness: Binance allows 1200 weight/min, ~50ms/req is safe
        time.sleep(0.06)

    if not all_rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(all_rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "trades", "taker_base", "taker_quote", "ignore",
    ])
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
    df = df.astype(float)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df


def get_bars(symbol: str, timeframe: str = "1h", days: int = 365) -> pd.DataFrame:
    """Adapter compatible with data.loader's signature."""
    step_ms = INTERVAL_MS.get(timeframe)
    if step_ms is None:
        raise ValueError(f"Unsupported timeframe {timeframe!r}")
    end_ms = _now_ms()
    start_ms = end_ms - days * 86_400_000
    return get_klines(symbol, timeframe, start_ms, end_ms)
