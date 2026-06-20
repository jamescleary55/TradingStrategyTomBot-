"""Unified live HTTP server.

Routes
------
``GET  /``                  — operational-validation dashboard (Now/Ops/Trades/Alerts/Positions)
``GET  /api/health``        — JSON health snapshot
``GET  /api/alerts``        — last N signals from live_signals.jsonl
``GET  /api/reconciliation``— reconciled CLOSED trades + metrics (production engine)
``GET  /api/ops``           — live safety/ops state (broker, kill switch, gate blocks)
``GET  /api/positions``     — most recent position snapshot from positions.jsonl
``POST /webhook``           — incoming TradingView / NinjaTrader / generic JSON signal

Performance is computed ONLY from reconciled CLOSED trades (the production
reconciliation engine), never from legacy alert/forward-report stats.

The dashboard polls every 10s and refreshes itself in place — no full
page reload, no separate UI process. Same Flask app handles the webhook
intake so prop-firm + retail platform alerts and the visualisation share
a single PID.

Usage:
    python -m live.server --port 5005
    python -m live.server --secret SHHH --auto-execute --execute-dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
from execution.base import ExecutionEvent, get_adapter
from live.forward_log import load_events, load_signals, load_skipped, load_trades
from live.tracker import _load_alerts, _resolve_one
from live.webhook import RUNTIME as WEBHOOK_RUNTIME, webhook as webhook_view, health as webhook_health
from reconciliation import CLOSED, OPEN, PARTIAL, compute_metrics, reconcile
from risk import kill_switch as ks
from utils.alerter import Alerter

log = logging.getLogger("live.server")
STATE_DIR = Path.home() / ".ict-bot"
ALERT_LOG = STATE_DIR / "alerts.jsonl"
POSITIONS_LOG = STATE_DIR / "positions.jsonl"
EXECS_LOG = STATE_DIR / "live_executions.jsonl"
RUNTIME_FILE = STATE_DIR / "monitor-runtime.json"

_EVENT_FIELDS = set(ExecutionEvent.__dataclass_fields__.keys())
_OPS_CACHE: dict = {"ts": 0.0, "data": None}
OPS_TTL_S = 8.0   # cache the broker read so a 10s poll triggers ≤1 connect

app = Flask("ict-server")


# ---------------------------------------------------------------------------
# Re-export the webhook endpoint as-is on this server too
# ---------------------------------------------------------------------------
app.add_url_rule("/webhook", view_func=webhook_view, methods=["POST"])
app.add_url_rule("/api/health", view_func=webhook_health, methods=["GET"])


# ---------------------------------------------------------------------------
def _read_positions(n: int = 1) -> list[dict]:
    if not POSITIONS_LOG.exists():
        return []
    rows: list[dict] = []
    with open(POSITIONS_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows[-n:]


# ---------------------------------------------------------------------------
# Reconciliation — the production engine over the raw execution log. Pure /
# file-based, so it never touches the broker and always works.
# ---------------------------------------------------------------------------
def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _load_executions() -> list[ExecutionEvent]:
    evs = []
    for r in _load_jsonl(EXECS_LOG):
        kw = {k: v for k, v in r.items() if k in _EVENT_FIELDS}
        kw.pop("raw", None)
        evs.append(ExecutionEvent(**kw))
    return evs


def _load_order_meta() -> dict:
    meta = {}
    for t in load_trades():
        oid = str(t.get("order_id") or "")
        if oid:
            meta[oid] = {k: t.get(k) for k in
                         ("intended_entry", "intended_stop", "intended_target", "planned_R")}
    return meta


def _reconcile_state():
    """Return (executions, trades, metrics) from the production engine."""
    execs = _load_executions()
    trades = reconcile(execs, order_meta=_load_order_meta())
    metrics = compute_metrics(trades)
    return execs, trades, metrics


def _read_runtime() -> dict | None:
    if not RUNTIME_FILE.exists():
        return None
    try:
        return json.loads(RUNTIME_FILE.read_text())
    except Exception:
        return None


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _broker_ops() -> dict:
    """Cached, fail-safe broker read for the ops panel.

    Uses a dedicated client id (set in main) so it never collides with a running
    monitor. On any failure it degrades to the latest positions.jsonl snapshot
    and reports broker offline — never raises.
    """
    now = time.time()
    if _OPS_CACHE["data"] is not None and now - _OPS_CACHE["ts"] < OPS_TTL_S:
        return _OPS_CACHE["data"]
    data = {"ok": False, "account_id": None, "paper": None, "cash": None,
            "equity": None, "currency": None, "positions": [], "open_orders": [],
            "error": None, "source": "broker", "stale_ts": None}
    try:
        a = get_adapter("ibkr")
        snap = a.snapshot()
        data.update(ok=True, account_id=snap.account_id,
                    paper=str(snap.account_id).startswith("DU"),
                    cash=snap.cash, equity=snap.equity, currency=snap.currency,
                    positions=[{"symbol": p.symbol, "side": p.side, "qty": p.qty,
                                "avg_entry": p.avg_entry,
                                "unrealised_pnl": p.unrealised_pnl}
                               for p in snap.positions])
        try:
            data["open_orders"] = a.list_open_orders(account_id=snap.account_id)
        except Exception as e:
            data["open_orders_error"] = f"{type(e).__name__}: {e}"
    except Exception as e:
        data["error"] = f"{type(e).__name__}: {e}"
        latest = _read_positions(1)
        if latest:
            s = latest[-1]
            data.update(source="positions.jsonl", stale_ts=s.get("ts"),
                        account_id=s.get("account_id"),
                        paper=str(s.get("account_id", "")).startswith("DU"),
                        cash=s.get("cash"), equity=s.get("equity"),
                        positions=s.get("positions", []))
    _OPS_CACHE.update(ts=now, data=data)
    return data


# NOTE: live performance now flows through forward_report.compile_report().
# The legacy _compute_stats() (operating on alerts.jsonl) was removed when
# the structured logs (live_signals.jsonl / live_trades.jsonl) became the
# single source of truth.


# ---------------------------------------------------------------------------
@app.route("/api/alerts", methods=["GET"])
def api_alerts():
    """Latest signals from live_signals.jsonl (the structured forward log)."""
    n = int(request.args.get("limit", 200))
    signals = load_signals()
    # Project into a uniform shape the dashboard JS already understands
    out = []
    for s in signals[-n:]:
        out.append({
            "ts_alerted": s.get("ts_logged") or s.get("timestamp"),
            "symbol": s.get("symbol"),
            "direction": s.get("direction"),
            "entry": s.get("entry"),
            "stop": s.get("stop"),
            "target": s.get("target"),
            "rr": s.get("planned_R"),
            "session": s.get("session"),
            "setup_type": s.get("setup_type"),
            "setup_subtype": s.get("setup_subtype"),
            "htf_bias": s.get("htf_bias"),
            "trade_allowed": s.get("trade_allowed"),
            "skip_reason": s.get("skip_reason"),
            "news_blackout": s.get("news_blackout"),
            "status": "blocked" if not s.get("trade_allowed") else "queued",
            "source": s.get("strategy_name"),
        })
    return jsonify({"alerts": out, "total": len(signals)})


@app.route("/api/reconciliation", methods=["GET"])
def api_reconciliation():
    """Closed trades + metrics from the production reconciliation engine.

    Metrics are derived ONLY from reconciled CLOSED trades — never from raw
    orders or legacy alert stats.
    """
    execs, trades, metrics = _reconcile_state()
    counts: dict[str, int] = {}
    for t in trades:
        counts[t.status] = counts.get(t.status, 0) + 1
    closed = [t.to_dict() for t in trades
              if t.status == CLOSED]
    closed.sort(key=lambda t: (t.get("exit_time") or ""))
    return jsonify({
        "metrics": metrics,
        "closed": closed,
        "counts": counts,
        "n_executions": len(execs),
        "note": "Performance metrics are based only on reconciled CLOSED trades.",
    })


@app.route("/api/ops", methods=["GET"])
def api_ops():
    """Operational state for supervised runs (Phase 2)."""
    broker = _broker_ops()
    runtime = _read_runtime()
    ks_path = (runtime or {}).get("kill_switch_path") or "~/.ict-bot/KILL_SWITCH"
    kstate = ks.check(ks_path)

    _execs, trades, metrics = _reconcile_state()
    n_open = sum(1 for t in trades if t.status in (OPEN, PARTIAL))

    events = load_events()
    gate_blocks = [e for e in events if e.get("event") == "gate_block"][-6:]

    monitor_running = _pid_alive((runtime or {}).get("pid"))
    positions = broker.get("positions", [])
    pending = broker.get("open_orders", [])
    flat = bool(broker.get("ok")) and len(positions) == 0

    # The 7 acceptance questions, answered.
    answers = {
        "safe_to_supervise": (not kstate.present) and bool(broker.get("ok"))
                              and bool(broker.get("paper")),
        "paper_only": broker.get("paper"),
        "flat_or_position": "flat" if flat else (f"{len(positions)} position(s)"
                                                 if broker.get("ok") else "unknown"),
        "pending_orders": len(pending),
        "gate_blocked_recently": len(gate_blocks) > 0,
        "closed_trades": metrics["n_closed"],
    }

    return jsonify({
        "broker": broker,
        "kill_switch": {"present": kstate.present, "path": kstate.path},
        "monitor": {"running": monitor_running,
                    "mode": (runtime or {}).get("mode"),
                    "auto_execute": (runtime or {}).get("auto_execute"),
                    "symbols": (runtime or {}).get("symbols"),
                    "data_status": (runtime or {}).get("data_status"),
                    "started_at": (runtime or {}).get("started_at")},
        "reconciliation": {"closed": metrics["n_closed"], "open_or_partial": n_open},
        "pending_orders": pending,
        "positions": positions,
        "flat": flat,
        "gate_blocks": gate_blocks,
        "answers": answers,
        "flatten_reminder": "Halt: touch ~/.ict-bot/KILL_SWITCH  ·  "
                            "Flatten: python scripts/flatten_account.py --execute",
    })


@app.route("/api/positions", methods=["GET"])
def api_positions():
    latest = _read_positions(n=1)
    if not latest:
        return jsonify({"snapshot": None})
    return jsonify({"snapshot": latest[-1]})


# ---------------------------------------------------------------------------
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>ICT live dashboard</title>
<style>
  :root {
    --bg: #07100d; --bg-2: #0a1612; --panel: #0f1a16; --panel-2: #14241e;
    --border: #1f3329; --border-strong: #2a4234;
    --text: #ecf6f0; --text-dim: #b3c5bc; --muted: #6b8a7f;
    --green: #22c55e; --green-soft: rgba(34,197,94,0.12);
    --red: #f87171; --red-soft: rgba(248,113,113,0.12);
    --yellow: #fbbf24; --cyan: #67e8f9; --purple: #c084fc;
  }
  * { box-sizing: border-box; }
  html, body { margin:0; padding:0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", system-ui, sans-serif;
    font-size: 14px; -webkit-font-smoothing: antialiased; letter-spacing: -0.005em; min-height: 100vh; }
  .layout { display: flex; min-height: 100vh; }
  .sidebar { width: 220px; background: var(--bg-2); border-right: 1px solid var(--border);
    padding: 18px 12px; display: flex; flex-direction: column; flex-shrink: 0;
    position: sticky; top: 0; height: 100vh; overflow-y: auto; }
  .brand { display:flex; align-items:center; gap:10px; padding:4px 12px 18px;
    font-size: 15px; font-weight:700; letter-spacing: -0.02em; }
  .brand-mark { width: 24px; height: 24px; border-radius: 7px;
    background: linear-gradient(135deg, var(--green), #15803d);
    box-shadow: 0 2px 6px rgba(34,197,94,0.3);
    display:flex; align-items:center; justify-content:center;
    color:#001b0a; font-weight:800; font-size:13px; }
  .meta { padding: 0 12px 16px; color: var(--muted); font-size: 11px;
    line-height: 1.5; border-bottom: 1px solid var(--border); margin-bottom: 14px; }
  .live-dot { display:inline-block; width:7px; height:7px; border-radius:50%;
    background: var(--green); margin-right:6px;
    box-shadow: 0 0 0 0 rgba(34,197,94,0.7); animation: pulse 2s infinite; }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(34,197,94,0.6); }
    70% { box-shadow: 0 0 0 6px rgba(34,197,94,0); }
    100% { box-shadow: 0 0 0 0 rgba(34,197,94,0); }
  }
  .nav { display:flex; flex-direction:column; gap:2px; }
  .nav button { display:flex; align-items:center; gap:10px;
    background: transparent; border:none; color: var(--text-dim);
    padding: 9px 12px; border-radius: 8px; cursor: pointer;
    font-size: 13.5px; font-weight: 500; text-align: left;
    font-family: inherit; transition: background 0.12s, color 0.12s; }
  .nav button:hover { background: var(--panel); color: var(--text); }
  .nav button.active { background: var(--green-soft); color: var(--green); }
  .nav .count { margin-left:auto; color: var(--muted); font-size: 11px;
    font-variant-numeric: tabular-nums; }
  .nav button.active .count { color: var(--green); opacity: 0.8; }
  .main { flex: 1; padding: 24px 28px 60px; overflow-y: auto; }
  .topbar { display:flex; align-items:baseline; justify-content:space-between;
    margin-bottom: 18px; padding-bottom: 14px; border-bottom: 1px solid var(--border); }
  .topbar h1 { font-size: 22px; margin:0; letter-spacing: -0.02em; font-weight: 700; }
  .topbar .sub { color: var(--muted); font-size: 12px; }
  .section { display: none; }
  .section.active { display: block; animation: fadeIn 0.15s ease; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity:1; transform:translateY(0); } }
  .grid { display: grid; gap: 14px; }
  .cols-4 { grid-template-columns: repeat(4, 1fr); }
  .cols-2 { grid-template-columns: 1fr 1fr; }
  @media (max-width:1100px) { .cols-4 { grid-template-columns: 1fr 1fr; } }
  @media (max-width:760px) { .cols-4, .cols-2 { grid-template-columns: 1fr; } }
  .card { background: var(--panel); border: 1px solid var(--border);
    border-radius: 14px; padding: 18px 20px; }
  .card h2 { margin: 0 0 10px; font-size: 11px; letter-spacing: 0.6px;
    text-transform: uppercase; color: var(--muted); font-weight: 600; }
  .kpi .v { font-size: 28px; font-weight: 700; letter-spacing: -0.02em;
    font-variant-numeric: tabular-nums; line-height: 1.1; }
  .kpi .s { color: var(--muted); font-size: 12px; margin-top: 6px; }
  .green { color: var(--green); } .red { color: var(--red); }
  .yellow { color: var(--yellow); } .cyan { color: var(--cyan); }
  .dim { color: var(--muted); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: var(--muted); padding: 9px 10px;
    border-bottom: 1px solid var(--border); font-size: 10.5px;
    letter-spacing: 0.6px; text-transform: uppercase; font-weight: 600; }
  td { padding: 9px 10px; border-bottom: 1px solid var(--border);
    font-variant-numeric: tabular-nums; }
  tr:last-child td { border-bottom: none; }
  td.right, th.right { text-align: right; }
  tbody tr:hover { background: var(--panel-2); }
  .pill { display:inline-block; padding: 2px 8px; border-radius: 100px;
    font-size: 10px; font-weight: 600; letter-spacing: 0.4px; text-transform: uppercase; }
  .pill.bull, .pill.target { background: var(--green-soft); color: var(--green); }
  .pill.bear, .pill.stop { background: var(--red-soft); color: var(--red); }
  .pill.pending, .pill.expired { background: rgba(124,138,135,0.18); color: var(--muted); }
  .pill.filled { background: rgba(103,232,249,0.15); color: var(--cyan); }
  .pill.voided { background: rgba(251,191,36,0.15); color: var(--yellow); }
  .footer { margin-top: 28px; color: var(--muted); font-size: 11px;
    text-align: center; padding-bottom: 16px; }
  .safety-strip { display:flex; flex-wrap:wrap; gap:10px; }
  .chip { display:flex; align-items:center; gap:7px; padding:9px 14px;
    border-radius: 11px; border:1px solid var(--border); background: var(--panel);
    font-size: 12.5px; font-weight: 600; }
  .chip .lbl { color: var(--muted); font-weight: 500; text-transform: uppercase;
    font-size: 10px; letter-spacing: 0.5px; }
  .chip.ok { border-color: rgba(34,197,94,0.4); background: var(--green-soft); color: var(--green); }
  .chip.bad { border-color: rgba(248,113,113,0.45); background: var(--red-soft); color: var(--red); }
  .chip.warn { border-color: rgba(251,191,36,0.4); background: rgba(251,191,36,0.12); color: var(--yellow); }
  .chip.dim { color: var(--text-dim); }
  .chip .dot { width:8px; height:8px; border-radius:50%; background: currentColor; }
  .metric-note { color: var(--muted); font-size: 11.5px; font-style: italic;
    margin: -2px 0 12px; }
  .kv { display:flex; justify-content:space-between; padding:7px 0;
    border-bottom:1px solid var(--border); font-size:13px; }
  .kv:last-child { border-bottom:none; }
  .kv .k { color: var(--muted); }
  .kv .v { font-variant-numeric: tabular-nums; font-weight:600; }
  code { background: var(--panel-2); padding:1px 6px; border-radius:5px;
    font-size:12px; color: var(--cyan); }
</style>
</head>
<body>
<div class="layout">
  <aside class="sidebar">
    <div class="brand"><span class="brand-mark">€</span><span>ICT live</span></div>
    <div class="meta">
      <span class="live-dot"></span><span id="live_label">live</span><br/>
      Auto-refresh every <strong>10s</strong><br/>
      Last update: <span id="last_update">—</span>
    </div>
    <nav class="nav">
      <button data-tab="now" class="active">Now</button>
      <button data-tab="ops">Ops <span class="count" id="count_ops">—</span></button>
      <button data-tab="trades">Trades <span class="count" id="count_trades">—</span></button>
      <button data-tab="alerts">Alerts <span class="count" id="count_alerts">—</span></button>
      <button data-tab="positions">Positions <span class="count" id="count_positions">—</span></button>
    </nav>
  </aside>
  <main class="main">
    <header class="topbar">
      <h1 id="page_title">Now</h1>
      <div class="sub" id="page_subtitle">snapshot of the bot's live state</div>
    </header>

    <section class="section active" data-panel="now">
      <div id="now_safety" class="safety-strip"></div>
      <div class="grid cols-4" id="now_kpis" style="margin-top:14px;"></div>
      <div class="grid cols-2" style="margin-top:14px;">
        <div class="card"><h2>Reconciled closed trades</h2><div id="now_recent"></div></div>
        <div class="card"><h2>Open positions</h2><div id="now_positions"></div></div>
      </div>
    </section>

    <section class="section" data-panel="ops">
      <div id="ops_answers" class="safety-strip"></div>
      <div class="grid cols-2" style="margin-top:14px;">
        <div class="card"><h2>Connection &amp; account</h2><div id="ops_account"></div></div>
        <div class="card"><h2>Run state</h2><div id="ops_run"></div></div>
      </div>
      <div class="grid cols-2" style="margin-top:14px;">
        <div class="card"><h2>Pending orders</h2><div id="ops_orders"></div></div>
        <div class="card"><h2>Recent gate blocks</h2><div id="ops_gate"></div></div>
      </div>
      <div class="card" style="margin-top:14px;"><h2>Emergency</h2><div id="ops_flatten"></div></div>
    </section>

    <section class="section" data-panel="trades">
      <div class="grid cols-4" id="trades_metrics"></div>
      <div class="card" style="margin-top:14px;"><h2>Equity curve — cumulative net P&amp;L (closed trades)</h2><div id="trades_equity"></div></div>
      <div class="card" style="margin-top:14px;"><h2>Metrics detail</h2><div id="trades_metrics2"></div></div>
      <div class="card" style="margin-top:14px;">
        <h2>Closed trade ledger</h2>
        <div class="metric-note">Performance metrics are based only on reconciled CLOSED trades.</div>
        <div id="trades_ledger"></div>
      </div>
    </section>

    <section class="section" data-panel="alerts">
      <div class="card"><h2>All signals</h2><div id="alerts_table"></div></div>
    </section>

    <section class="section" data-panel="positions">
      <div class="card"><h2>Account snapshot</h2><div id="positions_card"></div></div>
    </section>

    <div class="footer">ict-futures-bot · live server</div>
  </main>
</div>

<script>
const PAGE_TITLES = {
  now: ['Now', "snapshot of the bot's live state"],
  ops: ['Ops', 'live safety & operational state for supervised runs'],
  trades: ['Trades', 'reconciled CLOSED trades + real metrics'],
  alerts: ['Alerts', 'every signal logged to live_signals.jsonl'],
  positions: ['Positions', 'latest broker snapshot'],
};
function setTab(name) {
  document.querySelectorAll('.nav button[data-tab]').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('section[data-panel]').forEach(s =>
    s.classList.toggle('active', s.dataset.panel === name));
  const [t, s] = PAGE_TITLES[name] || ['', ''];
  document.getElementById('page_title').textContent = t;
  document.getElementById('page_subtitle').textContent = s || '';
  history.replaceState(null, '', '#' + name);
}
document.querySelectorAll('.nav button[data-tab]').forEach(b => {
  b.addEventListener('click', () => setTab(b.dataset.tab));
});
if (location.hash) {
  const t = location.hash.slice(1);
  if (PAGE_TITLES[t]) setTab(t);
}

function fmtUSD(n) {
  if (n == null || isNaN(n)) return '—';
  const sign = n < 0 ? '-' : '';
  return `${sign}$${Math.abs(n).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
}
function fmtPct(n) {
  if (n == null || isNaN(n)) return '—';
  return `${n.toFixed(1)}%`;
}
function pill(text, cls) {
  return `<span class="pill ${cls}">${text}</span>`;
}

async function fetchJSON(path) {
  const r = await fetch(path);
  return r.json();
}

function recentAlertsHtml(alerts) {
  if (!alerts.length) return '<div class="dim" style="padding:14px 0">No alerts yet.</div>';
  const rows = alerts.slice(-6).reverse().map(a => {
    const status = a.status || 'pending';
    const rdisp = a.r_multiple != null
      ? `<span class="${a.r_multiple >= 0 ? 'green' : 'red'}">${a.r_multiple >= 0 ? '+' : ''}${a.r_multiple.toFixed(2)}R</span>`
      : '<span class="dim">—</span>';
    return `<tr>
      <td class="dim">${(a.ts_alerted || '').slice(0,16).replace('T',' ')}</td>
      <td>${a.symbol || '?'}</td>
      <td>${pill(a.direction || '', a.direction || '')}</td>
      <td class="right">${(a.entry || 0).toFixed(2)}</td>
      <td>${pill(status, status)}</td>
      <td class="right">${rdisp}</td>
    </tr>`;
  }).join('');
  return `<table>
    <thead><tr><th>When</th><th>Sym</th><th>Dir</th><th class="right">Entry</th><th>Status</th><th class="right">R</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function allAlertsHtml(alerts) {
  if (!alerts.length) return '<div class="dim" style="padding:14px 0">No alerts yet.</div>';
  const rows = alerts.slice().reverse().map(a => {
    const status = a.status || 'pending';
    const rdisp = a.r_multiple != null
      ? `<span class="${a.r_multiple >= 0 ? 'green' : 'red'}">${a.r_multiple >= 0 ? '+' : ''}${a.r_multiple.toFixed(2)}R</span>`
      : '<span class="dim">—</span>';
    return `<tr>
      <td class="dim">${(a.ts_alerted || '').slice(0,16).replace('T',' ')}</td>
      <td>${a.symbol || '?'}</td>
      <td>${pill(a.direction || '', a.direction || '')}</td>
      <td class="right">${(a.entry || 0).toFixed(2)}</td>
      <td class="right">${(a.stop || 0).toFixed(2)}</td>
      <td class="right">${(a.target || 0).toFixed(2)}</td>
      <td class="right">${(a.rr || 0).toFixed(2)}</td>
      <td>${pill(status, status)}</td>
      <td class="right">${rdisp}</td>
      <td class="dim">${(a.source || '').replace('webhook:', '')}</td>
    </tr>`;
  }).join('');
  return `<table>
    <thead><tr><th>When</th><th>Sym</th><th>Dir</th>
      <th class="right">Entry</th><th class="right">Stop</th><th class="right">TP</th>
      <th class="right">RR</th><th>Status</th><th class="right">R</th><th>Source</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

// ---- reconciliation (Trades tab) ----
function num(n, d=2) { return (n == null || isNaN(n)) ? '—' : Number(n).toFixed(d); }
function rcls(n) { return (n || 0) >= 0 ? 'green' : 'red'; }

function tradesMetricCards(m) {
  const pf = m.profit_factor == null ? '—' : num(m.profit_factor, 2);
  const exp = m.expectancy == null ? '—' : fmtUSD(m.expectancy);
  return [
    kpiCard('Closed trades', m.n_closed,
            `${m.n_wins}W / ${m.n_losses}L` + (m.n_scratch ? ` / ${m.n_scratch}=` : '')),
    kpiCard('Win rate', m.win_rate == null ? '—' : fmtPct(m.win_rate * 100),
            `expectancy ${exp}`, rcls(m.expectancy)),
    kpiCard('Profit factor', pf,
            m.avg_R == null ? '' : `avg ${(m.avg_R >= 0 ? '+' : '') + num(m.avg_R)}R`,
            (m.profit_factor || 0) >= 1 ? 'green' : 'red'),
    kpiCard('Max drawdown', fmtUSD(m.max_drawdown),
            m.recovery_factor == null ? '' : `recovery ${num(m.recovery_factor)}`,
            'yellow'),
  ].join('');
}

function tradesMetricDetail(m) {
  const rows = [
    ['Total net P&L', fmtUSD(m.total_net_pnl)],
    ['Gross profit / loss', `${fmtUSD(m.gross_profit)} / ${fmtUSD(m.gross_loss)}`],
    ['Avg winner / loser', `${fmtUSD(m.avg_winner)} / ${fmtUSD(m.avg_loser)}`],
    ['Average R', m.avg_R == null ? '—' : (m.avg_R >= 0 ? '+' : '') + num(m.avg_R) + 'R'],
    ['Average slippage', m.avg_slippage == null ? '—' : num(m.avg_slippage) + ' pts'],
    ['Avg / total commission', `${fmtUSD(m.avg_commission)} / ${fmtUSD(m.total_commission)}`],
    ['Excluded (not closed)', `open ${m.excluded_open} · partial ${m.excluded_partial} · cancelled ${m.cancelled} · rejected ${m.rejected}`],
  ];
  return rows.map(([k, v]) => `<div class="kv"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('');
}

function closedLedgerHtml(closed, limit) {
  if (!closed.length) return '<div class="dim" style="padding:14px 0">No reconciled closed trades yet.</div>';
  const list = limit ? closed.slice(-limit).reverse() : closed.slice().reverse();
  const rows = list.map(t => `<tr>
    <td class="dim">${(t.exit_time || '').slice(0,16).replace('T',' ')}</td>
    <td>${t.symbol || '?'}</td>
    <td>${pill(t.side === 'Buy' ? 'bull' : 'bear', t.side === 'Buy' ? 'bull' : 'bear')}</td>
    <td class="right">${t.entry_qty}</td>
    <td class="right">${num(t.entry_price)}</td>
    <td class="right">${num(t.exit_price)}</td>
    <td class="right ${rcls(t.net_pnl)}">${fmtUSD(t.net_pnl)}</td>
    <td class="right ${rcls(t.realized_R)}">${t.realized_R == null ? '—' : (t.realized_R >= 0 ? '+' : '') + num(t.realized_R) + 'R'}</td>
    <td>${t.exit_reason ? pill(t.exit_reason, t.exit_reason === 'target' ? 'target' : 'stop') : '—'}</td>
  </tr>`).join('');
  return `<table>
    <thead><tr><th>Exit</th><th>Sym</th><th>Dir</th><th class="right">Qty</th>
      <th class="right">Entry</th><th class="right">Exit</th><th class="right">Net</th>
      <th class="right">R</th><th>Reason</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

// ---- equity curve (inline SVG; no external lib) ----
function equityCurveSvg(closed) {
  if (!closed || closed.length < 1)
    return '<div class="dim" style="padding:14px 0">No closed trades yet — the curve appears after the first reconciled trade.</div>';
  const ordered = closed.slice().sort((a,b) => (a.exit_time||'').localeCompare(b.exit_time||''));
  let cum = 0; const pts = [{x:0, y:0}];
  ordered.forEach((t,i) => { cum += (t.net_pnl || 0); pts.push({x:i+1, y:cum}); });
  const W = 720, H = 180, pad = 28;
  const xs = pts.map(p=>p.x), ys = pts.map(p=>p.y);
  const minY = Math.min(0, ...ys), maxY = Math.max(0, ...ys);
  const spanY = (maxY - minY) || 1, spanX = (Math.max(...xs)) || 1;
  const px = x => pad + (x/spanX) * (W - 2*pad);
  const py = y => H - pad - ((y - minY)/spanY) * (H - 2*pad);
  const line = pts.map((p,i)=>`${i?'L':'M'}${px(p.x).toFixed(1)},${py(p.y).toFixed(1)}`).join(' ');
  const zeroY = py(0).toFixed(1);
  const last = ys[ys.length-1];
  const col = last >= 0 ? 'var(--green)' : 'var(--red)';
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="none" style="max-height:200px">
    <line x1="${pad}" y1="${zeroY}" x2="${W-pad}" y2="${zeroY}" stroke="var(--border-strong)" stroke-dasharray="3,3"/>
    <path d="${line}" fill="none" stroke="${col}" stroke-width="2"/>
    <text x="${W-pad}" y="14" text-anchor="end" fill="${col}" font-size="12" font-weight="700">${fmtUSD(last)}</text>
    <text x="${pad}" y="${H-6}" fill="var(--muted)" font-size="10">${ordered.length} closed trade(s)</text>
  </svg>`;
}

// ---- ops (safety panel) ----
function chip(label, value, cls) {
  return `<div class="chip ${cls}"><span class="dot"></span>` +
    `<span><span class="lbl">${label}</span><br>${value}</span></div>`;
}
function opsAnswersHtml(o) {
  const a = o.answers, ks = o.kill_switch, b = o.broker;
  const chips = [];
  chips.push(chip('Safe to supervise', a.safe_to_supervise ? 'YES' : 'CHECK',
                  a.safe_to_supervise ? 'ok' : 'bad'));
  chips.push(chip('Kill switch', ks.present ? 'PRESENT' : 'absent', ks.present ? 'bad' : 'ok'));
  chips.push(chip('Account', a.paper_only === true ? 'PAPER' : (a.paper_only === false ? 'NON-PAPER' : 'unknown'),
                  a.paper_only ? 'ok' : 'bad'));
  chips.push(chip('Position', a.flat_or_position, a.flat_or_position === 'flat' ? 'ok' : 'warn'));
  chips.push(chip('Pending orders', a.pending_orders, a.pending_orders ? 'warn' : 'ok'));
  chips.push(chip('Gate blocks', a.gate_blocked_recently ? `${o.gate_blocks.length} recent` : 'none',
                  a.gate_blocked_recently ? 'warn' : 'dim'));
  chips.push(chip('Closed trades', a.closed_trades, a.closed_trades > 0 ? 'ok' : 'dim'));
  chips.push(chip('IBKR', b.ok ? 'connected' : 'offline', b.ok ? 'ok' : 'bad'));
  return chips.join('');
}
function opsAccountHtml(o) {
  const b = o.broker;
  const kv = [
    ['IBKR connection', b.ok ? '<span class="green">connected</span>' :
        `<span class="red">offline</span>${b.error ? ' <span class="dim">(' + b.error + ')</span>' : ''}`],
    ['Account id', b.account_id ? `<code>${b.account_id}</code>` : '—'],
    ['Account type', b.paper === true ? '<span class="green">paper (DU)</span>' :
        (b.paper === false ? '<span class="red">NON-PAPER</span>' : '—')],
    ['Cash / equity', `${fmtUSD(b.cash)} / ${fmtUSD(b.equity)} ${b.currency || ''}`],
    ['Open positions', (b.positions || []).length],
    ['Data source', b.source === 'broker' ? 'live broker' : `positions.jsonl (${(b.stale_ts||'').slice(0,19).replace('T',' ')})`],
  ];
  return kv.map(([k, v]) => `<div class="kv"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('');
}
function opsRunHtml(o) {
  const m = o.monitor || {};
  const ds = m.data_status ? Object.entries(m.data_status).map(([s,v]) => `${s}:${v}`).join(', ') : '—';
  const kv = [
    ['Monitor process', m.running ? '<span class="green">running</span>' : '<span class="dim">not running</span>'],
    ['Mode', m.mode ? `<code>${m.mode}</code>` : '—'],
    ['AUTO_PAPER_SAFE', m.mode === 'auto_paper_safe' ? '<span class="green">active</span>' : '<span class="dim">off</span>'],
    ['Auto-execute', m.auto_execute ? 'on' : 'off'],
    ['Symbols', (m.symbols || []).join(', ') || '—'],
    ['Market data', ds],
    ['Started', (m.started_at || '').slice(0,19).replace('T',' ') || '—'],
  ];
  return kv.map(([k, v]) => `<div class="kv"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('');
}
function opsOrdersHtml(o) {
  const p = o.pending_orders || [];
  if (!p.length) return '<div class="dim" style="padding:10px 0">No pending orders.</div>';
  const rows = p.map(x => `<tr><td>${x.orderId ?? '—'}</td><td>${x.symbol || '—'}</td>
    <td>${x.status || '—'}</td><td class="dim">${x.account || ''}</td></tr>`).join('');
  return `<table><thead><tr><th>Order</th><th>Sym</th><th>Status</th><th>Acct</th></tr></thead><tbody>${rows}</tbody></table>`;
}
function opsGateHtml(o) {
  const g = (o.gate_blocks || []).slice().reverse();
  if (!g.length) return '<div class="dim" style="padding:10px 0">No gate blocks logged.</div>';
  return g.map(e => `<div class="kv"><span class="k">${(e.ts_logged||'').slice(0,19).replace('T',' ')}</span>` +
    `<span class="v" style="color:var(--yellow);font-weight:500;text-align:right;max-width:70%">${e.detail || e.event}</span></div>`).join('');
}

function positionsHtml(snapshot) {
  if (!snapshot) return '<div class="dim" style="padding:14px 0">No position snapshots yet. Start <code>python -m live.positions</code>.</div>';
  const meta = `<div style="display:flex;gap:24px;margin-bottom:14px;">
    <div><div class="dim" style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Account</div>
      <div style="font-weight:600;font-size:14px;margin-top:3px;">${snapshot.account_id ?? '—'}</div></div>
    <div><div class="dim" style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Cash</div>
      <div style="font-weight:600;font-size:14px;margin-top:3px;">${fmtUSD(snapshot.cash)}</div></div>
    <div><div class="dim" style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Equity</div>
      <div style="font-weight:600;font-size:14px;margin-top:3px;">${fmtUSD(snapshot.equity)}</div></div>
    <div><div class="dim" style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Snapshot</div>
      <div style="font-weight:600;font-size:13px;margin-top:3px;" class="dim">${(snapshot.ts || '').slice(0,19).replace('T',' ')}</div></div>
  </div>`;
  if (!snapshot.positions.length) {
    return meta + '<div class="dim">No open positions.</div>';
  }
  const rows = snapshot.positions.map(p => `<tr>
    <td>${p.symbol}</td>
    <td>${pill(p.side === 'Buy' ? 'bull' : 'bear', p.side === 'Buy' ? 'bull' : 'bear')}</td>
    <td class="right">${p.qty}</td>
    <td class="right">${p.avg_entry.toFixed(2)}</td>
    <td class="right ${(p.unrealised_pnl || 0) >= 0 ? 'green' : 'red'}">${fmtUSD(p.unrealised_pnl)}</td>
  </tr>`).join('');
  return meta + `<table>
    <thead><tr><th>Symbol</th><th>Side</th><th class="right">Qty</th>
      <th class="right">Avg Entry</th><th class="right">Unrealised</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function kpiCard(title, value, sub, cls) {
  return `<div class="card kpi"><h2>${title}</h2>
    <div class="v ${cls || ''}">${value}</div>
    <div class="s">${sub || ''}</div></div>`;
}

function setHTML(id, html) { const el = document.getElementById(id); if (el) el.innerHTML = html; }

async function refresh() {
  try {
    const [alerts, recon, ops, positions] = await Promise.all([
      fetchJSON('/api/alerts?limit=500'),
      fetchJSON('/api/reconciliation'),
      fetchJSON('/api/ops'),
      fetchJSON('/api/positions'),
    ]);
    document.getElementById('live_label').textContent = 'live';

    const m = recon.metrics;
    document.getElementById('count_alerts').textContent = alerts.total;
    document.getElementById('count_trades').textContent = m.n_closed;
    document.getElementById('count_positions').textContent =
      positions.snapshot ? positions.snapshot.positions.length : (ops.positions || []).length;
    document.getElementById('count_ops').textContent = ops.kill_switch.present ? '⛔' : '✓';
    document.getElementById('last_update').textContent = new Date().toLocaleTimeString();

    // ---- Now: safety strip + reconciliation KPIs ----
    setHTML('now_safety', opsAnswersHtml(ops));
    document.getElementById('now_kpis').innerHTML = tradesMetricCards(m);
    setHTML('now_recent', closedLedgerHtml(recon.closed, 6));
    setHTML('now_positions', positionsHtml(positions.snapshot || opsSnapshotShim(ops)));

    // ---- Ops ----
    setHTML('ops_answers', opsAnswersHtml(ops));
    setHTML('ops_account', opsAccountHtml(ops));
    setHTML('ops_run', opsRunHtml(ops));
    setHTML('ops_orders', opsOrdersHtml(ops));
    setHTML('ops_gate', opsGateHtml(ops));
    setHTML('ops_flatten', `<div class="metric-note" style="font-style:normal">${ops.flatten_reminder}</div>`);

    // ---- Trades ----
    setHTML('trades_metrics', tradesMetricCards(m));
    setHTML('trades_equity', equityCurveSvg(recon.closed));
    setHTML('trades_metrics2', tradesMetricDetail(m));
    setHTML('trades_ledger', closedLedgerHtml(recon.closed));

    // ---- Alerts / Positions ----
    setHTML('alerts_table', allAlertsHtml(alerts.alerts));
    setHTML('positions_card', positionsHtml(positions.snapshot || opsSnapshotShim(ops)));
  } catch (e) {
    console.error(e);
    document.getElementById('live_label').textContent = 'offline';
  }
}
// Build a positions-card snapshot from the ops broker read when no
// positions.jsonl snapshot exists (so Positions works without the poller).
function opsSnapshotShim(ops) {
  const b = ops.broker;
  if (!b || (!b.ok && !b.account_id)) return null;
  return { account_id: b.account_id, cash: b.cash, equity: b.equity,
           ts: b.stale_ts || new Date().toISOString(), positions: b.positions || [] };
}
refresh();
setInterval(refresh, 10_000);
</script>
</body>
</html>"""


@app.route("/", methods=["GET"])
def dashboard():
    return render_template_string(DASHBOARD_HTML)


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ICT live server (dashboard + webhook)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int,
                        default=int(os.getenv("WEBHOOK_PORT", "5005")))
    parser.add_argument("--secret", default=os.getenv("WEBHOOK_SECRET", ""))
    parser.add_argument("--auto-execute", action="store_true")
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument("--risk-pct", type=float, default=cfg.RISK.max_risk_per_trade_pct)
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--execute-dry-run", action="store_true")
    parser.add_argument("--ib-client-id", type=int, default=91,
                        help="Dedicated IBKR API client id for the dashboard's "
                             "read-only ops snapshot (distinct from the monitor's, "
                             "so they never collide).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Use a distinct client id for the dashboard's broker reads so it can run
    # alongside the monitor without an IBKR client-id collision.
    try:
        import execution.ibkr_orders as _io
        _io.IB_CLIENT_ID = args.ib_client_id
        log.info("Dashboard IBKR client id = %d", args.ib_client_id)
    except Exception:
        pass

    from risk.controls import RiskGate
    from risk.rules import load as load_rules
    WEBHOOK_RUNTIME.alerter = Alerter()
    WEBHOOK_RUNTIME.secret = args.secret
    WEBHOOK_RUNTIME.auto_execute = args.auto_execute
    WEBHOOK_RUNTIME.equity = args.equity
    WEBHOOK_RUNTIME.risk_pct = args.risk_pct
    WEBHOOK_RUNTIME.allow_live = args.allow_live
    WEBHOOK_RUNTIME.execute_dry_run = args.execute_dry_run
    WEBHOOK_RUNTIME.risk_gate = RiskGate(load_rules())
    log.info("Personal rules loaded from %s · mode=%s · auto_execute=%s",
             WEBHOOK_RUNTIME.risk_gate.rules.source,
             WEBHOOK_RUNTIME.risk_gate.rules.mode,
             WEBHOOK_RUNTIME.risk_gate.rules.enable_auto_execute)

    log.info("Dashboard: http://%s:%d/", args.host, args.port)
    log.info("Webhook:   http://%s:%d/webhook", args.host, args.port)
    log.info("Auth required: %s  ·  Auto-execute: %s  ·  Dry-run: %s",
             "yes" if WEBHOOK_RUNTIME.secret else "no",
             "yes" if WEBHOOK_RUNTIME.auto_execute else "no",
             "yes" if WEBHOOK_RUNTIME.execute_dry_run else "no")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
