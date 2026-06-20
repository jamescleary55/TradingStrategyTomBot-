# Execution Safety Gates — Phases 2-5

**Date:** 2026-06-20 · **Status:** ✅ implemented + tested (97 pass)

No automated paper order is submitted unless it passes a mandatory, fail-safe
preflight. The gate is pure and unit-tested; the guard wires it to the live
broker; the monitor calls the guard immediately before every order.

## Phase 2 — the ten mandatory conditions

`risk/exec_gate.py :: evaluate_execution_gate(...) -> GateResult`. Execution is
blocked unless **all ten** hold. The gate is **non-short-circuit** — it returns
*every* failing reason at once for clean logging.

| # | Condition | Blocks when |
|---|---|---|
| 1 | broker == ibkr | any other broker |
| 2 | account starts with `DU` | non-paper account id |
| 3 | mode == paper | mode != paper |
| 3 | not a live account | `live_account` true |
| 4 | snapshot succeeded | snapshot is `None` (partial is OK) |
| 5 | market data valid | status != `LIVE` and no `--data-override` |
| 6 | kill switch absent | any sentinel file present |
| 7 | flat | open positions != 0 |
| 8 | no duplicate signal | setup already executed this run |
| 9 | no pending order | a resting order already exists |
| 10 | qty in [1, 1] | qty < 1 or qty > 1 |

On a NO-GO the monitor logs `skipped_setups.jsonl` (`rule=exec_gate`), emits a
`system/gate_block` event with the exact reasons, alerts the operator, and
`continue`s — **no order is sent.**

## Phase 3 — kill switch (`risk/kill_switch.py`)

Instant operator halt. Trips on either the configured
`personal_rules.yaml :: kill_switch_path` (default `~/.ict-bot/KILL_SWITCH`) **or**
any conventional flag dropped in `~/.ict-bot`: `KILL_SWITCH`, `kill_switch.txt`,
`halt.flag`, `kill.json`, `STOP`.

```bash
touch ~/.ict-bot/KILL_SWITCH     # halt now
rm    ~/.ict-bot/KILL_SWITCH     # resume
```

**Fail-safe:** the check is read-only and never raises; any error is treated as
*present* (halt), never absent. Checked **first** in every preflight and logged
as `system/kill_switch`.

## The wiring — `live/exec_guard.py :: preflight(...)`

One choke point. It: checks the kill switch → takes one account snapshot
(failure → `None` → hard block, logged as `system/snapshot_timeout`) → reads
open orders (read failure → assume pending, fail-safe) → checks the
duplicate-signature set → calls the gate → logs the decision. Returns
`PreflightResult(gate, snapshot)`.

Wired in `live/monitor.py`: for `mode in {paper, auto_paper_safe}` the order is
gated; a NO-GO is logged and skipped. After a successful submit the setup
signature is added to `executed_signatures` so the same setup can't fire twice.

## Phase 5 — observability (`live/forward_log.py :: log_event`)

Append-only `~/.ict-bot/events.jsonl`, categorised:

| category | events |
|---|---|
| `signal` | detected · rejected · executed |
| `order` | submitted · failed (accepted/cancelled/rejected reserved for reconcile) |
| `execution` | fill · partial_fill · stop_hit · target_hit (from reconcile) |
| `system` | snapshot_timeout · snapshot_partial · open_orders_read_failed · kill_switch · gate_block · data_failure · flatten_requested/result |

## Tests

`tests/test_safety.py` (gate conditions 8/9/10, kill switch, preflight GO/NO-GO
with a faked adapter) + `tests/test_exec_gate.py` (original 1-7) +
`tests/test_ibkr.py` (snapshot, list_open_orders, flatten). All green.
