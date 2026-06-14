# Paper-trading protocol

**Status:** operator behavioural checklist for the paper-validation
phase. Not a feature plan, not a roadmap — a list of rules the
operator commits to follow while running paper.

The sixth-pass adversarial review observed: "six reports written,
zero paper trades placed." This document is the contract that ends
that pattern.

---

## 0 · Read this first

> **The point of paper trading is to make the simulator wrong.**
> Every paper fill that disagrees with the modelled fill is a piece
> of evidence the audit could not produce. Until 50+ closed paper
> trades exist, no further adversarial review of the *strategy* is
> useful — the bottleneck is execution data, not analysis.

If a paper session feels like it confirmed the simulator, the
operator's job is to ask why and re-measure. If it feels like
the simulator was wrong, **good** — that's the evidence the audit
demanded.

---

## 1 · Pre-flight checklist (one-time)

Before any paper order goes out, verify:

- [ ] `python scripts/probe_broker.py --broker $BROKER` returns
      `[result  ] ok` for the chosen broker
- [ ] `~/.ict-bot/personal_rules.yaml` contains `mode: paper` (not
      `review`)
- [ ] `risk_per_trade_R` is **0.25** for the first 30 trades (one
      quarter of the strategy's normal sizing). Tighten the
      operator's psychological exposure while data is thin.
- [ ] `allowed_symbols` is **`[MES]`** for the first 30 trades. ES
      is the only symbol with a `CONTINUE_PAPER_TEST` verdict. NQ
      logs in signal-collection mode only.
- [ ] `max_daily_loss_R: 1.0`, `max_consecutive_losses: 2` —
      circuit breakers stay on
- [ ] `enable_auto_execute: false` until the first 5 paper trades
      have been manually triggered and reconciled successfully

---

## 2 · Daily protocol

### Before the session

- [ ] `python scripts/probe_broker.py` — confirm auth and snapshot
      still work
- [ ] `python -m live.signals_db sync` — pull yesterday's signals
      into SQLite
- [ ] `python -m live.paper_trades_db metrics` — print today's
      starting state

### During the session

- The monitor runs in `paper` mode. Every detected signal hits the
  risk gate. Approved signals trigger `paper_trade_runner` which
  places a bracket.
- Operator role: **observe and intervene if anything looks wrong**.
  Don't touch the strategy. Don't override decisions. Don't second-
  guess the gate. Note unusual events in a journal file:
  `~/.ict-bot/paper_journal.md`.
- Fill-disagreement examples worth journaling:
  - Limit hit but not filled
  - Stop filled at materially different price than requested
  - Partial fill where the simulator assumed full
  - Broker reject
  - Reconciler gap (order in `live_orders.jsonl` not joined to a
    fill within 10 min)

### After the session

- [ ] `python -m live.paper_trades_db metrics` — confirm counts
      increased
- [ ] Open trades from prior days: confirm they're either still
      open (carry forward) or were closed today
- [ ] If any trade has `outcome=open` after 5 trading days, manually
      flatten it or mark `timeout`

---

## 3 · Halt rules

Stop placing new orders if **any** of these fire. Restart only after
the cause is resolved and noted in `paper_journal.md`.

| Trigger | Why |
|---|---|
| 2 consecutive losses | Strategy + execution stress — investigate before more risk |
| Daily realised loss ≥ 1R | Daily loss cap (matches `personal_rules.yaml`) |
| 3 consecutive broker rejects | Broker integration is failing — fix before trading |
| Reconciler gap > 30 min on any open trade | Don't trade blind; resolve the join |
| Realised stop slip > 2× modelled NORMAL profile on any single trade | Execution model is materially off — pause to recalibrate |
| Largest open trade unrealised loss > 0.5R | Manual intervention point — the bot is bracket-only, but operator may flatten |
| `~/.ict-bot/KILL_SWITCH` exists | Manual halt — operator dropped the kill file |

---

## 4 · Daily journal entries (the only mandatory writing)

Every trading day, append to `~/.ict-bot/paper_journal.md`:

```
## 2026-06-13 NY_AM session

closed: 2 trades (1W 1L, +0.65R net)
open: 0
broker fills: 3 events
unexpected: stop on signal #abc123 filled 3 ticks worse than requested
            on MES (slip = 0.75 pts, NORMAL profile predicted 0.3 pts).
            Logged in paper_trades.db row #42.
halt triggered? no
next session: keep settings, monitor reconciler lag
```

No template enforcement — but the four lines `closed / open / fills
/ unexpected` are the minimum. The "unexpected" line is the most
important: it's the *data* that distinguishes a session that
informed the model from one that did not.

---

## 5 · Targets — when does paper-trading end?

The brief named two targets: 100+ signals and 50+ closed paper
trades. Translating to operator-visible milestones:

| Milestone | Threshold | What it unlocks |
|---|---|---|
| M1 — first real fill | 1 closed paper trade | All subsequent rounds of analysis are about reality, not models |
| M2 — calibration corpus | 30 closed paper trades, ≥ 10 stop hits | Run `python -m backtest.execution_calibrate` (to be built); update profile knobs |
| M3 — sufficient for verdict | 50 closed paper trades | Re-run `analysis/go_live` with the calibrated profile; report ES verdict in terms of *measured* execution, not modelled |
| M4 — go-live discussion | 100+ closed + go-live gates pass | Operator decides — this protocol does not |

**Timeline (operator's expectation, not a contract):**
ES at 1h fires ~12-14 closed trades per 180 days under NORMAL.
At 1 contract size, M3 = ~365 calendar days of paper. Faster
timeframes (30m, 15m) compress this — but those should be a
separate, deliberate decision and not a reaction to "paper is
slow." Slow is the cost of honest data.

---

## 6 · What signal-collection mode actually does

Per the controlled-RNG verdict report: signal-collection mode is
distinct from paper mode.

- **Paper mode**: places a real broker (demo) order. Fills, slip,
  rejects are MEASURED.
- **Signal-collection mode**: signal recorded to `signals.db`,
  marked `status='logged'`, **no broker order submitted**. The
  bot's theoretical fill outcome (from the simulator) is recorded
  for later analysis.

For the validation phase:

| Symbol | Mode |
|---|---|
| MES | paper (after first 5 dry-runs) |
| MNQ | signal-collection only — review tight-stop bucket replication |
| MCL | disabled (per Tier-1 NOT_PROVEN guardrail) |

---

## 7 · Go / no-go criteria (gate-level)

These mirror `analysis/go_live.py::evaluate()` but as a single-page
operator checklist. Real money authorisation requires **all** of:

| # | Criterion | Source |
|---|---|---|
| 1 | ≥ 50 closed paper trades on MES | `paper_trades.db` |
| 2 | Realised expectancy > +0.25R rolling 30 trades | `paper_trades.db` |
| 3 | Realised 5%ile > 0R (bootstrap over closed) | analyst computation |
| 4 | ≥ 25 measured stop fills with slip data | `paper_trades.db` |
| 5 | Realised stop slip ≤ 1.5× NORMAL-profile p50 | comparison vs `EXECUTION_CALIBRATION_PLAN.md` |
| 6 | All audit P0 findings resolved (marker file `~/.ict-bot/AUDIT_P0_RESOLVED` exists) | manual operator step |
| 7 | `analysis/go_live` reports `passed=True` | automated |
| 8 | No active halt triggers in past 7 days | `paper_journal.md` |
| 9 | Operator has written a single-paragraph "why now" justification in `paper_journal.md` | manual operator step |

**If any criterion fails, real money is not authorised.** No
overrides. No partial pass.

---

## 8 · What this protocol forbids

- Changing strategy parameters during the paper phase
- Adding new symbols outside MES + (signal-collection MNQ)
- Manually overriding the risk gate's decisions
- "Just one live trade" outside the protocol
- Writing more adversarial-review reports about the strategy until
  M2 (30 closed paper trades) is reached
- Switching brokers mid-validation without writing a one-paragraph
  "why" in `paper_journal.md` and resetting the count to M1

---

## 9 · Failure mode — what if paper expectancy goes negative?

The strategy's posterior was already < 50% before any of this
started. Paper data showing negative expectancy is not a surprise —
it's the *expected outcome* under the strict adversarial framing.

If rolling 30-trade expectancy is < 0R after 30+ closed trades:

1. Do NOT flip to "just keep going, sample is too small" without
   evidence
2. Do NOT switch strategies, timeframes, or markets — those are
   research decisions, not paper responses
3. Set `personal_rules.yaml::mode: review`. Stop the monitor.
4. Write a one-page summary: "paper showed -X.XR over N trades.
   Specific failure mode: [...]" and move it to
   `~/.ict-bot/post_mortem/`
5. Decide whether to invest more time in the strategy or shelve it

This protocol does NOT include a "what to do next after paper
fails" plan because that's a strategic question the operator owns,
not an automatable rule.

---

## 10 · Bottom line

Paper trading is not training, validation, optimisation, or
preparation. It's **measurement**. The strategy already has a
posterior verdict. Paper either confirms it or contradicts it. The
operator's only job here is to make the measurement clean.

Six rounds of adversarial review produced no new evidence. The
next 50 paper trades will produce more evidence than all six
combined. Place the trades.

— end of protocol —
