"""Operational health & failure detection — PURE, testable logic.

The dashboard hardening layer. Every alarm, staleness check, mismatch test,
reconciliation-health rollup, daily-risk check and the supervision verdict is a
deterministic function here, so it can be unit-tested without a broker, a server,
or the clock. ``live/server.py`` calls these and renders the result; it adds no
detection logic of its own.

Alarm shape (a plain dict):
    {"id": str, "level": "critical"|"warning"|"info", "title": str, "detail": str}

Levels GREEN/YELLOW/RED are used for staleness and health rollups.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

GREEN, YELLOW, RED = "GREEN", "YELLOW", "RED"
CRITICAL, WARNING, INFO = "critical", "warning", "info"

# Staleness thresholds (seconds)
FRESH_S = 10        # < 10s  → GREEN
STALE_S = 30        # 10-30s → YELLOW ; > 30s → RED

# AUTO_PAPER_SAFE policy (mirrors risk.exec_gate / the controlled run)
ALLOWED_SYMBOL_ROOTS = ("MES",)
MAX_QTY = 1
PENDING_ORDER_TIMEOUT_S = 300   # a resting order older than this → warning


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def _parse(ts: Optional[str]) -> Optional[dt.datetime]:
    if not ts:
        return None
    try:
        s = ts.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except Exception:
        return None


def age_seconds(now_iso: str, ts_iso: Optional[str]) -> Optional[float]:
    a, b = _parse(now_iso), _parse(ts_iso)
    if a is None or b is None:
        return None
    return (a - b).total_seconds()


def staleness_level(age_s: Optional[float]) -> str:
    """GREEN < 10s, YELLOW 10-30s, RED > 30s or unknown."""
    if age_s is None:
        return RED
    if age_s < FRESH_S:
        return GREEN
    if age_s <= STALE_S:
        return YELLOW
    return RED


def _worst(levels) -> str:
    order = {GREEN: 0, YELLOW: 1, RED: 2}
    return max(levels, key=lambda l: order.get(l, 0)) if levels else GREEN


# ---------------------------------------------------------------------------
# PHASE 1 — data staleness / freshness
# ---------------------------------------------------------------------------
def freshness(now_iso: str, *, broker_ts: Optional[str], event_ts: Optional[str],
              heartbeat_ts: Optional[str], monitor_running: bool) -> dict:
    """Freshness for each data source + overall degraded flag.

    ``degraded`` is driven ONLY by sources that should update continuously: the
    broker read and (when the monitor is running) its heartbeat. The event-log
    age is reported for display but is NOT folded into ``degraded`` — events are
    sporadic (signals/orders), so a quiet stream is normal, not a failure. The
    heartbeat is N/A when no monitor is running.
    """
    def src(ts):
        a = age_seconds(now_iso, ts)
        return {"ts": ts, "age_s": a, "level": staleness_level(a)}

    broker = src(broker_ts)
    event = src(event_ts)           # informational only
    dash = {"ts": now_iso, "age_s": 0.0, "level": GREEN}
    if monitor_running:
        hb = src(heartbeat_ts)
    else:
        hb = {"ts": heartbeat_ts, "age_s": age_seconds(now_iso, heartbeat_ts),
              "level": None, "na": True}

    graded = [broker["level"]]      # broker must be live
    if monitor_running:
        graded.append(hb["level"])  # a running monitor must heartbeat
    worst = _worst(graded)
    return {
        "broker": broker, "event": event, "heartbeat": hb, "dashboard": dash,
        "worst_level": worst, "degraded": worst == RED,
    }


# ---------------------------------------------------------------------------
# PHASE 2 — position reconciliation (broker vs bot)
# ---------------------------------------------------------------------------
def _net(side: str, qty) -> int:
    return int(qty) * (1 if str(side).lower().startswith("b") else -1)


def bot_open_positions(trades) -> dict:
    """Net open position per symbol from OPEN/PARTIAL reconciled trades.

    ``trades`` are ReconciledTrade objects (or dicts). Returns {symbol: net_qty}.
    """
    out: dict[str, int] = {}
    for t in trades:
        status = t["status"] if isinstance(t, dict) else t.status
        if status not in ("OPEN", "PARTIAL"):
            continue
        sym = t["symbol"] if isinstance(t, dict) else t.symbol
        side = t["side"] if isinstance(t, dict) else t.side
        eq = t["entry_qty"] if isinstance(t, dict) else t.entry_qty
        xq = t["exit_qty"] if isinstance(t, dict) else t.exit_qty
        net = (int(eq) - int(xq)) * (1 if str(side).lower().startswith("b") else -1)
        out[sym] = out.get(sym, 0) + net
    return {k: v for k, v in out.items() if v != 0}


def position_mismatch(broker_positions: list, bot_positions: dict) -> list:
    """Compare broker positions vs the bot's reconciled open positions.

    ``broker_positions``: list of {symbol, side, qty}. ``bot_positions``:
    {symbol: net_qty}. Returns a list of critical POSITION_MISMATCH alarms.
    """
    broker_net: dict[str, int] = {}
    for p in broker_positions or []:
        sym = p.get("symbol")
        broker_net[sym] = broker_net.get(sym, 0) + _net(p.get("side", "Buy"), p.get("qty", 0))
    broker_net = {k: v for k, v in broker_net.items() if v != 0}

    alarms = []
    for sym in sorted(set(broker_net) | set(bot_positions)):
        b = broker_net.get(sym, 0)
        o = bot_positions.get(sym, 0)
        if b == o:
            continue
        if b != 0 and o == 0:
            detail = f"broker shows {sym} net {b:+d} but bot thinks FLAT"
        elif b == 0 and o != 0:
            detail = f"bot thinks {sym} net {o:+d} but broker shows FLAT"
        else:
            detail = f"{sym} quantity/side mismatch — broker {b:+d} vs bot {o:+d}"
        alarms.append({"id": f"position_mismatch:{sym}", "level": CRITICAL,
                       "title": "POSITION_MISMATCH", "detail": detail})
    return alarms


# ---------------------------------------------------------------------------
# PHASE 3 — execution safety alarms
# ---------------------------------------------------------------------------
def _root(symbol: str) -> str:
    s = (symbol or "").upper()
    for r in ("MNQ", "MES", "MCL", "MGC"):   # micros first (longest prefixes)
        if s.startswith(r):
            return r
    for r in ("NQ", "ES", "CL", "GC"):
        if s.startswith(r):
            return r
    return s


def execution_safety_alarms(*, broker: dict, kill_switch: dict, runtime: dict,
                            trades_log: list, events: list, now_iso: str) -> list:
    """All Phase-3 execution-safety alarms as a list of alarm dicts."""
    alarms = []
    ok = bool(broker.get("ok"))
    acct = str(broker.get("account_id") or "")

    # broker disconnected
    if not ok:
        alarms.append({"id": "broker_disconnected", "level": CRITICAL,
                       "title": "BROKER_DISCONNECTED",
                       "detail": broker.get("error") or "broker read failed"})

    # account is paper / DU
    if ok and acct and not acct.startswith("DU"):
        alarms.append({"id": "live_account", "level": CRITICAL,
                       "title": "LIVE_ACCOUNT_DETECTED",
                       "detail": f"account {acct!r} is not a paper (DU) account"})

    # kill switch
    if kill_switch.get("present"):
        if kill_switch.get("path") == "<error>":
            alarms.append({"id": "kill_switch_unreadable", "level": CRITICAL,
                           "title": "KILL_SWITCH_UNREADABLE",
                           "detail": "kill-switch check errored — treating as HALT"})
        else:
            alarms.append({"id": "kill_switch_active", "level": WARNING,
                           "title": "KILL_SWITCH_ACTIVE",
                           "detail": f"halt file present: {kill_switch.get('path')}"})

    # AUTO_PAPER_SAFE disabled while auto-executing
    mode = (runtime or {}).get("mode")
    if (runtime or {}).get("auto_execute") and mode and mode != "auto_paper_safe":
        alarms.append({"id": "auto_paper_safe_off", "level": WARNING,
                       "title": "AUTO_PAPER_SAFE_DISABLED",
                       "detail": f"auto-executing in mode {mode!r}, not auto_paper_safe"})

    # unexpected symbol / qty > max across broker positions + pending orders
    for p in (broker.get("positions") or []):
        if _root(p.get("symbol", "")) not in ALLOWED_SYMBOL_ROOTS:
            alarms.append({"id": f"unexpected_symbol:{p.get('symbol')}", "level": CRITICAL,
                           "title": "UNEXPECTED_SYMBOL",
                           "detail": f"position in {p.get('symbol')} (allowed: {ALLOWED_SYMBOL_ROOTS})"})
        if int(p.get("qty", 0)) > MAX_QTY:
            alarms.append({"id": f"qty_over_max:{p.get('symbol')}", "level": CRITICAL,
                           "title": "QTY_OVER_MAX",
                           "detail": f"{p.get('symbol')} qty {p.get('qty')} > {MAX_QTY}"})

    # pending order timeout + duplicate order
    pend = broker.get("open_orders") or []
    submit_ts = {str(t.get("order_id")): t.get("ts_logged") for t in trades_log
                 if t.get("order_id")}
    for o in pend:
        oid = str(o.get("orderId"))
        age = age_seconds(now_iso, submit_ts.get(oid))
        if age is not None and age > PENDING_ORDER_TIMEOUT_S:
            alarms.append({"id": f"pending_timeout:{oid}", "level": WARNING,
                           "title": "PENDING_ORDER_TIMEOUT",
                           "detail": f"order {oid} resting {int(age)}s (> {PENDING_ORDER_TIMEOUT_S}s)"})

    # order without stop / bracket failure / duplicate order — from logs
    submitted_ids = [str(t.get("order_id")) for t in trades_log
                     if t.get("outcome") == "submitted" and t.get("order_id")]
    for oid in {x for x in submitted_ids if submitted_ids.count(x) > 1}:
        alarms.append({"id": f"duplicate_order:{oid}", "level": CRITICAL,
                       "title": "DUPLICATE_ORDER",
                       "detail": f"order id {oid} submitted more than once"})
    for t in trades_log:
        if t.get("outcome") == "submitted" and t.get("intended_stop") in (None, "", 0, 0.0):
            alarms.append({"id": f"no_stop:{t.get('order_id')}", "level": CRITICAL,
                           "title": "ORDER_WITHOUT_STOP",
                           "detail": f"order {t.get('order_id')} submitted with no stop"})
            break
    if any(t.get("outcome") == "failed" for t in trades_log):
        alarms.append({"id": "bracket_failure", "level": CRITICAL,
                       "title": "BRACKET_FAILURE",
                       "detail": "a trade attempt recorded outcome=failed"})

    # duplicate signal execution (from events) + duplicate broker events ignored
    dup_exec = sum(1 for e in events
                   if e.get("category") == "signal" and e.get("event") == "executed"
                   and e.get("duplicate"))
    if dup_exec:
        alarms.append({"id": "duplicate_signal_exec", "level": CRITICAL,
                       "title": "DUPLICATE_SIGNAL_EXECUTION",
                       "detail": f"{dup_exec} duplicate signal execution(s)"})
    return alarms


# ---------------------------------------------------------------------------
# PHASE 4 — reconciliation health
# ---------------------------------------------------------------------------
def reconciliation_health(*, trades, metrics: dict, raw_fill_count: int,
                          reconcile_error: Optional[str] = None) -> dict:
    """Roll up reconciliation health to GREEN/YELLOW/RED with the detail counts."""
    matched_exec_ids = set()
    open_n = partial_n = closed_n = 0
    for t in trades:
        status = t.status if not isinstance(t, dict) else t["status"]
        ids = t.execution_ids if not isinstance(t, dict) else t.get("execution_ids", [])
        matched_exec_ids.update(i for i in (ids or []) if i)
        if status == "OPEN":
            open_n += 1
        elif status == "PARTIAL":
            partial_n += 1
        elif status == "CLOSED":
            closed_n += 1

    # duplicate broker events ignored = raw fills minus unique matched fills
    unique_matched = len(matched_exec_ids)
    duplicates_ignored = max(0, raw_fill_count - unique_matched) if raw_fill_count else 0

    health = {
        "unmatched_executions": 0,      # engine assigns every fill to a trade
        "reconciliation_errors": 1 if reconcile_error else 0,
        "reconcile_error": reconcile_error,
        "open_trades": open_n,
        "closed_trades": closed_n,
        "partial_pending": partial_n,
        "duplicates_ignored": duplicates_ignored,
    }
    if reconcile_error:
        health["level"] = RED
    elif partial_n or open_n:
        health["level"] = YELLOW            # in-flight, not yet fully reconciled
    else:
        health["level"] = GREEN
    return health


# ---------------------------------------------------------------------------
# PHASE 5 — daily risk monitor
# ---------------------------------------------------------------------------
def daily_risk(*, trades, now_iso: str,
               max_daily_loss_R: Optional[float] = None,
               max_trades_per_day: Optional[int] = None,
               open_risk_R_cap: Optional[float] = None) -> dict:
    """Daily realized P&L / R / drawdown / open risk / trades + breach alarms."""
    today = (_parse(now_iso) or dt.datetime.now(dt.timezone.utc)).date().isoformat()

    def _is_today(ts):
        d = _parse(ts)
        return d is not None and d.date().isoformat() == today

    closed_today = [t for t in trades
                    if (t.status if not isinstance(t, dict) else t["status"]) == "CLOSED"
                    and _is_today(t.exit_time if not isinstance(t, dict) else t["exit_time"])]
    closed_today.sort(key=lambda t: (t.exit_time if not isinstance(t, dict) else t["exit_time"]) or "")

    def g(t, k):
        return getattr(t, k) if not isinstance(t, dict) else t.get(k)

    pnl = sum((g(t, "net_pnl") or 0) for t in closed_today)
    R = sum((g(t, "realized_R") or 0) for t in closed_today)
    eq = peak = dd = 0.0
    for t in closed_today:
        eq += (g(t, "net_pnl") or 0)
        peak = max(peak, eq)
        dd = max(dd, peak - eq)

    # open risk (in R) for OPEN/PARTIAL trades: |entry - stop| / |entry - stop| = 1R each leg open
    open_risk_R = 0.0
    for t in trades:
        status = g(t, "status")
        if status not in ("OPEN", "PARTIAL"):
            continue
        net_qty = abs((g(t, "entry_qty") or 0) - (g(t, "exit_qty") or 0))
        if net_qty <= 0:
            continue
        # each open contract carries ~1R of planned risk (entry→stop)
        open_risk_R += float(net_qty)

    alarms = []
    if max_daily_loss_R is not None and R <= -abs(max_daily_loss_R):
        alarms.append({"id": "daily_loss_limit", "level": CRITICAL,
                       "title": "DAILY_LOSS_LIMIT_EXCEEDED",
                       "detail": f"realized {R:+.2f}R ≤ -{abs(max_daily_loss_R)}R"})
    if max_trades_per_day is not None and len(closed_today) > max_trades_per_day:
        alarms.append({"id": "max_trades", "level": WARNING,
                       "title": "MAX_TRADES_EXCEEDED",
                       "detail": f"{len(closed_today)} trades today > {max_trades_per_day}"})
    if open_risk_R_cap is not None and open_risk_R > open_risk_R_cap:
        alarms.append({"id": "open_risk", "level": WARNING,
                       "title": "OPEN_RISK_EXCEEDS_POLICY",
                       "detail": f"open risk {open_risk_R:.1f}R > {open_risk_R_cap}R"})

    return {
        "daily_pnl": round(pnl, 2), "daily_R": round(R, 4),
        "daily_drawdown": round(dd, 2), "open_risk_R": round(open_risk_R, 2),
        "trades_today": len(closed_today), "alarms": alarms,
    }


# ---------------------------------------------------------------------------
# PHASE 6 — supervision verdict
# ---------------------------------------------------------------------------
def supervision(*, alarms: list, fresh: dict, broker: dict, kill_switch: dict) -> dict:
    """The headline: safe to supervise, or operator attention required + reasons."""
    reasons = []
    criticals = [a for a in alarms if a["level"] == CRITICAL]
    for a in criticals:
        reasons.append(f"{a['title']}: {a['detail']}")
    if fresh.get("degraded"):
        reasons.append(f"data degraded (stale > {STALE_S}s)")
    if not broker.get("ok"):
        reasons.append("broker disconnected")
    if broker.get("ok") and broker.get("paper") is False:
        reasons.append("account is not paper")
    if kill_switch.get("present"):
        reasons.append("kill switch active")
    safe = not reasons
    return {"safe": safe, "reasons": reasons, "n_critical": len(criticals)}


# ---------------------------------------------------------------------------
# Critical-alarm latch — criticals stay visible until the operator clears them
# ---------------------------------------------------------------------------
def merge_latch(latch: dict, current_alarms: list, now_iso: str) -> dict:
    """Update the latch: critical alarms persist (active or resolved) until cleared.

    Returns the latch dict {id: {**alarm, active, first_seen, last_seen}}.
    """
    latch = dict(latch or {})
    current_by_id = {a["id"]: a for a in current_alarms if a["level"] == CRITICAL}
    # refresh / add currently-active criticals
    for aid, a in current_by_id.items():
        if aid in latch:
            latch[aid] = {**latch[aid], **a, "active": True, "last_seen": now_iso}
        else:
            latch[aid] = {**a, "active": True, "first_seen": now_iso, "last_seen": now_iso}
    # criticals previously latched but no longer active stay, marked resolved
    for aid in list(latch):
        if aid not in current_by_id:
            latch[aid] = {**latch[aid], "active": False}
    return latch
