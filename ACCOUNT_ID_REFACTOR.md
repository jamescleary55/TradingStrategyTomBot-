# Account ID Refactor — alphanumeric, string, no coercion

**Date:** 2026-06-20 · **Status:** ✅ DONE + verified live · **Tests:** 97 pass

## Problem

IBKR account ids are alphanumeric (e.g. `DUQ834606`). The IBKR snapshot ran the
id through `_safe_int()`, which silently coerced `DUQ834606 → 0`. Any code that
routed, matched, or logged by `account_id` saw `0` instead of the real account —
a correctness and audit hazard before automated execution.

## Fix — `account_id` is a STRING end-to-end

| File | Change |
|---|---|
| `execution/base.py` | `AccountSnapshot.account_id: int → str` (with a comment forbidding coercion). `place_bracket` / `place_bracket_for_setup` / `snapshot` / `list_executions` param types `Optional[int] → Optional[str]`. `DryRunAdapter.snapshot` returns `""` not `0`. |
| `execution/ibkr_orders.py` | Snapshot now sets `account_id=str(acct)` — the raw managed-account string. **`_safe_int()` deleted.** |
| `execution/tradovate_orders.py` | Native numeric id stringified at the snapshot boundary: `account_id=str(account_id)`. |
| `execution/topstepx_orders.py` | Same: `account_id=str(aid)`. |
| `live/paper_trades_db.py` | Already `account_id TEXT` / `Optional[str]` — no change needed. |

Brokers whose native API genuinely uses integer ids (Tradovate, ProjectX) keep
their **internal** REST calls numeric where their API demands it; only the
`AccountSnapshot.account_id` field the rest of the bot sees is a string. No
numeric conversion is performed on the IBKR id anywhere.

## Regression tests (`tests/test_ibkr.py`)

- `test_snapshot_preserves_alphanumeric_account_id` — IBKR snapshot returns
  exactly `"DUQ834606"`, `isinstance str`, never `0`/`"0"`.
- `test_snapshot_account_id_is_string_type_in_dataclass` — dataclass keeps the
  value verbatim.
- `test_snapshot_position_account_filter_uses_string` — position account
  matching compares as strings (no `int()` on `DUQ834606`).

## Live verification

```
account_id: 'DUQ834606' type: str
PASS — live snapshot account_id is the exact string 'DUQ834606'
```

## Acceptance

- ✅ `account_id` remains exactly `"DUQ834606"`.
- ✅ No numeric conversion of the account id anywhere (`_safe_int` removed).
