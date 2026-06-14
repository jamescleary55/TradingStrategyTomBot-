"""Central risk gate.

Reads :class:`risk.rules.PersonalRules` and the forward logs and answers
one question per signal:

    is_trade_allowed(setup, context) -> RiskDecision(allowed, reason, rule)

Stateful checks (daily loss in R, weekly loss in R, max trades per
day/symbol, max consecutive losses, max open positions) are computed
from ``live_trades.jsonl`` so every process sees the same truth.

A kill switch file at ``rules.kill_switch_path`` blocks all new trades
unconditionally. ``touch ~/.ict-bot/KILL_SWITCH`` to halt the bot.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from live.forward_log import load_trades
from live.reconcile import load_resolved_trades
from risk.rules import PersonalRules, load as load_rules
from utils.time_utils import current_session

log = logging.getLogger("risk.controls")


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = "ok"
    rule: str = ""

    def __bool__(self) -> bool:
        return self.allowed


# ---------------------------------------------------------------------------
def _today_et() -> dt.date:
    """Today in US Eastern (the bot's trading day).

    Futures session boundary is 17:00 ET — after 17:00 ET, we treat it as
    the next trading day's session so caps reset correctly.
    """
    try:
        from zoneinfo import ZoneInfo
        now_et = dt.datetime.now(tz=ZoneInfo("America/New_York"))
    except Exception:
        # Older Python or missing tzdata — fall back to UTC (degrades to A3)
        return dt.datetime.utcnow().date()
    if now_et.hour >= 17:
        return (now_et + dt.timedelta(days=1)).date()
    return now_et.date()


def _week_key(d: dt.date) -> tuple[int, int]:
    iso = d.isocalendar()
    return (iso[0], iso[1])


# ---------------------------------------------------------------------------
class RiskGate:
    """Stateful risk-rule evaluator. Construct once per process."""

    def __init__(self, rules: Optional[PersonalRules] = None,
                 broker_adapter=None):
        self.rules = rules or load_rules()
        self.broker = broker_adapter

    # ---- single entry point ---------------------------------------
    def check(self, setup, news_blackout: bool = False,
              open_positions: Optional[int] = None) -> RiskDecision:
        r = self.rules

        # 0) Kill switch — always wins
        if r.kill_switch.exists():
            return RiskDecision(False, f"KILL_SWITCH present at {r.kill_switch}",
                                "kill_switch")

        # 1) Mode == review → never auto-execute
        if r.mode == "review":
            return RiskDecision(False, "mode=review (manual approval only)",
                                "mode_review")

        # 2) Auto-execute master switch
        if not r.enable_auto_execute:
            return RiskDecision(False, "enable_auto_execute=false",
                                "auto_execute_disabled")

        # 3) Universe
        if setup.symbol not in r.allowed_symbols:
            return RiskDecision(False, f"{setup.symbol} not in allowed_symbols",
                                "symbol_not_allowed")
        # A4 fix: missing session == reject. Only allow setups whose session
        # is explicitly in the allowlist. The previous `if setup.session and …`
        # silently let through setups outside any defined session window.
        if not setup.session or setup.session not in r.allowed_sessions:
            return RiskDecision(
                False,
                f"session {setup.session!r} not in allowed_sessions {r.allowed_sessions}",
                "session_not_allowed",
            )

        # 4) News blackout
        if news_blackout and r.news_filter_enabled:
            return RiskDecision(False, "news blackout window", "news_blackout")

        # 5) Setup quality gates
        if setup.rr < r.min_expected_R:
            return RiskDecision(False,
                                f"RR {setup.rr:.2f} < min_expected_R {r.min_expected_R}",
                                "below_min_rr")
        if setup.setup_score < r.min_setup_score:
            return RiskDecision(False,
                                f"score {setup.setup_score:.2f} < min_setup_score {r.min_setup_score}",
                                "below_min_score")

        # 6) Open-position cap (broker-truth if available, log-truth fallback)
        open_n = open_positions
        if open_n is None and self.broker is not None:
            try:
                snap = self.broker.snapshot()
                open_n = len(snap.positions)
            except Exception:
                open_n = None
        if open_n is None:
            open_n = self._open_from_logs()
        if open_n >= r.max_open_positions:
            return RiskDecision(False,
                                f"{open_n} open positions >= max_open_positions {r.max_open_positions}",
                                "max_open_positions")

        # 7) Trades-per-day / per-symbol-per-day
        # Use resolved trades (with r_realised) for financial caps, raw trades
        # (every attempt) for count caps. Today's resolved + still-pending
        # together = real attempts today.
        trades = load_trades()
        resolved = load_resolved_trades()
        today = _today_et()
        today_trades = [t for t in trades if _trade_date(t) == today]
        if len(today_trades) >= r.max_trades_per_day:
            return RiskDecision(False,
                                f"{len(today_trades)} trades today >= max_trades_per_day {r.max_trades_per_day}",
                                "max_trades_per_day")
        same_sym = [t for t in today_trades if t.get("symbol") == setup.symbol]
        if len(same_sym) >= r.max_trades_per_symbol_per_day:
            return RiskDecision(False,
                                f"{len(same_sym)} {setup.symbol} trades today >= "
                                f"max_trades_per_symbol_per_day {r.max_trades_per_symbol_per_day}",
                                "max_trades_per_symbol_per_day")

        # 8) Daily loss (in R) — from RESOLVED trades only
        today_resolved = [t for t in resolved if _trade_date(t) == today]
        day_R = self._sum_R(today_resolved)
        if day_R <= -r.max_daily_loss_R:
            return RiskDecision(False,
                                f"day P&L {day_R:+.2f}R <= -max_daily_loss_R {r.max_daily_loss_R}",
                                "max_daily_loss")

        # 9) Weekly loss (in R) — from RESOLVED trades only
        wk = _week_key(today)
        week_resolved = [t for t in resolved if _week_key(_trade_date(t)) == wk]
        wk_R = self._sum_R(week_resolved)
        if wk_R <= -r.max_weekly_loss_R:
            return RiskDecision(False,
                                f"week P&L {wk_R:+.2f}R <= -max_weekly_loss_R {r.max_weekly_loss_R}",
                                "max_weekly_loss")

        # 10) Consecutive losses — from RESOLVED trades only
        closed = [t for t in resolved
                  if t.get("status") in ("target", "stop") and "r_realised" in t]
        # Walk backwards
        streak = 0
        for t in reversed(closed):
            r_real = float(t.get("r_realised") or 0)
            if r_real < 0:
                streak += 1
            else:
                break
        if streak >= r.max_consecutive_losses:
            return RiskDecision(False,
                                f"{streak} consecutive losses >= max_consecutive_losses {r.max_consecutive_losses}",
                                "max_consecutive_losses")

        return RiskDecision(True, "ok", "")

    # ---- helpers --------------------------------------------------
    def _open_from_logs(self) -> int:
        """Best-effort open-position count from trade log: submitted/filled
        without a closing record (target/stop)."""
        trades = load_trades()
        open_orders: dict[str, int] = {}
        for t in trades:
            sym = t.get("symbol", "?")
            outcome = t.get("outcome", "")
            if outcome in ("submitted", "filled"):
                open_orders[sym] = open_orders.get(sym, 0) + 1
            elif outcome in ("target", "stop", "rejected", "failed", "voided"):
                open_orders[sym] = max(0, open_orders.get(sym, 0) - 1)
        return sum(open_orders.values())

    def _sum_R(self, trades: list[dict]) -> float:
        return sum(float(t.get("r_realised") or 0)
                   for t in trades if "r_realised" in t)


# ---------------------------------------------------------------------------
def _trade_date(t: dict) -> dt.date:
    ts = t.get("ts_logged") or t.get("timestamp") or ""
    try:
        v = pd.Timestamp(ts)
        if pd.isna(v):
            return dt.date.min
        return v.date()
    except Exception:
        return dt.date.min
