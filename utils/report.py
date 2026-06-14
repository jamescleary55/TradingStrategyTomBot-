"""Reporting helpers: rich-formatted terminal panels + self-contained HTML.

Two entry points:

- :func:`print_run` — replaces the verbose ``print`` walls in the runner
  with a tight set of panels: pipeline counts, latest setup, sizing,
  walk-forward stats.
- :func:`write_html_report` — produces a single ``report.html`` file with
  an **interactive TradingView lightweight-charts** widget (candles,
  setup markers, price-line stops/targets, pan/zoom), KPI cards, a
  setup ledger and a trade ledger. Self-contained — the charting JS
  library is inlined, so the file works offline and can be shared.
"""
from __future__ import annotations

import base64
import datetime as dt
import html
import json
from pathlib import Path

from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


# ---------------------------------------------------------------------------
# Terminal output
# ---------------------------------------------------------------------------
def _kv_table(rows: list[tuple[str, str]], title: str | None = None,
              border_style: str = "blue") -> Panel:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold")
    t.add_column(justify="right")
    for k, v in rows:
        t.add_row(k, v)
    return Panel(t, title=title, border_style=border_style, title_align="left")


def _fmt_usd(x: float, signed: bool = False) -> str:
    sign = "+" if signed and x >= 0 else ""
    return f"{sign}${x:,.2f}"


def _fmt_pct(x: float, signed: bool = False) -> str:
    sign = "+" if signed and x >= 0 else ""
    return f"{sign}{x:.2f}%"


def print_run(df, ms, liq, fvg, setups, sim, instrument, equity: float) -> None:
    """Pretty-print the entire run in panels."""
    console.rule(f"[bold]{instrument.symbol}  {len(df)} bars  "
                 f"{df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}[/bold]")

    structure_panel = _kv_table([
        ("Swing H / L", f"{ms['counts']['swing_high']} / {ms['counts']['swing_low']}"),
        ("BOS bull / bear", f"{ms['counts']['bos_bull']} / {ms['counts']['bos_bear']}"),
        ("CHoCH", str(ms['counts']['choch'])),
    ], title="Market structure", border_style="blue")

    liq_panel = _kv_table([
        ("EQH / EQL", f"{liq['counts']['EQH']} / {liq['counts']['EQL']}"),
        ("PDH / PDL", f"{liq['counts']['PDH']} / {liq['counts']['PDL']}"),
        ("PWH / PWL", f"{liq['counts']['PWH']} / {liq['counts']['PWL']}"),
        ("Session levels", str(liq['counts']['session_levels'])),
        ("Sweeps H / L", f"{liq['counts']['sweeps_high']} / {liq['counts']['sweeps_low']}"),
    ], title="Liquidity", border_style="cyan")

    fvg_panel = _kv_table([
        ("Total", str(fvg['counts']['total'])),
        ("Bull / Bear", f"{fvg['counts']['bull']} / {fvg['counts']['bear']}"),
        ("Filled / Open", f"{fvg['counts']['filled']} / {fvg['counts']['open']}"),
    ], title="Fair Value Gaps", border_style="magenta")

    console.print(Columns([structure_panel, liq_panel, fvg_panel], equal=True, expand=True))

    # Setup overview
    setup_panel = _kv_table([
        ("Setups detected", str(setups['counts']['total'])),
        ("Bull / Bear", f"{setups['counts']['bull']} / {setups['counts']['bear']}"),
        ("Aligned with bias", str(setups['counts']['aligned_with_bias'])),
    ], title="ICT setups", border_style="green")

    # Latest setup detail
    if setups['setups']:
        s = setups['setups'][-1]
        rows = [
            ("When", s.timestamp.strftime('%Y-%m-%d %H:%M')),
            ("Direction (bias)", f"{s.direction}  ({s.bias})"),
            ("Entry / Stop / TP", f"{s.entry:.2f} / {s.stop:.2f} / {s.target:.2f}"),
            ("RR", f"{s.rr:.2f}"),
            ("Confluence", " · ".join(s.confluence[:3])),
        ]
        latest_panel = _kv_table(rows, title="Latest setup", border_style="green")
        console.print(Columns([setup_panel, latest_panel], equal=False, expand=True))
    else:
        console.print(setup_panel)

    # Walk-forward
    st = sim.stats
    pnl_color = "green" if st['total_pnl_usd'] >= 0 else "red"
    ret_color = "green" if st['return_pct'] >= 0 else "red"
    rr_color = "green" if st['avg_R'] >= 0 else "red"

    sim_rows = [
        ("Starting equity", _fmt_usd(st['starting_equity'])),
        ("Ending equity", _fmt_usd(st['ending_equity'])),
        ("Total P&L", f"[{pnl_color}]{_fmt_usd(st['total_pnl_usd'], signed=True)}[/{pnl_color}]"),
        ("Return", f"[{ret_color}]{_fmt_pct(st['return_pct'], signed=True)}[/{ret_color}]"),
        ("Max drawdown", _fmt_pct(st['max_drawdown_pct'])),
    ]
    fill_color = "green" if st['fill_rate_pct'] >= 60 else ("yellow" if st['fill_rate_pct'] >= 30 else "red")
    trades_rows = [
        ("Detected / Filled",
         f"{st['n_setups']} / {st['n_filled']}  "
         f"([{fill_color}]{st['fill_rate_pct']:.0f}% fill[/{fill_color}])"),
        ("Wins / Losses", f"[green]{st['n_wins']}[/green] / [red]{st['n_losses']}[/red]"),
        ("Skipped / Voided / Timed-out",
         f"{st['n_skipped']} / {st['n_voided']} / {st['n_timed_out']}"),
        ("Win rate (filled)", _fmt_pct(st['win_rate_pct'])),
        ("Avg R / Expectancy", f"[{rr_color}]{st['avg_R']:+.2f}R / {st['expectancy_R']:+.2f}R[/{rr_color}]"),
    ]
    sim_panel = _kv_table(sim_rows, title="Walk-forward equity", border_style="green" if st['return_pct'] >= 0 else "red")
    trade_panel = _kv_table(trades_rows, title="Walk-forward trades", border_style="yellow")
    console.print(Columns([sim_panel, trade_panel], equal=True, expand=True))

    # Skip-reason breakdown — shows WHY setups didn't fill
    if st.get('skip_breakdown'):
        rows = []
        for reason, n in sorted(st['skip_breakdown'].items(), key=lambda kv: -kv[1]):
            rows.append((reason, f"{n}  ({n / max(st['n_setups'], 1) * 100:.0f}%)"))
        console.print(_kv_table(rows, title="Why setups didn't fill", border_style="red"))


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>ICT backtest — {symbol} {timeframe}</title>
<style>
  :root {{
    --bg: #07100d; --bg-2: #0a1612; --panel: #0f1a16; --panel-2: #14241e;
    --panel-3: #1a2e26; --border: #1f3329; --border-strong: #2a4234;
    --text: #ecf6f0; --text-dim: #b3c5bc; --muted: #6b8a7f;
    --green: #22c55e; --green-soft: rgba(34,197,94,0.12);
    --red: #f87171; --red-soft: rgba(248,113,113,0.12);
    --yellow: #fbbf24; --cyan: #67e8f9; --purple: #c084fc;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0; padding: 0;
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", system-ui, sans-serif;
    font-size: 14px; -webkit-font-smoothing: antialiased;
    letter-spacing: -0.005em; height: 100vh;
  }}

  /* ----- Layout ----- */
  .layout {{ display: flex; min-height: 100vh; }}
  .sidebar {{
    width: 220px; background: var(--bg-2);
    border-right: 1px solid var(--border); padding: 18px 12px;
    display: flex; flex-direction: column; flex-shrink: 0;
    position: sticky; top: 0; height: 100vh; overflow-y: auto;
  }}
  .brand {{
    display: flex; align-items: center; gap: 10px; padding: 4px 12px 18px;
    font-size: 15px; font-weight: 700; letter-spacing: -0.02em;
  }}
  .brand-mark {{
    width: 24px; height: 24px; border-radius: 7px;
    background: linear-gradient(135deg, var(--green), #15803d);
    box-shadow: 0 2px 6px rgba(34,197,94,0.3);
    display: flex; align-items: center; justify-content: center;
    color: #001b0a; font-weight: 800; font-size: 13px;
  }}
  .meta {{
    padding: 0 12px 16px; color: var(--muted); font-size: 11px;
    line-height: 1.5; border-bottom: 1px solid var(--border); margin-bottom: 14px;
  }}
  .meta strong {{ color: var(--text-dim); font-weight: 500; }}
  .nav {{ display: flex; flex-direction: column; gap: 2px; }}
  .nav button {{
    display: flex; align-items: center; gap: 10px;
    background: transparent; border: none; color: var(--text-dim);
    padding: 9px 12px; border-radius: 8px; cursor: pointer;
    font-size: 13.5px; font-weight: 500; text-align: left;
    font-family: inherit; transition: background 0.12s, color 0.12s;
  }}
  .nav button:hover {{ background: var(--panel); color: var(--text); }}
  .nav button.active {{ background: var(--green-soft); color: var(--green); }}
  .nav .count {{
    margin-left: auto; color: var(--muted); font-size: 11px;
    font-variant-numeric: tabular-nums;
  }}
  .nav button.active .count {{ color: var(--green); opacity: 0.8; }}

  .main {{ flex: 1; padding: 24px 28px 60px; overflow-y: auto; }}
  .topbar {{
    display: flex; align-items: baseline; justify-content: space-between;
    margin-bottom: 18px; padding-bottom: 14px; border-bottom: 1px solid var(--border);
  }}
  .topbar h1 {{ font-size: 22px; margin: 0; letter-spacing: -0.02em; font-weight: 700; }}
  .topbar .sub {{ color: var(--muted); font-size: 12px; }}

  /* ----- Sections (one per nav tab) ----- */
  .section {{ display: none; }}
  .section.active {{ display: block; animation: fadeIn 0.15s ease; }}
  @keyframes fadeIn {{
    from {{ opacity: 0; transform: translateY(4px); }}
    to {{ opacity: 1; transform: translateY(0); }}
  }}

  /* ----- Grids ----- */
  .grid {{ display: grid; gap: 14px; }}
  .cols-4 {{ grid-template-columns: repeat(4, 1fr); }}
  .cols-2 {{ grid-template-columns: 1fr 1fr; }}
  @media (max-width: 1100px) {{ .cols-4 {{ grid-template-columns: 1fr 1fr; }} }}
  @media (max-width: 760px) {{ .cols-4, .cols-2 {{ grid-template-columns: 1fr; }} }}

  /* ----- Cards ----- */
  .card {{
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 14px; padding: 18px 20px;
  }}
  .card h2 {{
    margin: 0 0 10px; font-size: 11px; letter-spacing: 0.6px;
    text-transform: uppercase; color: var(--muted); font-weight: 600;
  }}
  .kpi .v {{ font-size: 28px; font-weight: 700; letter-spacing: -0.02em;
            font-variant-numeric: tabular-nums; line-height: 1.1; }}
  .kpi .s {{ color: var(--muted); font-size: 12px; margin-top: 6px; }}

  .green {{ color: var(--green); }} .red {{ color: var(--red); }}
  .yellow {{ color: var(--yellow); }} .cyan {{ color: var(--cyan); }}
  .dim {{ color: var(--muted); }}

  /* ----- Tables ----- */
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{
    text-align: left; color: var(--muted); padding: 9px 10px;
    border-bottom: 1px solid var(--border);
    font-size: 10.5px; letter-spacing: 0.6px; text-transform: uppercase;
    font-weight: 600; user-select: none;
  }}
  th.sortable {{ cursor: pointer; transition: color 0.12s; }}
  th.sortable:hover {{ color: var(--text); }}
  th.sortable::after {{
    content: " ↕"; opacity: 0.4; font-size: 10px; margin-left: 2px;
  }}
  th.sortable.sort-asc::after {{ content: " ↑"; opacity: 1; color: var(--green); }}
  th.sortable.sort-desc::after {{ content: " ↓"; opacity: 1; color: var(--green); }}
  td {{ padding: 9px 10px; border-bottom: 1px solid var(--border);
        font-variant-numeric: tabular-nums; }}
  tr:last-child td {{ border-bottom: none; }}
  td.right, th.right {{ text-align: right; }}
  tbody tr:hover {{ background: var(--panel-2); }}

  /* ----- Chart ----- */
  .chart-card {{ padding: 14px; }}
  .chart-card img {{ width: 100%; display: block; border-radius: 8px; }}
  #tv_chart {{ width: 100%; height: 520px; border-radius: 8px; overflow: hidden; }}
  .chart-legend {{ display: flex; gap: 14px; flex-wrap: wrap; margin-top: 12px;
                    font-size: 11px; color: var(--muted); padding: 0 4px; }}
  .chart-legend .sw {{ display: inline-block; width: 10px; height: 10px;
                       border-radius: 3px; margin-right: 6px; vertical-align: middle; }}

  /* ----- Pills + chips ----- */
  .pill {{
    display: inline-block; padding: 2px 8px; border-radius: 100px;
    font-size: 10px; font-weight: 600; letter-spacing: 0.4px; text-transform: uppercase;
  }}
  .pill.bull, .pill.target {{ background: var(--green-soft); color: var(--green); }}
  .pill.bear, .pill.stop {{ background: var(--red-soft); color: var(--red); }}
  .pill.skipped, .pill.voided_before_entry, .pill.timeout_unfilled
        {{ background: rgba(124,138,135,0.18); color: var(--muted); }}

  .chip-row {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }}
  .chip {{
    display: inline-flex; align-items: center; gap: 5px;
    background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 100px; padding: 5px 12px; font-size: 12px;
    color: var(--text-dim); cursor: pointer; transition: all 0.12s;
    font-family: inherit;
  }}
  .chip:hover {{ border-color: var(--border-strong); color: var(--text); }}
  .chip.active {{ background: var(--green-soft); border-color: var(--green); color: var(--green); }}

  .search-bar {{
    display: flex; align-items: center; gap: 8px;
    background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 10px; padding: 8px 14px; margin-bottom: 12px;
  }}
  .search-bar input {{
    background: transparent; border: none; color: var(--text);
    flex: 1; font-family: inherit; font-size: 14px; outline: none;
  }}

  .footer {{ margin-top: 28px; color: var(--muted); font-size: 11px; text-align: center; padding-bottom: 16px; }}
</style>
</head>
<body>
<div class="layout">
  <aside class="sidebar">
    <div class="brand"><span class="brand-mark">€</span><span>ICT report</span></div>
    <div class="meta">
      <strong>{symbol}</strong> · {timeframe}<br/>
      {period}<br/>
      {bar_count} bars · {now}
    </div>
    <nav class="nav">
      <button data-tab="overview" class="active">Overview</button>
      <button data-tab="chart">Chart</button>
      <button data-tab="pipeline">Pipeline</button>
      <button data-tab="setups">Setups <span class="count">{n_setups}</span></button>
      <button data-tab="trades">Trades <span class="count">{n_trades}</span></button>
    </nav>
  </aside>

  <main class="main">
    <header class="topbar">
      <h1 id="page_title">Overview</h1>
      <div class="sub">risk cap {risk_pct:.2f}%/trade</div>
    </header>

    <!-- OVERVIEW -->
    <section class="section active" data-panel="overview">
      <div class="grid cols-4">{kpi_cards}</div>
      <div class="grid cols-2" style="margin-top:14px;">
        <div class="card"><h2>Pipeline counts</h2><table><tbody>{pipeline_rows}</tbody></table></div>
        <div class="card"><h2>Walk-forward</h2><table><tbody>{walk_rows}</tbody></table></div>
      </div>
    </section>

    <!-- CHART -->
    <section class="section" data-panel="chart">
      <div class="card chart-card">
        <h2>Annotated chart — drag to pan · scroll to zoom</h2>
        {chart_block}
      </div>
    </section>

    <!-- PIPELINE -->
    <section class="section" data-panel="pipeline">
      <div class="grid cols-2">
        <div class="card"><h2>Pipeline counts</h2><table><tbody>{pipeline_rows}</tbody></table></div>
        <div class="card"><h2>Walk-forward</h2><table><tbody>{walk_rows}</tbody></table></div>
      </div>
    </section>

    <!-- SETUPS -->
    <section class="section" data-panel="setups">
      <div class="card">
        <h2>Setup ledger ({n_setups})</h2>
        <div class="search-bar">
          <input id="setup_search" type="text" placeholder="Search description, bias, swept level…" />
        </div>
        <div class="chip-row" id="setup_filters">
          <button class="chip active" data-filter="all">All</button>
          <button class="chip" data-filter="bull">Bull</button>
          <button class="chip" data-filter="bear">Bear</button>
        </div>
        <table class="sortable" id="setup_table">
          <thead><tr>
            <th class="sortable" data-key="0">When</th>
            <th class="sortable" data-key="1">Dir</th>
            <th class="sortable" data-key="2">Bias</th>
            <th class="sortable right" data-key="3">Entry</th>
            <th class="sortable right" data-key="4">Stop</th>
            <th class="sortable right" data-key="5">Target</th>
            <th class="sortable right" data-key="6">RR</th>
            <th class="sortable" data-key="7">Swept</th>
          </tr></thead>
          <tbody>{setup_rows}</tbody>
        </table>
      </div>
    </section>

    <!-- TRADES -->
    <section class="section" data-panel="trades">
      <div class="card">
        <h2>Trade ledger ({n_trades})</h2>
        <div class="chip-row" id="trade_filters">
          <button class="chip active" data-filter="all">All</button>
          <button class="chip" data-filter="bull">Bull</button>
          <button class="chip" data-filter="bear">Bear</button>
          <button class="chip" data-filter="target">Wins</button>
          <button class="chip" data-filter="stop">Losses</button>
          <button class="chip" data-filter="skipped">Skipped</button>
        </div>
        <table class="sortable" id="trade_table">
          <thead><tr>
            <th class="sortable" data-key="0">Setup ts</th>
            <th class="sortable" data-key="1">Dir</th>
            <th class="sortable" data-key="2">Outcome</th>
            <th class="sortable right" data-key="3">Contracts</th>
            <th class="sortable right" data-key="4">Entry</th>
            <th class="sortable right" data-key="5">Exit</th>
            <th class="sortable right" data-key="6">P&amp;L</th>
            <th class="sortable right" data-key="7">R</th>
          </tr></thead>
          <tbody>{trade_rows}</tbody>
        </table>
      </div>
    </section>

    <div class="footer">ict-futures-bot · {symbol} {timeframe}</div>
  </main>
</div>

<script>
(function() {{
  // ----- Tab switching -----
  const navBtns = document.querySelectorAll('.nav button[data-tab]');
  const sections = document.querySelectorAll('section[data-panel]');
  const title = document.getElementById('page_title');
  function setTab(name) {{
    navBtns.forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    sections.forEach(s => s.classList.toggle('active', s.dataset.panel === name));
    const btn = Array.from(navBtns).find(b => b.dataset.tab === name);
    if (btn) {{
      title.textContent = btn.firstChild.textContent.trim();
    }}
    history.replaceState(null, '', '#' + name);
  }}
  navBtns.forEach(b => b.addEventListener('click', () => setTab(b.dataset.tab)));
  if (location.hash) {{
    const t = location.hash.slice(1);
    if (Array.from(navBtns).some(b => b.dataset.tab === t)) setTab(t);
  }}

  // ----- Sortable tables -----
  function parseCellValue(td) {{
    let txt = (td.textContent || '').trim();
    // Strip €, $, commas, %, +/- prefixes for numeric parsing
    const numMatch = txt.match(/^[-+]?\\$?€?[\\d,]+(\\.\\d+)?%?R?$/);
    if (numMatch) {{
      const cleaned = txt.replace(/[$€,%R+]/g, '');
      const v = parseFloat(cleaned);
      if (!isNaN(v)) return v;
    }}
    return txt.toLowerCase();
  }}
  function makeSortable(table) {{
    const tbody = table.tBodies[0];
    if (!tbody) return;
    const ths = table.querySelectorAll('th.sortable');
    ths.forEach((th, colIdx) => {{
      th.addEventListener('click', () => {{
        const asc = th.classList.contains('sort-asc');
        ths.forEach(x => x.classList.remove('sort-asc', 'sort-desc'));
        th.classList.add(asc ? 'sort-desc' : 'sort-asc');
        const rows = Array.from(tbody.rows);
        rows.sort((a, b) => {{
          const av = parseCellValue(a.cells[colIdx]);
          const bv = parseCellValue(b.cells[colIdx]);
          if (av < bv) return asc ? 1 : -1;
          if (av > bv) return asc ? -1 : 1;
          return 0;
        }});
        rows.forEach(r => tbody.appendChild(r));
      }});
    }});
  }}
  document.querySelectorAll('table.sortable').forEach(makeSortable);

  // ----- Filter chips -----
  function applyFilter(tableId, chipsRowId, predicate) {{
    const tbl = document.getElementById(tableId);
    const tbody = tbl.tBodies[0];
    Array.from(tbody.rows).forEach(r => {{
      r.style.display = predicate(r) ? '' : 'none';
    }});
  }}
  function wireFilters(chipsRowId, tableId, fieldFn) {{
    const chips = document.querySelectorAll('#' + chipsRowId + ' .chip');
    let activeFilter = 'all';
    let activeSearch = '';
    function check(row) {{
      const data = (fieldFn(row) || '').toLowerCase();
      const f = activeFilter;
      let matchFilter = true;
      if (f === 'bull') matchFilter = data.includes('bull');
      else if (f === 'bear') matchFilter = data.includes('bear');
      else if (f === 'target') matchFilter = data.includes('target');
      else if (f === 'stop') matchFilter = data.includes('stop');
      else if (f === 'skipped') matchFilter = (data.includes('skipped') || data.includes('voided') || data.includes('timeout'));
      const txt = (row.textContent || '').toLowerCase();
      const matchSearch = !activeSearch || txt.includes(activeSearch);
      return matchFilter && matchSearch;
    }}
    chips.forEach(c => {{
      c.addEventListener('click', () => {{
        chips.forEach(x => x.classList.remove('active'));
        c.classList.add('active');
        activeFilter = c.dataset.filter;
        applyFilter(tableId, chipsRowId, check);
      }});
    }});
    return setSearch => {{
      activeSearch = (setSearch || '').toLowerCase();
      applyFilter(tableId, chipsRowId, check);
    }};
  }}
  const setupSearchUpdate = wireFilters('setup_filters', 'setup_table', r => r.textContent);
  const tradeSearchUpdate = wireFilters('trade_filters', 'trade_table', r => r.textContent);

  const searchEl = document.getElementById('setup_search');
  if (searchEl) {{
    searchEl.addEventListener('input', e => setupSearchUpdate(e.target.value));
  }}
}})();
</script>
</body>
</html>"""


def _kpi(title: str, value: str, sub: str = "", cls: str = "") -> str:
    return (
        f'<div class="card kpi">'
        f'<h2>{html.escape(title)}</h2>'
        f'<div class="v {cls}">{value}</div>'
        f'<div class="s">{html.escape(sub)}</div>'
        f'</div>'
    )


def _pill(text: str, cls: str) -> str:
    return f'<span class="pill {cls}">{html.escape(text)}</span>'


# ---------------------------------------------------------------------------
# Interactive lightweight-charts builder
# ---------------------------------------------------------------------------
_LIGHTWEIGHT_CHARTS_JS_CACHE: str | None = None


def _load_lightweight_charts_js() -> str | None:
    """Read the cached lightweight-charts UMD bundle from utils/assets/."""
    global _LIGHTWEIGHT_CHARTS_JS_CACHE
    if _LIGHTWEIGHT_CHARTS_JS_CACHE is not None:
        return _LIGHTWEIGHT_CHARTS_JS_CACHE
    asset_path = Path(__file__).resolve().parent / "assets" / "lightweight-charts.js"
    if not asset_path.exists():
        return None
    _LIGHTWEIGHT_CHARTS_JS_CACHE = asset_path.read_text(encoding="utf-8")
    return _LIGHTWEIGHT_CHARTS_JS_CACHE


def _candles_payload(df) -> list[dict]:
    """Convert OHLCV DataFrame to lightweight-charts candlestick data."""
    out = []
    for ts, row in df.iterrows():
        # Library expects unix seconds OR business-day strings — use seconds.
        if hasattr(ts, "timestamp"):
            t = int(ts.timestamp())
        else:
            t = int(ts)
        out.append({
            "time": t,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low":  float(row["low"]),
            "close": float(row["close"]),
        })
    return out


def _markers_payload(df, ms: dict, liq: dict, setups: list) -> list[dict]:
    """Build the markers list — setups, CHoCH, sweeps. Each marker shows on
    its bar timestamp with a small label.
    """
    markers: list[dict] = []
    ts_to_unix = {ts: int(ts.timestamp()) for ts in df.index}

    for s in setups:
        if s.timestamp in ts_to_unix:
            markers.append({
                "time": ts_to_unix[s.timestamp],
                "position": "belowBar" if s.direction == "bull" else "aboveBar",
                "color": "#22c55e" if s.direction == "bull" else "#f87171",
                "shape": "arrowUp" if s.direction == "bull" else "arrowDown",
                "text": f"{s.direction.upper()} {s.rr:.1f}R",
            })
    for e in ms.get("choch", []):
        if e.timestamp in ts_to_unix:
            markers.append({
                "time": ts_to_unix[e.timestamp],
                "position": "aboveBar",
                "color": "#c084fc",
                "shape": "circle",
                "text": "CHoCH",
            })
    for sw in liq.get("sweeps", []):
        if sw.timestamp in ts_to_unix:
            markers.append({
                "time": ts_to_unix[sw.timestamp],
                "position": "aboveBar" if sw.side == "high" else "belowBar",
                "color": "#67e8f9",
                "shape": "square",
                "text": f"sweep {sw.level.kind}",
            })
    # lightweight-charts requires markers sorted by time
    markers.sort(key=lambda m: m["time"])
    return markers


def _setup_lines_payload(setups: list) -> list[dict]:
    """Stop & target horizontal price lines for the *latest* few setups
    (older lines would clutter the chart).
    """
    if not setups:
        return []
    recent = setups[-4:]
    lines = []
    for s in recent:
        lines.append({"price": s.stop,   "color": "#f87171",
                      "lineStyle": 2, "lineWidth": 1, "title": f"SL {s.stop:.2f}"})
        lines.append({"price": s.target, "color": "#22c55e",
                      "lineStyle": 2, "lineWidth": 1, "title": f"TP {s.target:.2f}"})
    return lines


def _build_chart_block(df, ms: dict, liq: dict, setups: list,
                       chart_png_path: Path | None) -> str:
    """Return the HTML block for the chart panel.

    Uses lightweight-charts when the JS bundle is available; otherwise
    falls back to embedding the PNG so the report still renders.
    """
    lib = _load_lightweight_charts_js()
    if lib is None:
        if chart_png_path is not None and chart_png_path.exists():
            b64 = base64.b64encode(chart_png_path.read_bytes()).decode()
            return f'<img alt="Chart" src="data:image/png;base64,{b64}" />'
        return '<div class="dim">No chart available</div>'

    candles = _candles_payload(df)
    markers = _markers_payload(df, ms, liq, setups)
    lines = _setup_lines_payload(setups)

    # Bundle JS literal — keep data as JSON to avoid quoting nightmares
    data_json = json.dumps(candles)
    markers_json = json.dumps(markers)
    lines_json = json.dumps(lines)

    return (
        '<div id="tv_chart"></div>'
        '<div class="chart-legend">'
        '<span><span class="sw" style="background:#22c55e"></span>Bull candle / bull setup</span>'
        '<span><span class="sw" style="background:#f87171"></span>Bear candle / bear setup</span>'
        '<span><span class="sw" style="background:#c084fc"></span>CHoCH</span>'
        '<span><span class="sw" style="background:#67e8f9"></span>Sweep</span>'
        '<span class="dim">Stop / target lines = last 4 setups</span>'
        '</div>'
        f'<script>{lib}</script>'
        '<script>'
        '(function() {'
        '  const chart = LightweightCharts.createChart(document.getElementById("tv_chart"), {'
        '    layout: { background: { type: "solid", color: "#0a0f0d" }, textColor: "#ecf6f0" },'
        '    grid: { vertLines: { color: "#1f3329" }, horzLines: { color: "#1f3329" } },'
        '    rightPriceScale: { borderColor: "#1f3329" },'
        '    timeScale: { borderColor: "#1f3329", timeVisible: true, secondsVisible: false },'
        '    crosshair: { mode: 1 }'
        '  });'
        '  const candleSeries = chart.addCandlestickSeries({'
        '    upColor: "#22c55e", downColor: "#f87171",'
        '    borderUpColor: "#22c55e", borderDownColor: "#f87171",'
        '    wickUpColor: "#22c55e", wickDownColor: "#f87171"'
        '  });'
        f'  const data = {data_json};'
        '  candleSeries.setData(data);'
        f'  const markers = {markers_json};'
        '  if (markers.length) candleSeries.setMarkers(markers);'
        f'  const lines = {lines_json};'
        '  lines.forEach(l => candleSeries.createPriceLine(l));'
        '  chart.timeScale().fitContent();'
        '  new ResizeObserver(() => chart.applyOptions({})).observe(document.getElementById("tv_chart"));'
        '})();'
        '</script>'
    )


def write_html_report(
    *,
    df,
    ms: dict,
    liq: dict,
    fvg: dict,
    setups_dict: dict,
    sim,
    chart_png_path: Path,
    instrument,
    timeframe: str,
    risk_pct: float,
    out_path: Path,
) -> Path:
    st = sim.stats
    # Interactive chart block (lightweight-charts) with PNG fallback
    chart_block = _build_chart_block(df, ms, liq, setups_dict["setups"], Path(chart_png_path))

    pnl_cls = "green" if st['total_pnl_usd'] >= 0 else "red"
    ret_cls = "green" if st['return_pct'] >= 0 else "red"
    rr_cls = "green" if st['avg_R'] >= 0 else "red"

    kpi_cards = "".join([
        _kpi("Net worth", _fmt_usd(st['ending_equity']),
             f"start {_fmt_usd(st['starting_equity'])}"),
        _kpi("P&L", _fmt_usd(st['total_pnl_usd'], signed=True),
             _fmt_pct(st['return_pct'], signed=True), cls=pnl_cls),
        _kpi("Win rate", _fmt_pct(st['win_rate_pct']),
             f"{st['n_wins']} wins / {st['n_losses']} losses · {st['n_filled']} filled"),
        _kpi("Avg R", f"{st['avg_R']:+.2f}R",
             f"expectancy {st['expectancy_R']:+.2f}R · max DD {_fmt_pct(abs(st['max_drawdown_pct']))}",
             cls=rr_cls),
    ])

    pipeline_rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td class='right'>{v}</td></tr>"
        for k, v in [
            ("Swing highs / lows",       f"{ms['counts']['swing_high']} / {ms['counts']['swing_low']}"),
            ("BOS bull / bear",          f"{ms['counts']['bos_bull']} / {ms['counts']['bos_bear']}"),
            ("CHoCH",                    str(ms['counts']['choch'])),
            ("EQH / EQL",                f"{liq['counts']['EQH']} / {liq['counts']['EQL']}"),
            ("PDH / PDL",                f"{liq['counts']['PDH']} / {liq['counts']['PDL']}"),
            ("PWH / PWL",                f"{liq['counts']['PWH']} / {liq['counts']['PWL']}"),
            ("Session H/L levels",       str(liq['counts']['session_levels'])),
            ("Sweeps high / low",        f"{liq['counts']['sweeps_high']} / {liq['counts']['sweeps_low']}"),
            ("FVGs total (bull / bear)", f"{fvg['counts']['total']} ({fvg['counts']['bull']} / {fvg['counts']['bear']})"),
            ("FVGs filled / open",       f"{fvg['counts']['filled']} / {fvg['counts']['open']}"),
        ]
    )

    walk_rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td class='right {cls}'>{v}</td></tr>"
        for k, v, cls in [
            ("Setups detected",       str(st['n_setups']), ""),
            ("Filled / Skipped / Voided",
                                       f"{st['n_filled']} / {st['n_skipped']} / {st['n_voided']}", ""),
            ("Wins / Losses",         f"{st['n_wins']} / {st['n_losses']}", ""),
            ("Hit rate",              _fmt_pct(st['hit_rate_pct']), ""),
            ("Win rate",              _fmt_pct(st['win_rate_pct']), ""),
            ("Avg R / Expectancy",    f"{st['avg_R']:+.2f}R / {st['expectancy_R']:+.2f}R", rr_cls),
            ("Total P&L",             _fmt_usd(st['total_pnl_usd'], signed=True), pnl_cls),
            ("Max drawdown",          _fmt_pct(abs(st['max_drawdown_pct'])), ""),
            ("Days traded / halted",  f"{st['loss_tracker']['days_traded']} / {st['loss_tracker']['days_halted']}", ""),
        ]
    )

    setup_rows_html = ""
    for s in setups_dict['setups']:
        dir_pill = _pill(s.direction, "bull" if s.direction == "bull" else "bear")
        setup_rows_html += (
            "<tr>"
            f"<td class='dim'>{s.timestamp.strftime('%Y-%m-%d %H:%M')}</td>"
            f"<td>{dir_pill}</td>"
            f"<td class='dim'>{html.escape(s.bias)}</td>"
            f"<td class='right'>{s.entry:.2f}</td>"
            f"<td class='right'>{s.stop:.2f}</td>"
            f"<td class='right'>{s.target:.2f}</td>"
            f"<td class='right'>{s.rr:.2f}</td>"
            f"<td class='dim'>{html.escape(s.sweep.level.label)}</td>"
            "</tr>"
        )
    if not setup_rows_html:
        setup_rows_html = "<tr><td colspan='8' class='dim' style='text-align:center;padding:20px'>No setups</td></tr>"

    trade_rows_html = ""
    for t in sim.trades:
        dir_pill = _pill(t.setup.direction, "bull" if t.setup.direction == "bull" else "bear")
        outcome_pill = _pill(t.outcome.replace("_", " "), t.outcome)
        pnl_cls_row = "green" if t.pnl_usd > 0 else ("red" if t.pnl_usd < 0 else "dim")
        contracts = t.plan.contracts if t.plan else 0
        entry = f"{t.plan.entry:.2f}" if (t.plan and t.fill_idx is not None) else "—"
        exit_px = f"{t.exit_price:.2f}" if t.exit_price is not None else "—"
        trade_rows_html += (
            "<tr>"
            f"<td class='dim'>{t.setup.timestamp.strftime('%Y-%m-%d %H:%M')}</td>"
            f"<td>{dir_pill}</td>"
            f"<td>{outcome_pill}</td>"
            f"<td class='right'>{contracts}</td>"
            f"<td class='right'>{entry}</td>"
            f"<td class='right'>{exit_px}</td>"
            f"<td class='right {pnl_cls_row}'>{_fmt_usd(t.pnl_usd, signed=True)}</td>"
            f"<td class='right {pnl_cls_row}'>{t.r_multiple:+.2f}</td>"
            "</tr>"
        )
    if not trade_rows_html:
        trade_rows_html = "<tr><td colspan='8' class='dim' style='text-align:center;padding:20px'>No trades</td></tr>"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        HTML_TEMPLATE.format(
            symbol=instrument.symbol,
            timeframe=timeframe,
            period=f"{df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}",
            bar_count=len(df),
            now=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            kpi_cards=kpi_cards,
            chart_block=chart_block,
            pipeline_rows=pipeline_rows,
            walk_rows=walk_rows,
            n_setups=len(setups_dict['setups']),
            n_trades=len(sim.trades),
            setup_rows=setup_rows_html,
            trade_rows=trade_rows_html,
            risk_pct=risk_pct * 100,
        ),
        encoding="utf-8",
    )
    return out_path


def write_trade_csv(sim, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import csv as csv_mod
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        if not sim.trades:
            f.write("no trades\n")
            return out_path
        rows = [t.to_row() for t in sim.trades]
        writer = csv_mod.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return out_path
