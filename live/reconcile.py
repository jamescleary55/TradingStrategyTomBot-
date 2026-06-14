"""Execution reconciler.

Closes audit findings A1, A5, B2 in one stroke. Polls the configured
broker's execution report endpoint, persists every event to
``~/.ict-bot/live_executions.jsonl``, and emits matched resolutions to
``~/.ict-bot/live_trades_resolved.jsonl`` once both entry and exit fills
are known for an order.

Why two files instead of mutating ``live_trades.jsonl``:

- ``live_trades.jsonl`` stays *append-only* — single-writer guarantee.
- ``live_executions.jsonl`` is the raw broker truth (append-only).
- ``live_trades_resolved.jsonl`` is the derived join with computed
  slippage and ``r_realised``.

Downstream code (RiskGate, forward_report, go_live) loads the resolved
file as the source of truth for closed-trade P&L.

CLI::

    python -m live.reconcile --once
    python -m live.reconcile --poll 30
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.base import ExecutionEvent, get_adapter
from live.forward_log import TRADES_LOG, _append, load_trades
from utils.alerter import Alerter

log = logging.getLogger("live.reconcile")
STATE_DIR = Path.home() / ".ict-bot"
STATE_DIR.mkdir(parents=True, exist_ok=True)
EXECS_LOG = STATE_DIR / "live_executions.jsonl"
RESOLVED_LOG = STATE_DIR / "live_trades_resolved.jsonl"
STATE_FILE = STATE_DIR / "reconcile-state.json"

UNRECONCILED_ALERT_MIN_AGE_S = 300   # 5 minutes


# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return dt.datetime.utcnow().isoformat() + "Z"


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_exec_ts": None, "resolved_order_ids": []}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"last_exec_ts": None, "resolved_order_ids": []}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _append_jsonl(path: Path, row: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(row, default=str) + "\n")


# ---------------------------------------------------------------------------
def _index_trades_by_order(trades: list[dict]) -> dict[str, dict]:
    """Newest submission per order_id (later submissions overwrite earlier)."""
    by_id: dict[str, dict] = {}
    for t in trades:
        oid = str(t.get("order_id") or "")
        if oid:
            by_id[oid] = t
    return by_id


def _direction_sign(side: str) -> int:
    return 1 if side.lower().startswith("b") else -1


def _compute_slippage_pts(intended: float, actual: float, side: str) -> float:
    """Adverse slippage is positive (entry: paid up for buys / sold down for sells)."""
    sign = _direction_sign(side)
    return (actual - intended) * sign


def _compute_r_realised(trade: dict, fills: list[ExecutionEvent]) -> Optional[dict]:
    """Given the trade-submission row + every fill on that order family,
    return a dict containing fill_price, exit_price, slippage_pts,
    r_realised, outcome. Returns None when the lifecycle is incomplete
    (no exit fill yet).
    """
    if not fills:
        return None
    # First fill = entry, last opposing fill = exit
    entry_side = "Buy" if (trade.get("direction") == "bull") else "Sell"
    entry_fills = [f for f in fills if f.side == entry_side]
    exit_fills = [f for f in fills if f.side != entry_side]
    if not entry_fills:
        return None
    entry_fill = entry_fills[0]
    fill_price = float(entry_fill.price)
    intended_entry = float(trade.get("intended_entry") or 0)
    slippage_pts = _compute_slippage_pts(intended_entry, fill_price, entry_side)

    out = {
        "fill_price": fill_price,
        "fill_ts": entry_fill.timestamp,
        "slippage_pts": slippage_pts,
    }

    if not exit_fills:
        out["status"] = "filled"   # entry filled, still open
        return out

    exit_fill = exit_fills[-1]
    exit_price = float(exit_fill.price)
    out["exit_price"] = exit_price
    out["exit_ts"] = exit_fill.timestamp

    # Decide outcome: target or stop, based on direction vs entry
    direction_sign = _direction_sign(entry_side)
    pnl_price = (exit_price - fill_price) * direction_sign

    intended_stop = float(trade.get("intended_stop") or 0)
    intended_target = float(trade.get("intended_target") or 0)
    risk_price = abs(intended_entry - intended_stop)
    r_realised = (pnl_price / risk_price) if risk_price > 0 else 0.0
    out["r_realised"] = r_realised

    # outcome heuristic: closer to target or to stop
    dist_to_target = abs(exit_price - intended_target)
    dist_to_stop = abs(exit_price - intended_stop)
    if pnl_price >= 0 and dist_to_target <= dist_to_stop:
        out["outcome"] = "target"
    else:
        out["outcome"] = "stop"
    out["status"] = out["outcome"]
    return out


# ---------------------------------------------------------------------------
def reconcile_once(adapter, alerter: Optional[Alerter] = None) -> dict:
    """One pass. Returns a stats dict for the operator."""
    state = _load_state()
    since = state.get("last_exec_ts")
    stats = {
        "new_executions": 0,
        "resolved_trades": 0,
        "still_open": 0,
        "unreconciled_alerts": 0,
    }

    try:
        events = adapter.list_executions(since_ts=since)
    except NotImplementedError as e:
        log.warning("%s — skipping reconciliation tick", e)
        return stats
    except Exception as e:
        log.exception("list_executions failed: %s", e)
        return stats

    # Persist raw execution events
    for ev in events:
        _append_jsonl(EXECS_LOG, asdict(ev))
        stats["new_executions"] += 1
    if events:
        state["last_exec_ts"] = max(e.timestamp for e in events if e.timestamp)

    # Reload everything for the join (execs are append-only, cheap to re-load)
    all_execs_raw = _load_jsonl(EXECS_LOG)
    all_execs = [ExecutionEvent(**r) for r in all_execs_raw]
    by_order: dict[str, list[ExecutionEvent]] = {}
    for ev in all_execs:
        keys = {ev.order_id}
        if ev.parent_order_id:
            keys.add(ev.parent_order_id)
        for k in keys:
            if k:
                by_order.setdefault(k, []).append(ev)

    trades = load_trades()
    trades_by_order = _index_trades_by_order(trades)
    already_resolved = set(state.get("resolved_order_ids", []))
    resolved_log = _load_jsonl(RESOLVED_LOG)
    open_resolved = {r["order_id"]: r for r in resolved_log if r.get("status") == "filled"}

    now = dt.datetime.utcnow()
    for order_id, trade in trades_by_order.items():
        if order_id in already_resolved:
            continue
        fills_family = by_order.get(order_id, [])
        # Bracket children — Tradovate sets parent_order_id on the bracket
        for child_oid, fills in by_order.items():
            if child_oid == order_id:
                continue
            if any(f.parent_order_id == order_id for f in fills):
                fills_family.extend(fills)

        derived = _compute_r_realised(trade, fills_family)
        if derived is None:
            # No fills yet — check age for an unreconciled alert
            try:
                t_age = (now - dt.datetime.fromisoformat(trade.get("ts_logged", "").rstrip("Z"))).total_seconds()
            except Exception:
                t_age = 0
            if t_age > UNRECONCILED_ALERT_MIN_AGE_S:
                if alerter is not None:
                    alerter.notify(
                        "Trade unreconciled",
                        f"order_id={order_id} {trade.get('symbol')} {trade.get('direction')}: "
                        f"no fills after {int(t_age/60)} min.",
                        severity="warning",
                    )
                stats["unreconciled_alerts"] += 1
            continue

        resolved_row = {
            "ts_resolved": _now_iso(),
            # carry the originating trade's ts_logged so date/week grouping works
            "ts_logged": trade.get("ts_logged") or trade.get("timestamp") or _now_iso(),
            "order_id": order_id,
            "symbol": trade.get("symbol"),
            "sim_symbol": trade.get("sim_symbol"),
            "timeframe": trade.get("timeframe"),
            "direction": trade.get("direction"),
            "intended_entry": trade.get("intended_entry"),
            "intended_stop": trade.get("intended_stop"),
            "intended_target": trade.get("intended_target"),
            "planned_R": trade.get("planned_R"),
            "contracts": trade.get("contracts"),
            **derived,
        }
        _append_jsonl(RESOLVED_LOG, resolved_row)
        if derived.get("status") == "filled":
            stats["still_open"] += 1
        else:
            already_resolved.add(order_id)
            stats["resolved_trades"] += 1

    state["resolved_order_ids"] = sorted(already_resolved)
    _save_state(state)
    return stats


# ---------------------------------------------------------------------------
def load_resolved_trades() -> list[dict]:
    """Public accessor used by RiskGate + analytics."""
    return _load_jsonl(RESOLVED_LOG)


# ---------------------------------------------------------------------------
_should_stop = False


def _handle_signal(signum, frame):
    global _should_stop
    _should_stop = True


def main():
    parser = argparse.ArgumentParser(description="Live execution reconciler")
    parser.add_argument("--broker", default=None,
                        help="Override BROKER env var (tradovate / topstepx / dryrun)")
    parser.add_argument("--poll", type=int, default=30, help="Seconds between ticks")
    parser.add_argument("--once", action="store_true", help="One pass and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    adapter = get_adapter(args.broker)
    alerter = Alerter()

    log.info("Reconciler started: broker=%s, every=%ds", adapter.name, args.poll)
    log.info("Execs log:    %s", EXECS_LOG)
    log.info("Resolved log: %s", RESOLVED_LOG)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    def one():
        s = reconcile_once(adapter, alerter)
        log.info("tick: %d new exec(s), %d resolved, %d still open, %d unreconciled alert(s)",
                 s["new_executions"], s["resolved_trades"],
                 s["still_open"], s["unreconciled_alerts"])

    if args.once:
        one()
        return
    while not _should_stop:
        try:
            one()
        except Exception:
            log.exception("reconcile tick failed")
        for _ in range(args.poll):
            if _should_stop:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
