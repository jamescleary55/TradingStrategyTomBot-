# Dashboard Hardening Report

**Date:** 2026-06-20 · **Status:** ✅ implemented · **Tests:** 139 passing
(+23 new) · **Verdict:** READY_FOR_ATTENDED_PAPER_RUN

Failure detection and operational visibility for the first supervised paper run.
All detection logic lives in a **pure, tested module** (`live/ops_health.py`);
`live/server.py` renders it. No strategy logic, parameters, markets, or risk
rules were touched.

## New alerts added (Phase 3 + 2 + 5)

| Alarm | Level | Trigger |
|---|---|---|
| POSITION_MISMATCH | critical | broker vs bot net position differ (flat-vs-position, qty, side) |
| LIVE_ACCOUNT_DETECTED | critical | account id not `DU*` |
| BROKER_DISCONNECTED | critical | broker read failed |
| KILL_SWITCH_ACTIVE | warning | halt file present |
| KILL_SWITCH_UNREADABLE | critical | kill-switch check errored (fail-safe halt) |
| AUTO_PAPER_SAFE_DISABLED | warning | auto-executing in a non-`auto_paper_safe` mode |
| UNEXPECTED_SYMBOL | critical | position/order outside the MES allowlist |
| QTY_OVER_MAX | critical | qty > 1 |
| DUPLICATE_ORDER | critical | same order id submitted twice |
| ORDER_WITHOUT_STOP | critical | submitted order with no stop |
| BRACKET_FAILURE | critical | a trade attempt recorded `outcome=failed` |
| DUPLICATE_SIGNAL_EXECUTION | critical | duplicate signal execution event |
| PENDING_ORDER_TIMEOUT | warning | order resting > 300s |
| DAILY_LOSS_LIMIT_EXCEEDED | critical | realized R ≤ −max_daily_loss_R |
| MAX_TRADES_EXCEEDED / OPEN_RISK_EXCEEDS_POLICY | warning | daily caps breached |

**Critical alarms latch** — they stay visible (with a Clear button) until the
operator dismisses them, and **re-latch automatically if the condition is still
active**, so a real ongoing danger cannot be clicked away.

## New panels added

| Panel | Phase | Content |
|---|---|---|
| **Supervision banner** (always visible, top) | 6 | ✓ SAFE TO SUPERVISE / ✗ OPERATOR ATTENTION REQUIRED + reasons |
| **Critical-alarm banner** (always visible) | 3 | latched criticals + Clear |
| **Data freshness** (Ops) | 1 | broker / heartbeat / last-event / dashboard, GREEN <10s · YELLOW 10–30s · RED >30s |
| **Reconciliation health** (Ops) | 4 | open/closed/partial, unmatched executions, duplicates ignored, errors → GREEN/YELLOW/RED |
| **Daily risk monitor** (Ops) | 5 | realized P&L, R, drawdown, open risk, trades today + breach alarms |

The supervision banner is the **first thing visible** on every tab.

## Data freshness semantics

`degraded` is driven only by sources that *should* update continuously — the
broker read and (when a monitor is running) its **heartbeat** (a new ~1s beacon
the monitor writes to `~/.ict-bot/monitor-heartbeat.txt`, removed on shutdown).
The **last-event** age is shown but deliberately **not** folded into `degraded`:
events are sporadic (signals/orders), so a quiet stream is normal, not a failure.
This prevents false "degraded" alarms during quiet periods while still catching a
frozen broker read or a dead/stalled monitor.

## Test coverage added (`tests/test_ops_health.py`, 23 tests)

Stale broker data · fresh-broker-not-degraded · stale heartbeat (monitor
running) · broker disconnect · live-account detection · kill-switch
active/unreadable · unexpected symbol & qty>max · duplicate order & no-stop ·
auto-paper-safe disabled · position mismatch (all four kinds) · reconciliation
health GREEN/YELLOW/RED · daily-loss-limit & today-only filtering · supervision
safe / blocked-with-reasons · latch persist-until-clear & ignores non-critical.

Plus an integration check through `/api/ops`: a simulated live-account + NQ×3 +
bot-flat state surfaces all four criticals, flips supervision to "attention
required", latches them, and re-latches after a clear while still active.

## Files changed

- **`live/ops_health.py`** (new) — all pure detection logic + latch.
- **`live/server.py`** — `/api/ops` rebuilt on ops_health; `POST /api/ops/clear`;
  freshness/recon-health/daily-risk panels; supervision + critical banners.
- **`live/monitor.py`** — ~1s heartbeat beacon (startup + main loop), cleared on exit.
- **`tests/test_ops_health.py`** (new) — 23 tests.

## How to run

```bash
python -m live.server --port 5005     # dashboard (dedicated IBKR client id 91)
# Banner + Ops tab update every 10s; criticals stay until cleared.
```

## Front-end verification (updated)

The dashboard JavaScript is now verified by **executing it in a real JS engine**
(macOS JavaScriptCore `jsc`), not just structurally:

- All 21 render functions run without error against a **populated dangerous
  state** (live account, position mismatch, critical alarms, degraded freshness)
  AND **empty/null states** — all return valid markup.
- The actual `refresh()` data→render→DOM flow runs end-to-end with mocked fetch:
  `live_label` becomes `live` (no exception path), the supervision banner renders
  "SAFE TO SUPERVISE", and all 11 key panels populate.

Run it yourself any time as a pre-flight gate:

```bash
python scripts/dashboard_selfcheck.py     # endpoints + front-end render → READY/NOT READY
```

Only the literal CSS pixel layout and tab-click interactions are unverified by
code (low risk — static CSS, simple tab switch with verified element ids); a
30-second eyeball in a browser is still worthwhile.

## Remaining operational risks
- **Open-risk in R is approximated** as net open contracts (≈1R each) rather than
  read from each order's live stop — fine for the 1-lot MES run; revisit if
  scaling.
- **PENDING_ORDER_TIMEOUT** needs the order's submit time in `live_trades.jsonl`;
  if absent it can't age the order (skips rather than false-alarms).
- **Polling at 10s** means up to ~10s detection latency; acceptable for attended
  supervision, but a hard fill→stop failure could occur between polls (the broker
  bracket, not the dashboard, is the real safety net there).
- Dashboard is the Flask dev server, single local operator; not hardened for
  network exposure.

## Verdict

**READY_FOR_ATTENDED_PAPER_RUN** — if anything dangerous happens during the first
paper trades (mismatch, live account, disconnect, stale data, missing stop,
duplicate, daily-loss breach), the supervision banner turns red, a latching
critical alarm appears, and it cannot be dismissed while the condition persists.
