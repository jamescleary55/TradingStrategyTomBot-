"""Local CSV historical feed.

Reads OHLCV CSVs from two locations, in order:

1. ``~/.ict-bot/historical/`` — user dumps from ``scripts/bulk_download.py``
2. ``<repo>/market_data/``    — checked-in history (e.g. James's 2-yr CSVs)

File names follow the pattern ``<source>_<symbol>_<timeframe>.csv`` (e.g.
``binance_BTCUSDT_1h.csv``). Used for backtests over downloaded crypto / equity
history without re-hitting external APIs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

HISTORICAL_DIR = Path.home() / ".ict-bot" / "historical"
REPO_MARKET_DATA = Path(__file__).resolve().parent.parent / "market_data"
SEARCH_DIRS = (HISTORICAL_DIR, REPO_MARKET_DATA)


def _candidate_paths(symbol: str, timeframe: str) -> list[Path]:
    """All files that could plausibly hold this symbol+timeframe."""
    sym = symbol.upper().replace("/", "_")
    paths = []
    for d in SEARCH_DIRS:
        if d.exists():
            paths.extend(d.glob(f"*_{sym}_{timeframe}.csv"))
    return paths


def get_bars(symbol: str, timeframe: str = "1h", days: int = 730) -> pd.DataFrame:
    paths = _candidate_paths(symbol, timeframe)
    if not paths:
        log.warning("No local CSV for %s %s in %s", symbol, timeframe, HISTORICAL_DIR)
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    # Prefer binance over fmp when both exist (binance is the higher-granularity source)
    paths.sort(key=lambda p: 0 if "binance" in p.name else 1)
    path = paths[0]

    df = pd.read_csv(path)
    if "timestamp" not in df.columns:
        # Fall back to first column as the timestamp
        df = df.rename(columns={df.columns[0]: "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp")
    cols = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    df = df[cols].astype(float).sort_index()
    df = df[~df.index.duplicated(keep="first")]

    if days and days > 0:
        cutoff = df.index[-1] - pd.Timedelta(days=days)
        df = df[df.index >= cutoff]

    return df
