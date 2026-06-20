"""EMERGENCY: flatten the account — cancel all orders, close all positions.

Operator-only. This is the panic button. It is NEVER called automatically by
the monitor or any scheduled job; you run it by hand when you want the account
forced flat right now.

Safety:
  - Defaults to a DRY RUN (shows what it would do, sends nothing).
  - Refuses to act on a non-paper account unless --allow-live is passed.
  - Requires typing the account id to confirm a real (non-dry) flatten.

Usage:
    python scripts/flatten_account.py                 # dry run (default)
    python scripts/flatten_account.py --execute       # really flatten (paper)
    python scripts/flatten_account.py --execute --yes  # skip the typed confirm

Every action is logged to ~/.ict-bot/events.jsonl and printed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.base import get_adapter
from live.forward_log import EV_SYSTEM, log_event


def main() -> int:
    ap = argparse.ArgumentParser(description="Emergency flatten + cancel-all (operator only).")
    ap.add_argument("--broker", default="ibkr", help="broker adapter (default ibkr)")
    ap.add_argument("--account", default=None, help="account id (default: broker's first)")
    ap.add_argument("--execute", action="store_true",
                    help="actually cancel/close. Without this it's a DRY RUN.")
    ap.add_argument("--allow-live", action="store_true",
                    help="permit flattening a non-paper (non-DU) account. USE WITH CARE.")
    ap.add_argument("--yes", action="store_true", help="skip the typed confirmation prompt")
    args = ap.parse_args()

    dry_run = not args.execute
    adapter = get_adapter(args.broker)

    if not hasattr(adapter, "flatten_and_cancel_all"):
        print(f"[ERROR] {args.broker} adapter has no flatten_and_cancel_all()")
        return 1

    # Look before you leap — show the account and its paper/live status.
    try:
        snap = adapter.snapshot()
        acct = snap.account_id
        is_paper = (acct or "").startswith("DU")
        print(f"[account ] {acct}  ({'PAPER' if is_paper else 'NON-PAPER'})  "
              f"cash={snap.cash:,.2f} {snap.currency or ''}  positions={len(snap.positions)}")
    except Exception as e:
        print(f"[account ] could not snapshot: {e}")
        acct = args.account or ""
        is_paper = (acct or "").startswith("DU")

    if not dry_run and not is_paper and not args.allow_live:
        print("[BLOCKED] refusing to flatten a non-paper account without --allow-live.")
        return 2

    if not dry_run and not args.yes:
        typed = input(f"Type the account id ({acct}) to confirm a REAL flatten: ").strip()
        if typed != acct:
            print("[ABORTED] confirmation did not match.")
            return 3

    log_event(EV_SYSTEM, "flatten_requested", severity="warning",
              detail=f"dry_run={dry_run} account={acct}")
    report = adapter.flatten_and_cancel_all(account_id=args.account, dry_run=dry_run)
    log_event(EV_SYSTEM, "flatten_result", severity="warning",
              detail=f"flat={report.get('flat')} errors={len(report.get('errors', []))}",
              report=report)

    print(json.dumps(report, indent=2, default=str))
    if report.get("errors"):
        print(f"[WARN] {len(report['errors'])} error(s) during flatten")
        return 1
    if dry_run:
        print("[DRY RUN] nothing was sent. Re-run with --execute to act.")
        return 0
    print("[OK] flat" if report.get("flat") else "[WARN] account NOT confirmed flat")
    return 0 if report.get("flat") else 1


if __name__ == "__main__":
    sys.exit(main())
