# Forward-testing playbook

This document is the operating manual for `ict-futures-bot`. Read it
before flipping any mode flag.

The bot is built for one purpose: validate or invalidate the
**sweep → CHoCH → FVG** strategy on live data **without risking real
money first**. Everything is wired to surface lies in your own backtest.

---

## 0 · Tour of the new files

| Path | What it does |
|---|---|
| `personal_rules.example.yaml` | Template config. Copied to `~/.ict-bot/personal_rules.yaml` on first run. |
| `risk/rules.py` | YAML loader. Strict defaults; safe even if you forget to copy the file. |
| `risk/controls.py` | `RiskGate.check(setup)` — single entry point that enforces 10 rules in order. Honors `~/.ict-bot/KILL_SWITCH`. |
| `signals/strategies/base.py` | `Strategy` ABC. `StrategySetup` is the normalised shape every layer below depends on. |
| `signals/strategies/sweep_choch_fvg.py` | Current strategy. Wraps existing `find_setups` without rewriting. |
| `live/forward_log.py` | The 3 structured JSONL writers (`live_signals.jsonl`, `skipped_setups.jsonl`, `live_trades.jsonl`). |
| `live/forward_report.py` | `python -m live.forward_report` — CLI + HTML + CSV + JSON outputs. |
| `live/overfitting.py` | "Do Not Trust Yet" heuristics surfaced in the report. |
| `live/monitor.py` | Added `--mode review\|paper\|live` and `--strategy`/`--rules-file`. Now routes every setup through strategy → news → risk gate → 3 logs. |
| `live/webhook.py` | TradingView/etc inbound webhook. Now uses the same `StrategySetup` + `RiskGate` + structured logs as the monitor. |
| `live/server.py` | `/api/alerts` and `/api/stats` flow through the forward log + report (single source of truth). |
| `tests/test_phases.py` | 17 unit tests covering logging, rules, kill switch, risk rules, strategy interface, paper/live execution guard, report generation, overfitting flags. |
| `scripts/run_monitor.sh` | Wrapper that starts the monitor in review mode with the configured rules. |
| `scripts/com.thomasbruijns.ictbot.plist` | launchd plist — keeps the monitor running across reboots and crashes. |

Run the test suite any time you change risk/strategy code:

```bash
cd ~/projects/ict-futures-bot && source .venv/bin/activate
python -m pytest tests/test_phases.py -v
```

---

## 1 · Run review mode (default — never executes)

Manual launch (foreground, for inspection):

```bash
cd ~/projects/ict-futures-bot && source .venv/bin/activate
python -m live.monitor \
    --symbols MNQ,MES,MCL \
    --timeframe 1h --htf 1d --news-filter \
    --entry-mode closer_edge \
    --source yfinance --poll 300 \
    --mode review
```

Behaviour:

- Detects setups every 5 minutes per symbol.
- Sends a console panel + macOS banner + Telegram alert (with chart) for every detected setup.
- **Never sends an order** — payload contains "approve manually in broker, do not auto-execute".
- Writes 16-field rows to `~/.ict-bot/live_signals.jsonl` with `trade_allowed: false` and `skip_reason: "mode=review (manual approval only)"`.
- Also appends to `~/.ict-bot/skipped_setups.jsonl` so you can slice skip reasons later.

To run as a background service across reboots:

```bash
cp ~/projects/ict-futures-bot/scripts/com.thomasbruijns.ictbot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.thomasbruijns.ictbot.plist
launchctl start com.thomasbruijns.ictbot   # immediate kick
# Tail the logs
tail -f ~/.ict-bot/logs/monitor.out.log ~/.ict-bot/logs/monitor.err.log
```

Stop it:

```bash
launchctl unload ~/Library/LaunchAgents/com.thomasbruijns.ictbot.plist
```

Emergency stop (regardless of mode):

```bash
touch ~/.ict-bot/KILL_SWITCH   # blocks every new trade until removed
rm    ~/.ict-bot/KILL_SWITCH   # resume
```

---

## 2 · Run paper execution mode (real broker, fake money)

**Pre-requisite:** Tradovate demo account credentials in `~/projects/ict-futures-bot/.env`:

```dotenv
TRADOVATE_USERNAME=...
TRADOVATE_PASSWORD=...
TRADOVATE_CID=...
TRADOVATE_SECRET=...
TRADOVATE_ENV=demo
BROKER=tradovate
```

Edit `~/.ict-bot/personal_rules.yaml`:

```yaml
mode: paper
enable_auto_execute: true        # MUST be explicit
```

Then run:

```bash
python -m live.monitor \
    --symbols MNQ,MES,MCL --timeframe 1h --htf 1d --news-filter \
    --entry-mode closer_edge --source yfinance --poll 300 \
    --auto-execute --equity 50000 --risk-pct 0.0025
```

Every setup that passes the risk gate (kill switch + rules + RR + score
+ daily/weekly loss caps + max trades/day + max consecutive losses)
will:

1. Be sized via `plan_trade()`.
2. Be sent as an OSO bracket to Tradovate's demo endpoint.
3. Be logged into `live_trades.jsonl` with intended vs broker-reported entry/stop/target, contracts, risk USD, order id, broker response, outcome.

Wrong-side prevention is enforced by the adapter — orders with stop on the wrong side of entry are refused before they reach the wire.

---

## 3 · Generate reports

Anytime after the bot has logged signals:

```bash
python -m live.forward_report
python -m live.forward_report --since 7d
python -m live.forward_report --backtest-expectancy 0.96   # for the IS↔OOS gap check
```

Outputs (terminal + files written to `~/.ict-bot/reports/`):

- `forward_report_<ts>.html` — sidebar UI, by-symbol/session/subtype/HTF slices, the **Do Not Trust Yet** verdict.
- `forward_signals_<ts>.csv` — every signal as a row (drop into Excel / DuckDB for ad-hoc).
- `forward_summary_<ts>.json` — machine-readable stats.

The terminal section "Do Not Trust Yet" automatically flags:

- `few_signals` (<50)
- `few_closed_trades` (<20)
- `single_symbol_only` (≥30 signals all on one symbol)
- `symbol_concentration` (>70% one symbol)
- `unrealistic_win_rate` (>85%)
- `low_win_rate` (<30%)
- `is_oos_gap` (>0.7R gap = BLOCK; >0.3R = WARN)
- `setup_subtype_concentration` (>50% of trades same subtype)
- `zero_slippage` (instrumentation bug)
- `high_skip_ratio` (>80% — filters likely too tight)

`ready_for_real_money` evaluates to `true` only when **no `block`-level
concern is present** AND there are ≥30 closed trades. This is the
machine-readable gate. It is **necessary, not sufficient** — read the
checklist below before acting on a green light.

---

## 4 · The 4-week forward-testing protocol

Goal: **100+ signals** across 3 symbols × ≥2 sessions, with forward
expectancy within 0.3R of the backtest, before a single dollar of real
money goes anywhere.

### Week 0 (today) — Wire-up & sanity

- [ ] Copy `personal_rules.example.yaml` to `~/.ict-bot/personal_rules.yaml`. Verify defaults (`mode: review`, `enable_auto_execute: false`).
- [ ] Smoke test: `python -m live.monitor --symbols MNQ --timeframe 1h --days 14 --once --reset --mode review` — should fire 5–10 signals, write to `~/.ict-bot/live_signals.jsonl`, alert console + macOS.
- [ ] Install Telegram bot (so phone alerts work). Fill `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in `.env`. Verify with `python -m live.monitor --test-alert`.
- [ ] Install launchd service. Watch logs for a few hours.
- [ ] **Do not change** the strategy code during weeks 1–4. Truth requires a frozen detector.

### Week 1 — Quiet observation (review mode)

- [ ] Bot is running 24/7 in review mode against MNQ + MES + MCL on 1h.
- [ ] Every morning: skim `live_signals.jsonl` from the night. Eyeball-validate the chart on TradingView. Note discrepancies (bot fired on a setup you'd skip; bot missed an obvious one).
- [ ] **Decision rule**: at end of week 1, run `python -m live.forward_report`. Target ≥ 15 signals logged. If the bot is firing on garbage, fix detection before continuing.

### Week 2 — Manual paper trading

- [ ] When the bot alerts, you place the trade **manually on Tradovate demo**, exactly as instructed (entry/stop/target).
- [ ] Record fill prices and exits in a simple journal (Notion / paper). At end of week, hand-add those outcomes into `live_trades.jsonl` (or wait for the position monitor — see below).
- [ ] Goal: 5–10 trades. Identify if you're emotionally OK with the setups.
- [ ] **Decision rule**: run forward report. If concerns include `single_symbol_only` or `low_win_rate`, do not advance.

### Week 3 — Paper auto-execution (lowest possible risk)

- [ ] Flip `~/.ict-bot/personal_rules.yaml`:
  - `mode: paper`
  - `enable_auto_execute: true`
  - keep `risk_per_trade_R: 0.25` and `max_trades_per_day: 3`
- [ ] Drop demo creds in `.env`. Restart the launchd service.
- [ ] Start `python -m live.positions --poll 30` in a second launchd job so fills/exits flow into the logs automatically.
- [ ] Goal: ≥ 25 closed trades by end of week.
- [ ] **Daily ritual**: `python -m live.forward_report --backtest-expectancy <your IS number>`. If gap > 0.3R or `unrealistic_win_rate` fires, slow down.

### Week 4 — Hold the line + decision

- [ ] Same setup. Goal: total **≥ 100 signals**, **≥ 30 closed trades**, ≥ 2 symbols with closed trades, ≥ 2 sessions represented.
- [ ] End of week: full forward report. Compare to go-live criteria below.

If the criteria are not met at end of week 4 → extend by another 2 weeks. If they are met → see the next section.

---

## 5 · Exact criteria before risking real money

The bot evaluates these in `live/overfitting.py` and `compile_report()`.
You should evaluate them manually too — automation can miss context.

### Required (hard gates)

| # | Criterion | Threshold | Where to check |
|---|---|---|---|
| 1 | Forward signals logged | **≥ 100** | report.totals.n_signals_detected |
| 2 | Closed trades (target or stop) | **≥ 30** | report.overall.n |
| 3 | Symbols with ≥ 5 closed trades each | **≥ 2** | report.by_symbol |
| 4 | Sessions with ≥ 5 closed trades each | **≥ 2** | report.by_session |
| 5 | Forward expectancy | **≥ +0.3R** | report.overall.avg_R |
| 6 | Win rate | **35% ≤ wr ≤ 80%** | report.overall.win_rate |
| 7 | Gap to backtest expectancy | **< 0.3R** | computed via `--backtest-expectancy` |
| 8 | Max forward drawdown | **≤ 3R** | report.overall.max_dd_R |
| 9 | Zero `block`-level concerns from "Do Not Trust Yet" | hard | report.concerns |
| 10 | At least one bear-leaning week observed during the period | hard | manual journal |

### Soft gates (warnings — proceed cautiously)

| Soft signal | What it might mean |
|---|---|
| Single setup_subtype carrying >50% of P&L | Strategy is really one specific pattern dressed up as ICT. Reduce risk on go-live. |
| All winners cluster around one symbol | Symbol-specific tape, not strategy edge. Trade only that symbol on go-live, smaller size. |
| Avg slippage > 1 tick on micros | Execution model is off. Investigate before scaling beyond 1 contract. |
| `high_skip_ratio` (>80%) | Either rules are too tight (try loosening one at a time and re-running on the same logs) or strategy fires on too much noise. |
| Sharpe-equivalent < 1.0 on forward data | Edge is too thin to survive transaction costs. Do not go live. |

### Behavioural gates (the ones backtests can't measure)

| Question | Honest answer required |
|---|---|
| Can I sit through a 6-trade losing streak without changing the rules mid-flight? | If no → stay on paper. |
| Have I run the bot during a week I was traveling / sick / stressed? | If no → wait for one. |
| If real money goes on, what's my hard stop — total $ and total %? | Write it down before the first live trade. |
| What's my plan when the bot is wrong and I disagree with a fill? | Procedure must exist before it happens. |

### When all gates pass

1. Reset position state and logs: `rm ~/.ict-bot/live_*.jsonl ~/.ict-bot/positions*.jsonl`. (Archive first: `cp -r ~/.ict-bot ~/.ict-bot.paper-archive.<date>/`.)
2. Switch to a *separate* Tradovate live account, **funded with at most 5× your max_daily_loss_R in cash**. So if `max_daily_loss_R: 1.0` and you've sized 1R ≈ $50 on micros, fund $250.
3. Edit `personal_rules.yaml`:
   - `mode: live`
   - `risk_per_trade_R: 0.10`  (yes, lower than paper — go-live tax for unknown unknowns)
   - `max_trades_per_day: 1`
   - `max_consecutive_losses: 1`
   - `news_filter_enabled: true`
4. In `.env`: `TRADOVATE_ENV=live`. (The Tradovate adapter refuses non-demo unless `allow_live=True` is set per call — review which paths pass that flag and confirm you're comfortable.)
5. Run the launchd service. After **10 closed live trades**: run forward report restricted to live. Compare to paper expectancy. If live underperforms paper by > 0.4R → kill switch, back to paper, fix the gap before scaling.

There is no "graduation". The bot stays in this configuration until you
have a written, dated reason in your journal to change it.

---

## 6 · When something goes sideways

- **Emergency halt** — `touch ~/.ict-bot/KILL_SWITCH`. Risk gate blocks every new trade with rule `kill_switch`. Existing brackets stay open (broker manages them).
- **Cancel open orders** at the broker UI manually. The bot doesn't manage open positions yet — that's a roadmap item.
- **Inspect why a trade fired**: `python -m live.tracker recent -n 20` or `cat ~/.ict-bot/live_signals.jsonl | python -m json.tool | less`.
- **Inspect why a trade was skipped**: `cat ~/.ict-bot/skipped_setups.jsonl | jq -r '.reason' | sort | uniq -c | sort -rn`.
- **Generate a fresh report for a specific window**: `python -m live.forward_report --since 24h`.
- **Reset state for a fresh window** (be very sure): `rm ~/.ict-bot/live_*.jsonl ~/.ict-bot/positions*.jsonl ~/.ict-bot/monitor-*.json`.

---

## 7 · What this playbook does not cover yet

- **Open-position management** — the bot places brackets and forgets them. Need a position reconciliation loop that detects partial fills and reports drift. Roadmap item.
- **Adverse news handling beyond static calendar** — the news filter uses recurring rules + a static FOMC list. Live macroeconomic surprises (geopolitical, intra-day FOMC speakers) are not handled.
- **Multiple strategies** — only `sweep_choch_fvg` is implemented. Wait until it has 100 forward-tested signals before adding a second.
- **Real-time data feed** — currently polls yfinance with a 1m+ delay. For live mode, swap in Tradovate WS streaming.

When you hit any of these gaps in practice, file the lesson in your
journal *before* changing the code. Half the project is keeping a clean
record of what didn't work.
