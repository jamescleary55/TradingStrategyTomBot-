# Broker connection blocker

**Status:** BLOCKED — cannot reach either broker. No real broker response
collected yet.

**Probe time:** 2026-06-14 (local)
**Probe command:**
```
python scripts/probe_broker.py --broker tradovate
python scripts/probe_broker.py --broker topstepx
```

**Result:** both probes fail at the auth step with "missing
credentials." Neither broker was actually contacted — the failure
is local, not network.

---

## 1 · What's actually configured

| Variable | Status | Value visible |
|---|---|---|
| `BROKER` | set | `tradovate` |
| `TRADOVATE_ENV` | set | `demo` |
| `TRADOVATE_APP_ID` | set (15 chars) | hidden |
| `TRADOVATE_APP_VERSION` | set (5 chars) | hidden |
| `PROJECTX_BASE` | set | `https://api.topstepx.com` |

## 2 · What's missing (the blockers)

### Tradovate (paper / demo)

| Variable | Required for | Where it comes from |
|---|---|---|
| `TRADOVATE_USERNAME` | login | your Tradovate demo username |
| `TRADOVATE_PASSWORD` | login | your Tradovate demo password |
| `TRADOVATE_CID` | API auth | Tradovate API console → "Client ID" (decimal int) |
| `TRADOVATE_SECRET` | API auth | Tradovate API console → "Secret" (long string) |

**How to obtain:**

1. Log in to <https://trader.tradovate.com> (demo).
2. Go to **Settings → API Access** (or visit
   <https://api.tradovate.com> if it's been moved).
3. Create an API app if you haven't. Note the `cid` and `secret`.
4. Paste into `.env`:
   ```
   TRADOVATE_USERNAME=<your demo username>
   TRADOVATE_PASSWORD=<your demo password>
   TRADOVATE_CID=<integer>
   TRADOVATE_SECRET=<long alphanumeric>
   ```

### TopstepX (ProjectX API)

| Variable | Required for | Where it comes from |
|---|---|---|
| `PROJECTX_USERNAME` | login | your TopstepX account email or username |
| `PROJECTX_API_KEY` | API auth | TopstepX dashboard → API Keys page |
| `PROJECTX_ACCOUNT_ID` | scope reads to one account | integer shown in the dashboard |

**How to obtain:**

1. Log in to TopstepX.
2. Go to **API / Developer settings**.
3. Generate an API key (note this is **distinct** from your normal
   password — it's the one ProjectX accepts on `/api/Auth/loginKey`).
4. Find the numeric account id (usually 5-7 digits).
5. Paste into `.env`:
   ```
   PROJECTX_USERNAME=<email-or-username>
   PROJECTX_API_KEY=<api key string>
   PROJECTX_ACCOUNT_ID=<numeric account id>
   ```

---

## 3 · Probe output (raw)

```
=== TRADOVATE PROBE ===
[broker  ] tradovate
[auth    ] ERROR: Missing Tradovate credentials in .env

=== TOPSTEPX PROBE ===
[broker  ] topstepx
[auth    ] ERROR: Missing PROJECTX_USERNAME / PROJECTX_API_KEY in .env
```

Failure point: at the start of `_authenticate()` in
`data/tradovate_feed.py` (Tradovate) and `_authenticate()` in
`execution/topstepx_orders.py` (TopstepX). Both raise *before* any
network call is attempted.

---

## 4 · The actually-blocking question

> **The operator must drop real credentials into `.env`.** No engineering
> work resolves this. No infrastructure change unblocks it.

Pick ONE broker to get connected first (the sixth-pass review made
the case for picking one). The fastest paths to first response:

- **Tradovate** — has the most code maturity in the repo. Demo account
  is free and instant to provision. Best choice if "first real
  response" is the goal.
- **TopstepX** — fits the operator's earlier preference. Demo account
  also free.

---

## 5 · What unblocks immediately when credentials land

Once the variables above are populated in `.env`, the operator runs:

```bash
python scripts/probe_broker.py --broker tradovate   # or topstepx
```

Expected output if everything works:

```
[broker  ] tradovate
[auth    ] ok
[account ] id=<int>  cash=$<demo balance>  equity=$<...>
[positions] 0 open
[fills   ] 0 in last 24h
[orders ] 0 open
[result  ] ok
```

Expected output if auth works but no demo positions exist (most
likely first-time outcome): same as above with zeros. **That's still
success** — the broker confirmed the bot's identity and account.

If the probe shows `[auth] ok` followed by `ERROR` on any later line,
that's the next blocker (likely an endpoint shape mismatch).

---

## 6 · What the operator must NOT do

- Do not implement, fix, refactor, or rename anything in the bot.
- Do not generate more reports about the strategy.
- Do not switch brokers again without populating credentials for
  the previous one first.

---

## 7 · The next file

When the probe returns `[result  ] ok`, replace this file with
`FIRST_REAL_BROKER_RESPONSE.md` capturing:

- timestamp of the successful probe
- broker, environment
- account id, cash, equity
- which endpoints succeeded
- which (if any) failed

That file is the milestone the project has been blocked on for six
rounds. This file (`BROKER_CONNECTION_BLOCKER.md`) is the obstacle.
Delete it when resolved.

— end of blocker —
