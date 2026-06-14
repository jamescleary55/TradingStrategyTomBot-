# Pre-launch adversarial audit

**Stance:** Treat the strategy as **probably worthless** until live forward
data proves otherwise. The job here is to find every reason the backtest is
lying and the live system might burn money.

Severity / Likelihood / Impact use:

- **P0** (must-fix before any paper exec) · **P1** (must-fix before live money)
- **P2** (must-fix before scaling beyond 1 contract) · **P3** (improve when time permits)

---

## A · Architecture audit

### A1 · `RiskGate` daily-loss / weekly-loss / consecutive-loss gates are silently broken — **P0**

**Location:** `risk/controls.py:137–168` reads `r_realised` from trade rows.
**Impact:** No code path in the monitor/webhook/position-poller currently *writes*
`r_realised`. `log_trade_attempt` is called at submission with `outcome="submitted"` only.
`live/positions.py` polls broker positions but never reconciles back to the trade log.
**Net effect:** in paper mode the bot will happily keep opening trades past
the configured `max_daily_loss_R` because the gate sees `day_R == 0.0` always.
The kill-switch / mode / RR / score / max-trades-per-day gates still work, but
the financial-cap gates are **inoperative**.

**Likelihood:** 100% (definitely broken right now).
**Fix:** introduce a `live/reconcile.py` job that, every poll, joins the trade
log with the broker's order-history endpoint (Tradovate has `/order/list` and
`/executionReport/list`) and writes an updated row to `live_trades.jsonl` with
`outcome`, `exit_price`, `slippage_pts`, `r_realised`. **Treat the daily-loss
gate as non-functional until reconciliation lands.** Add a self-check assertion
in `RiskGate.check()` that prints a one-time warning when no row in `load_trades()`
has `r_realised` despite `outcome ∈ {target, stop, filled}`.

### A2 · Race condition: multi-symbol gate check is non-atomic — **P1**

**Location:** `live/monitor.py:_watch_loop` spawns one thread per symbol. Each
thread calls `risk_gate.check()` then submits a trade. Between the gate's read
of `load_trades()` and the writer's append, another thread can pass the same
gate.
**Concrete failure:** with `max_trades_per_day: 3`, three concurrent setups in
the same minute can all see 0 trades today, all pass, all submit. You end up
with 4 trades that day.

**Likelihood:** Medium during news / killzone overlap (when concurrent setups
are common).
**Fix:** introduce a single `threading.Lock` shared by all gates in the process,
held for the gate check + the immediately-following trade submission. Long-term:
move execution into a single-writer queue thread, where worker threads only
*propose* setups.

### A3 · `_today_et()` returns UTC, not ET — **P1**

**Location:** `risk/controls.py:42–44`.
**Concrete failure:** between 19:00–23:59 ET (24:00–04:59 UTC, depending on DST),
a setup fires; its `ts_logged` is on UTC date `T+1`. The gate at that moment
treats today as `T+1`, so the daily-trade counter resets early. Operator
expectation: trading day ends 17:00 ET (CME futures session boundary).

**Likelihood:** 100% within that window.
**Fix:** compute `dt.datetime.now(tz=ZoneInfo("America/New_York")).date()` and
align both the gate and the report's "today" semantics to the CME 17:00 ET roll.

### A4 · `session is None` silently bypasses session-allowlist — **P1**

**Location:** `risk/controls.py:85` — `if setup.session and setup.session not in r.allowed_sessions`.
When `current_session()` returns `None` (outside ASIA/LONDON/NY_AM/NY_PM —
includes lunch, early Asian session start, and a 1-hour gap before London),
the gate **does not reject**.
**Concrete failure:** with `allowed_sessions: [LONDON, NY_AM]`, a setup that
fires at 10:30 UTC (London ended at 09:00 UTC, NY_AM not started until 11:00 UTC)
will pass session gate.

**Fix:** invert the test: reject unless `setup.session in r.allowed_sessions`.
Make `None` mean "reject".

### A5 · `_open_from_logs()` cannot decrement without bug #A1 fixed — **P1**

**Location:** `risk/controls.py:172–184`. Increments on `submitted/filled`,
decrements on `target/stop/rejected/failed/voided`. Since monitor never writes
`target/stop` to the trade log (depends on A1), counter only grows.
**Concrete failure:** after 1 trade is logged as `submitted` and fills + closes
at TP, the gate still thinks 1 position is open. With `max_open_positions: 1`,
all subsequent setups are blocked.

**Fix:** depends on A1.

### A6 · Live monitor consumes possibly-incomplete current bar — **P0 for live, P1 for review**

**Location:** `live/monitor.py:_tick`. `load_bars(days=...)` returns yfinance bars
including the current (still-forming) bar.
**Concrete failure:** an FVG defined as `bar[i-2].high < bar[i].low` is "detected"
mid-bar when `bar[i].low` happens to be above the threshold by chance. Five
minutes later that low ticks below and the FVG disappears. The monitor has
already alerted, possibly executed.

**Likelihood:** 50%+ on every poll near a candle close.
**Fix:** in the strategy adapter (`signals/strategies/sweep_choch_fvg.py`),
slice `df = df.iloc[:-1]` if the last bar's `timestamp + tf_duration > now`.
Document this guarantee in the `Strategy` interface.

### A7 · No `signal_id` linking signal → skip → trade rows — **P2**

**Location:** `live/forward_log.py` emits three JSONL files but no shared key.
**Impact:** reconstructing the lifecycle of a single setup requires fragile
timestamp + (entry, stop, target) matching.

**Fix:** generate `signal_id = uuid4()` in the strategy adapter, propagate
through `StrategySetup`, all log calls write it, all reports group on it.

### A8 · Logs are append-only without `fsync()` — **P2**

**Location:** `live/forward_log.py:_append` uses standard buffered append.
**Impact:** OS crash or `kill -9` can drop the tail of `live_trades.jsonl`. For
an audit log of trades against real money, that's not acceptable. (Same for
the kill switch — `touch ~/.ict-bot/KILL_SWITCH` is durable, good.)

**Fix:** `f.flush(); os.fsync(f.fileno())` after every append.

### A9 · No backpressure on webhook — **P2**

**Location:** `live/webhook.py:webhook()`. Each POST does file IO, risk gate
file scan, alerter (which may render a chart), and possibly broker call.
**Impact:** a spammy webhook (TradingView "alert every bar close" on 1m) can
DOS the bot. Disk fills, alerter delays, log mutex contention.

**Fix:** add a per-IP token bucket (e.g., max 10/min) in front of `/webhook`.
Reject 429 over the limit. Also require the shared secret in production.

### A10 · `setup_score` heuristic is uncalibrated — **P3**

**Location:** `signals/strategies/sweep_choch_fvg.py:_score`. Hand-picked
weights with no grounding in realised outcomes.
**Impact:** `min_setup_score: 0.55` in personal_rules is therefore arbitrary
and could be silently filtering good setups or admitting bad ones.

**Fix:** once forward data has ≥ 30 closed trades, regress `r_realised` on the
individual score components. Drop or re-weight whichever don't correlate. For
now, treat `setup_score` as informational only — do not include in production
gate.

---

## B · Risk audit

### B1 · Slippage is a flat 1 tick everywhere — **P0 for any deployment**

**Location:** `backtest/simulator.py:_apply_slippage`. Applied symmetrically
to entry + exit, always 1 tick adverse.
**Reality:** stop orders during a CPI / NFP spike on NQ commonly slip 4–10
ticks. Limit-at-FVG-mid fills are *optimistic*: in a fast move, your limit
sits in queue and may not fill at all even though price tagged it.
**Impact on backtest results:** quoted expectancy of +0.96R likely
overstated by 0.2–0.4R once realistic execution is modelled.

**Fix:**
1. Stop slippage: ATR(20)-fraction model (e.g. 0.25 × ATR for normal vol,
   ramp to 1.0 × ATR within ±15 min of news).
2. Limit-at-FVG entry: require *trade-through* (price must cross the limit
   by N ticks within the bar) rather than just touch.
3. Configurable per-instrument because tick-spacing and depth differ.

### B2 · No reconciliation against actual broker fills — **P0 before live money**

**Location:** there is no `execution/reconcile.py`. `live/positions.py` polls
positions but never connects an open position back to the trade row that
opened it.
**Impact:** every R-based gate (daily, weekly, consecutive) is blind. P&L
in the dashboard is fabricated from intended prices, not actual ones.

**Fix:** add a reconciler that:
1. Polls `/order/list` + `/executionReport/list` every 30s.
2. For each new execution event, finds the matching `live_trades.jsonl` row
   by `order_id` and writes an *update* row containing `fill_price`, `exit_price`,
   `slippage_pts`, `r_realised`, `outcome` (overwriting the file is fine if
   atomic).
3. Surfaces *unreconciled* rows after T+5min in the dashboard as a red flag.

### B3 · `RISK.max_concurrent_positions` is checked in the simulator but `personal_rules.max_open_positions` is what the live gate uses — **P2**

**Inconsistency.** Simulator uses `config.RISK.max_concurrent_positions` (default 1).
Live gate uses `rules.max_open_positions`. Two different knobs for the same
intent; will drift.

**Fix:** delete `RISK.max_concurrent_positions`, route the simulator through
`PersonalRules` too.

### B4 · No "kill the open position" mechanism — **P1**

**Operator scenario:** kill switch goes on. New trades blocked. But the
position opened 30s before the kill switch is still live, with its bracket
at the broker. If you want to flatten immediately, you have to log into
Tradovate manually.

**Fix:** add `python -m live.flatten --confirm` that sends market-close for
every open position via the broker adapter. Triggered manually only.

### B5 · No instrument-level risk budget — **P3**

**Currently:** `risk_per_trade_R` is global. A losing day on CL is treated
equally with a losing day on MNQ even though their volatility regimes differ.

**Fix (later, after the first 100 forward signals):** per-instrument
`risk_budget` block in personal_rules. Out of scope for the 4-week test.

---

## C · Data quality audit

### C1 · yfinance is the only live feed and it's delayed — **P0 for live, P1 for paper**

**Location:** `data/yfinance_feed.py`, used by both backtest and live monitor.
**Reality:**
- Futures data on yfinance is **15-minute delayed**.
- Pre-/post-market and overnight session bars are inconsistent and sometimes
  missing.
- Yahoo intraday rate limits (~2000 req/day across all symbols).

**Impact:** in live mode, you're trading on bars that were "current" 15
minutes ago. Setups may have already been invalidated. Backtest *also* uses
this data — if you're going to live-trade against Tradovate's real-time feed,
your backtest expectancy is computed on a different distribution than what
will execute.

**Fix:** for paper/live, **stop using yfinance**. Wire `data/tradovate_feed.py`
WS-streaming path into the monitor. For backtest, accept that yfinance is the
free option, document its delay characteristics, and re-run the canonical
6-month backtest on Tradovate historical (paid) before betting any money.

### C2 · `previous_day_levels` / `previous_week_levels` use raw groupby on the index date — **P2**

**Location:** `engine/liquidity.py:previous_day_levels`. Groups by
`et_index.date` then uses the high/low of all bars in that group.
**Reality:** futures trading day runs 18:00 ET (Sunday open) to 17:00 ET next
day, with a 1-hour break. A "day" in this groupby spans two calendar dates and
should be defined by the 17:00 ET session boundary. The current code reports
PDH/PDL that mix Tuesday afternoon Asian session bars with Wednesday morning
European bars.

**Fix:** translate timestamps by 17 hours (or use a `pd.Grouper` with custom
origin) so each "day" starts at 17:00 ET.

### C3 · Time zones across modules use a mix of `utcnow()` and `to_et()` — **P2**

**Locations:** `risk/controls.py`, `live/forward_log.py`, `utils/time_utils.py`,
`utils/news.py`. Some use `dt.datetime.utcnow()` (which is **deprecated** as
of Python 3.12 and lacks tz), some compute ET. DST edges will produce off-by-one-hour bugs
that are hard to spot.

**Fix:** standardise on `datetime.now(timezone.utc)` for storage, `to_et()` for
display/grouping. Add a tiny `clock.py` with `now_utc()` and `now_et()` so the
test suite can stub them in.

### C4 · `FOMC_DATES_2024_2026` is hardcoded and expires Dec 2026 — **P2**

**Location:** `utils/news.py:FOMC_DATES_2024_2026`. After the last entry, the
news filter silently misses FOMC.

**Fix:** extend the list and add a runtime check: if `today` is past the last
known FOMC date by > 90 days, warn loudly in startup logs and `/api/health`.

### C5 · No detection of missing-candle gaps — **P2**

**Reality:** yfinance returns bars with implicit gaps (long weekends,
exchange holidays, single missing bars). The detector treats the bar that
follows a 3-day gap as the immediate next bar — a "BOS" on Monday morning
could be the result of the Friday close vs Monday open gap, which is not a
tradeable break of structure.

**Fix:** post-process the loaded DF: detect gaps > 2× the median bar interval,
mark them, and exclude any detector output that straddles them.

---

## D · Forward-test readiness audit

### D1 · `forward_report.compile_report` doesn't apply a "since" filter to trades — **P2**

**Location:** `live/forward_report.py:compile_report`. Signals and skipped
get filtered by `_within(since)`, but `trades` does too — except the stat
aggregators on `closed` use the original `trades` list passed through. Need to
verify the slicing. (Smaller bug than the others but is in the verification
path of every report.)

### D2 · "Do Not Trust Yet" backtest-vs-forward gap requires user to pass `--backtest-expectancy` — **P3**

**Location:** `live/overfitting.py:evaluate`. The single most important
overfitting check is gated on a CLI flag. Easy to forget.

**Fix:** persist the backtest expectancy to `~/.ict-bot/backtest_baseline.json`
whenever any backtest runner completes; `compile_report` auto-loads it.

### D3 · No automated "are the logs sane?" check — **P2**

**Examples of pathologies the report won't catch:**
- All `live_signals.jsonl` rows have identical `setup_score` (the scoring is
  broken).
- All trades have `slippage_pts: 0.0` (instrumentation bug, A8 flags this but
  only after >5 trades).
- Last log entry is > 2h old (monitor crashed silently).
- More skipped rows than signal rows (filter cardinality inverted).

**Fix:** add `python -m live.sanity_check` that runs these and exits non-zero
on issues. Run it from launchd every hour.

---

## Summary table

| # | Severity | Title |
|---|---|---|
| A1 | **P0** | RiskGate financial-cap gates silently broken (no `r_realised` writer) |
| A6 | **P0** | Live monitor consumes incomplete current bar |
| B1 | **P0** | Slippage model is unrealistic (flat 1 tick) |
| B2 | **P0** | No reconciliation against actual broker fills |
| C1 | **P0** | yfinance delayed feed used in live mode |
| A2 | **P1** | Race condition: concurrent gate check |
| A3 | **P1** | `_today_et()` returns UTC date, not ET |
| A4 | **P1** | `session is None` bypasses session-allowlist |
| A5 | **P1** | `_open_from_logs()` cannot decrement (depends on A1) |
| B4 | **P1** | No "flatten open positions" path |
| A7 | **P2** | No `signal_id` linking |
| A8 | **P2** | No fsync on log appends |
| A9 | **P2** | No webhook backpressure |
| B3 | **P2** | Two separate `max_concurrent_positions` knobs |
| C2 | **P2** | PDH/PDL grouped by calendar date, not 17:00 ET session |
| C3 | **P2** | Mixed UTC/ET datetime handling |
| C4 | **P2** | Hardcoded FOMC dates expire Dec 2026 |
| C5 | **P2** | No missing-candle gap detection |
| D1 | **P2** | `forward_report` since-filter on trades unverified |
| D3 | **P2** | No automated logs-sanity check |
| A10| **P3** | Uncalibrated `setup_score` |
| B5 | **P3** | No per-instrument risk budget |
| D2 | **P3** | `--backtest-expectancy` is a CLI flag, easy to forget |

**Verdict on going beyond review-mode today:** Do not. **All five P0 issues
prevent honest measurement** of the live strategy. Fix A1, A6, B1, B2, C1
before any paper auto-execution.

---

## What this audit does NOT cover

- **The underlying ICT theory.** This audit assumes that *if* the detector
  fires only on legitimate sweep → CHoCH → FVG patterns *and* the execution
  matches the backtest assumptions, then the published backtest numbers are
  the honest expected forward performance. Whether sweep → CHoCH → FVG has a
  durable edge is **the question forward-testing answers, not this audit.**
- **Broker-side ergonomics.** Tradovate's API behaviour under high vol, their
  rate limits, their bracket-fill semantics. Will become visible during week 3.
- **Human factors.** Whether you'll actually press the kill switch when down
  4R on a Friday. This audit treats the operator as an automaton.
