"""HTTP webhook receiver — turn TradingView / NinjaTrader / any platform
into trade signals for this bot.

Endpoints
---------
``POST /webhook``
    Accepts a JSON body in one of two shapes:

    **Generic schema** (recommended for non-TradingView platforms)::

        {
          "symbol":    "MNQ",       # or NQ, ES, MES, etc. — see config.INSTRUMENTS
          "direction": "bull" | "bear",
          "entry":     17500.25,
          "stop":      17450.00,
          "target":    17600.00,
          "comment":   "ICT setup",          (optional)
          "platform":  "tradingview",        (optional, for logging)
          "execute":   false                 (optional — opt-in auto-execute)
        }

    **TradingView Pine Script alert** — same shape, plus a fallback that
    accepts ``"action": "buy" | "sell"`` instead of ``"direction"``,
    and ``"ticker"``/``"contract"`` aliases for ``"symbol"``.

Auth
----
If ``WEBHOOK_SECRET`` is set in ``.env``, every request must carry the
matching value in the header ``X-Webhook-Secret`` (or the JSON field
``"secret"``). Otherwise it's rejected 401.

Pipeline on a valid payload
---------------------------
1. Build a synthetic :class:`signals.setup.Setup`-like object so we can
   reuse the alerter + sizing + execution code unchanged.
2. Fire alert via :class:`utils.alerter.Alerter` (console + macOS +
   Telegram, with chart if a small data window is available).
3. If ``execute=true`` AND the receiver was launched with
   ``--auto-execute``, route through :func:`risk.sizing.plan_trade` and
   :func:`execution.tradovate_orders.place_bracket_for_setup`.
4. Append to ``~/.ict-bot/alerts.jsonl`` so the tracker resolves the
   outcome later.

CLI
---
    python -m live.webhook --port 5005 --equity 50000 --risk-pct 0.015
    python -m live.webhook --auto-execute --execute-dry-run    # safe smoke test
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd
from flask import Flask, abort, jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
from data.loader import load_bars
from live.forward_log import log_signal, log_skipped, log_trade_attempt
from risk.controls import RiskGate
from risk.rules import load as load_rules
from risk.sizing import plan_trade
from signals.strategies.base import StrategySetup
from utils.alerter import Alerter

log = logging.getLogger("live.webhook")
STATE_DIR = Path.home() / ".ict-bot"
STATE_DIR.mkdir(parents=True, exist_ok=True)

app = Flask("ict-webhook")


# ---------------------------------------------------------------------------
# Synthetic Setup so the alerter + risk + execution paths work unchanged.
# ---------------------------------------------------------------------------
@dataclass
class _StubSweepLevel:
    kind: str = "WEBHOOK"
    price: float = 0.0
    label: str = "external signal"


@dataclass
class _StubSweep:
    level: _StubSweepLevel = field(default_factory=_StubSweepLevel)
    wick_extreme: float = 0.0


@dataclass
class _StubChoch:
    timestamp: pd.Timestamp = field(default_factory=lambda: pd.Timestamp.utcnow())
    price: float = 0.0
    idx: int = 0


@dataclass
class _StubFvg:
    direction: str = "bull"
    top: float = 0.0
    bottom: float = 0.0
    idx: int = 0


@dataclass
class WebhookSetup:
    timestamp: pd.Timestamp
    direction: str        # "bull" | "bear"
    entry: float
    stop: float
    target: float
    rr: float
    bias: str             # echo from caller, or computed
    confluence: list[str] = field(default_factory=list)
    sweep: _StubSweep = field(default_factory=_StubSweep)
    choch: _StubChoch = field(default_factory=_StubChoch)
    fvg: _StubFvg = field(default_factory=_StubFvg)


# ---------------------------------------------------------------------------
# Runtime state injected by main()
# ---------------------------------------------------------------------------
class _Runtime:
    alerter: Optional[Alerter] = None
    secret: str = ""
    auto_execute: bool = False
    equity: float = 10_000.0
    risk_pct: float = cfg.RISK.max_risk_per_trade_pct
    allow_live: bool = False
    execute_dry_run: bool = False
    risk_gate: Optional[RiskGate] = None


RUNTIME = _Runtime()


def _ensure_risk_gate() -> RiskGate:
    if RUNTIME.risk_gate is None:
        RUNTIME.risk_gate = RiskGate(load_rules())
    return RUNTIME.risk_gate


# ---------------------------------------------------------------------------
def _normalise_direction(payload: dict) -> Optional[str]:
    d = payload.get("direction") or payload.get("side") or payload.get("action")
    if not d:
        return None
    d = str(d).strip().lower()
    if d in ("bull", "long", "buy"):
        return "bull"
    if d in ("bear", "short", "sell"):
        return "bear"
    return None


def _normalise_symbol(payload: dict) -> Optional[str]:
    raw = (payload.get("symbol") or payload.get("ticker")
           or payload.get("contract") or "")
    raw = str(raw).strip().upper()
    if not raw:
        return None
    # TradingView often sends "NQ1!" or "ES1!" — strip the futures suffix
    raw = raw.replace("1!", "").replace("!", "")
    # Map root → micro by default (matches sim symbol logic elsewhere)
    return raw


def _resolve_sim_symbol(symbol: str) -> str:
    if symbol in cfg.INSTRUMENTS:
        return symbol
    # Map root → micro if present
    micro_map = {"NQ": "MNQ", "ES": "MES", "GC": "MGC", "CL": "MCL"}
    return micro_map.get(symbol, symbol)


def _build_strategy_setup(symbol: str, sim_symbol: str, direction: str,
                          entry: float, stop: float, target: float,
                          rr: float, platform: str, comment: str,
                          bias: str) -> StrategySetup:
    """Materialise a real StrategySetup for the new logging + risk paths."""
    ts = pd.Timestamp.utcnow()
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    confluence = [f"platform={platform}"]
    if comment:
        confluence.append(comment[:120])
    fvg_top = max(entry, stop)
    fvg_bot = min(entry, stop)
    return StrategySetup(
        strategy_name=f"webhook:{platform}",
        strategy_version="1.0.0",
        timestamp=ts,
        symbol=sim_symbol,                       # the contract we'd actually trade
        timeframe="webhook",
        direction=direction,
        entry=entry, stop=stop, target=target, rr=rr,
        setup_type=f"webhook_{platform}",
        setup_subtype=f"WEBHOOK_{platform.upper()}",
        htf_bias=bias if bias in ("bull", "bear") else None,
        setup_score=0.5,                          # external signal — neutral score
        invalidation_level=stop,
        sweep_level_price=None,
        sweep_level_kind=None,
        choch_price=None,
        bos_state=None,
        fvg_top=fvg_top,
        fvg_bottom=fvg_bot,
        session=None,
        confluence=confluence,
    )


# ---------------------------------------------------------------------------
def _check_auth(payload: dict) -> None:
    if not RUNTIME.secret:
        return
    supplied = (request.headers.get("X-Webhook-Secret")
                or payload.get("secret") or "")
    if supplied != RUNTIME.secret:
        log.warning("rejected request: bad/missing secret")
        abort(401, description="bad secret")


@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json(silent=True) or {}
    # TradingView often sends raw text — try to parse it
    if not payload and request.data:
        try:
            payload = json.loads(request.data.decode("utf-8"))
        except Exception:
            log.warning("non-JSON body: %s", request.data[:200])
            abort(400, description="json body required")

    _check_auth(payload)

    direction = _normalise_direction(payload)
    if direction not in ("bull", "bear"):
        abort(400, description="direction missing or invalid")
    symbol = _normalise_symbol(payload)
    if not symbol:
        abort(400, description="symbol missing")
    sim_symbol = _resolve_sim_symbol(symbol)
    if sim_symbol not in cfg.INSTRUMENTS:
        abort(400, description=f"unknown symbol {symbol}")

    try:
        entry = float(payload["entry"])
        stop = float(payload["stop"])
        target = float(payload["target"])
    except (KeyError, TypeError, ValueError):
        abort(400, description="entry/stop/target must be numeric")

    # Validate price geometry
    if direction == "bull" and not (stop < entry < target):
        abort(400, description=f"bull: need stop({stop}) < entry({entry}) < target({target})")
    if direction == "bear" and not (target < entry < stop):
        abort(400, description=f"bear: need target({target}) < entry({entry}) < stop({stop})")

    rr = abs(target - entry) / abs(entry - stop) if entry != stop else 0.0
    platform = (payload.get("platform") or payload.get("source") or "generic").lower()
    bias = payload.get("bias", "external")
    comment = payload.get("comment", "")
    execute_flag = bool(payload.get("execute", False))

    instrument = cfg.INSTRUMENTS[sim_symbol]
    strategy_setup = _build_strategy_setup(
        symbol=symbol, sim_symbol=sim_symbol, direction=direction,
        entry=entry, stop=stop, target=target, rr=rr,
        platform=platform, comment=comment, bias=bias,
    )

    # Risk gate — same rules as the internal monitor. Webhook does NOT
    # bypass kill switch / daily loss / session restrictions.
    gate = _ensure_risk_gate()
    decision = gate.check(strategy_setup, news_blackout=False)
    trade_allowed = decision.allowed

    # Persist to the unified structured logs
    log_signal(
        strategy_setup=strategy_setup,
        news_blackout=False,
        spread_estimate=0.0,
        trade_allowed=trade_allowed,
        skip_reason=(decision.reason if not trade_allowed else None),
        extra={"platform": platform, "raw_payload": payload},
    )
    if not trade_allowed:
        log_skipped(strategy_setup=strategy_setup, reason=decision.reason,
                    rule_name=decision.rule,
                    extra={"platform": platform})

    # Fetch a small data window for the alert chart (best-effort, non-blocking)
    df = None
    try:
        df = load_bars(symbol, "15m", days=2, source="yfinance")
        if df is None or df.empty:
            df = load_bars(sim_symbol, "15m", days=2, source="yfinance")
    except Exception as e:
        log.info("chart data fetch skipped: %s", e)

    # Build a minimal stub Setup for the alerter (which expects the legacy shape)
    webhook_setup_legacy = WebhookSetup(
        timestamp=strategy_setup.timestamp, direction=direction,
        entry=entry, stop=stop, target=target, rr=rr,
        bias=bias, confluence=strategy_setup.confluence,
        sweep=_StubSweep(level=_StubSweepLevel(price=stop, label=f"{platform} signal")),
        choch=_StubChoch(timestamp=strategy_setup.timestamp, price=entry),
        fvg=_StubFvg(direction=direction,
                     top=max(entry, stop), bottom=min(entry, stop)),
    )
    try:
        RUNTIME.alerter.notify_setup(webhook_setup_legacy, instrument,
                                     sim_symbol=sim_symbol, df=df)
    except Exception:
        log.exception("notify_setup failed")

    order_id = None
    if RUNTIME.auto_execute and execute_flag and trade_allowed:
        try:
            plan = plan_trade(
                equity=RUNTIME.equity, entry=entry, stop=stop, target=target,
                instrument=instrument, risk_pct=RUNTIME.risk_pct, min_rr=1.0,
            )
            if not plan.approved:
                log_skipped(strategy_setup=strategy_setup, reason=plan.reason,
                            rule_name="sizing")
                RUNTIME.alerter.notify(
                    f"Webhook order skipped ({sim_symbol})",
                    f"{direction} setup not sized: {plan.reason}",
                    severity="warning",
                )
            else:
                from execution.tradovate_orders import place_bracket_for_setup
                result = place_bracket_for_setup(
                    webhook_setup_legacy, plan, instrument,
                    allow_live=RUNTIME.allow_live,
                    dry_run=RUNTIME.execute_dry_run,
                )
                order_id = result.order_id
                log_trade_attempt(
                    strategy_setup=strategy_setup, plan=plan, broker_name="tradovate",
                    intended_entry=entry, intended_stop=stop, intended_target=target,
                    planned_R=rr, risk_usd=plan.total_risk_usd, contracts=plan.contracts,
                    order_id=order_id, broker_response=result.raw_response,
                    outcome="submitted",
                )
                RUNTIME.alerter.notify(
                    f"Webhook order placed ({sim_symbol})",
                    f"Bracket #{order_id} for {sim_symbol} "
                    f"{direction} @ {entry:.2f} (qty {plan.contracts})",
                    severity="success",
                )
        except Exception as ex:
            log_trade_attempt(
                strategy_setup=strategy_setup, plan=None, broker_name="tradovate",
                intended_entry=entry, intended_stop=stop, intended_target=target,
                planned_R=rr, risk_usd=0.0, contracts=0,
                outcome="failed", error=str(ex),
            )
            RUNTIME.alerter.notify(f"Webhook order failed ({sim_symbol})",
                                   str(ex), severity="error")
            log.exception("auto-execute failed")
    elif RUNTIME.auto_execute and execute_flag and not trade_allowed:
        # Caller asked for execute but the gate blocked
        RUNTIME.alerter.notify(
            f"Webhook order blocked ({sim_symbol})",
            f"{decision.rule}: {decision.reason}",
            severity="warning",
        )

    return jsonify({
        "ok": True,
        "symbol": symbol,
        "sim_symbol": sim_symbol,
        "direction": direction,
        "entry": entry, "stop": stop, "target": target, "rr": round(rr, 2),
        "executed": order_id is not None,
        "order_id": order_id,
        "trade_allowed": trade_allowed,
        "block_reason": (decision.reason if not trade_allowed else None),
        "block_rule": (decision.rule if not trade_allowed else None),
    })


@app.route("/health", methods=["GET"])
def health():
    from live.forward_log import SIGNALS_LOG
    n = 0
    if SIGNALS_LOG.exists():
        with open(SIGNALS_LOG) as f:
            n = sum(1 for line in f if line.strip())
    rules = (RUNTIME.risk_gate.rules if RUNTIME.risk_gate else None)
    return jsonify({
        "ok": True,
        "auth_required": bool(RUNTIME.secret),
        "auto_execute": RUNTIME.auto_execute,
        "execute_dry_run": RUNTIME.execute_dry_run,
        "signals_logged_total": n,
        "rules_source": rules.source if rules else None,
        "mode": rules.mode if rules else None,
    })


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ICT webhook receiver")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int,
                        default=int(os.getenv("WEBHOOK_PORT", "5005")))
    parser.add_argument("--secret", default=os.getenv("WEBHOOK_SECRET", ""),
                        help="Override .env WEBHOOK_SECRET")
    parser.add_argument("--auto-execute", action="store_true",
                        help="Allow payloads with execute=true to place Tradovate orders.")
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument("--risk-pct", type=float, default=cfg.RISK.max_risk_per_trade_pct)
    parser.add_argument("--allow-live", action="store_true",
                        help="Allow orders when TRADOVATE_ENV != demo. USE WITH CARE.")
    parser.add_argument("--execute-dry-run", action="store_true",
                        help="Build order body and log it, but don't send.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    RUNTIME.alerter = Alerter()
    RUNTIME.secret = args.secret
    RUNTIME.auto_execute = args.auto_execute
    RUNTIME.equity = args.equity
    RUNTIME.risk_pct = args.risk_pct
    RUNTIME.allow_live = args.allow_live
    RUNTIME.execute_dry_run = args.execute_dry_run

    log.info("Listening on http://%s:%d/webhook", args.host, args.port)
    log.info("Auth required: %s  ·  Auto-execute: %s  ·  Dry-run: %s",
             "yes" if RUNTIME.secret else "no",
             "yes" if RUNTIME.auto_execute else "no",
             "yes" if RUNTIME.execute_dry_run else "no")
    log.info("Health check: curl http://%s:%d/health", args.host, args.port)

    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
