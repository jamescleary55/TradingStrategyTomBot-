"""Dashboard hardening — failure-detection logic (Phase 7).

Pure tests against live/ops_health.py: staleness, mismatch, execution-safety
alarms, reconciliation health, daily risk, supervision verdict, and the latch.
No server, no broker, no real clock.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from live import ops_health as oh

NOW = "2026-06-22T14:00:30+00:00"


def _ks(present=False, path=None):
    return {"present": present, "path": path}


def _broker(ok=True, account="DUQ834606", positions=None, orders=None, error=None):
    return {"ok": ok, "account_id": account, "paper": str(account or "").startswith("DU"),
            "positions": positions or [], "open_orders": orders or [], "error": error,
            "read_ts": NOW}


# ---- Phase 1: staleness ----
def test_staleness_levels():
    assert oh.staleness_level(3) == oh.GREEN
    assert oh.staleness_level(10) == oh.YELLOW
    assert oh.staleness_level(30) == oh.YELLOW
    assert oh.staleness_level(31) == oh.RED
    assert oh.staleness_level(None) == oh.RED


def test_stale_broker_data_degrades():
    f = oh.freshness(NOW, broker_ts="2026-06-22T13:59:00+00:00",  # 90s old
                     event_ts=NOW, heartbeat_ts=NOW, monitor_running=False)
    assert f["broker"]["level"] == oh.RED
    assert f["degraded"] is True


def test_fresh_broker_not_degraded_and_events_informational():
    # broker fresh, monitor not running, events very old → NOT degraded
    f = oh.freshness(NOW, broker_ts=NOW, event_ts="2020-01-01T00:00:00Z",
                     heartbeat_ts=None, monitor_running=False)
    assert f["broker"]["level"] == oh.GREEN
    assert f["event"]["level"] == oh.RED        # shown, but…
    assert f["degraded"] is False               # …does not degrade
    assert f["heartbeat"].get("na") is True


def test_stale_heartbeat_degrades_when_monitor_running():
    f = oh.freshness(NOW, broker_ts=NOW, event_ts=NOW,
                     heartbeat_ts="2026-06-22T13:59:00+00:00",  # 90s old
                     monitor_running=True)
    assert f["heartbeat"]["level"] == oh.RED
    assert f["degraded"] is True


# ---- Phase 2: position mismatch ----
def test_mismatch_broker_position_bot_flat():
    al = oh.position_mismatch([{"symbol": "MESU6", "side": "Buy", "qty": 1}], {})
    assert len(al) == 1 and al[0]["title"] == "POSITION_MISMATCH"
    assert al[0]["level"] == oh.CRITICAL and "FLAT" in al[0]["detail"]


def test_mismatch_bot_position_broker_flat():
    al = oh.position_mismatch([], {"MESU6": 1})
    assert al and "broker shows FLAT" in al[0]["detail"]


def test_mismatch_quantity():
    al = oh.position_mismatch([{"symbol": "MESU6", "side": "Buy", "qty": 2}], {"MESU6": 1})
    assert al and "mismatch" in al[0]["detail"]


def test_no_mismatch_when_aligned():
    assert oh.position_mismatch([{"symbol": "MESU6", "side": "Buy", "qty": 1}], {"MESU6": 1}) == []


# ---- Phase 3: execution-safety alarms ----
def test_live_account_detected():
    al = oh.execution_safety_alarms(broker=_broker(account="U1234567"), kill_switch=_ks(),
                                    runtime={}, trades_log=[], events=[], now_iso=NOW)
    assert any(a["title"] == "LIVE_ACCOUNT_DETECTED" and a["level"] == oh.CRITICAL for a in al)


def test_broker_disconnect_alarm():
    al = oh.execution_safety_alarms(broker=_broker(ok=False, error="timeout"), kill_switch=_ks(),
                                    runtime={}, trades_log=[], events=[], now_iso=NOW)
    assert any(a["title"] == "BROKER_DISCONNECTED" for a in al)


def test_kill_switch_active_and_unreadable():
    al = oh.execution_safety_alarms(broker=_broker(), kill_switch=_ks(True, "~/.ict-bot/KILL_SWITCH"),
                                    runtime={}, trades_log=[], events=[], now_iso=NOW)
    assert any(a["title"] == "KILL_SWITCH_ACTIVE" for a in al)
    al2 = oh.execution_safety_alarms(broker=_broker(), kill_switch=_ks(True, "<error>"),
                                     runtime={}, trades_log=[], events=[], now_iso=NOW)
    assert any(a["title"] == "KILL_SWITCH_UNREADABLE" and a["level"] == oh.CRITICAL for a in al2)


def test_unexpected_symbol_and_qty_over_max():
    al = oh.execution_safety_alarms(
        broker=_broker(positions=[{"symbol": "NQZ6", "side": "Buy", "qty": 3}]),
        kill_switch=_ks(), runtime={}, trades_log=[], events=[], now_iso=NOW)
    titles = {a["title"] for a in al}
    assert "UNEXPECTED_SYMBOL" in titles and "QTY_OVER_MAX" in titles


def test_duplicate_order_and_no_stop():
    trades_log = [
        {"order_id": "O1", "outcome": "submitted", "intended_stop": 6794.0},
        {"order_id": "O1", "outcome": "submitted", "intended_stop": 6794.0},  # dup
        {"order_id": "O2", "outcome": "submitted", "intended_stop": None},    # no stop
    ]
    al = oh.execution_safety_alarms(broker=_broker(), kill_switch=_ks(), runtime={},
                                    trades_log=trades_log, events=[], now_iso=NOW)
    titles = {a["title"] for a in al}
    assert "DUPLICATE_ORDER" in titles and "ORDER_WITHOUT_STOP" in titles


def test_auto_paper_safe_disabled_while_executing():
    al = oh.execution_safety_alarms(
        broker=_broker(), kill_switch=_ks(),
        runtime={"auto_execute": True, "mode": "paper"},
        trades_log=[], events=[], now_iso=NOW)
    assert any(a["title"] == "AUTO_PAPER_SAFE_DISABLED" for a in al)


# ---- Phase 4: reconciliation health ----
class _T:
    def __init__(self, status, ids, **kw):
        self.status = status; self.execution_ids = ids
        self.__dict__.update(kw)


def test_recon_health_green_and_duplicates_ignored():
    trades = [_T("CLOSED", ["EX1", "EX2"], symbol="MESU6", side="Buy", entry_qty=1, exit_qty=1)]
    h = oh.reconciliation_health(trades=trades, metrics={}, raw_fill_count=3)  # 3 raw, 2 unique → 1 dup
    assert h["level"] == oh.GREEN and h["closed_trades"] == 1
    assert h["duplicates_ignored"] == 1


def test_recon_health_yellow_when_open():
    trades = [_T("OPEN", ["EX1"], symbol="MESU6", side="Buy", entry_qty=1, exit_qty=0)]
    h = oh.reconciliation_health(trades=trades, metrics={}, raw_fill_count=1)
    assert h["level"] == oh.YELLOW and h["open_trades"] == 1


def test_recon_health_red_on_error():
    h = oh.reconciliation_health(trades=[], metrics={}, raw_fill_count=0, reconcile_error="boom")
    assert h["level"] == oh.RED and h["reconciliation_errors"] == 1


# ---- Phase 5: daily risk ----
def test_daily_risk_loss_limit():
    trades = [
        _T("CLOSED", ["a"], net_pnl=-50.0, realized_R=-1.0, exit_time="2026-06-22T13:00:00Z",
           entry_qty=1, exit_qty=1),
        _T("CLOSED", ["b"], net_pnl=-50.0, realized_R=-1.0, exit_time="2026-06-22T13:30:00Z",
           entry_qty=1, exit_qty=1),
    ]
    dr = oh.daily_risk(trades=trades, now_iso=NOW, max_daily_loss_R=1.0, max_trades_per_day=10)
    assert dr["daily_R"] == -2.0 and dr["trades_today"] == 2
    assert any(a["title"] == "DAILY_LOSS_LIMIT_EXCEEDED" for a in dr["alarms"])


def test_daily_risk_only_counts_today():
    trades = [_T("CLOSED", ["a"], net_pnl=100.0, realized_R=2.0,
                 exit_time="2020-01-01T00:00:00Z", entry_qty=1, exit_qty=1)]
    dr = oh.daily_risk(trades=trades, now_iso=NOW, max_daily_loss_R=1.0)
    assert dr["trades_today"] == 0 and dr["daily_pnl"] == 0


# ---- Phase 6: supervision verdict ----
def test_supervision_safe():
    f = oh.freshness(NOW, broker_ts=NOW, event_ts=NOW, heartbeat_ts=None, monitor_running=False)
    s = oh.supervision(alarms=[], fresh=f, broker=_broker(), kill_switch=_ks())
    assert s["safe"] is True and s["reasons"] == []


def test_supervision_blocks_on_critical_and_lists_reason():
    crit = [{"id": "x", "level": oh.CRITICAL, "title": "LIVE_ACCOUNT_DETECTED", "detail": "U123"}]
    f = oh.freshness(NOW, broker_ts=NOW, event_ts=NOW, heartbeat_ts=None, monitor_running=False)
    s = oh.supervision(alarms=crit, fresh=f, broker=_broker(account="U123"), kill_switch=_ks())
    assert s["safe"] is False
    assert any("LIVE_ACCOUNT_DETECTED" in r for r in s["reasons"])


# ---- latch ----
def test_latch_persists_critical_until_cleared():
    crit = [{"id": "broker_disconnected", "level": oh.CRITICAL, "title": "X", "detail": "d"}]
    latch = oh.merge_latch({}, crit, NOW)
    assert latch["broker_disconnected"]["active"] is True
    # condition resolves — alarm stays latched but inactive
    latch = oh.merge_latch(latch, [], "2026-06-22T14:01:00+00:00")
    assert "broker_disconnected" in latch
    assert latch["broker_disconnected"]["active"] is False


def test_latch_ignores_non_critical():
    warn = [{"id": "w", "level": oh.WARNING, "title": "W", "detail": "d"}]
    assert oh.merge_latch({}, warn, NOW) == {}
