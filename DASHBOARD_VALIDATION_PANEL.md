# Dashboard — Operational Validation Panel

**Date:** 2026-06-20 · **Status:** ✅ implemented · **Tests:** 116 passing ·
**Verdict:** READY_FOR_ATTENDED_PAPER_RUN_DASHBOARD

An operational-validation dashboard for the attended AUTO_PAPER_SAFE run. It
answers, at a glance, whether the bot is safe to supervise right now and what the
*reconciled* numbers actually say — no strategy tuning, no performance claims.

## Files changed

| File | Change |
|---|---|
| `live/server.py` | New `/api/reconciliation` + `/api/ops` endpoints; removed legacy `/api/stats` (forward_report) from the UI; new **Ops** + **Trades** tabs; safety strip; equity curve; account-id fix; dedicated dashboard IBKR client id (`--ib-client-id`, default 91). |
| `live/monitor.py` | Writes `~/.ict-bot/monitor-runtime.json` (mode, auto_execute, symbols, data_status, PID) at startup; removes it on clean shutdown — lets the ops panel show what's actually running. |
| `live/positions.py` | `--account-id` is now a string (alphanumeric broker ids). |

## Tabs

| Tab | Content | Source |
|---|---|---|
| **Now** | Safety strip + reconciliation KPIs + recent closed trades + open positions | `/api/ops`, `/api/reconciliation` |
| **Ops** | Acceptance-question chips, connection & account, run state, pending orders, recent gate blocks, flatten reminder | `/api/ops` |
| **Trades** | Metric cards, **equity curve** (cumulative net P&L), metric detail, closed-trade ledger | `/api/reconciliation` |
| **Alerts** | Every signal logged | `/api/alerts` (live_signals.jsonl) |
| **Positions** | Broker account snapshot | `/api/positions` / ops broker read |

## Data sources

- **Reconciliation/metrics:** `reconciliation.reconcile` + `compute_metrics` over
  `~/.ict-bot/live_executions.jsonl` (+ `live_trades.jsonl` for intended
  entry/stop/target). Pure/file-based — never touches the broker, always works.
  **Metrics are CLOSED-trades only** (labeled in the UI).
- **Ops:** a cached (8s TTL), fail-safe broker read (snapshot + open orders) on a
  **dedicated client id** so it never collides with the running monitor; degrades
  to `positions.jsonl` and reports "offline" if the gateway is down. Plus the
  kill-switch file, `events.jsonl` gate blocks, and `monitor-runtime.json`.

## Legacy stats removed / replaced

The old **Performance** tab used `live.forward_report.compile_report()` (alert-
derived, not reconciliation-backed). It is **removed from the UI** and replaced by
the **Trades** tab driven by the production reconciliation engine. `/api/stats`
was deleted. No non-reconciliation performance numbers remain in the dashboard.

## How to run

```bash
cd ~/projects/ict-futures-bot && source .venv/bin/activate
python -m live.server --port 5005            # http://127.0.0.1:5005/
# runs safely alongside the monitor (distinct IBKR client id 91)
```

Pair it with the attended run: `live.monitor --mode auto_paper_safe` populates
the Ops "run state"; `first_trade_watch.py` / `live.reconcile` populate
`live_executions.jsonl` which the Trades tab reconciles.

## Acceptance criteria — answered by the dashboard

| Question | Where |
|---|---|
| 1. Safe to supervise now? | Ops chip `Safe to supervise` (kill-switch absent ∧ broker ok ∧ paper) |
| 2. Paper account only? | Ops chip `Account: PAPER` + account id `DUQ834606` |
| 3. Flat or in position? | Ops chip `Position` + positions list |
| 4. Pending orders? | Ops `Pending orders` count + table |
| 5. Did any gate block execution? | Ops `Recent gate blocks` (events.jsonl) |
| 6. How many reconciled closed trades? | Trades `Closed trades` KPI |
| 7. What do the real metrics say? | Trades cards + ledger (CLOSED only) |

## Verification

- All endpoints return 200 against the live paper account (`/api/ops` read
  `DUQ834606`, paper, flat via the dedicated client id).
- Reconciliation endpoint validated with a synthetic winner+loser: net +48.76 /
  −31.24, PF 1.56, win-rate 0.5, max DD 31.24 — matches hand calculation.
- Embedded JS: all element ids referenced are defined; render functions all
  present. 116 unit tests pass; server/monitor/positions import clean.

## Known limitations

- **No fills yet** — the Trades tab is empty until the first reconciled CLOSED
  trade (market opens Sunday ~21:00 UTC). Empty states are handled.
- **Ops "run state"** needs the monitor running to populate mode/data-status
  (PID-liveness checked); otherwise shows "not running".
- **Phase 4 (charts):** delivered the low-risk **equity curve** (inline SVG). The
  full price-chart-with-entry/stop/target/fill markers (lightweight-charts) is
  **deferred** — it needs bar-data plumbing and was higher-risk than the "only if
  low-risk" rule allows; not built so it wouldn't delay Phases 1-3.
- Flask dev server (single user / local); not hardened for exposure.
- No one-click trading; emergency actions are shown as commands, not buttons.

## Verdict

**READY_FOR_ATTENDED_PAPER_RUN_DASHBOARD** — the dashboard answers all seven
acceptance questions, surfaces reconciliation-backed metrics only, and runs
safely alongside the live monitor.
