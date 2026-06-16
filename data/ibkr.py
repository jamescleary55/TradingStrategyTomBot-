"""
Interactive Brokers data loader (futures: NQ, ES, micros).

Pulls historical OHLCV for CME index futures through a running TWS or IB
Gateway via ib_insync.  Same return contract as the other loaders: a tz-naive
DatetimeIndex with Open/High/Low/Close/Volume, so it drops straight into
load_data() / run_backtest() / validation.py.

IBKR is the one source that gives BOTH deep intraday futures history AND live
execution, which is why we picked it for the NQ/ES focus.

PREREQUISITES (you must do these once -- see the checklist printed by
`python -m data.ibkr --help-setup`):
  1. An IBKR account (paper is fine to start).
  2. TWS or IB Gateway running and logged in.
  3. API enabled: Settings > API > "Enable ActiveX and Socket Clients".
  4. A CME real-time/historical market-data subscription (~$10-15/mo) so NQ/ES
     historical bars are permitted.

Connection is configured via env vars (defaults = TWS paper):
  IB_HOST (127.0.0.1)  IB_PORT (7497)  IB_CLIENT_ID (17)
  Common ports: 7497 TWS paper | 7496 TWS live | 4002 Gateway paper | 4001 Gateway live
"""
from __future__ import annotations

import os
import time
from typing import Optional

import pandas as pd

# Symbol -> (IBKR symbol, exchange).  E-mini + micro index futures on CME.
IB_CONTRACTS = {
    "NQ": ("NQ", "CME"), "ES": ("ES", "CME"),
    "MNQ": ("MNQ", "CME"), "MES": ("MES", "CME"),
}

# our interval -> (IBKR barSizeSetting, per-request chunk duration)
_BARSIZE = {
    "1m":  ("1 min",   "2 D"),
    "5m":  ("5 mins",  "1 W"),
    "15m": ("15 mins", "2 W"),
    "1h":  ("1 hour",  "1 M"),
    "1d":  ("1 day",   "1 Y"),
}

SETUP_HELP = """\
IBKR SETUP CHECKLIST
--------------------
1. Install + log in to TWS or IB Gateway (paper account is fine to start).
2. Enable the API:  TWS > File > Global Config > API > Settings
     [x] Enable ActiveX and Socket Clients
     [x] (optional) Read-Only API   <- safe while only pulling data
     Socket port:  7497 (TWS paper) / 4002 (Gateway paper)
     Add 127.0.0.1 to "Trusted IPs".
3. Subscribe to CME data:  Account > Market Data Subscriptions >
     "CME Real-Time (NP,L1)"  (~$10-15/mo).  Without it, historical NQ/ES
     requests return 'No market data permissions'.
4. Point the loader at your session:
     export IB_PORT=7497        # or 4002 for Gateway paper
   Then:  python -m data.ibkr --symbol NQ --interval 1h --start 2021-01-01
"""


def _ib_lib():
    """Use the maintained ib_async if present (py3.10+), else ib_insync."""
    try:
        import ib_async as lib
    except ImportError:
        import ib_insync as lib
    return lib


def _connect():
    lib = _ib_lib()
    ib = lib.IB()
    host = os.environ.get("IB_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_PORT", "7497"))
    cid = int(os.environ.get("IB_CLIENT_ID", "17"))
    ib.connect(host, port, clientId=cid, timeout=20)
    return ib


def load_ibkr(symbol: str, interval: str = "1h",
              start: str = "2021-01-01", end: Optional[str] = None,
              use_rth: bool = False) -> pd.DataFrame:
    """
    Download historical bars for a CME future from a running TWS/IB Gateway,
    paginating backwards until `start` is covered.
    """
    lib = _ib_lib()
    ContFuture, util = lib.ContFuture, lib.util

    sym = symbol.upper()
    ib_sym, exch = IB_CONTRACTS.get(sym, (sym, "CME"))
    if interval not in _BARSIZE:
        raise ValueError(f"IBKR loader supports {list(_BARSIZE)}, not '{interval}'.")
    bar_size, chunk = _BARSIZE[interval]

    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) if end else pd.Timestamp.utcnow().tz_localize(None)

    ib = _connect()
    try:
        contract = ContFuture(ib_sym, exch)          # continuous front-month
        ib.qualifyContracts(contract)

        frames = []
        cursor = end_ts
        while cursor > start_ts:
            bars = ib.reqHistoricalData(
                contract,
                endDateTime=cursor.strftime("%Y%m%d %H:%M:%S"),
                durationStr=chunk,
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=use_rth,
                formatDate=1,
            )
            if not bars:
                break
            df = util.df(bars)
            frames.append(df)
            earliest = pd.Timestamp(df["date"].iloc[0])
            if earliest <= start_ts or earliest >= cursor:
                break
            cursor = earliest
            time.sleep(0.4)                          # respect IBKR pacing limits
    finally:
        ib.disconnect()

    if not frames:
        raise RuntimeError(f"IBKR returned no data for {sym} {interval}. "
                           f"Check TWS/Gateway is running and CME data is subscribed.")

    out = pd.concat(frames).drop_duplicates(subset="date").sort_values("date")
    out["Date"] = pd.to_datetime(out["date"]).dt.tz_localize(None)
    out = out.set_index("Date")[["open", "high", "low", "close", "volume"]]
    out.columns = ["Open", "High", "Low", "Close", "Volume"]
    out = out[(out.index >= start_ts) & (out.index <= end_ts)]
    return out.astype(float)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="IBKR futures data loader")
    ap.add_argument("--symbol", default="NQ")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--help-setup", action="store_true")
    args = ap.parse_args()
    if args.help_setup:
        print(SETUP_HELP); raise SystemExit
    df = load_ibkr(args.symbol, args.interval, args.start, args.end)
    print(f"{args.symbol} {args.interval}: {len(df)} bars  "
          f"{df.index.min()} -> {df.index.max()}")
    md = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "market_data", f"{args.symbol}_ibkr_{args.interval}.csv")
    df.to_csv(md)
    print(f"saved -> {md}")
