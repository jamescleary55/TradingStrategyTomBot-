"""Read-only broker probe.

The smallest possible "is my broker integration alive?" check. Runs
in seconds, requires only the credentials already in ``.env``.

Output:

    $ python scripts/probe_broker.py
    [broker  ] topstepx
    [auth    ] ok
    [account ] id=12345  cash=$50000.00  equity=$50123.45
    [positions] 1 open
        MES  Buy x1  avg=4521.25  unreal=+25.50
    [recent fills] 3 in last 24h
        2026-06-12T14:33:01 MES Buy 1 @ 4520.75
        2026-06-12T15:01:22 MES Sell 1 @ 4521.50
        2026-06-12T15:02:11 MES Sell 1 @ 4528.25
    [orders ] 0 open

If any line shows "ERROR", the broker integration is the problem —
not the strategy, not the simulator, not the data feed.

This script writes NOTHING. No orders. No state changes. Read-only.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.base import BrokerAdapter, get_adapter

log = logging.getLogger(__name__)


def _fmt(value, prefix=""):
    if value is None or value == "":
        return f"{prefix}—"
    return f"{prefix}{value}"


def probe(broker_name: str, hours_back: int = 24,
          show_raw_orders: bool = False) -> int:
    """Returns exit code: 0 = ok, 1 = at least one read failed."""
    print(f"[broker  ] {broker_name}")
    try:
        adapter: BrokerAdapter = get_adapter(broker_name)
    except Exception as e:
        print(f"[broker  ] ERROR: cannot resolve adapter: {e}")
        return 1

    # Auth — implicit in any read. snapshot() is the lightest probe.
    try:
        snap = adapter.snapshot()
        print(f"[auth    ] ok")
        print(f"[account ] id={snap.account_id}  "
              f"cash=${snap.cash:,.2f}  equity=${snap.equity:,.2f}")
    except NotImplementedError:
        print(f"[auth    ] SKIP (adapter has no snapshot() yet)")
        return 1
    except Exception as e:
        print(f"[auth    ] ERROR: {e}")
        return 1

    # Positions
    try:
        if snap.positions:
            print(f"[positions] {len(snap.positions)} open")
            for p in snap.positions:
                unreal = (f"  unreal={p.unrealised_pnl:+.2f}"
                          if p.unrealised_pnl is not None else "")
                print(f"    {p.symbol}  {p.side} x{p.qty}  avg={p.avg_entry:.2f}{unreal}")
        else:
            print(f"[positions] 0 open")
    except Exception as e:
        print(f"[positions] ERROR: {e}")
        return 1

    # Recent fills
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()
        fills = adapter.list_executions(since_ts=since)
        print(f"[fills   ] {len(fills)} in last {hours_back}h")
        for f in fills[-20:]:    # show last 20 max
            print(f"    {f.timestamp[:19]}  {f.symbol}  "
                  f"{f.side} {f.qty} @ {f.price:.2f}  ({f.kind})")
    except NotImplementedError:
        print(f"[fills   ] SKIP (adapter has no list_executions() yet)")
    except Exception as e:
        print(f"[fills   ] ERROR: {e}")
        return 1

    # Open orders (best effort — only adapters that expose list_orders)
    if hasattr(adapter, "list_orders"):
        try:
            orders = adapter.list_orders(open_only=True)
            print(f"[orders ] {len(orders)} open")
            if show_raw_orders:
                import json
                for o in orders[:5]:
                    print(f"    {json.dumps(o, default=str)[:200]}")
        except Exception as e:
            print(f"[orders ] ERROR: {e}")
            return 1

    print(f"[result  ] ok")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Read-only broker probe.")
    parser.add_argument("--broker", default=os.getenv("BROKER", "tradovate"),
                        help="tradovate / topstepx / dryrun (defaults to BROKER env)")
    parser.add_argument("--hours", type=int, default=24,
                        help="how far back to fetch fills")
    parser.add_argument("--raw-orders", action="store_true",
                        help="print raw order rows (truncated)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    sys.exit(probe(args.broker, args.hours, args.raw_orders))


if __name__ == "__main__":
    main()
