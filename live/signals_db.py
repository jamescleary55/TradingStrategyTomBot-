"""SQLite-backed signal store + CSV/JSON exporters.

Single source of truth: ``live_signals.jsonl`` is still the append-only
log. ``signals.db`` is a *materialised* view of that log into a queryable
shape, plus the operator's "what account size am I running, what
profile, what status" annotations.

Lifecycle:
1. The live monitor writes every detected signal to
   ``live_signals.jsonl`` (existing behavior, unchanged).
2. ``sync_from_jsonl()`` reads new lines from the JSONL into SQLite.
   Idempotent — re-runs only ingest unseen rows.
3. Exporters render to CSV / JSON for offline analysis or sharing.

Schema goal: capture **everything needed to evaluate an unfilled
signal in hindsight** (was it well-formed, what was the regime, was
it skipped/filled, etc.) without coupling to broker / trade outcome.
That coupling lives in ``paper_trades.db``.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
DB_PATH = Path.home() / ".ict-bot" / "signals.db"
JSONL_PATH = Path.home() / ".ict-bot" / "live_signals.jsonl"

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id          TEXT PRIMARY KEY,    -- hash(symbol, ts, direction, entry, stop, target)
    ts_logged          TEXT NOT NULL,       -- when the bot saw it
    ts_setup           TEXT NOT NULL,       -- setup's own timestamp (CHoCH bar)
    symbol             TEXT NOT NULL,
    timeframe          TEXT NOT NULL,
    strategy           TEXT NOT NULL,
    strategy_version   TEXT,
    direction          TEXT NOT NULL,       -- bull / bear
    setup_type         TEXT,
    setup_subtype      TEXT,
    entry_price        REAL NOT NULL,
    stop_price         REAL NOT NULL,
    target_price       REAL NOT NULL,
    rr                 REAL,
    risk_R             REAL,                -- planned R (always 1.0 by definition)
    htf_bias           TEXT,
    setup_score        REAL,
    session            TEXT,
    confluence         TEXT,                -- JSON list as string
    execution_profile  TEXT,                -- OPTIMISTIC / NORMAL / PUNITIVE used by sim
    account_size_usd   REAL,                -- operator-tagged
    status             TEXT NOT NULL DEFAULT 'logged',
                                            -- logged / skipped / sized / placed / closed / void
    skip_reason        TEXT,
    raw_json           TEXT NOT NULL        -- the original JSONL row, opaque
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol_ts ON signals(symbol, ts_setup);
CREATE INDEX IF NOT EXISTS idx_signals_status     ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_session    ON signals(session);

-- Append-only audit row so we can replay "when did this signal change status"
CREATE TABLE IF NOT EXISTS signal_status_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id    TEXT NOT NULL,
    ts           TEXT NOT NULL,
    old_status   TEXT,
    new_status   TEXT NOT NULL,
    note         TEXT,
    FOREIGN KEY(signal_id) REFERENCES signals(signal_id)
);
"""


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------------------
def _signal_id(row: dict) -> str:
    """Stable id from the price geometry + timestamp."""
    parts = (
        str(row.get("symbol", "")),
        str(row.get("timestamp") or row.get("ts") or ""),
        str(row.get("direction", "")),
        f"{float(row.get('entry', 0)):.4f}",
        f"{float(row.get('stop', 0)):.4f}",
        f"{float(row.get('target', 0)):.4f}",
    )
    return hashlib.blake2b("|".join(parts).encode(), digest_size=12).hexdigest()


def _row_from_jsonl(line: str) -> Optional[dict]:
    try:
        raw = json.loads(line)
    except Exception:
        return None
    return {
        "signal_id":        _signal_id(raw),
        "ts_logged":        raw.get("ts_logged") or raw.get("logged_at") or raw.get("timestamp") or "",
        "ts_setup":         str(raw.get("timestamp") or raw.get("ts_setup") or ""),
        "symbol":           raw.get("symbol", ""),
        "timeframe":        raw.get("timeframe", ""),
        "strategy":         raw.get("strategy") or raw.get("strategy_name", "sweep_choch_fvg"),
        "strategy_version": raw.get("strategy_version"),
        "direction":        raw.get("direction", ""),
        "setup_type":       raw.get("setup_type"),
        "setup_subtype":    raw.get("setup_subtype"),
        "entry_price":      float(raw.get("entry", 0)),
        "stop_price":       float(raw.get("stop", 0)),
        "target_price":     float(raw.get("target", 0)),
        "rr":               float(raw.get("rr", 0)) if raw.get("rr") is not None else None,
        "risk_R":           1.0,
        "htf_bias":         raw.get("htf_bias") or raw.get("bias"),
        "setup_score":      raw.get("setup_score") or raw.get("score"),
        "session":          raw.get("session"),
        "confluence":       json.dumps(raw.get("confluence", [])) if raw.get("confluence") else None,
        "execution_profile": raw.get("execution_profile"),
        "account_size_usd": raw.get("account_size_usd") or raw.get("account_size"),
        "status":           raw.get("status", "logged"),
        "skip_reason":      raw.get("skip_reason"),
        "raw_json":         line.strip(),
    }


# ---------------------------------------------------------------------------
def sync_from_jsonl(jsonl_path: Path = JSONL_PATH,
                    db_path: Path = DB_PATH) -> dict:
    """Ingest unseen rows from ``jsonl_path`` into the SQLite store.

    Idempotent — uses INSERT OR IGNORE on signal_id.
    """
    if not jsonl_path.exists():
        log.info("no JSONL at %s — nothing to sync", jsonl_path)
        return {"inserted": 0, "skipped": 0}
    conn = _connect(db_path)
    inserted = 0; skipped = 0
    with jsonl_path.open("r") as f:
        rows = []
        for line in f:
            r = _row_from_jsonl(line)
            if r is None:
                continue
            rows.append(r)
        for r in rows:
            try:
                conn.execute(
                    """INSERT INTO signals(
                        signal_id, ts_logged, ts_setup, symbol, timeframe,
                        strategy, strategy_version, direction, setup_type, setup_subtype,
                        entry_price, stop_price, target_price, rr, risk_R,
                        htf_bias, setup_score, session, confluence,
                        execution_profile, account_size_usd, status, skip_reason, raw_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (r["signal_id"], r["ts_logged"], r["ts_setup"], r["symbol"], r["timeframe"],
                     r["strategy"], r["strategy_version"], r["direction"], r["setup_type"], r["setup_subtype"],
                     r["entry_price"], r["stop_price"], r["target_price"], r["rr"], r["risk_R"],
                     r["htf_bias"], r["setup_score"], r["session"], r["confluence"],
                     r["execution_profile"], r["account_size_usd"], r["status"], r["skip_reason"], r["raw_json"]),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1
    conn.commit(); conn.close()
    log.info("synced %d new signals (skipped %d already-known)", inserted, skipped)
    return {"inserted": inserted, "skipped": skipped}


# ---------------------------------------------------------------------------
def update_status(signal_id: str, new_status: str,
                  note: Optional[str] = None, db_path: Path = DB_PATH):
    """Set a signal's status + append an audit row."""
    conn = _connect(db_path)
    old_row = conn.execute("SELECT status FROM signals WHERE signal_id=?",
                           (signal_id,)).fetchone()
    if old_row is None:
        conn.close()
        raise ValueError(f"signal_id {signal_id} not found")
    old = old_row["status"]
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE signals SET status=? WHERE signal_id=?",
                 (new_status, signal_id))
    conn.execute(
        """INSERT INTO signal_status_history(signal_id, ts, old_status, new_status, note)
           VALUES (?,?,?,?,?)""",
        (signal_id, ts, old, new_status, note),
    )
    conn.commit(); conn.close()


# ---------------------------------------------------------------------------
EXPORT_COLS = (
    "signal_id", "ts_logged", "ts_setup", "symbol", "timeframe",
    "strategy", "direction", "setup_type", "setup_subtype",
    "entry_price", "stop_price", "target_price", "rr", "risk_R",
    "htf_bias", "setup_score", "session", "execution_profile",
    "account_size_usd", "status", "skip_reason",
)


def export_csv(out_path: Path, db_path: Path = DB_PATH):
    conn = _connect(db_path)
    rows = conn.execute(f"SELECT {','.join(EXPORT_COLS)} FROM signals "
                        f"ORDER BY ts_setup").fetchall()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        w = csv.writer(f); w.writerow(EXPORT_COLS)
        for r in rows:
            w.writerow([r[c] for c in EXPORT_COLS])
    conn.close()
    log.info("wrote %d rows to %s", len(rows), out_path)


def export_json(out_path: Path, db_path: Path = DB_PATH):
    conn = _connect(db_path)
    rows = conn.execute(f"SELECT {','.join(EXPORT_COLS)} FROM signals "
                        f"ORDER BY ts_setup").fetchall()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps([dict(r) for r in rows], indent=2, default=str))
    conn.close()
    log.info("wrote %d rows to %s", len(rows), out_path)


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="signals.db sync + export")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync", help="ingest unseen rows from live_signals.jsonl")
    e = sub.add_parser("export", help="dump to CSV/JSON")
    e.add_argument("--csv", type=Path, default=None)
    e.add_argument("--json", type=Path, default=None)
    s = sub.add_parser("count", help="print row count")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.cmd == "sync":
        sync_from_jsonl()
    elif args.cmd == "export":
        if not args.csv and not args.json:
            print("provide --csv and/or --json"); sys.exit(2)
        if args.csv: export_csv(args.csv)
        if args.json: export_json(args.json)
    elif args.cmd == "count":
        conn = _connect()
        n = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        by_status = dict(conn.execute(
            "SELECT status, COUNT(*) FROM signals GROUP BY status").fetchall())
        by_sym = dict(conn.execute(
            "SELECT symbol, COUNT(*) FROM signals GROUP BY symbol").fetchall())
        print(f"total: {n}\nby status: {by_status}\nby symbol: {by_sym}")
        conn.close()


if __name__ == "__main__":
    main()
