"""Free historical bars via yfinance (no signup, no creds).

Maps our internal symbols (NQ, ES, CL, GC) to Yahoo's continuous futures
tickers and returns the same DataFrame contract as ``tradovate_feed``:
UTC-indexed, columns = ``open / high / low / close / volume``.

yfinance limits on intraday data:

    interval=1m   → last 7 days
    interval=5m   → last 60 days
    interval=15m  → last 60 days
    interval=30m  → last 60 days
    interval=60m  → last 730 days
    interval=1d   → ~all history

If the requested ``days`` exceeds the cap we clip and warn.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

log = logging.getLogger(__name__)

# Map our internal symbol → Yahoo continuous-futures ticker
SYMBOL_MAP = {
    "NQ":  "NQ=F",
    "MNQ": "NQ=F",   # micro doesn't exist on Yahoo; use big-NQ as a proxy
    "ES":  "ES=F",
    "MES": "ES=F",
    "CL":  "CL=F",
    "MCL": "CL=F",   # micro shares price feed with full
    "GC":  "GC=F",
    "MGC": "GC=F",
}

TIMEFRAME_MAP = {
    "1m":  ("1m",  7),
    "5m":  ("5m",  60),
    "15m": ("15m", 60),
    "30m": ("30m", 60),
    "1h":  ("60m", 730),
    "4h":  ("60m", 730),   # resample 1h → 4h
    "1d":  ("1d",  10_000),
}


def get_bars(symbol: str, timeframe: str, days: int = 30) -> pd.DataFrame:
    import yfinance as yf  # lazy import — keeps tradovate-only users light

    if symbol not in SYMBOL_MAP:
        raise ValueError(f"yfinance source has no mapping for symbol '{symbol}'")
    if timeframe not in TIMEFRAME_MAP:
        raise ValueError(f"yfinance source has no mapping for timeframe '{timeframe}'")

    yf_symbol = SYMBOL_MAP[symbol]
    yf_interval, max_days = TIMEFRAME_MAP[timeframe]
    if days > max_days:
        log.warning("yfinance limits %s to %d days; clipping (requested %d)",
                    yf_interval, max_days, days)
        days = max_days

    # For ranges close to the per-interval cap, Yahoo rejects explicit
    # start/end dates but accepts the `period` parameter. Pick the
    # smallest valid period bucket that covers what we want.
    period_buckets = [
        (7,    "7d"),
        (30,   "1mo"),
        (60,   "2mo"),
        (90,   "3mo"),
        (180,  "6mo"),
        (365,  "1y"),
        (730,  "2y"),
    ]
    use_period = None
    for cap, label in period_buckets:
        if days <= cap:
            use_period = label
            break
    if use_period is None:
        use_period = "2y"

    # Retry on transient yfinance failures (TypeError, ConnectionError, etc.)
    def _try(period_or_dates: str):
        attempts = 3
        last_err = None
        for i in range(attempts):
            try:
                if period_or_dates == "period":
                    return yf.download(yf_symbol, period=use_period, interval=yf_interval,
                                       auto_adjust=False, progress=False, prepost=True,
                                       threads=False)
                else:
                    end = datetime.now(timezone.utc)
                    start = end - timedelta(days=days)
                    return yf.download(yf_symbol,
                                       start=start.strftime("%Y-%m-%d"),
                                       end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
                                       interval=yf_interval,
                                       auto_adjust=False, progress=False, prepost=True,
                                       threads=False)
            except Exception as e:
                last_err = e
                log.warning("yfinance %s attempt %d/%d failed: %s",
                            yf_symbol, i + 1, attempts, e)
        log.error("yfinance %s gave up: %s", yf_symbol, last_err)
        return None

    if days > 60 or yf_interval == "60m":
        raw = _try("period")
        if raw is None or len(raw) == 0:
            raw = _try("dates")    # fallback path
    else:
        raw = _try("dates")
        if raw is None or len(raw) == 0:
            raw = _try("period")
    if raw is None:
        raw = pd.DataFrame()
    if raw is None or raw.empty:
        log.error("yfinance returned no rows for %s %s", yf_symbol, yf_interval)
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    # yfinance can return a MultiIndex when downloading single tickers — flatten.
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [c[0] for c in raw.columns]

    df = raw.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })
    # Defensive: yfinance sometimes returns duplicate column labels. Keep first.
    df = df.loc[:, ~df.columns.duplicated()]
    df = df[["open", "high", "low", "close", "volume"]].copy()

    # Normalise index to UTC
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    df.index.name = "timestamp"

    # 4h resample (yfinance has no native 4h)
    if timeframe == "4h":
        df = df.resample("4h").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna()

    return df
