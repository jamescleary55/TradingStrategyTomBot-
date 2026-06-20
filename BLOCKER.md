# BLOCKER — IBKR order execution

**Status:** Data flowing ✅ · Order path PROVEN ✅ · `snapshot()` FIXED ✅ · Automated paper trading UNBLOCKED ✅
**Last verified:** 2026-06-20

> **UPDATE 2026-06-20 — this blocker is cleared.** Read-Only API is off and the
> first paper order path is proven end-to-end (see `IBKR_FIRST_PAPER_ORDER_SMOKE_TEST.md`:
> orderId 6, submit→cancel, no fill, paper account `DUQ834606`). The `snapshot()`
> hang that previously blocked the live monitor is fixed (timeout-bounded async
> rewrite) and verified live (0.67s, €1M, 0 positions, no warnings). The sections
> below are retained as the historical credential checklist. Next step is running
> the live monitor in auto-execute against the paper account.

---

## The blocker

IBKR **order execution** is not yet possible, for two independent reasons:

1. **IB Gateway API is in Read-Only mode.** The connection works and data pulls
   succeed, but order endpoints reject with `Error 321: The API interface is
   currently in Read-Only mode`.
2. **IBKR paper account approval is pending.** Until the paper trading account is
   approved, no paper orders can be placed and no real fills can be collected.

Because of this:

- No broker execution testing is possible.
- No paper orders can be placed.
- No real fills can be collected.

**What is NOT blocked:** market data. IB Gateway is connected on `127.0.0.1:4002`
and CME real-time/historical data is subscribed — ES/NQ signal collection runs on
live IBKR data right now (verified: 437 bars/symbol; 3 setups detected this run).

---

## Credentials / setup required

IBKR uses **no API key/secret in `.env`** — you authenticate by logging into the
Gateway app itself. "Credentials" here means the Gateway session + API settings.

| Item | Where | Status |
|---|---|---|
| IBKR account login (username/password) | typed into IB Gateway login screen | ✅ logged in |
| IB Gateway running + API enabled | Configure → Settings → API → Settings → "Enable ActiveX and Socket Clients" | ✅ |
| `127.0.0.1` in Trusted IPs | same API Settings panel | ✅ |
| CME market-data subscription | Account → Market Data Subscriptions | ✅ (data confirmed) |
| **Read-Only API UNCHECKED** | same API Settings panel | ⛔ currently checked — blocks orders |
| **Paper account approved** | IBKR account portal | ⛔ pending approval |
| `.env`: `BROKER=ibkr`, `IB_PORT=4002`, `IB_HOST=127.0.0.1`, `IB_CLIENT_ID=17` | repo `.env` | ✅ |

No secrets belong in `.env`. Keep the IBKR password out of the repo.

---

## Exact command to run once both ⛔ items are resolved

```bash
source .venv/bin/activate
python scripts/probe_broker.py --broker ibkr
```

Expected on success:

```
[broker  ] ibkr
[auth    ] ok
[account ] id=<int>  cash=$<paper balance>  equity=$<...>
[positions] 0 open
[fills   ] 0 in last 24h
[result  ] ok
```

If it still shows `Error 321 / Read-Only mode`, the Read-Only API box is still
checked. If it connects but no account/balance, the paper account isn't approved
yet.

---

## Allowed while waiting (the ONLY permitted work)

1. Keep ES/NQ signal collection running:
   ```bash
   BROKER=ibkr python -m live.monitor --symbols ES,NQ --timeframe 15m --source ibkr --mode review
   ```
2. Clean `.env.example`.
3. Confirm existing probe commands (`scripts/probe_broker.py --broker ibkr`).
4. Maintain this IBKR credential checklist.
5. Verify current tests still pass (`python -m pytest -q`).
6. Keep this `BLOCKER.md` updated.

## Forbidden while waiting

- New strategy research
- New broker architecture / execution models / future-broker abstractions
- Adding crypto, forex, or gold
- More adversarial reviews or Monte Carlo reports
- Database redesigns or dashboards
- Any work that requires broker order access

**Loop rule:** each iteration asks *"What is the smallest useful action while IBKR
order access is pending?"* If the answer requires order access, STOP.
