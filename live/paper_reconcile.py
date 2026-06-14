"""Bridge broker executions → ``paper_trades.db``.

Companion to :mod:`live.reconcile` (which feeds the JSONL pipeline).
This one updates the SQLite trade journal so the operator's metrics
(``python -m live.paper_trades_db metrics``) reflect real broker
events.

Matching strategy:

- The broker reports executions tagged with ``order_id`` (and
  ``parent_order_id`` for bracket children).
- ``paper_trades.db`` rows have ``parent_order_id`` (the entry) and
  ``stop_child_order_id`` / ``target_child_order_id`` (the brackets,
  if the broker disclosed them at placement; many don't).
- For brokers that only report a flat order id per leg, we fall
  back to **side + symbol + time-since-entry** heuristics.

Idempotent: re-running won't double-apply fills. We track which
``execution_id`` values have been consumed via a side table.

CLI::

    python -m live.paper_reconcile --once
    python -m live.paper_reconcile --poll 30
"""
from __future__ import annotations

import argparse
import json
import logging
import signal as signal_mod
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.base import ExecutionEvent, get_adapter
from live.paper_trades_db import (
    DB_PATH, _connect, record_entry_fill, record_exit,
)

log = logging.getLogger("paper_reconcile")


# ---------------------------------------------------------------------------
def _ensure_consumed_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS consumed_executions (
        execution_id TEXT PRIMARY KEY,
        ts_consumed  TEXT NOT NULL,
        trade_id     INTEGER,
        kind         TEXT
    );
    """)


def _already_consumed(conn: sqlite3.Connection, execution_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM consumed_executions WHERE execution_id=?",
        (execution_id,)).fetchone()
    return row is not None


def _mark_consumed(conn: sqlite3.Connection, execution_id: str,
                   trade_id: Optional[int], kind: str) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO consumed_executions(
            execution_id, ts_consumed, trade_id, kind)
           VALUES (?,?,?,?)""",
        (execution_id,
         datetime.now(timezone.utc).isoformat(), trade_id, kind))


# ---------------------------------------------------------------------------
def _open_trades(conn: sqlite3.Connection) -> list[dict]:
    """Open trades = those with parent_order_id set and no exit yet."""
    rows = conn.execute(
        """SELECT id, parent_order_id, stop_child_order_id,
                  target_child_order_id, requested_side, requested_entry,
                  requested_stop, requested_target, actual_entry,
                  symbol, direction, ts_placed
           FROM paper_trades
           WHERE parent_order_id IS NOT NULL
             AND outcome = 'open'
           ORDER BY id""").fetchall()
    return [dict(r) for r in rows]


def _classify_event(ev: ExecutionEvent, trade: dict) -> str:
    """Map a broker execution to entry / stop / target / cancel / partial / unknown."""
    if ev.kind in ("cancel", "reject"):
        return ev.kind
    # If broker discloses the parent of the bracket child, use it
    if ev.parent_order_id:
        if str(trade["parent_order_id"]) == str(ev.parent_order_id):
            # this fill is a bracket child of the entry
            if trade.get("stop_child_order_id") and str(ev.order_id) == str(trade["stop_child_order_id"]):
                return "stop"
            if trade.get("target_child_order_id") and str(ev.order_id) == str(trade["target_child_order_id"]):
                return "target"
        # else: not ours
    # Direct parent match means this is the entry leg
    if str(ev.order_id) == str(trade["parent_order_id"]):
        # If entry hasn't been recorded yet, this is the entry fill.
        if trade.get("actual_entry") is None:
            return "entry"
        # Already had an entry; if same side again, it's anomalous
        return "partial" if ev.kind == "partial" else "unknown"

    # Fallback: side-based heuristic. The bracket children fire on the
    # OPPOSITE side of the entry. If actual_entry exists and this fill is
    # opposite-sided, decide stop vs target by price.
    if trade.get("actual_entry") is not None:
        opp = "Sell" if trade["requested_side"] == "Buy" else "Buy"
        if ev.side == opp:
            # Distance from requested_stop vs requested_target
            d_stop = abs(ev.price - trade["requested_stop"])
            d_target = abs(ev.price - trade["requested_target"])
            return "stop" if d_stop < d_target else "target"
    return "unknown"


# ---------------------------------------------------------------------------
def reconcile_once(broker_name: Optional[str] = None,
                   hours_back: int = 24) -> dict:
    adapter = get_adapter(broker_name)
    conn = _connect()
    _ensure_consumed_table(conn)

    trades = _open_trades(conn)
    if not trades:
        log.info("no open paper trades to reconcile")
        conn.close()
        return {"matched": 0, "skipped": 0, "trades_open": 0}

    # Time horizon for fetch
    since = trades[0].get("ts_placed") if trades else None
    if not since:
        since = datetime.now(timezone.utc).isoformat()

    try:
        events = adapter.list_executions(since_ts=since)
    except NotImplementedError:
        log.warning("adapter has no list_executions yet")
        conn.close()
        return {"matched": 0, "skipped": 0, "trades_open": len(trades)}
    except Exception as e:
        log.error("broker list_executions failed: %s", e)
        conn.close()
        return {"matched": 0, "skipped": 0, "trades_open": len(trades), "error": str(e)}

    matched = 0; skipped = 0
    # Index trades by parent_order_id for fast lookup
    trades_by_oid = {str(t["parent_order_id"]): t for t in trades}
    # Also by child order ids (when known)
    child_index: dict[str, dict] = {}
    for t in trades:
        if t.get("stop_child_order_id"):
            child_index[str(t["stop_child_order_id"])] = t
        if t.get("target_child_order_id"):
            child_index[str(t["target_child_order_id"])] = t

    for ev in events:
        if _already_consumed(conn, ev.execution_id):
            continue
        # Find candidate trade
        trade = trades_by_oid.get(str(ev.order_id))
        if trade is None:
            trade = trades_by_oid.get(str(ev.parent_order_id or ""))
        if trade is None:
            trade = child_index.get(str(ev.order_id))
        if trade is None:
            skipped += 1
            continue

        kind = _classify_event(ev, trade)
        if kind == "entry":
            partial = ev.kind == "partial" or ev.qty < (trade.get("actual_qty_filled") or 999999)
            try:
                record_entry_fill(
                    trade["id"],
                    actual_entry=ev.price,
                    actual_qty_filled=ev.qty,
                    ts_filled=ev.timestamp or None,
                    partial=bool(partial),
                )
                matched += 1
                _mark_consumed(conn, ev.execution_id, trade["id"], kind)
            except Exception as e:
                log.error("record_entry_fill failed: %s", e)
        elif kind in ("stop", "target"):
            try:
                record_exit(
                    trade["id"], outcome=kind, exit_kind="limit" if kind == "target" else "stop",
                    actual_exit_price=ev.price,
                    ts_exit=ev.timestamp or None,
                )
                matched += 1
                _mark_consumed(conn, ev.execution_id, trade["id"], kind)
            except Exception as e:
                log.error("record_exit failed: %s", e)
        elif kind == "partial":
            log.info("partial fill on trade %d — leaving in 'open'", trade["id"])
            _mark_consumed(conn, ev.execution_id, trade["id"], kind)
        elif kind == "cancel" or kind == "reject":
            log.info("cancel/reject on trade %d — skipping (handled elsewhere)",
                     trade["id"])
            _mark_consumed(conn, ev.execution_id, trade["id"], kind)
        else:
            skipped += 1

    conn.commit()
    conn.close()
    log.info("reconcile: matched=%d skipped=%d open_trades=%d events=%d",
             matched, skipped, len(trades), len(events))
    return {"matched": matched, "skipped": skipped,
            "trades_open": len(trades), "events": len(events)}


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker", default=None,
                        help="override BROKER env")
    parser.add_argument("--once", action="store_true",
                        help="run a single pass and exit")
    parser.add_argument("--poll", type=int, default=30,
                        help="seconds between polls when running as daemon")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.once:
        result = reconcile_once(args.broker)
        print(json.dumps(result, indent=2))
        return

    # Graceful shutdown
    stop = {"flag": False}
    def _sigterm(*_): stop["flag"] = True
    signal_mod.signal(signal_mod.SIGTERM, _sigterm)
    signal_mod.signal(signal_mod.SIGINT, _sigterm)

    log.info("polling broker every %ds (SIGTERM to stop)", args.poll)
    while not stop["flag"]:
        try:
            reconcile_once(args.broker)
        except Exception:
            log.exception("reconcile pass failed")
        # Sleep with prompt shutdown
        for _ in range(args.poll):
            if stop["flag"]:
                break
            time.sleep(1)
    log.info("shutdown clean")


if __name__ == "__main__":
    main()
