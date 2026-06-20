"""Attended watcher for the first reconciled CLOSED paper trade(s).

Drives the existing engines — it builds NO new trading logic. Each tick it:

  1. captures new broker executions  → appends to live_executions.jsonl
  2. reconciles all executions       → reconciliation.reconcile (production engine)
  3. computes metrics                → reconciliation.compute_metrics (CLOSED only)
  4. detects failures (Phase 5)      → halts via kill switch + FAILURE_REPORT.md
  5. on milestones                   → writes validation / 10-trade reports

Run it alongside the attended `live.monitor --mode auto_paper_safe` run:

    python scripts/first_trade_watch.py --poll 30           # stop at 1 CLOSED
    python scripts/first_trade_watch.py --poll 30 --max-closed 10

READ-MOSTLY: it only appends to the executions log and (on failure) trips the
kill switch. It never places or cancels orders.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.base import ExecutionEvent, get_adapter
from reconciliation import CLOSED, OPEN, PARTIAL, compute_metrics, reconcile

STATE_DIR = Path.home() / ".ict-bot"
EXECS_LOG = STATE_DIR / "live_executions.jsonl"
TRADES_LOG = STATE_DIR / "live_trades.jsonl"
STATE_FILE = STATE_DIR / "first-trade-watch-state.json"
KILL_SWITCH = STATE_DIR / "KILL_SWITCH"
REPO = Path(__file__).resolve().parent.parent

_EVENT_FIELDS = set(ExecutionEvent.__dataclass_fields__.keys())


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _append_jsonl(path: Path, row: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(row, default=str) + "\n")


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen_exec_ids": [], "last_ts": None, "first_validated": False}


def _save_state(s: dict) -> None:
    STATE_FILE.write_text(json.dumps(s, indent=2))


def _load_executions() -> list[ExecutionEvent]:
    evs = []
    for r in _load_jsonl(EXECS_LOG):
        kw = {k: v for k, v in r.items() if k in _EVENT_FIELDS}
        kw.pop("raw", None)
        evs.append(ExecutionEvent(**kw))
    return evs


def _load_order_meta() -> dict:
    meta = {}
    for t in _load_jsonl(TRADES_LOG):
        oid = str(t.get("order_id") or "")
        if oid:
            meta[oid] = {k: t.get(k) for k in
                         ("intended_entry", "intended_stop", "intended_target", "planned_R")}
    return meta


def _capture(adapter, state: dict) -> int:
    """Pull new executions, append unique ones to the raw log. Returns new count."""
    seen = set(state.get("seen_exec_ids", []))
    events = adapter.list_executions(since_ts=state.get("last_ts"))
    n = 0
    for e in events:
        exid = str(getattr(e, "execution_id", "") or "")
        key = exid or f"{e.order_id}|{e.timestamp}|{e.side}|{e.qty}|{e.price}"
        if key in seen:
            continue
        seen.add(key)
        row = {k: getattr(e, k) for k in _EVENT_FIELDS if k != "raw"}
        _append_jsonl(EXECS_LOG, row)
        n += 1
    state["seen_exec_ids"] = sorted(seen)
    ts = [e.timestamp for e in events if e.timestamp]
    if ts:
        state["last_ts"] = max(ts)
    return n


def _detect_failures(adapter, trades) -> list[str]:
    """Phase 5 failure detection (best-effort, halts on any)."""
    fails = []
    # orphan position: broker shows a position with no OPEN/PARTIAL trade for it
    try:
        snap = adapter.snapshot()
        if snap is None:
            fails.append("broker disconnect / snapshot hard-failed")
        else:
            open_syms = {t.symbol for t in trades if t.status in (OPEN, PARTIAL)}
            for p in snap.positions:
                if p.symbol not in open_syms:
                    fails.append(f"orphan position: {p.symbol} {p.side} x{p.qty} "
                                 f"with no OPEN/PARTIAL reconciled trade")
    except Exception as e:
        fails.append(f"broker disconnect: {e.__class__.__name__}: {e}")
    # duplicate raw execIds in the log (should never happen after dedup)
    raw_ids = [r.get("execution_id") for r in _load_jsonl(EXECS_LOG) if r.get("execution_id")]
    if len(raw_ids) != len(set(raw_ids)):
        fails.append("duplicate execution_id present in raw log")
    return fails


def _halt(reason: str, trades, metrics) -> None:
    KILL_SWITCH.write_text(f"halted by first_trade_watch at {_now()}: {reason}\n")
    body = [
        "# FAILURE REPORT — first automated paper trade",
        f"\n**Time:** {_now()}\n**Halted:** kill switch written to `{KILL_SWITCH}`\n",
        f"## Reason\n\n- {reason}\n",
        "## State at halt\n",
        f"- reconciled trades: {len(trades)}",
        f"- statuses: {sorted(set(t.status for t in trades))}",
        f"- metrics: {json.dumps(metrics, default=str)}\n",
        "## Logs preserved\n",
        f"- `{EXECS_LOG}`\n- `{TRADES_LOG}`\n- `{STATE_DIR / 'events.jsonl'}`\n",
        "Auto-execution is halted. Investigate before resuming; run "
        "`python scripts/flatten_account.py --execute` if a position is open.\n",
    ]
    (REPO / "FAILURE_REPORT.md").write_text("\n".join(body))
    print(f"\n!!! HALTED: {reason}\n!!! kill switch set, FAILURE_REPORT.md written.")


def _validate_first(trade, metrics) -> None:
    t = trade
    lines = [
        "# First Closed Trade — Validation",
        f"\n**Generated:** {_now()} · **Status:** {t.status}\n",
        "## Trade\n",
        f"- trade_id: `{t.trade_id}`",
        f"- account: {t.account_id} · symbol: {t.symbol} · side: {t.side}",
        f"- entry: {t.entry_qty} @ {t.entry_price}  ({t.entry_time})",
        f"- exit:  {t.exit_qty} @ {t.exit_price}  ({t.exit_time})",
        f"- entry_order_id {t.entry_order_id} / perm {t.entry_perm_id}",
        f"- exit_order_id {t.exit_order_id} / perm {t.exit_perm_id}",
        f"- execution_ids: {t.execution_ids}\n",
        "## Checks\n",
        f"- [{'x' if t.entry_qty > 0 else ' '}] entry fills matched ({t.entry_qty})",
        f"- [{'x' if t.exit_qty == t.entry_qty else ' '}] exit fills matched ({t.exit_qty}=={t.entry_qty})",
        f"- [{'x' if t.commission is not None else ' '}] commission applied: {t.commission}",
        f"- [{'x' if t.slippage is not None else ' '}] slippage recorded: {t.slippage}",
        f"- [{'x' if t.realized_R is not None else ' '}] realized R computed: {t.realized_R}",
        f"- [{'x' if t.status == CLOSED else ' '}] status == CLOSED",
        f"- [{'x' if metrics['n_closed'] >= 1 else ' '}] metrics updated (n_closed={metrics['n_closed']})\n",
        f"- gross_pnl {t.gross_pnl} · net_pnl {t.net_pnl} · exit_reason {t.exit_reason}\n",
        "## Metrics snapshot\n```json",
        json.dumps(metrics, indent=2, default=str),
        "```\n",
    ]
    (REPO / "FIRST_CLOSED_TRADE_VALIDATION.md").write_text("\n".join(lines))
    print("\n*** FIRST CLOSED TRADE — FIRST_CLOSED_TRADE_VALIDATION.md written ***")


def _report_ten(closed, metrics) -> None:
    lines = [
        "# First 10 Closed Trades — Report",
        f"\n**Generated:** {_now()} · **Closed trades:** {len(closed)}\n",
        "> Operational validation only. No conclusions about edge.\n",
        "## Metrics (CLOSED trades only)\n",
        f"- win rate: {metrics['win_rate']}",
        f"- expectancy: {metrics['expectancy']} (R: {metrics['expectancy_R']})",
        f"- profit factor: {metrics['profit_factor']}",
        f"- average R: {metrics['avg_R']}",
        f"- max drawdown: {metrics['max_drawdown']} (recovery {metrics['recovery_factor']})",
        f"- avg slippage: {metrics['avg_slippage']} pts",
        f"- avg commission: {metrics['avg_commission']} (total {metrics['total_commission']})\n",
        "## Trades\n",
        "| # | symbol | side | qty | entry | exit | net | R | reason |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, t in enumerate(closed, 1):
        lines.append(f"| {i} | {t.symbol} | {t.side} | {t.entry_qty} | {t.entry_price} | "
                     f"{t.exit_price} | {t.net_pnl} | {t.realized_R} | {t.exit_reason} |")
    (REPO / "FIRST_10_TRADES_REPORT.md").write_text("\n".join(lines))
    print("\n*** 10 CLOSED TRADES — FIRST_10_TRADES_REPORT.md written ***")


def tick(adapter, state, max_closed) -> bool:
    """One pass. Returns True when the stop condition is reached."""
    new = _capture(adapter, state)
    execs = _load_executions()
    try:
        trades = reconcile(execs, order_meta=_load_order_meta())
    except Exception as e:
        _halt(f"reconciliation error: {e.__class__.__name__}: {e}", [], {})
        return True
    metrics = compute_metrics(trades)
    closed = sorted([t for t in trades if t.status == CLOSED],
                    key=lambda t: (t.exit_time or "", t.trade_id))

    fails = _detect_failures(adapter, trades)
    if fails:
        _halt("; ".join(fails), trades, metrics)
        return True

    n_open = sum(1 for t in trades if t.status in (OPEN, PARTIAL))
    print(f"[{_now()}] +{new} exec · execs={len(execs)} · "
          f"closed={len(closed)} open/partial={n_open} · "
          f"net={metrics['total_net_pnl']}")

    if closed and not state.get("first_validated"):
        _validate_first(closed[0], metrics)
        state["first_validated"] = True
    _save_state(state)

    if len(closed) >= 1 and max_closed == 1:
        print("\nMilestone 1 reached: first reconciled CLOSED trade.")
        return True
    if len(closed) >= max_closed:
        _report_ten(closed[:max_closed], metrics)
        print(f"\nMilestone reached: {max_closed} reconciled CLOSED trades. Stopping.")
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Attended first-closed-trade watcher")
    ap.add_argument("--broker", default="ibkr")
    ap.add_argument("--poll", type=int, default=30, help="seconds between ticks")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--max-closed", type=int, default=1,
                    help="stop after this many CLOSED trades (1 = first milestone)")
    args = ap.parse_args()

    adapter = get_adapter(args.broker)
    state = _load_state()
    print(f"first_trade_watch: broker={adapter.name} poll={args.poll}s "
          f"target={args.max_closed} CLOSED · execs log={EXECS_LOG}")

    if args.once:
        tick(adapter, state, args.max_closed)
        return 0
    try:
        while True:
            if tick(adapter, state, args.max_closed):
                break
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\nstopped (Ctrl-C).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
