"""Forward-testing structured loggers.

Three append-only JSONL files under ``~/.ict-bot``:

- ``live_signals.jsonl``       — every setup the detector emits (16-field schema)
- ``skipped_setups.jsonl``     — setups blocked by risk gate / filters, with reason
- ``live_trades.jsonl``        — every order attempt incl. broker response + fill

The shape is intentionally JSON-flat so it survives schema drift and is
easy to load into pandas / DuckDB for ad-hoc analysis.

Functions are tiny and atomic (open-append-close) so multiple processes
(monitor threads, webhook receiver, positions poller) can write
concurrently without coordination beyond OS-level append semantics.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("live.forward_log")
STATE_DIR = Path.home() / ".ict-bot"
STATE_DIR.mkdir(parents=True, exist_ok=True)

SIGNALS_LOG = STATE_DIR / "live_signals.jsonl"
SKIPPED_LOG = STATE_DIR / "skipped_setups.jsonl"
TRADES_LOG = STATE_DIR / "live_trades.jsonl"
EVENTS_LOG = STATE_DIR / "events.jsonl"

# Phase 5 observability taxonomy. Every meaningful state change is one event row
# in events.jsonl, tagged with a category + event name so the go/no-go reader
# can count fills, rejects, disconnects, kill-switch halts, etc.
EV_SIGNAL = "signal"        # detected | rejected | executed
EV_ORDER = "order"          # submitted | accepted | cancelled | rejected
EV_EXECUTION = "execution"  # fill | partial_fill | stop_hit | target_hit
EV_SYSTEM = "system"        # disconnect | reconnect | snapshot_timeout | data_failure | kill_switch | gate_block


def _now_iso() -> str:
    return dt.datetime.utcnow().isoformat() + "Z"


def _append(path: Path, row: dict) -> None:
    try:
        with open(path, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        log.exception("Failed to append to %s", path)


# ---------------------------------------------------------------------------
# Signal log (16-field schema per the brief)
# ---------------------------------------------------------------------------
def log_signal(
    *,
    strategy_setup,          # signals.strategies.base.StrategySetup
    news_blackout: bool,
    spread_estimate: float,
    trade_allowed: bool,
    skip_reason: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    """Append a signal-detection row. Returns the row written."""
    s = strategy_setup
    row = {
        "ts_logged": _now_iso(),
        "timestamp": s.timestamp.isoformat() if hasattr(s.timestamp, "isoformat") else str(s.timestamp),
        "symbol": s.symbol,
        "timeframe": s.timeframe,
        "session": s.session,
        "strategy_name": s.strategy_name,
        "strategy_version": s.strategy_version,
        "setup_type": s.setup_type,
        "setup_subtype": s.setup_subtype,
        "direction": s.direction,
        "htf_bias": s.htf_bias,
        "sweep_level_price": s.sweep_level_price,
        "sweep_level_kind": s.sweep_level_kind,
        "choch_price": s.choch_price,
        "bos_state": s.bos_state,
        "fvg_top": s.fvg_top,
        "fvg_bottom": s.fvg_bottom,
        "entry": s.entry,
        "stop": s.stop,
        "target": s.target,
        "planned_R": s.rr,
        "invalidation_level": s.invalidation_level,
        "setup_score": s.setup_score,
        "news_blackout": news_blackout,
        "spread_estimate": spread_estimate,
        "trade_allowed": trade_allowed,
        "skip_reason": skip_reason,
        "confluence": list(s.confluence),
        "extra": extra or {},
    }
    _append(SIGNALS_LOG, row)
    return row


# ---------------------------------------------------------------------------
# Skipped log — every block reason, indexed for stats
# ---------------------------------------------------------------------------
def log_skipped(
    *,
    strategy_setup,
    reason: str,
    rule_name: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    s = strategy_setup
    row = {
        "ts_logged": _now_iso(),
        "timestamp": s.timestamp.isoformat() if hasattr(s.timestamp, "isoformat") else str(s.timestamp),
        "symbol": s.symbol,
        "timeframe": s.timeframe,
        "session": s.session,
        "direction": s.direction,
        "strategy_name": s.strategy_name,
        "setup_type": s.setup_type,
        "setup_subtype": s.setup_subtype,
        "entry": s.entry,
        "stop": s.stop,
        "target": s.target,
        "planned_R": s.rr,
        "setup_score": s.setup_score,
        "reason": reason,
        "rule": rule_name,
        "extra": extra or {},
    }
    _append(SKIPPED_LOG, row)
    return row


# ---------------------------------------------------------------------------
# Trade log — full execution forensics
# ---------------------------------------------------------------------------
def log_trade_attempt(
    *,
    strategy_setup,
    plan,                              # risk.sizing.TradePlan
    broker_name: str,
    intended_entry: float,
    intended_stop: float,
    intended_target: float,
    planned_R: float,
    risk_usd: float,
    contracts: int,
    fill_price: Optional[float] = None,
    slippage_pts: Optional[float] = None,
    order_id: Optional[Any] = None,
    broker_response: Optional[dict] = None,
    outcome: str = "submitted",        # submitted | filled | rejected | failed
    error: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    s = strategy_setup
    row = {
        "ts_logged": _now_iso(),
        "timestamp": s.timestamp.isoformat() if hasattr(s.timestamp, "isoformat") else str(s.timestamp),
        "symbol": s.symbol,
        "timeframe": s.timeframe,
        "session": s.session,
        "direction": s.direction,
        "strategy_name": s.strategy_name,
        "setup_type": s.setup_type,
        "setup_subtype": s.setup_subtype,
        "broker": broker_name,
        "intended_entry": intended_entry,
        "intended_stop": intended_stop,
        "intended_target": intended_target,
        "planned_R": planned_R,
        "risk_usd": risk_usd,
        "contracts": contracts,
        "fill_price": fill_price,
        "slippage_pts": slippage_pts,
        "order_id": order_id,
        "broker_response": broker_response,
        "outcome": outcome,
        "error": error,
        "extra": extra or {},
    }
    _append(TRADES_LOG, row)
    return row


# ---------------------------------------------------------------------------
# Event log — Phase 5 observability. One append-only row per state change.
# ---------------------------------------------------------------------------
def log_event(
    category: str,                     # EV_SIGNAL | EV_ORDER | EV_EXECUTION | EV_SYSTEM
    event: str,                        # e.g. "detected", "submitted", "fill", "disconnect"
    *,
    symbol: Optional[str] = None,
    detail: Optional[str] = None,
    severity: str = "info",            # info | warning | error
    **fields: Any,
) -> dict:
    """Append one structured event row to events.jsonl. Never raises."""
    row = {
        "ts_logged": _now_iso(),
        "category": category,
        "event": event,
        "symbol": symbol,
        "severity": severity,
        "detail": detail,
        **fields,
    }
    _append(EVENTS_LOG, row)
    return row


def load_events() -> list[dict]:
    return _load(EVENTS_LOG)


# ---------------------------------------------------------------------------
def load_signals() -> list[dict]:
    return _load(SIGNALS_LOG)


def load_skipped() -> list[dict]:
    return _load(SKIPPED_LOG)


def load_trades() -> list[dict]:
    return _load(TRADES_LOG)


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
