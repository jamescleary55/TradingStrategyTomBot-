"""Unified live HTTP server.

Routes
------
``GET  /``               — dashboard HTML (sidebar UI, same look as backtest report)
``GET  /api/health``     — JSON health snapshot
``GET  /api/alerts``     — last N alerts from ``alerts.jsonl``
``GET  /api/stats``      — per-symbol + portfolio performance (resolved trades)
``GET  /api/positions``  — most recent position snapshot from positions.jsonl
``POST /webhook``        — incoming TradingView / NinjaTrader / generic JSON signal

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
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
from live.forward_log import load_signals, load_skipped, load_trades
from live.forward_report import compile_report
from live.tracker import _load_alerts, _resolve_one
from live.webhook import RUNTIME as WEBHOOK_RUNTIME, webhook as webhook_view, health as webhook_health
from utils.alerter import Alerter

log = logging.getLogger("live.server")
STATE_DIR = Path.home() / ".ict-bot"
ALERT_LOG = STATE_DIR / "alerts.jsonl"
POSITIONS_LOG = STATE_DIR / "positions.jsonl"

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


@app.route("/api/stats", methods=["GET"])
def api_stats():
    """Live performance computed by the same engine the CLI report uses."""
    rep = compile_report()
    # Flatten to the shape the dashboard JS already renders
    by_symbol = {}
    for sym, b in rep["by_symbol"].items():
        by_symbol[sym] = {
            "alerts": b["n"],
            "filled": b["n"],
            "wins": b["wins"],
            "losses": b["losses"],
            "voided": 0, "pending": 0, "expired": 0,
            "sum_r": b["total_R"],
            "sum_pnl": 0.0,
            "win_rate_pct": b["win_rate"],
            "avg_R": b["avg_R"],
        }
    p = rep["overall"]
    portfolio = {
        "alerts": rep["totals"]["n_signals_detected"],
        "filled": p["n"],
        "wins": p["wins"],
        "losses": p["losses"],
        "voided": 0,
        "pending": rep["totals"]["n_signals_blocked"],
        "expired": 0,
        "sum_r": p["total_R"],
        "sum_pnl": 0.0,
        "win_rate_pct": p["win_rate"],
        "avg_R": p["avg_R"],
    }
    return jsonify({
        "symbols": by_symbol,
        "portfolio": portfolio,
        "totals": rep["totals"],
        "skip_reasons": rep["skip_reasons"],
        "concerns": rep["concerns"],
        "ready_for_real_money": rep["ready_for_real_money"],
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
      <button data-tab="alerts">Alerts <span class="count" id="count_alerts">—</span></button>
      <button data-tab="performance">Performance</button>
      <button data-tab="positions">Positions <span class="count" id="count_positions">—</span></button>
    </nav>
  </aside>
  <main class="main">
    <header class="topbar">
      <h1 id="page_title">Now</h1>
      <div class="sub" id="page_subtitle">snapshot of the bot's live state</div>
    </header>

    <section class="section active" data-panel="now">
      <div class="grid cols-4" id="now_kpis"></div>
      <div class="grid cols-2" style="margin-top:14px;">
        <div class="card"><h2>Latest alerts</h2><div id="now_recent"></div></div>
        <div class="card"><h2>Open positions</h2><div id="now_positions"></div></div>
      </div>
    </section>

    <section class="section" data-panel="alerts">
      <div class="card"><h2>All alerts</h2><div id="alerts_table"></div></div>
    </section>

    <section class="section" data-panel="performance">
      <div class="card"><h2>Per-symbol performance</h2><div id="perf_table"></div></div>
    </section>

    <section class="section" data-panel="positions">
      <div class="card"><h2>Account snapshot</h2><div id="positions_card"></div></div>
    </section>

    <div class="footer">ict-futures-bot · live server</div>
  </main>
</div>

<script>
const PAGE_TITLES = {
  now: ['Now', 'snapshot of the bot\'s live state'],
  alerts: ['Alerts', 'every signal logged to ~/.ict-bot/alerts.jsonl'],
  performance: ['Performance', 'resolved outcomes per symbol + portfolio'],
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

function perfHtml(stats) {
  const rows = Object.entries(stats.symbols).map(([sym, b]) => {
    const avg = b.avg_R || 0;
    const pnl = b.sum_pnl || 0;
    return `<tr>
      <td>${sym}</td>
      <td class="right">${b.alerts}</td>
      <td class="right">${b.filled}</td>
      <td class="right"><span class="green">${b.wins}</span> / <span class="red">${b.losses}</span></td>
      <td class="right">${b.filled ? fmtPct(b.win_rate_pct) : '—'}</td>
      <td class="right"><span class="${avg >= 0 ? 'green' : 'red'}">${avg >= 0 ? '+' : ''}${avg.toFixed(2)}R</span></td>
      <td class="right"><span class="${pnl >= 0 ? 'green' : 'red'}">${fmtUSD(pnl)}</span></td>
    </tr>`;
  }).join('');
  const p = stats.portfolio;
  const avg = p.avg_R || 0, pnl = p.sum_pnl || 0;
  const totRow = `<tr style="border-top:2px solid var(--border-strong)"><td><strong>Portfolio</strong></td>
    <td class="right"><strong>${p.alerts}</strong></td>
    <td class="right"><strong>${p.filled}</strong></td>
    <td class="right"><strong><span class="green">${p.wins}</span> / <span class="red">${p.losses}</span></strong></td>
    <td class="right"><strong>${p.filled ? fmtPct(p.win_rate_pct) : '—'}</strong></td>
    <td class="right"><strong><span class="${avg >= 0 ? 'green' : 'red'}">${avg >= 0 ? '+' : ''}${avg.toFixed(2)}R</span></strong></td>
    <td class="right"><strong><span class="${pnl >= 0 ? 'green' : 'red'}">${fmtUSD(pnl)}</span></strong></td></tr>`;
  return `<table>
    <thead><tr><th>Symbol</th><th class="right">Alerts</th>
      <th class="right">Filled</th><th class="right">W / L</th>
      <th class="right">Win %</th><th class="right">Avg R</th><th class="right">P&L</th></tr></thead>
    <tbody>${rows}${totRow}</tbody></table>`;
}

function positionsHtml(snapshot) {
  if (!snapshot) return '<div class="dim" style="padding:14px 0">No position snapshots yet. Start <code>python -m live.positions</code>.</div>';
  const meta = `<div style="display:flex;gap:24px;margin-bottom:14px;">
    <div><div class="dim" style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Account</div>
      <div style="font-weight:600;font-size:14px;margin-top:3px;">#${snapshot.account_id}</div></div>
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

async function refresh() {
  try {
    const [alerts, stats, positions] = await Promise.all([
      fetchJSON('/api/alerts?limit=500'),
      fetchJSON('/api/stats'),
      fetchJSON('/api/positions'),
    ]);

    document.getElementById('count_alerts').textContent = alerts.total;
    document.getElementById('count_positions').textContent =
      positions.snapshot ? positions.snapshot.positions.length : 0;
    document.getElementById('last_update').textContent =
      new Date().toLocaleTimeString();

    // Now KPIs
    const p = stats.portfolio;
    const equity = positions.snapshot ? positions.snapshot.equity : null;
    const avgRcls = (p.avg_R || 0) >= 0 ? 'green' : 'red';
    const pnlCls = (p.sum_pnl || 0) >= 0 ? 'green' : 'red';
    const wins = p.wins, losses = p.losses;
    document.getElementById('now_kpis').innerHTML = [
      kpiCard('Total alerts', alerts.total, `${p.filled} filled · ${p.pending} pending`),
      kpiCard('Win rate', p.filled ? fmtPct(p.win_rate_pct) : '—',
              `${wins}W / ${losses}L`, avgRcls),
      kpiCard('Avg R', (p.avg_R >= 0 ? '+' : '') + (p.avg_R || 0).toFixed(2) + 'R',
              `realised ${fmtUSD(p.sum_pnl)}`, avgRcls),
      kpiCard('Account equity', equity != null ? fmtUSD(equity) : '—',
              positions.snapshot ? `cash ${fmtUSD(positions.snapshot.cash)}` : 'broker offline',
              pnlCls),
    ].join('');

    document.getElementById('now_recent').innerHTML = recentAlertsHtml(alerts.alerts);
    document.getElementById('now_positions').innerHTML = positionsHtml(positions.snapshot);
    document.getElementById('alerts_table').innerHTML = allAlertsHtml(alerts.alerts);
    document.getElementById('perf_table').innerHTML = perfHtml(stats);
    document.getElementById('positions_card').innerHTML = positionsHtml(positions.snapshot);
  } catch (e) {
    console.error(e);
    document.getElementById('live_label').textContent = 'offline';
  }
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
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

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
