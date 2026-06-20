"""Operator kill switch — an instant, file-based halt.

The operator can stop all new orders and signal execution *immediately* by
creating a sentinel file. No process restart, no config edit, no code: just
``touch`` a file and the next tick refuses to act.

Two ways to trip it:

1. The path configured in ``personal_rules.yaml`` (``kill_switch_path``,
   default ``~/.ict-bot/KILL_SWITCH``).
2. Any of a handful of conventional flag filenames dropped in the state dir
   (``~/.ict-bot``): ``KILL_SWITCH``, ``kill_switch.txt``, ``halt.flag``,
   ``kill.json``, ``STOP``. These exist so an operator under pressure can use
   whatever name they remember.

Detection is read-only and never raises — a kill switch must NEVER fail open.
If anything goes wrong while checking, we treat it as PRESENT (fail safe).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("risk.kill_switch")

STATE_DIR = Path.home() / ".ict-bot"

# Conventional flag filenames recognised in the state dir, in addition to the
# operator's configured kill_switch_path.
COMMON_FLAG_NAMES = (
    "KILL_SWITCH",
    "kill_switch.txt",
    "halt.flag",
    "kill.json",
    "STOP",
)


@dataclass(frozen=True)
class KillSwitchState:
    present: bool
    path: Optional[str] = None   # the file that tripped it (for logging)

    def __bool__(self) -> bool:
        return self.present


def _candidate_paths(configured_path: Optional[str]) -> list[Path]:
    paths: list[Path] = []
    if configured_path:
        paths.append(Path(os.path.expanduser(configured_path)))
    for name in COMMON_FLAG_NAMES:
        paths.append(STATE_DIR / name)
    # de-dup while preserving order
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def check(configured_path: Optional[str] = None) -> KillSwitchState:
    """Return KillSwitchState. PRESENT if any sentinel file exists.

    Fail-safe: any unexpected error is treated as PRESENT, never absent.
    """
    try:
        for p in _candidate_paths(configured_path):
            try:
                if p.exists():
                    return KillSwitchState(present=True, path=str(p))
            except OSError:
                # Can't stat the path — refuse to run rather than guess.
                return KillSwitchState(present=True, path=str(p))
        return KillSwitchState(present=False, path=None)
    except Exception:  # pragma: no cover - defensive, must never fail open
        log.exception("kill-switch check errored — treating as PRESENT (fail safe)")
        return KillSwitchState(present=True, path="<error>")


def is_present(configured_path: Optional[str] = None) -> bool:
    return check(configured_path).present
