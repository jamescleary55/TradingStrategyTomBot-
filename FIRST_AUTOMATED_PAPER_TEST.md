# First Automated Paper Test — design (Phases 7-8)

**Date:** 2026-06-20 · **Status:** designed, NOT yet run · **Goal:** validate
plumbing, not profitability.

This is the first time the bot places orders on its own. We validate the chain:

```
signal → order → fill → stop/target → logging
```

## Preconditions (all currently TRUE)

- IB Gateway up on `127.0.0.1:4002`, Read-Only API off, paper account `DUQ834606`.
- Snapshot returns < 1s (verified), `account_id` == `"DUQ834606"` (verified).
- Order submit→cancel path proven (earlier smoke test, orderId 6).
- 97 unit tests pass; flatten dry-run + `list_open_orders` verified live.

## Test parameters (fixed — do not tune)

| Param | Value |
|---|---|
| Mode | `auto_paper_safe` |
| Symbol | MES only |
| Qty | 1 |
| Max positions | 1 |
| Account | `DUQ834606` (paper) |
| Broker | ibkr |

## Procedure

1. **Pre-flight (operator):**
   ```bash
   python scripts/probe_broker.py --broker ibkr   # account + snapshot OK
   python scripts/flatten_account.py              # dry-run: confirm flat
   ```
2. **Arm the kill switch path** (know the halt command before starting):
   `touch ~/.ict-bot/KILL_SWITCH` halts; `rm` resumes.
3. **Run** (see `AUTO_PAPER_SAFE.md`), with `--data-override` if live L1 isn't
   subscribed.
4. **Observe** `~/.ict-bot/events.jsonl` for the expected sequence per setup:
   `signal/detected → system/gate_block?` (if blocked, why) →
   `order/submitted → signal/executed →` (later, via reconcile)
   `execution/fill → execution/stop_hit|target_hit`.
5. **Stop** with Ctrl-C or the kill switch. Then:
   ```bash
   python scripts/flatten_account.py --execute    # leave the account flat
   python scripts/go_no_go.py                      # readiness snapshot
   ```

## Pass criteria for THIS test (plumbing only)

- At least one order submitted through the gate to the paper account.
- Bracket carried a stop and a target (geometry enforced by `place_bracket`).
- Every state change appears in `events.jsonl`.
- No duplicate order for the same setup; never more than one open position.
- Kill switch halts new orders within one tick when tripped.
- No `system/snapshot_timeout` events.

## Go/No-Go for any future LIVE-capital discussion (Phase 7)

Measured by `scripts/go_no_go.py`:

- 100+ signals · 50+ paper trades · 25+ fills
- positive expectancy · profit factor > 1.2 (require reconciled round-trips)
- no critical execution failures / orphan positions / duplicate orders /
  stop-loss failures / account-routing mistakes

**Automatic NO-GO** if any occur: live account accessed · order without a stop ·
kill-switch failure · duplicate execution · snapshot hang. The reader detects
these from the logs and forces NO-GO.

Current reading: **NO-GO** (criteria not yet met — collection just starting).
