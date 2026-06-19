"""Bulk historical OHLCV downloader.

Pulls multi-year history from Binance and / or Financial Modeling Prep
into per-symbol CSV files under ``~/.ict-bot/historical/``.

Usage::

    python -m scripts.bulk_download --source binance --years 5
    python -m scripts.bulk_download --source fmp --years 5
    python -m scripts.bulk_download --source both --years 2 --interval 1h

CSV columns: timestamp, open, high, low, close, volume
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table

console = Console()
log = logging.getLogger("bulk_download")

OUT_DIR = Path.home() / ".ict-bot" / "historical"
OUT_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_BINANCE_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
DEFAULT_FMP_SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "TSLA", "NVDA"]
DEFAULT_YF_SYMBOLS = ["NQ", "ES", "CL", "GC"]   # index/commodity futures (mapped to =F)


def _save_csv(df, source: str, symbol: str, interval: str) -> Path:
    safe = symbol.replace("/", "_")
    out = OUT_DIR / f"{source}_{safe}_{interval}.csv"
    df.to_csv(out, index_label="timestamp")
    return out


def _summary_table(rows: list[dict]) -> Table:
    tbl = Table(title=f"Downloaded files → {OUT_DIR}", header_style="bold")
    tbl.add_column("Source"); tbl.add_column("Symbol")
    tbl.add_column("Interval"); tbl.add_column("Rows", justify="right")
    tbl.add_column("From"); tbl.add_column("To"); tbl.add_column("Path", style="dim")
    for r in rows:
        tbl.add_row(r["source"], r["symbol"], r["interval"], str(r["rows"]),
                    r["from"], r["to"], r["path"])
    return tbl


def main():
    parser = argparse.ArgumentParser(description="Bulk OHLCV downloader")
    parser.add_argument("--source", default="binance",
                        choices=["binance", "fmp", "yfinance", "both", "all"],
                        help="'both' = binance+fmp (legacy); 'all' = +yfinance")
    parser.add_argument("--interval", default="1h",
                        help="1m / 5m / 15m / 30m / 1h / 4h / 1d")
    parser.add_argument("--years", type=float, default=2.0)
    parser.add_argument("--binance-symbols", default=",".join(DEFAULT_BINANCE_SYMBOLS))
    parser.add_argument("--fmp-symbols", default=",".join(DEFAULT_FMP_SYMBOLS))
    parser.add_argument("--yf-symbols", default=",".join(DEFAULT_YF_SYMBOLS))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    days = int(args.years * 365.25)
    rows: list[dict] = []

    if args.source in ("binance", "both", "all"):
        from data.binance_feed import get_klines, _now_ms, INTERVAL_MS
        if args.interval not in INTERVAL_MS:
            log.error("Binance interval %s not supported", args.interval); sys.exit(1)
        end_ms = _now_ms()
        start_ms = end_ms - days * 86_400_000
        for sym in [s.strip().upper() for s in args.binance_symbols.split(",") if s.strip()]:
            log.info("[Binance] %s %s × %.1fy", sym, args.interval, args.years)
            df = get_klines(sym, args.interval, start_ms, end_ms)
            if df.empty:
                log.warning("[Binance] %s returned no rows", sym); continue
            path = _save_csv(df, "binance", sym, args.interval)
            rows.append({
                "source": "binance", "symbol": sym, "interval": args.interval,
                "rows": len(df), "from": str(df.index[0]), "to": str(df.index[-1]),
                "path": str(path),
            })
            log.info("[Binance] %s → %d rows → %s", sym, len(df), path)

    if args.source in ("yfinance", "all"):
        from data.yfinance_feed import get_bars as yf_get_bars, SYMBOL_MAP
        for sym in [s.strip().upper() for s in args.yf_symbols.split(",") if s.strip()]:
            if sym not in SYMBOL_MAP:
                log.warning("[yfinance] %s has no Yahoo mapping — skipping", sym); continue
            log.info("[yfinance] %s %s × %.1fy (Yahoo caps short intervals)", sym, args.interval, args.years)
            try:
                df = yf_get_bars(sym, args.interval, days=days)
            except Exception as e:
                log.warning("[yfinance] %s failed: %s", sym, e); continue
            if df.empty:
                log.warning("[yfinance] %s returned no rows", sym); continue
            path = _save_csv(df, "yfinance", sym, args.interval)
            rows.append({
                "source": "yfinance", "symbol": sym, "interval": args.interval,
                "rows": len(df), "from": str(df.index[0]), "to": str(df.index[-1]),
                "path": str(path),
            })
            log.info("[yfinance] %s → %d rows → %s", sym, len(df), path)

    if args.source in ("fmp", "both", "all"):
        import os
        if not os.getenv("FMP_API_KEY"):
            log.error("FMP_API_KEY not set in .env — skipping FMP")
        else:
            from data.fmp_feed import get_bars as fmp_get_bars
            for sym in [s.strip().upper() for s in args.fmp_symbols.split(",") if s.strip()]:
                log.info("[FMP] %s %s × %.1fy", sym, args.interval, args.years)
                try:
                    df = fmp_get_bars(sym, args.interval, days=days)
                except Exception as e:
                    log.warning("[FMP] %s failed: %s", sym, e); continue
                if df.empty:
                    log.warning("[FMP] %s returned no rows", sym); continue
                path = _save_csv(df, "fmp", sym, args.interval)
                rows.append({
                    "source": "fmp", "symbol": sym, "interval": args.interval,
                    "rows": len(df), "from": str(df.index[0]), "to": str(df.index[-1]),
                    "path": str(path),
                })
                log.info("[FMP] %s → %d rows → %s", sym, len(df), path)

    if rows:
        console.print(_summary_table(rows))
    else:
        console.print("[red]Nothing downloaded.[/red]")


if __name__ == "__main__":
    main()
