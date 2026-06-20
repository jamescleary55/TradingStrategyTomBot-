"""GO / NO-GO readiness reader for the controlled paper run (Phase 7).

Reads the forward-test logs under ~/.ict-bot and reports each go/no-go
criterion as PASS / PENDING / FAIL, plus any automatic NO-GO trigger. This
makes readiness measurable instead of a matter of opinion.

It is READ-ONLY. It evaluates *operational validation* criteria — NOT
profitability optimisation. Expectancy / profit-factor require reconciled
round-trips; until enough fills exist they report PENDING (not FAIL).

    python scripts/go_no_go.py

Thresholds (from the directive):
  - 100+ signals observed
  - 50+ paper trades (order attempts)
  - 25+ actual fills
  - positive expectancy, profit factor > 1.2
  - no critical execution failures / orphan positions / duplicate orders /
    stop-loss failures / account-routing mistakes

Automatic NO-GO if any of: live account accessed, order submitted without a
stop, kill-switch failure, duplicate execution, snapshot hang.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live.forward_log import load_events, load_signals, load_trades

SIGNALS_MIN = 100
TRADES_MIN = 50
FILLS_MIN = 25
PROFIT_FACTOR_MIN = 1.2

PASS, PENDING, FAIL = "PASS", "PENDING", "FAIL"
MARK = {PASS: "✅", PENDING: "⏳", FAIL: "❌"}


def _count_fills(events: list[dict], trades: list[dict]) -> int:
    n = sum(1 for e in events if e.get("category") == "execution"
            and e.get("event") in ("fill", "partial_fill"))
    if n == 0:   # fall back to trade outcomes if execution events absent
        n = sum(1 for t in trades if t.get("outcome") == "filled")
    return n


def _critical_triggers(events: list[dict], trades: list[dict]) -> list[str]:
    """Automatic NO-GO triggers detectable from the logs."""
    trig: list[str] = []

    # live account accessed — any gate input or order on a non-DU account
    for e in events:
        acct = str(e.get("account_id") or "")
        if acct and not acct.startswith("DU"):
            trig.append(f"non-paper account seen in events: {acct}")
            break

    # order submitted without a stop
    for t in trades:
        if t.get("outcome") == "submitted" and t.get("intended_stop") in (None, "", 0, 0.0):
            trig.append(f"order without stop (order_id={t.get('order_id')})")
            break

    # snapshot hang / timeout
    if any(e.get("event") == "snapshot_timeout" for e in events):
        trig.append("snapshot timeout/hang recorded")

    # duplicate execution — same order_id submitted twice
    submitted_ids = [t.get("order_id") for t in trades
                     if t.get("outcome") == "submitted" and t.get("order_id") not in (None, 0)]
    dupes = [oid for oid, c in Counter(submitted_ids).items() if c > 1]
    if dupes:
        trig.append(f"duplicate order ids submitted: {dupes}")

    return trig


def main() -> int:
    signals = load_signals()
    trades = load_trades()
    events = load_events()

    n_signals = len(signals)
    n_trades = sum(1 for t in trades if t.get("outcome") in ("submitted", "filled"))
    n_fills = _count_fills(events, trades)

    gate_blocks = sum(1 for e in events if e.get("event") == "gate_block")
    order_fails = sum(1 for e in events if e.get("category") == "order" and e.get("event") == "failed")
    kill_trips = sum(1 for e in events if e.get("event") == "kill_switch")

    rows = [
        ("Signals observed (>=100)", PASS if n_signals >= SIGNALS_MIN else PENDING, f"{n_signals}"),
        ("Paper trades (>=50)", PASS if n_trades >= TRADES_MIN else PENDING, f"{n_trades}"),
        ("Actual fills (>=25)", PASS if n_fills >= FILLS_MIN else PENDING, f"{n_fills}"),
        ("Positive expectancy", PENDING, "needs reconcile (run live/reconcile)"),
        (f"Profit factor (>{PROFIT_FACTOR_MIN})", PENDING, "needs reconcile"),
    ]

    triggers = _critical_triggers(events, trades)

    print("=" * 64)
    print(" GO / NO-GO — controlled paper run (operational validation)")
    print("=" * 64)
    for name, status, detail in rows:
        print(f" {MARK[status]} {status:<8} {name:<32} {detail}")
    print("-" * 64)
    print(f"   observability: gate_blocks={gate_blocks}  order_failures={order_fails}  "
          f"kill_switch_trips={kill_trips}")
    print("-" * 64)

    if triggers:
        print(" ❌ AUTOMATIC NO-GO — critical trigger(s):")
        for t in triggers:
            print(f"      - {t}")
        verdict = "NO-GO"
    elif all(s == PASS for _, s, _ in rows):
        verdict = "GO"
    else:
        verdict = "NO-GO (criteria not yet met — keep collecting)"

    print(f"\n VERDICT: {verdict}")
    print(" Note: this validates plumbing + safety, not edge/profitability.")
    return 0 if verdict == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())
