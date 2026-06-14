"""Paper-trade journal SQLite store.

One row per **trade attempt** (signal → order → fill → exit). Links
back to ``signals.db.signal_id`` so we never duplicate signal
metadata.

Lifecycle:
1. ``insert_attempt(signal_id, plan, broker, ...)`` — called when the
   bot decides to place an order (or simulates placing one).
2. ``record_placement(...)`` — broker responded with an order id.
3. ``record_fill(...)`` — broker reported an execution event for
   this order (or one of its bracket children).
4. ``record_exit(...)`` — stop / target / manual exit recorded.
5. ``compute_metrics()`` — slippage, R-multiple, duration computed
   from the recorded prices.

Schema documents itself — every column has a comment in the CREATE.
No separate markdown.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

log = logging.getLogger(__name__)

DB_PATH = Path.home() / ".ict-bot" / "paper_trades.db"

SCHEMA = """
-- One row per (signal → trade attempt → execution → outcome).
-- A signal can have at most one attempt rows except for retries / replacements.
CREATE TABLE IF NOT EXISTS paper_trades (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id             TEXT NOT NULL,         -- FK signals.db::signals.signal_id

    -- ----- signal info (denormalised for offline analysis) -----
    ts_signal             TEXT NOT NULL,         -- when bot detected the setup
    symbol                TEXT NOT NULL,
    direction             TEXT NOT NULL,         -- bull / bear

    -- ----- requested execution (what the bot asked for) -----
    requested_entry       REAL NOT NULL,
    requested_stop        REAL NOT NULL,
    requested_target      REAL NOT NULL,
    requested_qty         INTEGER NOT NULL,
    requested_side        TEXT NOT NULL,         -- Buy / Sell
    risk_R_per_contract   REAL,
    total_risk_usd        REAL,
    rr_planned            REAL,

    -- ----- broker / mode -----
    broker                TEXT NOT NULL,         -- tradovate / topstepx / dryrun / rithmic
    mode                  TEXT NOT NULL,         -- review / paper / live
    account_id            TEXT,
    account_size_usd      REAL,

    -- ----- order ids -----
    parent_order_id       TEXT,                  -- broker order id of the limit entry
    stop_child_order_id   TEXT,                  -- bracket child (SL)
    target_child_order_id TEXT,                  -- bracket child (TP)

    -- ----- actual execution (filled in by reconciler) -----
    actual_entry          REAL,                  -- fill price of the entry
    actual_stop_fill      REAL,                  -- fill price of the stop exit (if hit)
    actual_target_fill    REAL,                  -- fill price of the target exit (if hit)
    actual_qty_filled     INTEGER,               -- may be < requested_qty on partial

    -- ----- execution metrics (derived) -----
    entry_slippage_pts    REAL,                  -- actual_entry vs requested_entry (signed)
    stop_slippage_pts     REAL,                  -- actual_stop_fill vs requested_stop (signed, ≥0 = adverse)
    target_slippage_pts   REAL,                  -- vs requested_target (signed)
    total_slippage_pts    REAL,                  -- entry_slip + exit_slip
    fill_latency_sec      REAL,                  -- ts_placed → ts_filled

    -- ----- trade outcome -----
    outcome               TEXT,                  -- target / stop / void / timeout / manual / open
    exit_kind             TEXT,                  -- limit / stop / cancel / manual
    realized_R            REAL,                  -- (actual_exit - actual_entry) / risk_price * sign
    realized_pnl_usd      REAL,
    duration_sec          INTEGER,               -- ts_filled → ts_exit
    session               TEXT,

    -- ----- timestamps -----
    ts_placed             TEXT,                  -- when we submitted the order
    ts_filled             TEXT,                  -- when entry filled
    ts_exit               TEXT,                  -- when exit filled

    -- ----- exec events -----
    partial_fill          INTEGER NOT NULL DEFAULT 0,
    missed_fill           INTEGER NOT NULL DEFAULT 0,
    rejected_order        INTEGER NOT NULL DEFAULT 0,
    reject_reason         TEXT,

    -- ----- audit -----
    notes                 TEXT,
    raw_placement_json    TEXT,                  -- broker placement raw response
    raw_executions_json   TEXT                   -- joined exec events as JSON list
);

CREATE INDEX IF NOT EXISTS idx_pt_signal_id   ON paper_trades(signal_id);
CREATE INDEX IF NOT EXISTS idx_pt_symbol      ON paper_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_pt_outcome     ON paper_trades(outcome);
CREATE INDEX IF NOT EXISTS idx_pt_parent_oid  ON paper_trades(parent_order_id);
CREATE INDEX IF NOT EXISTS idx_pt_broker_mode ON paper_trades(broker, mode);
"""


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
@dataclass
class AttemptInput:
    signal_id: str
    ts_signal: str
    symbol: str
    direction: str
    requested_entry: float
    requested_stop: float
    requested_target: float
    requested_qty: int
    requested_side: str
    risk_R_per_contract: float
    total_risk_usd: float
    rr_planned: float
    broker: str
    mode: str
    account_id: Optional[str] = None
    account_size_usd: Optional[float] = None
    session: Optional[str] = None
    notes: Optional[str] = None


def insert_attempt(att: AttemptInput, db_path: Path = DB_PATH) -> int:
    conn = _connect(db_path)
    cur = conn.execute(
        """INSERT INTO paper_trades(
            signal_id, ts_signal, symbol, direction,
            requested_entry, requested_stop, requested_target,
            requested_qty, requested_side,
            risk_R_per_contract, total_risk_usd, rr_planned,
            broker, mode, account_id, account_size_usd,
            session, notes, outcome
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (att.signal_id, att.ts_signal, att.symbol, att.direction,
         att.requested_entry, att.requested_stop, att.requested_target,
         att.requested_qty, att.requested_side,
         att.risk_R_per_contract, att.total_risk_usd, att.rr_planned,
         att.broker, att.mode, att.account_id, att.account_size_usd,
         att.session, att.notes, "open"),
    )
    pid = cur.lastrowid
    conn.commit(); conn.close()
    return pid


def record_placement(trade_id: int, *,
                     parent_order_id: str,
                     stop_child_order_id: Optional[str] = None,
                     target_child_order_id: Optional[str] = None,
                     raw_placement_json: Optional[str] = None,
                     ts_placed: Optional[str] = None,
                     db_path: Path = DB_PATH):
    conn = _connect(db_path)
    conn.execute(
        """UPDATE paper_trades SET
            parent_order_id=?, stop_child_order_id=?, target_child_order_id=?,
            raw_placement_json=?, ts_placed=?
           WHERE id=?""",
        (parent_order_id, stop_child_order_id, target_child_order_id,
         raw_placement_json, ts_placed or _now_iso(), trade_id),
    )
    conn.commit(); conn.close()


def record_entry_fill(trade_id: int, *,
                      actual_entry: float, actual_qty_filled: int,
                      ts_filled: Optional[str] = None,
                      partial: bool = False,
                      db_path: Path = DB_PATH):
    conn = _connect(db_path)
    row = conn.execute("SELECT requested_entry, ts_placed FROM paper_trades WHERE id=?",
                       (trade_id,)).fetchone()
    if row is None:
        conn.close(); raise ValueError(f"trade_id {trade_id} not found")
    ts_filled = ts_filled or _now_iso()
    entry_slip = actual_entry - row["requested_entry"]
    latency = None
    if row["ts_placed"]:
        try:
            placed = datetime.fromisoformat(row["ts_placed"])
            filled = datetime.fromisoformat(ts_filled)
            latency = (filled - placed).total_seconds()
        except Exception:
            pass
    conn.execute(
        """UPDATE paper_trades SET
            actual_entry=?, actual_qty_filled=?,
            entry_slippage_pts=?, ts_filled=?,
            fill_latency_sec=?, partial_fill=?
           WHERE id=?""",
        (actual_entry, actual_qty_filled, entry_slip, ts_filled,
         latency, 1 if partial else 0, trade_id),
    )
    conn.commit(); conn.close()


def record_exit(trade_id: int, *,
                outcome: str,                   # target / stop / void / timeout / manual
                exit_kind: str,                 # limit / stop / cancel / manual
                actual_exit_price: float,
                ts_exit: Optional[str] = None,
                db_path: Path = DB_PATH):
    if outcome not in ("target", "stop", "void", "timeout", "manual"):
        raise ValueError(f"unknown outcome: {outcome}")
    conn = _connect(db_path)
    row = conn.execute(
        """SELECT requested_entry, requested_stop, requested_target,
                  actual_entry, actual_qty_filled, ts_filled, direction
           FROM paper_trades WHERE id=?""", (trade_id,)).fetchone()
    if row is None:
        conn.close(); raise ValueError(f"trade_id {trade_id} not found")
    ts_exit = ts_exit or _now_iso()
    sign = 1 if row["direction"] == "bull" else -1
    entry = row["actual_entry"] if row["actual_entry"] is not None else row["requested_entry"]
    risk_price = abs(entry - row["requested_stop"]) or 1.0
    realized_R = (actual_exit_price - entry) * sign / risk_price

    # Stop / target slippage (signed; positive = adverse)
    stop_slip = None; target_slip = None
    if outcome == "stop":
        stop_slip = -sign * (actual_exit_price - row["requested_stop"])
    elif outcome == "target":
        target_slip = sign * (row["requested_target"] - actual_exit_price)
    entry_slip_row = conn.execute(
        "SELECT entry_slippage_pts FROM paper_trades WHERE id=?",
        (trade_id,)).fetchone()
    entry_slip = float(entry_slip_row["entry_slippage_pts"] or 0)
    total_slip = entry_slip + (stop_slip or 0) + (target_slip or 0)

    duration = None
    if row["ts_filled"]:
        try:
            duration = int((datetime.fromisoformat(ts_exit)
                            - datetime.fromisoformat(row["ts_filled"])).total_seconds())
        except Exception:
            pass

    conn.execute(
        """UPDATE paper_trades SET
            outcome=?, exit_kind=?,
            actual_stop_fill=CASE WHEN ?='stop' THEN ? ELSE actual_stop_fill END,
            actual_target_fill=CASE WHEN ?='target' THEN ? ELSE actual_target_fill END,
            stop_slippage_pts=COALESCE(?, stop_slippage_pts),
            target_slippage_pts=COALESCE(?, target_slippage_pts),
            total_slippage_pts=?,
            realized_R=?,
            ts_exit=?, duration_sec=?
           WHERE id=?""",
        (outcome, exit_kind,
         outcome, actual_exit_price,
         outcome, actual_exit_price,
         stop_slip, target_slip, total_slip, realized_R,
         ts_exit, duration, trade_id),
    )
    conn.commit(); conn.close()


def mark_missed(trade_id: int, reason: str, db_path: Path = DB_PATH):
    conn = _connect(db_path)
    conn.execute(
        """UPDATE paper_trades SET missed_fill=1, outcome='timeout',
            reject_reason=COALESCE(reject_reason || ' | ', '') || ?
           WHERE id=?""", (reason, trade_id))
    conn.commit(); conn.close()


def mark_rejected(trade_id: int, reason: str, db_path: Path = DB_PATH):
    conn = _connect(db_path)
    conn.execute(
        """UPDATE paper_trades SET rejected_order=1, outcome='void',
            reject_reason=?, ts_exit=?
           WHERE id=?""", (reason, _now_iso(), trade_id))
    conn.commit(); conn.close()


# ---------------------------------------------------------------------------
def metrics_summary(db_path: Path = DB_PATH) -> dict:
    """Quick summary for the operator: count, win rate, expectancy, fill rate."""
    conn = _connect(db_path)
    rows = conn.execute("SELECT * FROM paper_trades").fetchall()
    closed = [r for r in rows if r["outcome"] in ("target", "stop")]
    wins = [r for r in closed if r["outcome"] == "target"]
    losses = [r for r in closed if r["outcome"] == "stop"]
    Rs = [r["realized_R"] for r in closed if r["realized_R"] is not None]
    stop_slips = [r["stop_slippage_pts"] for r in closed
                  if r["stop_slippage_pts"] is not None]
    return {
        "total_attempts": len(rows),
        "filled": sum(1 for r in rows if r["actual_entry"] is not None),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "void": sum(1 for r in rows if r["outcome"] == "void"),
        "timeout": sum(1 for r in rows if r["outcome"] == "timeout"),
        "open": sum(1 for r in rows if r["outcome"] == "open"),
        "win_rate_pct": (len(wins) / len(closed) * 100) if closed else 0.0,
        "mean_R": sum(Rs) / len(Rs) if Rs else 0.0,
        "stop_slip_median": (sorted(stop_slips)[len(stop_slips)//2] if stop_slips else 0.0),
        "stop_slip_n": len(stop_slips),
    }


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the db if missing")
    sub.add_parser("metrics", help="print summary metrics")
    sub.add_parser("list", help="print recent rows")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.cmd == "init":
        _connect()
        print(f"initialised {DB_PATH}")
    elif args.cmd == "metrics":
        print(json.dumps(metrics_summary(), indent=2))
    elif args.cmd == "list":
        conn = _connect()
        for r in conn.execute(
            "SELECT id, ts_signal, symbol, direction, broker, mode, outcome, "
            "realized_R FROM paper_trades ORDER BY id DESC LIMIT 50"):
            print(dict(r))


if __name__ == "__main__":
    main()
