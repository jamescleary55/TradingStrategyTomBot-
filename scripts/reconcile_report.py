"""Reconcile the raw execution log into closed trades + trustworthy metrics.

Reads broker truth from ``~/.ict-bot/live_executions.jsonl`` (written by the
reconciler/poller) and order intentions from ``~/.ict-bot/live_trades.jsonl``
(for slippage / realized-R / exit-reason only), runs the production
reconciliation engine, and prints reconciled trades + metrics computed strictly
from CLOSED round-trips.

    python scripts/reconcile_report.py            # human summary
    python scripts/reconcile_report.py --json     # machine-readable

READ-ONLY. Computes nothing from raw orders — only from reconciled trades.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.base import ExecutionEvent
from reconciliation import CLOSED, compute_metrics, reconcile

STATE_DIR = Path.home() / ".ict-bot"
EXECS_LOG = STATE_DIR / "live_executions.jsonl"
TRADES_LOG = STATE_DIR / "live_trades.jsonl"

_EVENT_FIELDS = set(ExecutionEvent.__dataclass_fields__.keys())


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
            meta[oid] = {
                "intended_entry": t.get("intended_entry"),
                "intended_stop": t.get("intended_stop"),
                "intended_target": t.get("intended_target"),
                "planned_R": t.get("planned_R"),
            }
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description="Reconcile executions → trades → metrics")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    execs = _load_executions()
    meta = _load_order_meta()
    trades = reconcile(execs, order_meta=meta)
    metrics = compute_metrics(trades)

    if args.json:
        print(json.dumps({
            "trades": [t.to_dict() for t in trades],
            "metrics": metrics,
        }, indent=2, default=str))
        return 0

    by_status: dict[str, int] = {}
    for t in trades:
        by_status[t.status] = by_status.get(t.status, 0) + 1

    print(f"Executions read : {len(execs)}  ({EXECS_LOG})")
    print(f"Trades derived  : {len(trades)}  {by_status}")
    print("-" * 60)
    for t in trades:
        if t.status == CLOSED:
            print(f"  {t.symbol} {t.side:<4} {t.entry_qty}x  "
                  f"{t.entry_price}→{t.exit_price}  net={t.net_pnl}  "
                  f"R={t.realized_R}  {t.exit_reason}")
    print("-" * 60)
    print("METRICS (CLOSED trades only):")
    for k in ("n_closed", "win_rate", "expectancy", "expectancy_R", "profit_factor",
              "avg_R", "avg_winner", "avg_loser", "max_drawdown", "recovery_factor",
              "avg_slippage", "avg_commission"):
        print(f"  {k:<16} {metrics.get(k)}")
    print(f"  excluded: open={metrics['excluded_open']} partial={metrics['excluded_partial']} "
          f"cancelled={metrics['cancelled']} rejected={metrics['rejected']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
