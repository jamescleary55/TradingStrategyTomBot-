"""Turn a signal into a paper-traded order.

End-to-end pipeline used during the validation phase:

    signals.db (a row in 'logged' status)
        ↓
    risk gate (existing RiskGate from risk/controls.py)
        ↓
    risk sizing (existing plan_trade)
        ↓
    broker adapter (TopstepX / Tradovate / DryRun)
        ↓
    paper_trades.db (the attempt + placement)
        ↓
    signals.db status → 'placed'

Dry-run mode is the default. ``--submit`` is required to actually call
the broker. ``--allow-live`` is required for non-demo environments
(the broker adapter enforces this separately too).

This script is intentionally one-shot:

    python -m live.paper_trade_runner --signal-id <hex> --submit

The live monitor calls into the same primitives. Operator can also
invoke it manually to retry / replay a specific signal.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import INSTRUMENTS
from execution.base import get_adapter
from live.paper_trades_db import (
    AttemptInput, insert_attempt, mark_rejected, record_placement,
)
from live.signals_db import _connect as signals_connect
from live.signals_db import update_status as update_signal_status
from risk.sizing import plan_trade

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_signal(signal_id: str) -> Optional[dict]:
    conn = signals_connect()
    row = conn.execute("SELECT * FROM signals WHERE signal_id=?",
                       (signal_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def run_signal(signal_id: str, *,
               equity: float,
               risk_pct: float = 0.005,
               broker_name: Optional[str] = None,
               mode: str = "paper",
               submit: bool = False,
               allow_live: bool = False) -> dict:
    """Plan + place + record. Returns a dict the operator can inspect."""
    sig = _fetch_signal(signal_id)
    if sig is None:
        raise SystemExit(f"signal_id {signal_id} not in signals.db — run "
                         f"`python -m live.signals_db sync` first")
    sym = sig["symbol"]
    if sym not in INSTRUMENTS:
        # Map root → micro
        micro = {"NQ": "MNQ", "ES": "MES", "GC": "MGC", "CL": "MCL"}.get(sym, sym)
        if micro not in INSTRUMENTS:
            raise SystemExit(f"symbol {sym!r} unknown to config.INSTRUMENTS")
        instrument = INSTRUMENTS[micro]
    else:
        instrument = INSTRUMENTS[sym]

    plan = plan_trade(
        equity=equity,
        entry=sig["entry_price"], stop=sig["stop_price"],
        target=sig["target_price"], instrument=instrument,
        risk_pct=risk_pct, min_rr=1.0,
    )
    if not plan.approved:
        log.warning("plan rejected for %s: %s", signal_id, plan.reason)
        return {"status": "rejected_at_plan", "reason": plan.reason}

    side = "Buy" if sig["direction"] == "bull" else "Sell"
    att = AttemptInput(
        signal_id=signal_id,
        ts_signal=sig["ts_setup"],
        symbol=sym,
        direction=sig["direction"],
        requested_entry=float(sig["entry_price"]),
        requested_stop=float(sig["stop_price"]),
        requested_target=float(sig["target_price"]),
        requested_qty=plan.contracts,
        requested_side=side,
        risk_R_per_contract=plan.risk_per_contract,
        total_risk_usd=plan.total_risk_usd,
        rr_planned=plan.rr,
        broker=(broker_name or os.getenv("BROKER", "topstepx")),
        mode=mode,
        account_id=os.getenv("PROJECTX_ACCOUNT_ID")
                   or os.getenv("TRADOVATE_ACCOUNT_ID"),
        account_size_usd=equity,
        session=sig.get("session"),
        notes=None,
    )
    trade_id = insert_attempt(att)
    log.info("paper_trades.id=%d created for signal %s", trade_id, signal_id)

    if not submit:
        log.info("DRY (no --submit) — skipping broker call.")
        update_signal_status(signal_id, "sized",
                             note=f"paper_trades.id={trade_id} dry_run")
        return {"status": "dry_run", "trade_id": trade_id,
                "plan": plan.__dict__, "side": side}

    try:
        adapter = get_adapter(broker_name)
        placement = adapter.place_bracket(
            instrument=instrument, side=side, qty=plan.contracts,
            entry=plan.entry, stop=plan.stop, target=plan.target,
            account_id=None,    # default to broker's account discovery
            allow_live=allow_live,
            dry_run=False,
        )
    except Exception as e:
        log.error("broker rejected order: %s", e)
        mark_rejected(trade_id, str(e))
        update_signal_status(signal_id, "void",
                             note=f"broker rejected: {e}")
        return {"status": "broker_rejected", "trade_id": trade_id,
                "reason": str(e)}

    record_placement(
        trade_id,
        parent_order_id=str(placement.order_id),
        stop_child_order_id=None,    # broker reports child ids on fills
        target_child_order_id=None,
        raw_placement_json=json.dumps(placement.raw_response, default=str),
        ts_placed=_now_iso(),
    )
    update_signal_status(signal_id, "placed",
                         note=f"paper_trades.id={trade_id} oid={placement.order_id}")
    return {"status": "placed", "trade_id": trade_id,
            "broker_order_id": placement.order_id}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--signal-id", required=True)
    parser.add_argument("--equity", type=float, default=100_000)
    parser.add_argument("--risk-pct", type=float, default=0.005)
    parser.add_argument("--broker", default=None,
                        help="override BROKER env (tradovate / topstepx / dryrun)")
    parser.add_argument("--mode", default="paper",
                        choices=("review", "paper", "live"))
    parser.add_argument("--submit", action="store_true",
                        help="actually call the broker. Without this, dry-run only.")
    parser.add_argument("--allow-live", action="store_true",
                        help="permit non-demo environments (broker also enforces).")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    result = run_signal(
        args.signal_id,
        equity=args.equity, risk_pct=args.risk_pct,
        broker_name=args.broker, mode=args.mode,
        submit=args.submit, allow_live=args.allow_live,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
