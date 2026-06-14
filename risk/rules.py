"""Loader for ``personal_rules.yaml``.

Resolution order:

1. ``~/.ict-bot/personal_rules.yaml``  (the user's edited copy)
2. ``personal_rules.example.yaml`` in the repo root (template)

Everything is opinionated toward safety: defaults err on the side of
fewer trades, smaller risk, and ``mode: review`` (no auto-execute).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


_DEFAULTS = {
    "allowed_symbols": ["MNQ", "MES", "MCL"],
    "allowed_sessions": ["LONDON", "NY_AM"],
    "risk_per_trade_R": 0.25,
    "min_expected_R": 1.5,
    "min_setup_score": 0.55,
    "max_daily_loss_R": 1.0,
    "max_weekly_loss_R": 3.0,
    "max_trades_per_day": 3,
    "max_trades_per_symbol_per_day": 2,
    "max_open_positions": 1,
    "max_consecutive_losses": 2,
    "news_blackout_minutes": 30,
    "news_filter_enabled": True,
    "enable_auto_execute": False,
    "broker": "tradovate",
    "mode": "review",
    "kill_switch_path": "~/.ict-bot/KILL_SWITCH",
}


@dataclass
class PersonalRules:
    allowed_symbols: list[str]
    allowed_sessions: list[str]
    risk_per_trade_R: float
    min_expected_R: float
    min_setup_score: float
    max_daily_loss_R: float
    max_weekly_loss_R: float
    max_trades_per_day: int
    max_trades_per_symbol_per_day: int
    max_open_positions: int
    max_consecutive_losses: int
    news_blackout_minutes: int
    news_filter_enabled: bool
    enable_auto_execute: bool
    broker: str
    mode: str
    kill_switch_path: str
    source: str = field(default="defaults")    # path the rules were loaded from

    @property
    def kill_switch(self) -> Path:
        return Path(os.path.expanduser(self.kill_switch_path))


def _user_path() -> Path:
    return Path.home() / ".ict-bot" / "personal_rules.yaml"


def _repo_example() -> Path:
    return Path(__file__).resolve().parent.parent / "personal_rules.example.yaml"


def load(path: Optional[Path] = None) -> PersonalRules:
    """Load rules from ``path`` if given, else user file, else example, else defaults."""
    src = "defaults"
    raw: dict = {}
    if path is not None:
        if not Path(path).exists():
            raise FileNotFoundError(f"Personal rules not found at {path}")
        raw = yaml.safe_load(Path(path).read_text()) or {}
        src = str(path)
    else:
        for candidate in (_user_path(), _repo_example()):
            if candidate.exists():
                raw = yaml.safe_load(candidate.read_text()) or {}
                src = str(candidate)
                break
    merged = {**_DEFAULTS, **raw}
    return PersonalRules(
        allowed_symbols=list(merged["allowed_symbols"]),
        allowed_sessions=list(merged["allowed_sessions"]),
        risk_per_trade_R=float(merged["risk_per_trade_R"]),
        min_expected_R=float(merged["min_expected_R"]),
        min_setup_score=float(merged["min_setup_score"]),
        max_daily_loss_R=float(merged["max_daily_loss_R"]),
        max_weekly_loss_R=float(merged["max_weekly_loss_R"]),
        max_trades_per_day=int(merged["max_trades_per_day"]),
        max_trades_per_symbol_per_day=int(merged["max_trades_per_symbol_per_day"]),
        max_open_positions=int(merged["max_open_positions"]),
        max_consecutive_losses=int(merged["max_consecutive_losses"]),
        news_blackout_minutes=int(merged["news_blackout_minutes"]),
        news_filter_enabled=bool(merged["news_filter_enabled"]),
        enable_auto_execute=bool(merged["enable_auto_execute"]),
        broker=str(merged["broker"]).lower(),
        mode=str(merged["mode"]).lower(),
        kill_switch_path=str(merged["kill_switch_path"]),
        source=src,
    )
