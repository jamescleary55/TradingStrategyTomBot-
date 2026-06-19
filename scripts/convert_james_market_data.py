"""One-off: convert James's market_data CSVs to this bot's local_csv contract.

James's files use capitalized OHLCV columns, a tz-naive Datetime, and mixed
filename ordering (NQ_15m_yfinance.csv, BTCUSDT_binance_1h.csv, ...). This bot's
``data.local_csv`` expects lowercase columns, a UTC ``timestamp`` column, and the
name pattern ``<source>_<symbol>_<timeframe>.csv``.

Run once after `git checkout origin/james-ibkr-original -- market_data/`:
    python scripts/convert_james_market_data.py
Idempotent: files already in the target shape/name are left alone.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

MD = Path(__file__).resolve().parent.parent / "market_data"
SOURCES = {"yfinance", "binance", "fmp"}
TIMEFRAMES = {"5m", "15m", "1h", "4h", "1d"}
OHLCV = ["open", "high", "low", "close", "volume"]


def parse_name(stem: str) -> tuple[str, str, str] | None:
    """Return (source, symbol, timeframe) from a James filename stem, or None."""
    tokens = stem.split("_")
    source = next((t for t in tokens if t in SOURCES), None)
    tf = next((t for t in tokens if t in TIMEFRAMES), None)
    if not source or not tf:
        return None
    rest = [t for t in tokens if t not in SOURCES and t not in TIMEFRAMES]
    if not rest:
        return None
    symbol = rest[0]
    if "long" in tokens:          # disambiguate the extended-history dumps
        tf = f"{tf}long"
    return source, symbol.upper(), tf


def convert(path: Path) -> str:
    df = pd.read_csv(path)
    # Identify the timestamp column (first column / Datetime / Date).
    ts_col = next((c for c in df.columns if c.lower() in ("datetime", "date", "timestamp")),
                  df.columns[0])
    rename_map = {c: c.lower() for c in df.columns}
    rename_map[ts_col] = "timestamp"          # must win over the lowercase map
    df = df.rename(columns=rename_map)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp"])
    cols = [c for c in OHLCV if c in df.columns]
    if "close" not in cols:
        return f"skip (no OHLCV): {path.name}"
    out = df[["timestamp", *cols]].sort_values("timestamp").drop_duplicates("timestamp")

    parsed = parse_name(path.stem)
    if not parsed:
        return f"skip (unparseable name): {path.name}"
    source, symbol, tf = parsed
    target = MD / f"{source}_{symbol}_{tf}.csv"
    out.to_csv(target, index=False)
    if target != path:
        path.unlink()
    return f"{path.name}  ->  {target.name}  ({len(out)} rows)"


def main():
    for path in sorted(MD.glob("*.csv")):
        # Already in target shape? (lowercase header + matching name)
        print(convert(path))


if __name__ == "__main__":
    main()
