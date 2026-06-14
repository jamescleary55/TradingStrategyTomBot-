"""Financial Modeling Prep historical OHLC.

Two relevant endpoints:

- ``/historical-price-full/{symbol}`` — daily OHLCV, multi-year
- ``/historical-chart/{interval}/{symbol}`` — intraday OHLCV, up to
  the limit of your tier

The bot reads ``FMP_API_KEY`` from ``.env``. Free tier supports a
limited set of US stocks and a daily refresh; paid tiers raise the
intraday & symbol limits.

API ref: https://site.financialmodelingprep.com/developer/docs
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import requests

log = logging.getLogger(__name__)

BASE = "https://financialmodelingprep.com/stable"
INTRADAY_INTERVALS = {"1m", "5m", "15m", "30m", "1h", "4h"}


def _key() -> Optional[str]:
    return os.getenv("FMP_API_KEY", "").strip() or None


def get_daily(symbol: str, years: int = 5) -> pd.DataFrame:
    """Daily OHLCV, indexed by UTC midnight."""
    api_key = _key()
    if not api_key:
        raise RuntimeError("Set FMP_API_KEY in .env")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(years * 365.25))
    r = requests.get(
        f"{BASE}/historical-price-eod/full",
        params={
            "symbol": symbol.upper(),
            "from": start.strftime("%Y-%m-%d"),
            "to": end.strftime("%Y-%m-%d"),
            "apikey": api_key,
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    # New /stable endpoint returns a flat list, not {"historical": [...]}
    rows = data if isinstance(data, list) else (data.get("historical") or [])
    if not rows:
        log.warning("FMP returned no historical rows for %s", symbol)
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["date"], utc=True)
    df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
    df = df.astype(float).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    return df


def get_intraday(symbol: str, interval: str = "1hour",
                 years: float = 1.0) -> pd.DataFrame:
    """Intraday OHLCV. ``interval`` must be FMP's format (1min, 5min, 15min,
    30min, 1hour, 4hour). For multi-year intraday FMP requires paid plans.
    """
    api_key = _key()
    if not api_key:
        raise RuntimeError("Set FMP_API_KEY in .env")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(years * 365.25))
    r = requests.get(
        f"{BASE}/historical-chart/{interval}",
        params={
            "symbol": symbol.upper(),
            "from": start.strftime("%Y-%m-%d"),
            "to": end.strftime("%Y-%m-%d"),
            "apikey": api_key,
        },
        timeout=30,
    )
    r.raise_for_status()
    rows = r.json() or []
    if not rows:
        log.warning("FMP intraday returned 0 rows for %s %s", symbol, interval)
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["date"], utc=True)
    df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
    df = df.astype(float).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    return df


# ---------------------------------------------------------------------------
def _to_fmp_interval(tf: str) -> str:
    return {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
            "1h": "1hour", "4h": "4hour"}.get(tf, "1hour")


def get_bars(symbol: str, timeframe: str = "1d", days: int = 365) -> pd.DataFrame:
    """Adapter shaped like data.loader expects."""
    years = days / 365.25
    if timeframe == "1d":
        return get_daily(symbol, years=max(1, int(years) + 1))
    if timeframe in INTRADAY_INTERVALS:
        return get_intraday(symbol, _to_fmp_interval(timeframe), years=years)
    raise ValueError(f"FMP feed: unsupported timeframe {timeframe!r}")
