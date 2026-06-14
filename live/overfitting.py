"""'Do Not Trust Yet' — overfitting + readiness heuristics.

Cheap, conservative checks against the forward logs. Each heuristic
returns a :class:`Concern` with a clear, copy-pasteable explanation.
The forward report renders these into a "Do Not Trust Yet" section.

The checks are intentionally pessimistic: bias toward refusing to call
something an edge until the evidence is solid.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


SEVERITY_BLOCK = "block"     # do NOT go live
SEVERITY_WARN = "warn"       # caution, still suspicious
SEVERITY_INFO = "info"       # informational, not a blocker


@dataclass
class Concern:
    severity: str
    code: str
    message: str
    detail: str = ""


# ---------------------------------------------------------------------------
def _signals_per_symbol(signals: list[dict]) -> Counter:
    return Counter(s.get("symbol", "?") for s in signals)


def _closed_trades(trades: list[dict]) -> list[dict]:
    return [t for t in trades if t.get("outcome") in ("target", "stop")
            and "r_realised" in t]


def evaluate(signals: list[dict], trades: list[dict],
             skipped: list[dict],
             backtest_expectancy_R: float | None = None) -> list[Concern]:
    concerns: list[Concern] = []
    n_signals = len(signals)
    closed = _closed_trades(trades)
    n_closed = len(closed)

    # 1. Sample size — forward signals
    if n_signals < 50:
        concerns.append(Concern(
            severity=SEVERITY_BLOCK,
            code="few_signals",
            message=f"Only {n_signals} forward signals logged.",
            detail="Need at least 50 forward signals (target: 100+) before drawing conclusions.",
        ))
    elif n_signals < 100:
        concerns.append(Concern(
            severity=SEVERITY_WARN,
            code="thin_signals",
            message=f"{n_signals} forward signals — thin sample.",
            detail="Treat all stats as tentative until you hit 100.",
        ))

    # 2. Sample size — closed trades
    if n_closed < 20:
        concerns.append(Concern(
            severity=SEVERITY_BLOCK,
            code="few_closed_trades",
            message=f"Only {n_closed} forward trades have closed (target/stop).",
            detail="Statistical significance kicks in around 30 closed trades. Below 20 is anecdote.",
        ))

    # 3. Symbol concentration
    by_sym = _signals_per_symbol(signals)
    if by_sym and n_signals > 0:
        top_sym, top_n = by_sym.most_common(1)[0]
        if top_n / n_signals > 0.7 and len(by_sym) > 1:
            concerns.append(Concern(
                severity=SEVERITY_WARN,
                code="symbol_concentration",
                message=f"{top_n}/{n_signals} signals are {top_sym} ({top_n/n_signals:.0%}).",
                detail="An edge that only exists on one symbol is usually that symbol's regime, not your strategy.",
            ))
    if len(by_sym) <= 1 and n_signals >= 30:
        concerns.append(Concern(
            severity=SEVERITY_BLOCK,
            code="single_symbol_only",
            message="All forward data comes from a single symbol.",
            detail="Add at least two more symbols before drawing any conclusion.",
        ))

    # 4. Win rate sanity (unrealistic)
    if n_closed >= 20:
        wins = sum(1 for t in closed if float(t.get("r_realised") or 0) > 0)
        wr = wins / n_closed
        if wr > 0.85:
            concerns.append(Concern(
                severity=SEVERITY_WARN,
                code="unrealistic_win_rate",
                message=f"Win rate {wr:.0%} is suspiciously high for an RR≥1.5 strategy.",
                detail="Either fills are unrealistic (slippage missing?) or the stop is too far.",
            ))
        elif wr < 0.30:
            concerns.append(Concern(
                severity=SEVERITY_WARN,
                code="low_win_rate",
                message=f"Win rate {wr:.0%}; payoff will need to be very high for positive expectancy.",
            ))

    # 5. IS vs forward expectancy gap
    if backtest_expectancy_R is not None and n_closed >= 20:
        avg_r = sum(float(t.get("r_realised") or 0) for t in closed) / n_closed
        gap = backtest_expectancy_R - avg_r
        if gap > 0.7:
            concerns.append(Concern(
                severity=SEVERITY_BLOCK,
                code="is_oos_gap",
                message=f"Backtest expectancy {backtest_expectancy_R:+.2f}R vs forward {avg_r:+.2f}R.",
                detail=f"Gap {gap:+.2f}R suggests the backtest overstated edge. Look for look-ahead bias and slippage modelling errors.",
            ))
        elif gap > 0.3:
            concerns.append(Concern(
                severity=SEVERITY_WARN,
                code="is_oos_gap_small",
                message=f"Backtest expectancy {backtest_expectancy_R:+.2f}R vs forward {avg_r:+.2f}R (gap {gap:+.2f}R).",
            ))

    # 6. Setup subtype concentration (one bucket carrying the result)
    if n_closed >= 30:
        by_sub = Counter(t.get("setup_subtype", "?") for t in closed)
        for subtype, n in by_sub.most_common():
            if n_closed and n / n_closed > 0.5 and len(by_sub) > 1:
                concerns.append(Concern(
                    severity=SEVERITY_WARN,
                    code="setup_subtype_concentration",
                    message=f"{n}/{n_closed} closed trades are subtype {subtype}.",
                    detail="The strategy may be one specific pattern dressed up as 'ICT'.",
                ))
                break

    # 7. Slippage / unrealistic execution
    fills = [t for t in trades if t.get("fill_price") is not None and t.get("intended_entry")]
    if fills:
        avg_slip = sum(abs(float(t.get("slippage_pts") or 0)) for t in fills) / len(fills)
        if avg_slip == 0 and len(fills) > 5:
            concerns.append(Concern(
                severity=SEVERITY_WARN,
                code="zero_slippage",
                message="All fills logged with 0 slippage — almost certainly an instrumentation bug.",
                detail="Wire L1 quote to spread_estimate when on Tradovate WS.",
            ))

    # 8. Skipped-setup explosion (filters too aggressive?)
    if signals:
        skip_ratio = len(skipped) / max(1, n_signals + len(skipped))
        if skip_ratio > 0.8:
            concerns.append(Concern(
                severity=SEVERITY_INFO,
                code="high_skip_ratio",
                message=f"{skip_ratio:.0%} of detected setups get skipped.",
                detail="Either rules are tuned too tight or the strategy fires too freely. Worth eyeballing skip reasons.",
            ))

    return concerns


def render_lines(concerns: list[Concern]) -> list[str]:
    """Human-readable formatting for terminal output."""
    if not concerns:
        return ["No automatic concerns flagged. (This is not the same as 'safe to trade live'.)"]
    out = []
    for c in concerns:
        prefix = {"block": "✗", "warn": "△", "info": "·"}.get(c.severity, "·")
        line = f"  {prefix}  [{c.severity.upper()}] {c.message}"
        out.append(line)
        if c.detail:
            out.append(f"        → {c.detail}")
    return out
