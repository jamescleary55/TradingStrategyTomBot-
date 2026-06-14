"""Reconciler tests + RiskGate-uses-resolved-trades verification."""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def temp_state(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    state = tmp_path / ".ict-bot"
    state.mkdir()
    import importlib
    import live.forward_log as fl
    import live.reconcile as rc
    import risk.rules as rules
    import risk.controls as controls
    importlib.reload(fl)
    importlib.reload(rc)
    importlib.reload(rules)
    importlib.reload(controls)
    yield state


@pytest.fixture
def stub_setup():
    from signals.strategies.base import StrategySetup
    return StrategySetup(
        strategy_name="sweep_choch_fvg", strategy_version="1.0.0",
        timestamp=pd.Timestamp("2026-06-12 14:00", tz="UTC"),
        symbol="MNQ", timeframe="1h", direction="bull",
        entry=29000.0, stop=28900.0, target=29200.0, rr=2.0,
        setup_type="sweep_choch_fvg", setup_subtype="EQL_LONDON",
        htf_bias="bull", setup_score=0.75, invalidation_level=28900.0,
        sweep_level_price=28890.0, sweep_level_kind="EQL",
        choch_price=28980.0, bos_state="bull_break",
        fvg_top=29020.0, fvg_bottom=28980.0,
        session="LONDON", confluence=["test"],
    )


class _FakeBroker:
    """In-memory broker with a scriptable execution stream."""
    name = "fake"

    def __init__(self, events):
        self._events = list(events)

    def list_executions(self, account_id=None, since_ts=None):
        out = self._events
        if since_ts:
            out = [e for e in out if e.timestamp > since_ts]
        return out


def _exec(execution_id, order_id, ts, side, qty, price, *, kind="fill", parent=None):
    from execution.base import ExecutionEvent
    return ExecutionEvent(
        execution_id=execution_id, order_id=order_id, parent_order_id=parent,
        timestamp=ts, symbol="MNQ", side=side, qty=qty, price=price,
        kind=kind,
    )


# ---------------------------------------------------------------------------
def test_reconcile_handles_no_executions(temp_state):
    from live.reconcile import reconcile_once
    stats = reconcile_once(_FakeBroker([]))
    assert stats["new_executions"] == 0
    assert stats["resolved_trades"] == 0


def test_reconcile_resolves_winning_trade(temp_state, stub_setup):
    """Submit → entry fill → target fill → r_realised ≈ +2R."""
    from live.forward_log import log_trade_attempt
    from live.reconcile import reconcile_once, load_resolved_trades

    log_trade_attempt(
        strategy_setup=stub_setup, plan=None, broker_name="fake",
        intended_entry=29000.0, intended_stop=28900.0, intended_target=29200.0,
        planned_R=2.0, risk_usd=50.0, contracts=1,
        order_id="ORD1", outcome="submitted",
    )

    events = [
        _exec("EX1", "ORD1", "2026-06-12T14:01:00Z", "Buy", 1, 29000.50),     # entry +0.5pt slip
        _exec("EX2", "ORD2", "2026-06-12T16:00:00Z", "Sell", 1, 29199.75, parent="ORD1"),  # target
    ]
    stats = reconcile_once(_FakeBroker(events))
    assert stats["new_executions"] == 2
    assert stats["resolved_trades"] == 1

    resolved = load_resolved_trades()
    assert len(resolved) == 1
    r = resolved[0]
    assert r["order_id"] == "ORD1"
    assert r["outcome"] == "target"
    assert r["fill_price"] == 29000.50
    assert abs(r["slippage_pts"] - 0.5) < 0.01    # adverse 0.5 pt
    # r_realised ≈ (29199.75 - 29000.50) / (29000 - 28900) = 1.9925
    assert 1.9 < r["r_realised"] < 2.0
    assert r["status"] == "target"


def test_reconcile_resolves_losing_trade(temp_state, stub_setup):
    from live.forward_log import log_trade_attempt
    from live.reconcile import reconcile_once, load_resolved_trades

    log_trade_attempt(
        strategy_setup=stub_setup, plan=None, broker_name="fake",
        intended_entry=29000.0, intended_stop=28900.0, intended_target=29200.0,
        planned_R=2.0, risk_usd=50.0, contracts=1,
        order_id="ORD2", outcome="submitted",
    )
    events = [
        _exec("EX1", "ORD2", "2026-06-12T14:01:00Z", "Buy", 1, 29000.0),
        _exec("EX2", "ORD3", "2026-06-12T15:00:00Z", "Sell", 1, 28898.0, parent="ORD2"),  # stop slip
    ]
    stats = reconcile_once(_FakeBroker(events))
    resolved = load_resolved_trades()
    assert resolved[0]["outcome"] == "stop"
    # r_realised = (28898 - 29000) / (29000 - 28900) = -1.02R (slipped past stop)
    assert -1.1 < resolved[0]["r_realised"] < -1.0


def test_reconcile_still_open_when_no_exit(temp_state, stub_setup):
    from live.forward_log import log_trade_attempt
    from live.reconcile import reconcile_once, load_resolved_trades

    log_trade_attempt(
        strategy_setup=stub_setup, plan=None, broker_name="fake",
        intended_entry=29000.0, intended_stop=28900.0, intended_target=29200.0,
        planned_R=2.0, risk_usd=50.0, contracts=1,
        order_id="ORD3", outcome="submitted",
    )
    events = [_exec("EX1", "ORD3", "2026-06-12T14:01:00Z", "Buy", 1, 29001.0)]
    stats = reconcile_once(_FakeBroker(events))
    assert stats["still_open"] == 1
    resolved = load_resolved_trades()
    assert resolved[0]["status"] == "filled"
    assert "r_realised" not in resolved[0]


def test_reconcile_no_double_resolution(temp_state, stub_setup):
    """Second pass should not duplicate a fully-resolved row."""
    from live.forward_log import log_trade_attempt
    from live.reconcile import reconcile_once, load_resolved_trades

    log_trade_attempt(
        strategy_setup=stub_setup, plan=None, broker_name="fake",
        intended_entry=29000.0, intended_stop=28900.0, intended_target=29200.0,
        planned_R=2.0, risk_usd=50.0, contracts=1,
        order_id="ORD4", outcome="submitted",
    )
    events = [
        _exec("EX1", "ORD4", "2026-06-12T14:01:00Z", "Buy", 1, 29000.0),
        _exec("EX2", "ORD5", "2026-06-12T16:00:00Z", "Sell", 1, 29200.0, parent="ORD4"),
    ]
    reconcile_once(_FakeBroker(events))
    reconcile_once(_FakeBroker([]))    # second tick, no new events
    assert len(load_resolved_trades()) == 1


# ---------------------------------------------------------------------------
def test_risk_gate_now_blocks_after_daily_loss(temp_state, stub_setup):
    """Three losing resolved trades for 1R each must trip max_daily_loss."""
    from live.forward_log import log_trade_attempt
    from live.reconcile import reconcile_once
    from risk.controls import RiskGate
    from risk.rules import load

    # Wire rules to paper mode so the gate evaluates financial caps
    r = load()
    r.mode = "paper"
    r.enable_auto_execute = True
    r.allowed_sessions = ["LONDON"]
    r.allowed_symbols = ["MNQ"]
    r.max_daily_loss_R = 1.0
    r.min_setup_score = 0.0
    r.min_expected_R = 0.0
    r.max_trades_per_day = 99   # so we hit loss cap before trade-count cap
    r.max_trades_per_symbol_per_day = 99
    r.max_open_positions = 99

    # Submit + resolve 2 losing trades totaling -2R today
    today_iso = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    for i, oid in enumerate(["L1", "L2"], start=1):
        log_trade_attempt(
            strategy_setup=stub_setup, plan=None, broker_name="fake",
            intended_entry=29000.0, intended_stop=28900.0, intended_target=29200.0,
            planned_R=2.0, risk_usd=50.0, contracts=1,
            order_id=oid, outcome="submitted",
        )
        events = [
            _exec(f"EX{i}A", oid, f"{today_iso}Z", "Buy", 1, 29000.0),
            _exec(f"EX{i}B", oid + "x", f"{today_iso}Z", "Sell", 1, 28900.0, parent=oid),
        ]
        reconcile_once(_FakeBroker(events))

    gate = RiskGate(r)
    d = gate.check(stub_setup)
    assert not d.allowed
    assert d.rule == "max_daily_loss"
