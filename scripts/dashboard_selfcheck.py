"""Dashboard readiness self-check — run before the attended paper run.

A one-command health gate for the supervision dashboard. It does NOT need a
running server or the network: it drives the Flask app in-process via the test
client, asserts every endpoint returns 200 with the keys the UI relies on, and
(if macOS JavaScriptCore `jsc` is available) executes the real `refresh()` flow
with mocked fetch to prove the front-end renders without runtime errors.

    python scripts/dashboard_selfcheck.py

Exit 0 = READY, non-zero = NOT READY (with the failing check named).
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"

REQUIRED_KEYS = {
    "/api/ops": ["supervision", "freshness", "alarms", "latched_critical",
                 "reconciliation_health", "daily_risk", "broker", "kill_switch",
                 "position_mismatch", "answers"],
    "/api/reconciliation": ["metrics", "closed", "counts", "note"],
    "/api/positions": ["snapshot"],
    "/api/alerts": ["alerts", "total"],
}


def _check_endpoints(client) -> list[str]:
    fails = []
    r = client.get("/")
    if r.status_code != 200 or b"supervision_banner" not in r.data:
        fails.append("GET / did not return the dashboard HTML")
    for path, keys in REQUIRED_KEYS.items():
        rr = client.get(path)
        if rr.status_code != 200:
            fails.append(f"{path} -> HTTP {rr.status_code}")
            continue
        body = rr.get_json()
        for k in keys:
            if k not in body:
                fails.append(f"{path} missing key '{k}'")
    # supervision must expose a boolean verdict + reasons list
    ops = client.get("/api/ops").get_json()
    if not isinstance(ops.get("supervision", {}).get("safe"), bool):
        fails.append("/api/ops supervision.safe is not boolean")
    if not isinstance(ops.get("supervision", {}).get("reasons"), list):
        fails.append("/api/ops supervision.reasons is not a list")
    return fails


def _check_render(dashboard_html: str) -> tuple[bool, str]:
    """Execute the real refresh() flow with mocked fetch via jsc. Returns (ok, note)."""
    if not Path(JSC).exists():
        return True, "jsc unavailable — front-end render check skipped (run in a browser)"
    js = re.search(r"<script>(.*)</script>", dashboard_html, re.S).group(1)
    js = js.replace("refresh();\nsetInterval(refresh, 10_000);", "")
    harness = _HARNESS_HEAD + js + _HARNESS_TAIL
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(harness)
        path = f.name
    out = subprocess.run([JSC, path], capture_output=True, text=True)
    text = (out.stdout + out.stderr).strip()
    ok = "RENDER_OK" in text and "REFRESH THREW" not in text
    return ok, text.splitlines()[-1] if text else "no output"


_HARNESS_HEAD = r"""
var __h={}; function el(id){return {set innerHTML(v){__h[id]=v;},get innerHTML(){return __h[id]||'';},
 set textContent(v){},classList:{toggle:function(){},add:function(){}}};}
var document={getElementById:function(id){return el(id);},querySelectorAll:function(){return [];}};
var history={replaceState:function(){}};var location={hash:''};function setInterval(){}
var OK_OPS={now:"t",supervision:{safe:true,reasons:[],n_critical:0},
 freshness:{broker:{level:"GREEN",age_s:1},heartbeat:{na:true,level:null},event:{level:"GREEN",age_s:1},dashboard:{level:"GREEN",age_s:0},worst_level:"GREEN",degraded:false},
 alarms:[],latched_critical:[],position_mismatch:[],
 reconciliation_health:{level:"GREEN",closed_trades:0,open_trades:0,partial_pending:0,unmatched_executions:0,duplicates_ignored:0,reconciliation_errors:0,reconcile_error:null},
 daily_risk:{daily_pnl:0,daily_R:0,daily_drawdown:0,open_risk_R:0,trades_today:0,alarms:[]},
 broker:{ok:true,account_id:"DUQ834606",paper:true,cash:1e6,equity:1e6,currency:"EUR",positions:[],open_orders:[],source:"broker",read_ts:"t"},
 kill_switch:{present:false,path:null},monitor:{running:false,mode:null,auto_execute:null,symbols:null,data_status:null,started_at:null},
 reconciliation:{closed:0,open_or_partial:0},pending_orders:[],positions:[],flat:true,gate_blocks:[],
 answers:{safe_to_supervise:true,paper_only:true,flat_or_position:"flat",pending_orders:0,gate_blocked_recently:false,closed_trades:0},flatten_reminder:"halt"};
var OK_RECON={metrics:{n_closed:0,n_wins:0,n_losses:0,n_scratch:0,win_rate:null,expectancy:null,expectancy_R:null,profit_factor:null,avg_R:null,avg_winner:null,avg_loser:null,max_drawdown:0,recovery_factor:null,avg_slippage:null,avg_commission:null,total_commission:0,gross_profit:0,gross_loss:0,total_net_pnl:0,excluded_open:0,excluded_partial:0,cancelled:0,rejected:0},closed:[],counts:{},n_executions:0};
function fetch(p){var d;if(p.indexOf('/api/ops')===0)d=OK_OPS;else if(p.indexOf('/api/reconciliation')===0)d=OK_RECON;else if(p.indexOf('/api/alerts')===0)d={alerts:[],total:0};else d={snapshot:null};
 return Promise.resolve({json:function(){return Promise.resolve(d);}});}
"""

_HARNESS_TAIL = r"""
refresh().then(function(){
  var need=['supervision_banner','now_kpis','ops_freshness','ops_recon','ops_risk','trades_metrics','trades_ledger','positions_card','alerts_table'];
  var miss=need.filter(function(id){return !__h[id]||__h[id].length<2;});
  print(miss.length? ('RENDER_MISSING:'+miss):'RENDER_OK');
}).catch(function(e){print('REFRESH THREW: '+e);});
"""


def main() -> int:
    from live.server import app, DASHBOARD_HTML
    client = app.test_client()

    print("Dashboard self-check")
    print("-" * 48)
    ep_fails = _check_endpoints(client)
    print(f"[endpoints] {'OK ✓' if not ep_fails else 'FAIL'}")
    for f in ep_fails:
        print(f"   - {f}")

    render_ok, note = _check_render(DASHBOARD_HTML)
    print(f"[front-end] {'OK ✓' if render_ok else 'FAIL'} — {note}")

    ready = not ep_fails and render_ok
    print("-" * 48)
    print("VERDICT:", "READY ✓" if ready else "NOT READY ✗")
    print("Launch: python -m live.server --port 5005   →  http://127.0.0.1:5005/")
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
