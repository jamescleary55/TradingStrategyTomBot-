"""
Data loading utilities.

Supports two sources:
  * Binance public REST API (spot klines)  -> crypto (BTCUSDT, ETHUSDT)
  * Yahoo Finance via yfinance            -> crypto, FX (EURUSD), equities (SPY)

Every loader returns a pandas DataFrame indexed by a tz-naive DatetimeIndex
with the columns expected by Backtesting.py:  Open, High, Low, Close, Volume.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import pandas as pd
import requests

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import SYMBOL_MAP, DEFAULT_SOURCE


BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

# Binance interval -> milliseconds (used for pagination).
_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "12h": 43_200_000, "1d": 86_400_000, "1w": 604_800_000,
}


def _to_ms(ts: str) -> int:
    return int(pd.Timestamp(ts, tz="UTC").timestamp() * 1000)


def load_binance(symbol: str, interval: str = "1h",
                 start: str = "2023-01-01", end: Optional[str] = None) -> pd.DataFrame:
    """Download spot klines from Binance, paginating past the 1000-row limit."""
    end_ms = _to_ms(end) if end else int(time.time() * 1000)
    start_ms = _to_ms(start)
    step = _INTERVAL_MS.get(interval, 3_600_000)

    rows = []
    cursor = start_ms
    while cursor < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1000,
        }
        resp = requests.get(BINANCE_KLINES, params=params, timeout=30)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        cursor = batch[-1][0] + step
        if len(batch) < 1000:
            break
        time.sleep(0.25)  # be polite to the API

    if not rows:
        raise RuntimeError(f"Binance returned no data for {symbol} {interval}")

    df = pd.DataFrame(rows, columns=[
        "open_time", "Open", "High", "Low", "Close", "Volume",
        "close_time", "qav", "trades", "tbav", "tqav", "ignore",
    ])
    df["Date"] = pd.to_datetime(df["open_time"], unit="ms")
    df = df.set_index("Date")[["Open", "High", "Low", "Close", "Volume"]]
    return df.astype(float)


def load_yfinance(symbol: str, interval: str = "1h",
                  start: str = "2023-01-01", end: Optional[str] = None) -> pd.DataFrame:
    """Download OHLCV from Yahoo Finance."""
    import yfinance as yf

    df = yf.download(symbol, interval=interval, start=start, end=end,
                     auto_adjust=True, progress=False)
    if df.empty:
        raise RuntimeError(f"yfinance returned no data for {symbol} {interval}")

    # yfinance can return a MultiIndex column frame for single tickers.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.rename(columns=str.title)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.astype(float)


# FMP's legacy /api/v3 endpoints were retired for keys issued after
# 2025-08-31; the current API lives under /stable.
FMP_BASE = "https://financialmodelingprep.com/stable"

# Our interval names -> FMP intraday interval names.
_FMP_INTERVAL = {
    "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1hour", "4h": "4hour",
}


def load_fmp(symbol: str, interval: str = "1h",
             start: str = "2023-01-01", end: Optional[str] = None,
             api_key: Optional[str] = None) -> pd.DataFrame:
    """
    Download OHLCV from Financial Modeling Prep (current /stable API).

    Daily ('1d') uses historical-price-eod/full; intraday uses
    historical-chart/<interval>.  Pass api_key, or set the FMP_API_KEY env var.

    Note: on FMP's free tier only daily EOD data is available — intraday
    endpoints return a "Restricted Endpoint" error and need a paid plan.
    """
    api_key = api_key or os.environ.get("FMP_API_KEY")
    if not api_key:
        raise ValueError(
            "No FMP API key. Pass api_key=... or set FMP_API_KEY in the env.")

    params = {"apikey": api_key, "symbol": symbol, "from": start}
    if end:
        params["to"] = end

    if interval in ("1d", "1day", "daily"):
        url = f"{FMP_BASE}/historical-price-eod/full"
    else:
        fmp_int = _FMP_INTERVAL.get(interval)
        if fmp_int is None:
            raise ValueError(f"FMP does not support interval '{interval}'. "
                             f"Use one of {list(_FMP_INTERVAL)} or '1d'.")
        url = f"{FMP_BASE}/historical-chart/{fmp_int}"

    resp = requests.get(url, params=params, timeout=30)
    is_intraday = interval not in ("1d", "1day", "daily")

    # Plan/auth problems can come back either as a JSON object with a message
    # or as a bare 402/403 with plain text — surface a useful error for both.
    body = resp.text or ""
    if resp.status_code in (401, 402, 403) or "Restricted" in body:
        if is_intraday:
            raise RuntimeError(
                f"FMP intraday ({interval}) is not available on this plan — "
                f"the free tier is daily-only. Use --interval 1d, or Binance "
                f"for intraday crypto. (HTTP {resp.status_code}: {body[:160]})")
        raise RuntimeError(
            f"FMP denied {symbol} {interval} "
            f"(HTTP {resp.status_code}: {body[:160]})")

    try:
        payload = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise RuntimeError(f"FMP returned non-JSON for {symbol} {interval}: "
                           f"{body[:200]}")

    if isinstance(payload, dict):
        msg = payload.get("Error Message") or payload.get("message") or str(payload)
        raise RuntimeError(f"FMP error for {symbol} {interval}: {msg}")

    resp.raise_for_status()
    if not payload:
        raise RuntimeError(f"FMP returned no data for {symbol} {interval}")

    df = pd.DataFrame(payload)
    df = df.rename(columns=str.title)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date")[["Open", "High", "Low", "Close", "Volume"]]
    df = df.sort_index().astype(float)
    return df


def load_data(symbol: str, source: Optional[str] = None, interval: str = "1h",
              start: str = "2023-01-01", end: Optional[str] = None) -> pd.DataFrame:
    """
    Unified entry point.

    Parameters
    ----------
    symbol : one of BTCUSDT, ETHUSDT, EURUSD, SPY  (or any raw ticker)
    source : "binance" | "yfinance" | None (auto)
    """
    symbol = symbol.upper()
    source = source or DEFAULT_SOURCE.get(symbol, "yfinance")

    mapping = SYMBOL_MAP.get(symbol, {})
    if source == "binance":
        ticker = mapping.get("binance", symbol)
        if ticker is None:
            raise ValueError(f"{symbol} is not available on Binance; use yfinance.")
        df = load_binance(ticker, interval, start, end)
    elif source == "fmp":
        ticker = mapping.get("fmp", symbol)
        df = load_fmp(ticker, interval, start, end)
    elif source == "ibkr":
        from data.ibkr import load_ibkr
        ticker = mapping.get("ibkr", symbol)
        df = load_ibkr(ticker, interval, start, end)
    else:
        ticker = mapping.get("yfinance", symbol)
        df = load_yfinance(ticker, interval, start, end)

    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df


if __name__ == "__main__":
    # Smoke test.
    data = load_data("BTCUSDT", interval="1h", start="2024-01-01", end="2024-02-01")
    print(data.tail())
    print(f"Loaded {len(data)} rows.")
