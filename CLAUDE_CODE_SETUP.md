# Claude Code setup — ict-futures-bot

**Generated:** 2026-06-14
**Goal:** identify what blocks the first real paper trade, and the
exact commands to remove the blocker.

---

## 1 · Python + virtualenv

| Item | Value |
|---|---|
| Python | 3.9.6 |
| Virtualenv | `.venv/bin/python` (symlink → `python3`) |
| Activate | `source .venv/bin/activate` |

✅ OK. Note: 3.9 is old; nothing currently breaks on it.

---

## 2 · `.env`

| File | Present? | Size |
|---|---|---|
| `.env` | ✅ yes | 1136 bytes |
| `.env.example` | ✅ yes | 1091 bytes |

Both exist. `.env.example` is the up-to-date template; no fix
needed.

---

## 3 · Environment variables used by the code

Scanned all non-venv `.py` files. Only these variables matter for
the first paper trade:

### Required (broker)

| Variable | Used in | Status in `.env` |
|---|---|---|
| `BROKER` | `execution/base.py:159`, `scripts/probe_broker.py:117` | ✅ set = `tradovate` |
| `TRADOVATE_USERNAME` | `config.py:120` | ❌ EMPTY |
| `TRADOVATE_PASSWORD` | `config.py:121` | ❌ EMPTY |
| `TRADOVATE_CID` | `config.py:124` | ❌ EMPTY |
| `TRADOVATE_SECRET` | `config.py:125` | ❌ EMPTY |
| `TRADOVATE_ENV` | `config.py:126` | ✅ `demo` |
| `TRADOVATE_APP_ID` | `config.py:122` | ✅ set |
| `TRADOVATE_APP_VERSION` | `config.py:123` | ✅ set |

### Required if `BROKER=topstepx`

| Variable | Used in | Status in `.env` |
|---|---|---|
| `PROJECTX_USERNAME` | `execution/topstepx_orders.py:51` | ❌ EMPTY |
| `PROJECTX_API_KEY` | `execution/topstepx_orders.py:52` | ❌ EMPTY |
| `PROJECTX_ACCOUNT_ID` | `execution/topstepx_orders.py:68` | ❌ EMPTY |
| `PROJECTX_BASE` | `execution/topstepx_orders.py:46` | ✅ `https://api.topstepx.com` |

### Optional (data + alerts)

| Variable | Used in | Status |
|---|---|---|
| `FMP_API_KEY` | `data/fmp_feed.py:33` | ✅ set (32 chars) |
| `TELEGRAM_BOT_TOKEN` | `utils/alerter.py:44` | empty |
| `TELEGRAM_CHAT_ID` | `utils/alerter.py:45` | empty |
| `ALERT_MACOS` | `utils/alerter.py:46` | `1` |
| `WEBHOOK_SECRET` | `live/server.py:514` | empty |
| `WEBHOOK_PORT` | `live/server.py:513` | `5005` |

### Rithmic

**Not in code.** No Python file references Rithmic. If you want
Rithmic, the adapter has to be built — not a credential question.

---

## 4 · Dependencies

| Package | Required | Installed |
|---|---|---|
| pandas | ≥ 2.0 | 2.3.3 ✅ |
| numpy | ≥ 1.24 | 2.0.2 ✅ |
| requests | ≥ 2.31 | 2.32.5 ✅ |
| websocket-client | ≥ 1.6 | 1.9.0 ✅ |
| pyyaml | ≥ 6.0 | 6.0.3 ✅ |
| flask | ≥ 3.0 | 3.1.3 ✅ |
| yfinance | ≥ 0.2.40 | 1.2.0 ✅ |
| pytest | ≥ 7.4 | 8.4.2 ✅ |
| rich | ≥ 13.7 | 15.0.0 ✅ |

`pip check` returns: **"No broken requirements found."** ✅

---

## 5 · Tests

```
$ python -m pytest tests/ -q
.........................................  [100%]
41 passed in 10.55s
```

Test files:
- `tests/test_phases.py`
- `tests/test_failures.py`
- `tests/test_reconcile.py`
- `tests/test_a6_incomplete_bar.py`

✅ All green.

---

## 6 · Commands

### Broker probe (read-only, no orders)

```bash
python scripts/probe_broker.py --broker tradovate
# or
python scripts/probe_broker.py --broker topstepx
```

Expected on success:

```
[broker  ] tradovate
[auth    ] ok
[account ] id=<int>  cash=$<balance>  equity=$<...>
[positions] 0 open
[fills   ] 0 in last 24h
[result  ] ok
```

### Run the monitor (review mode — no orders sent)

```bash
./scripts/run_monitor.sh
# equivalent to:
python -m live.monitor \
    --symbols MNQ,MES,MCL \
    --timeframe 1h \
    --htf 1d \
    --days 14 \
    --poll 300 \
    --news-filter \
    --entry-mode closer_edge \
    --source yfinance \
    --mode review \
    --strategy sweep_choch_fvg
```

### Place a paper trade for a logged signal

```bash
python -m live.signals_db sync                                # ingest signals
python -m live.paper_trade_runner --signal-id <hex> --submit  # actually order
python -m live.paper_reconcile --once                         # poll fills
```

### Inspect state

```bash
python -m live.signals_db count
python -m live.paper_trades_db metrics
```

---

## 7 · What is blocking the first paper trade

**The .env file is missing 4 broker credential values.**

That is the only blocker. Code, tests, deps, infrastructure all
verified working. The bot cannot authenticate to Tradovate (current
`BROKER=tradovate`) until these are set:

```
TRADOVATE_USERNAME=<your demo username>
TRADOVATE_PASSWORD=<your demo password>
TRADOVATE_CID=<integer from Tradovate API console>
TRADOVATE_SECRET=<long string from same console>
```

`TRADOVATE_ENV=demo` is already correct.

---

## 8 · Final checklist (operator only)

In order, the operator does:

- [ ] **Step 1** — log into <https://trader.tradovate.com> (demo account)
- [ ] **Step 2** — go to **Account → API Access** → create app if needed
- [ ] **Step 3** — copy: username, password, CID (integer), secret (long string)
- [ ] **Step 4** — open `.env`, fill in the 4 values, save
- [ ] **Step 5** — run: `python scripts/probe_broker.py --broker tradovate`
- [ ] **Step 6** — if output ends with `[result  ] ok`: success. Note the account id + equity.
- [ ] **Step 7** — if output shows `ERROR`: capture the error line and report it back here.

After step 6 succeeds:

- [ ] Optionally start the monitor in review mode: `./scripts/run_monitor.sh`
- [ ] Optionally switch monitor to paper mode by editing
      `~/.ict-bot/personal_rules.yaml` → `mode: paper`
- [ ] First paper order: pick a logged signal_id and run:
      `python -m live.paper_trade_runner --signal-id <hex> --submit`

The probe in step 5 is the only thing required for "first real
broker response." Everything else is downstream.

---

## 9 · Risks / known limitations

- Tradovate auth requires that the API App is associated with the
  demo account, not a live account. If you see "401 unauthorized,"
  the most common cause is the App being live-only.
- Tradovate's `/auth/accesstokenrequest` endpoint may rate-limit
  if you hit it multiple times in quick succession. Space probe
  runs ≥ 30 seconds apart.
- `personal_rules.yaml` currently has `allowed_symbols: [MNQ, MES, MCL]`.
  Tier-1 v2 verdict says **disable MCL**. Edit before going live.
- Tradovate demo accounts are sometimes reset; if your saved
  credentials suddenly fail, log in via the web UI first.

— end of setup —
