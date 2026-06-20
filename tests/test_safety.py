"""Phase 2/3 safety: new gate conditions, kill switch, execution preflight.

No broker connection — the adapter is faked. These prove the guardrails block
in exactly the situations the controlled paper run requires.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from execution.base import AccountSnapshot, OpenPosition
from risk import kill_switch as ks
from risk.exec_gate import evaluate_execution_gate
import live.exec_guard as guard


def _good(**over):
    base = dict(
        account_id="DUQ834606", broker="ibkr", mode="paper", live_account=False,
        snapshot=types.SimpleNamespace(partial=False, positions=[]),
        data_status="LIVE", order_qty=1, symbol="MES",
        kill_switch_present=False, open_positions=0,
    )
    base.update(over)
    return base


# ---- new gate conditions (8, 9, 10) ----
def test_gate_blocks_duplicate_signal():
    r = evaluate_execution_gate(**_good(duplicate_signal=True))
    assert not r.allowed and any("duplicate" in x for x in r.reasons)


def test_gate_blocks_pending_order():
    r = evaluate_execution_gate(**_good(pending_order_exists=True))
    assert not r.allowed and any("pending order" in x for x in r.reasons)


def test_gate_blocks_qty_zero():
    r = evaluate_execution_gate(**_good(order_qty=0))
    assert not r.allowed and any("order_qty" in x for x in r.reasons)


def test_gate_blocks_qty_two():
    assert not evaluate_execution_gate(**_good(order_qty=2)).allowed


def test_gate_allows_qty_one():
    assert evaluate_execution_gate(**_good(order_qty=1)).allowed


def test_gate_reports_all_failures_at_once():
    r = evaluate_execution_gate(**_good(
        broker="tradovate", account_id="U1", order_qty=5, duplicate_signal=True))
    # broker, account, qty, duplicate all reported — gate is not short-circuit.
    assert not r.allowed and len(r.reasons) >= 4


# ---- kill switch ----
def test_kill_switch_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(ks, "STATE_DIR", tmp_path)
    state = ks.check(str(tmp_path / "KILL_SWITCH"))
    assert state.present is False and bool(state) is False


def test_kill_switch_configured_path(tmp_path, monkeypatch):
    monkeypatch.setattr(ks, "STATE_DIR", tmp_path)
    flag = tmp_path / "KILL_SWITCH"
    flag.write_text("halt")
    state = ks.check(str(flag))
    assert state.present is True and str(flag) in state.path


def test_kill_switch_common_flag_name(tmp_path, monkeypatch):
    """Any conventional flag name in the state dir trips it, even if not configured."""
    monkeypatch.setattr(ks, "STATE_DIR", tmp_path)
    (tmp_path / "halt.flag").write_text("x")
    state = ks.check(configured_path=None)
    assert state.present is True and "halt.flag" in state.path


# ---- preflight guard (the live choke point, faked adapter) ----
class _FakeAdapter:
    name = "ibkr"

    def __init__(self, *, positions=0, open_orders=None, account="DUQ834606",
                 raise_snapshot=False):
        self._positions = positions
        self._open_orders = open_orders or []
        self._account = account
        self._raise = raise_snapshot

    def snapshot(self, account_id=None):
        if self._raise:
            raise RuntimeError("snapshot boom")
        return AccountSnapshot(
            account_id=self._account, cash=1e6, equity=1e6,
            positions=[OpenPosition(symbol="MESU6", side="Buy", qty=1, avg_entry=1.0)
                       for _ in range(self._positions)])

    def list_open_orders(self, account_id=None):
        return list(self._open_orders)


def _preflight(adapter, monkeypatch, tmp_path, **over):
    # keep tests pure: no real events.jsonl writes, no real kill-switch dir.
    monkeypatch.setattr(guard, "log_event", lambda *a, **k: {})
    monkeypatch.setattr(ks, "STATE_DIR", tmp_path)
    kw = dict(
        adapter=adapter, mode="paper", allow_live=False, symbol="MES",
        order_qty=1, setup_signature="MES|bull|t1", executed_signatures=set(),
        data_status="LIVE", data_override=False,
        kill_switch_path=str(tmp_path / "KILL_SWITCH"),
    )
    kw.update(over)
    return guard.preflight(**kw)


def test_preflight_allows_clean_account(monkeypatch, tmp_path):
    r = _preflight(_FakeAdapter(positions=0), monkeypatch, tmp_path)
    assert r.allowed is True and r.snapshot is not None


def test_preflight_blocks_open_position(monkeypatch, tmp_path):
    r = _preflight(_FakeAdapter(positions=1), monkeypatch, tmp_path)
    assert not r.allowed and any("open_positions" in x for x in r.gate.reasons)


def test_preflight_blocks_pending_order(monkeypatch, tmp_path):
    r = _preflight(_FakeAdapter(open_orders=[{"orderId": 7}]), monkeypatch, tmp_path)
    assert not r.allowed and any("pending order" in x for x in r.gate.reasons)


def test_preflight_blocks_on_snapshot_failure(monkeypatch, tmp_path):
    r = _preflight(_FakeAdapter(raise_snapshot=True), monkeypatch, tmp_path)
    assert not r.allowed
    assert r.snapshot is None
    assert any("snapshot hard-failed" in x for x in r.gate.reasons)


def test_preflight_blocks_duplicate(monkeypatch, tmp_path):
    r = _preflight(_FakeAdapter(), monkeypatch, tmp_path,
                   executed_signatures={"MES|bull|t1"})
    assert not r.allowed and any("duplicate" in x for x in r.gate.reasons)


def test_preflight_blocks_when_kill_switch_present(monkeypatch, tmp_path):
    (tmp_path / "KILL_SWITCH").write_text("stop")
    r = _preflight(_FakeAdapter(), monkeypatch, tmp_path)
    assert not r.allowed and any("kill switch" in x for x in r.gate.reasons)


def test_preflight_blocks_non_du_account(monkeypatch, tmp_path):
    r = _preflight(_FakeAdapter(account="U7654321"), monkeypatch, tmp_path)
    assert not r.allowed and any("paper (DU)" in x for x in r.gate.reasons)


def test_preflight_blocks_non_live_data_without_override(monkeypatch, tmp_path):
    r = _preflight(_FakeAdapter(), monkeypatch, tmp_path, data_status="HISTORICAL_ONLY")
    assert not r.allowed and any("market-data" in x for x in r.gate.reasons)


def test_preflight_allows_non_live_data_with_override(monkeypatch, tmp_path):
    r = _preflight(_FakeAdapter(), monkeypatch, tmp_path,
                   data_status="HISTORICAL_ONLY", data_override=True)
    assert r.allowed is True
