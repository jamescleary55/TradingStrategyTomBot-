"""Broker position poller.

Polls the configured broker adapter on a fixed cadence and surfaces every
*state transition* via the alerter:

- New position opened (fill detected)
- Position size changed (partial fill / scale-in / scale-out)
- Position closed (full exit — target / stop / manual)

Also writes per-tick snapshots to ``~/.ict-bot/positions.jsonl`` so the
live dashboard can render the recent account history.

Usage:
    python -m live.positions --poll 30
    python -m live.positions --once       # one snapshot and exit

Requires the configured BROKER's snapshot() implementation. Tradovate is
implemented; TopstepX stub will raise NotImplementedError until you wire
the ProjectX position endpoints.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.base import OpenPosition, get_adapter
from utils.alerter import Alerter

log = logging.getLogger("live.positions")
STATE_DIR = Path.home() / ".ict-bot"
STATE_DIR.mkdir(parents=True, exist_ok=True)
POSITIONS_LOG = STATE_DIR / "positions.jsonl"
STATE_FILE = STATE_DIR / "positions-state.json"

_should_stop = False


def _handle_signal(signum, frame):
    global _should_stop
    _should_stop = True


# ---------------------------------------------------------------------------
@dataclass
class PrevState:
    by_symbol: dict[str, dict] = field(default_factory=dict)
    last_equity: Optional[float] = None


def _load_state() -> PrevState:
    if not STATE_FILE.exists():
        return PrevState()
    try:
        d = json.loads(STATE_FILE.read_text())
        return PrevState(by_symbol=d.get("by_symbol", {}),
                         last_equity=d.get("last_equity"))
    except Exception:
        return PrevState()


def _save_state(state: PrevState) -> None:
    STATE_FILE.write_text(json.dumps({
        "by_symbol": state.by_symbol,
        "last_equity": state.last_equity,
    }, indent=2))


def _append_snapshot(snapshot) -> None:
    row = {
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "account_id": snapshot.account_id,
        "cash": snapshot.cash,
        "equity": snapshot.equity,
        "positions": [
            {"symbol": p.symbol, "side": p.side, "qty": p.qty,
             "avg_entry": p.avg_entry, "unrealised_pnl": p.unrealised_pnl}
            for p in snapshot.positions
        ],
    }
    with open(POSITIONS_LOG, "a") as f:
        f.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
def _diff_and_alert(state: PrevState, snapshot, alerter: Alerter) -> int:
    """Compare current snapshot vs previous state; fire alerts on changes."""
    new_by_sym = {p.symbol: p for p in snapshot.positions}
    n_events = 0

    # Closed positions (were in prev, gone now)
    for sym, prev in list(state.by_symbol.items()):
        if sym not in new_by_sym:
            alerter.notify(
                f"Position closed: {sym}",
                f"Side {prev['side']}  qty {prev['qty']}  avg entry {prev['avg_entry']:.2f}\n"
                f"Final unrealised P&L (last seen): "
                f"${(prev.get('unrealised_pnl') or 0):,.2f}",
                severity="success" if (prev.get('unrealised_pnl') or 0) >= 0 else "warning",
            )
            n_events += 1

    # Opened or changed
    for sym, p in new_by_sym.items():
        prev = state.by_symbol.get(sym)
        if prev is None:
            alerter.notify(
                f"Position opened: {sym}",
                f"{p.side}  qty {p.qty}  avg entry {p.avg_entry:.2f}\n"
                f"Unrealised: ${(p.unrealised_pnl or 0):,.2f}",
                severity="info",
            )
            n_events += 1
        elif prev["qty"] != p.qty:
            direction = "added" if p.qty > prev["qty"] else "reduced"
            alerter.notify(
                f"Position {direction}: {sym}",
                f"Was {prev['qty']} → now {p.qty}  (avg {p.avg_entry:.2f})\n"
                f"Unrealised: ${(p.unrealised_pnl or 0):,.2f}",
                severity="info",
            )
            n_events += 1

    state.by_symbol = {
        sym: {"side": p.side, "qty": p.qty, "avg_entry": p.avg_entry,
              "unrealised_pnl": p.unrealised_pnl}
        for sym, p in new_by_sym.items()
    }
    state.last_equity = snapshot.equity
    return n_events


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Live broker position poller")
    parser.add_argument("--poll", type=int, default=30, help="Seconds between snapshots")
    parser.add_argument("--account-id", default=None,
                        help="Account id (string; broker ids are alphanumeric)")
    parser.add_argument("--broker", default=None, help="Override BROKER env var")
    parser.add_argument("--once", action="store_true",
                        help="One snapshot then exit (for smoke tests)")
    parser.add_argument("--reset", action="store_true",
                        help="Clear stored state (treats current positions as 'just opened')")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()
        log.info("State cleared.")

    adapter = get_adapter(args.broker)
    alerter = Alerter()
    state = _load_state()
    log.info("Polling %s for positions every %ds (prev equity = %s, %d known)",
             adapter.name, args.poll, state.last_equity, len(state.by_symbol))

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    def one_tick(i: int):
        try:
            snap = adapter.snapshot(account_id=args.account_id)
        except NotImplementedError as e:
            log.error("%s", e)
            return False
        except Exception:
            log.exception("snapshot failed")
            return True  # keep going

        _append_snapshot(snap)
        n = _diff_and_alert(state, snap, alerter)
        _save_state(state)
        log.info("tick #%d: equity $%.2f  ·  %d open position(s)  ·  %d event(s)",
                 i, snap.equity, len(snap.positions), n)
        return True

    if args.once:
        one_tick(1)
        return

    tick = 0
    while not _should_stop:
        tick += 1
        if not one_tick(tick):
            return
        for _ in range(args.poll):
            if _should_stop:
                break
            time.sleep(1)
    log.info("Stopped.")


if __name__ == "__main__":
    main()
