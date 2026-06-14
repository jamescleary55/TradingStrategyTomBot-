"""Automated go-live gate.

Evaluates 10 hard criteria + the audit's P0/P1 issues against the current
forward / paper data. Refuses to return a PASS unless every gate is met.

Designed to be called from CLI:

    python -m analysis.go_live
    python -m analysis.go_live --backtest-expectancy 0.96

Exit code 0 only when all hard gates pass. Non-zero otherwise. This is
the script that the operator should put in a launchd watchdog so they
get reminded *not* to flip to live until the data agrees.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from analysis.edge_validation import compute_metrics, evaluate_stability
from live.forward_log import load_signals, load_skipped, load_trades
from live.overfitting import evaluate as evaluate_concerns

console = Console()
STATE_DIR = Path.home() / ".ict-bot"


# ---------------------------------------------------------------------------
@dataclass
class Gate:
    code: str
    description: str
    required: str             # human-readable threshold
    actual: str
    passed: bool
    severity: str = "hard"    # hard | soft


@dataclass
class GoLiveReport:
    gates: list[Gate] = field(default_factory=list)
    concerns: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    answers: dict[str, str] = field(default_factory=dict)
    verdict: str = ""

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates if g.severity == "hard") and \
               not any(c["severity"] == "block" for c in self.concerns)


# ---------------------------------------------------------------------------
def evaluate(backtest_expectancy_R: Optional[float] = None) -> GoLiveReport:
    signals = load_signals()
    skipped = load_skipped()
    trades = load_trades()

    closed = [t for t in trades if t.get("outcome") in ("target", "stop")
              and "r_realised" in t]
    metrics = compute_metrics(trades)
    stability = evaluate_stability(trades)
    concerns = [asdict(c) for c in evaluate_concerns(
        signals=signals, trades=trades, skipped=skipped,
        backtest_expectancy_R=backtest_expectancy_R,
    )]

    gates: list[Gate] = []

    # 1. ≥ 100 forward signals
    gates.append(Gate(
        code="signals_100",
        description="At least 100 forward signals logged",
        required="≥ 100",
        actual=str(len(signals)),
        passed=len(signals) >= 100,
    ))

    # 2. ≥ 50 closed paper trades
    gates.append(Gate(
        code="closed_50",
        description="At least 50 closed paper trades (target / stop)",
        required="≥ 50",
        actual=str(metrics.n),
        passed=metrics.n >= 50,
    ))

    # 3. Forward expectancy beats Tier-1 NORMAL threshold
    # Tightened post-Tier-1 MC: NORMAL profile equivalent is +0.25R.
    # Anything thinner is inside the modelled execution noise.
    gates.append(Gate(
        code="positive_expectancy",
        description="Forward expectancy beats NORMAL-profile threshold (Tier-1 MC)",
        required="> +0.25R",
        actual=f"{metrics.expectancy_R:+.2f}R",
        passed=metrics.expectancy_R > 0.25,
    ))

    # 4. Gap to backtest expectancy < 0.3R
    if backtest_expectancy_R is not None and metrics.n > 0:
        gap = abs(backtest_expectancy_R - metrics.expectancy_R)
        gates.append(Gate(
            code="is_oos_gap",
            description="Forward expectancy within 0.3R of backtest",
            required=f"|fwd - bt| < 0.3R   (bt = {backtest_expectancy_R:+.2f}R)",
            actual=f"|{metrics.expectancy_R:+.2f}R - {backtest_expectancy_R:+.2f}R| = {gap:.2f}R",
            passed=gap < 0.3,
        ))
    else:
        gates.append(Gate(
            code="is_oos_gap",
            description="Forward expectancy within 0.3R of backtest",
            required="--backtest-expectancy required",
            actual="not provided",
            passed=False,
        ))

    # 5. Measured slippage on majority of fills
    slips = [t for t in trades if t.get("slippage_pts") is not None
             and float(t["slippage_pts"] or 0) > 0]
    gates.append(Gate(
        code="slippage_measured",
        description="Slippage measured on real fills (instrumentation works)",
        required="≥ 25 rows with non-zero slippage",
        actual=str(len(slips)),
        passed=len(slips) >= 25,
    ))

    # 6. Stable across ≥ 2 symbols
    positive_syms = sum(1 for m in stability.by_symbol.values()
                        if m.n >= 5 and m.expectancy_R > 0)
    gates.append(Gate(
        code="symbol_robust",
        description="Positive expectancy on ≥ 2 symbols (≥ 5 trades each)",
        required="≥ 2 symbols",
        actual=f"{positive_syms} symbol(s) qualify",
        passed=positive_syms >= 2,
    ))

    # 7. Stable across ≥ 2 sessions
    positive_sess = sum(1 for m in stability.by_session.values()
                        if m.n >= 5 and m.expectancy_R > 0)
    gates.append(Gate(
        code="session_robust",
        description="Positive expectancy on ≥ 2 sessions (≥ 5 trades each)",
        required="≥ 2 sessions",
        actual=f"{positive_sess} session(s) qualify",
        passed=positive_sess >= 2,
    ))

    # 8. Max DD acceptable
    gates.append(Gate(
        code="max_dd_R",
        description="Max forward drawdown ≤ 3R",
        required="≥ -3.0R",
        actual=f"{metrics.max_drawdown_R:+.2f}R",
        passed=metrics.max_drawdown_R >= -3.0 if metrics.n > 0 else False,
    ))

    # 9. No P0 audit issues unresolved (manual signal — checks for marker file)
    marker = STATE_DIR / "AUDIT_P0_RESOLVED"
    gates.append(Gate(
        code="audit_p0_resolved",
        description="Operator has resolved every P0 audit finding (manual)",
        required=f"touch {marker} after fixing A1, A6, B1, B2, C1",
        actual="present" if marker.exists() else "missing",
        passed=marker.exists(),
    ))

    # 10. No critical 'block'-severity concerns from overfitting heuristics
    blockers = [c for c in concerns if c["severity"] == "block"]
    gates.append(Gate(
        code="no_block_concerns",
        description="'Do Not Trust Yet' has no block-severity concerns",
        required="zero block concerns",
        actual=f"{len(blockers)} block concern(s): " + ", ".join(c["code"] for c in blockers),
        passed=len(blockers) == 0,
    ))

    # 11. Execution-model calibrated against ≥ 100 resolved trades
    #     (Tier-1 MC posterior is meaningless without empirical fill data)
    try:
        from live.reconcile import load_resolved_trades
        resolved = load_resolved_trades()
        n_resolved = len(resolved) if resolved is not None else 0
    except Exception:
        n_resolved = 0
    gates.append(Gate(
        code="execution_calibrated",
        description="Execution model calibrated against ≥100 resolved live trades",
        required="≥ 100 rows in live_trades_resolved.jsonl",
        actual=str(n_resolved),
        passed=n_resolved >= 100,
    ))

    # 12. No symbol in the live universe that Tier-1 MC marked NOT PROVEN.
    #     Currently: CL is NOT PROVEN (NORMAL 5%ile = -0.33R).
    NOT_PROVEN_SYMBOLS = {"CL", "MCL"}
    try:
        from risk.rules import load as load_personal_rules
        rules = load_personal_rules()
        allowed = set(getattr(rules, "allowed_symbols", []) or [])
    except Exception:
        allowed = set()
    overlap = allowed & NOT_PROVEN_SYMBOLS
    gates.append(Gate(
        code="tier1_not_proven_excluded",
        description="Tier-1 NOT-PROVEN symbols excluded from allowed_symbols",
        required="allowed_symbols ∩ {CL, MCL} = ∅",
        actual=("excluded" if not overlap else f"contains {sorted(overlap)}"),
        passed=not overlap,
    ))

    # --- Soft (advisory) gates ---
    if metrics.n > 0:
        gates.append(Gate(
            code="win_rate_realistic",
            description="Win rate inside [35, 80] %",
            required="35% ≤ win_rate ≤ 80%",
            actual=f"{metrics.win_rate:.1f}%",
            passed=35 <= metrics.win_rate <= 80,
            severity="soft",
        ))
        gates.append(Gate(
            code="profit_factor",
            description="Profit factor ≥ 1.5",
            required="≥ 1.5",
            actual=f"{metrics.profit_factor:.2f}",
            passed=metrics.profit_factor >= 1.5,
            severity="soft",
        ))
        gates.append(Gate(
            code="recovery_factor",
            description="Recovery factor ≥ 3",
            required="≥ 3.0",
            actual=f"{metrics.recovery_factor:.2f}" if metrics.recovery_factor != float("inf") else "∞",
            passed=metrics.recovery_factor >= 3.0,
            severity="soft",
        ))

    rep = GoLiveReport(gates=gates, concerns=concerns,
                       metrics=asdict(metrics), answers=stability.answers)
    rep.verdict = ("PASS — all hard gates met. Soft gates marked. Real money allowed."
                   if rep.passed
                   else "FAIL — at least one hard gate not met. Real money NOT allowed.")
    return rep


# ---------------------------------------------------------------------------
def print_terminal(rep: GoLiveReport) -> None:
    console.rule("[bold]Go-live evaluation[/bold]")

    tbl = Table(header_style="bold", title="Hard gates")
    tbl.add_column("Code")
    tbl.add_column("Required")
    tbl.add_column("Actual")
    tbl.add_column("Pass")
    for g in rep.gates:
        if g.severity != "hard":
            continue
        cell = "[green]✓[/green]" if g.passed else "[red]✗[/red]"
        tbl.add_row(g.code, g.required, g.actual, cell)
    console.print(tbl)

    soft = [g for g in rep.gates if g.severity == "soft"]
    if soft:
        tbl2 = Table(header_style="bold", title="Soft gates (advisory)")
        tbl2.add_column("Code")
        tbl2.add_column("Required")
        tbl2.add_column("Actual")
        tbl2.add_column("Pass")
        for g in soft:
            cell = "[green]✓[/green]" if g.passed else "[yellow]△[/yellow]"
            tbl2.add_row(g.code, g.required, g.actual, cell)
        console.print(tbl2)

    if rep.answers:
        a = Table.grid(padding=(0, 2))
        a.add_column(style="bold")
        a.add_column()
        for k, v in rep.answers.items():
            colour = "green" if v.startswith("YES") else ("red" if v.startswith("NO") else "yellow")
            a.add_row(k, f"[{colour}]{v}[/{colour}]")
        console.print(Panel(a, title="Adversarial questions",
                            border_style="blue", title_align="left"))

    blockers = [c for c in rep.concerns if c["severity"] == "block"]
    if blockers:
        lines = "\n".join(f"  ✗  [{c['code']}] {c['message']}" for c in blockers)
        console.print(Panel(lines, title="Blocking concerns from overfitting analysis",
                            border_style="red", title_align="left"))

    style = "green bold" if rep.passed else "red bold"
    console.print(f"\n[{style}]{rep.verdict}[/{style}]")


def main():
    parser = argparse.ArgumentParser(description="Go-live gate evaluator")
    parser.add_argument("--backtest-expectancy", type=float, default=None)
    parser.add_argument("--json", action="store_true",
                        help="Print machine-readable JSON instead of terminal report")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    rep = evaluate(backtest_expectancy_R=args.backtest_expectancy)
    if args.json:
        print(json.dumps({
            "passed": rep.passed,
            "verdict": rep.verdict,
            "gates": [asdict(g) for g in rep.gates],
            "concerns": rep.concerns,
            "metrics": rep.metrics,
            "answers": rep.answers,
        }, default=str, indent=2))
    else:
        print_terminal(rep)

    sys.exit(0 if rep.passed else 1)


if __name__ == "__main__":
    main()
