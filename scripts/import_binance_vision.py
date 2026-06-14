"""Import Binance Vision monthly OHLC zips into ~/.ict-bot/historical/.

Binance Vision (https://data.binance.vision/) ships historical klines as
monthly ZIP files containing a single header-less CSV with columns:

    open_time, open, high, low, close, volume,
    close_time, qav, num_trades, taker_buy_base, taker_buy_quote, ignore

File names look like ``BTCUSDT-4h-2024-06.zip``.

This script groups by ``(SYMBOL, INTERVAL)``, concatenates every month,
sorts, deduplicates and saves a single CSV per group in the same
``~/.ict-bot/historical/binance_<SYMBOL>_<interval>.csv`` format the
local-csv loader already understands.

Usage::

    python -m scripts.import_binance_vision "/path/to/folder"
    python -m scripts.import_binance_vision "/path/to/folder" --out-dir /tmp/foo
"""
from __future__ import annotations

import argparse
import io
import logging
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

console = Console()
log = logging.getLogger("import_binance_vision")

DEFAULT_OUT = Path.home() / ".ict-bot" / "historical"
FN_PATTERN = re.compile(r"^([A-Z0-9]+)-([0-9a-z]+)-(\d{4})-(\d{2})\.zip$")

COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "qav", "num_trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]


def _read_zip(zip_path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if not names:
            return pd.DataFrame()
        with zf.open(names[0]) as f:
            df = pd.read_csv(f, header=None, names=COLS)
    if df.empty:
        return df
    # Detect unit on THIS file's first row and convert to UTC timestamps.
    sample = float(df["open_time"].iloc[0])
    if sample > 1e14:
        unit = "us"
    elif sample > 1e11:
        unit = "ms"
    else:
        unit = "s"
    df["timestamp"] = pd.to_datetime(df["open_time"], unit=unit, utc=True)
    return df


def import_folder(src: Path, out_dir: Path) -> list[dict]:
    by_key: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for p in sorted(src.iterdir()):
        m = FN_PATTERN.match(p.name)
        if not m:
            continue
        sym, interval, _, _ = m.groups()
        by_key[(sym.upper(), interval.lower())].append(p)

    if not by_key:
        log.error("No Binance Vision zips matched the expected naming under %s", src)
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    summary: list[dict] = []

    for (sym, interval), paths in by_key.items():
        log.info("[%s %s] merging %d month(s)", sym, interval, len(paths))
        frames = []
        for p in paths:
            try:
                frames.append(_read_zip(p))
            except Exception as e:
                log.warning("[%s %s] %s failed: %s", sym, interval, p.name, e)
        if not frames:
            continue
        df = pd.concat(frames, ignore_index=True)
        df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
        df = df.astype(float).sort_index()
        df = df[~df.index.duplicated(keep="first")]

        out_path = out_dir / f"binance_{sym}_{interval}.csv"
        df.to_csv(out_path, index_label="timestamp")
        summary.append({
            "symbol": sym,
            "interval": interval,
            "rows": len(df),
            "from": str(df.index[0]),
            "to": str(df.index[-1]),
            "path": str(out_path),
        })
    return summary


def main():
    parser = argparse.ArgumentParser(description="Import Binance Vision monthly zips")
    parser.add_argument("src", help="Path to folder of Binance Vision zip files")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    src = Path(args.src).expanduser()
    if not src.is_dir():
        log.error("Not a directory: %s", src); sys.exit(1)

    summary = import_folder(src, Path(args.out_dir).expanduser())
    if not summary:
        console.print("[red]No files imported.[/red]"); sys.exit(1)

    tbl = Table(title=f"Imported → {args.out_dir}", header_style="bold")
    tbl.add_column("Symbol"); tbl.add_column("Interval")
    tbl.add_column("Rows", justify="right")
    tbl.add_column("From"); tbl.add_column("To"); tbl.add_column("Path", style="dim")
    for r in summary:
        tbl.add_row(r["symbol"], r["interval"], str(r["rows"]),
                    r["from"], r["to"], r["path"])
    console.print(tbl)


if __name__ == "__main__":
    main()
