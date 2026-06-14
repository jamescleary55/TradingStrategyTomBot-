"""Time zone + session helpers."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pandas as pd
import pytz

from config import SESSION_TZ, SESSIONS, Session

ET = pytz.timezone(SESSION_TZ)


# ---------------------------------------------------------------------------
_TIMEFRAME_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


def trim_incomplete_bar(df: pd.DataFrame, timeframe: str,
                         now: datetime | None = None) -> pd.DataFrame:
    """Drop the trailing bar when its window hasn't fully closed.

    Audit finding **A6** — the live monitor previously consumed the
    still-forming current bar, which can register a "complete" FVG that
    vanishes once the bar actually closes (phantom signal).

    Rule: a bar with ``timestamp`` ``T`` represents the window
    ``[T, T + Δ)``. We keep it only when ``now >= T + Δ``.

    Safe to call on any DataFrame; idempotent — calling twice removes
    at most one bar total.
    """
    if df is None or df.empty or timeframe not in _TIMEFRAME_SECONDS:
        return df
    delta = timedelta(seconds=_TIMEFRAME_SECONDS[timeframe])
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    last_ts = df.index[-1]
    if hasattr(last_ts, "to_pydatetime"):
        last_ts = last_ts.to_pydatetime()
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    bar_close = last_ts + delta
    if now < bar_close:
        return df.iloc[:-1]
    return df


def to_et(ts) -> datetime:
    """Convert a datetime / Timestamp to US Eastern Time (DST-aware)."""
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(ET)


def in_session(ts, session: Session) -> bool:
    """True if ts (any tz) falls inside the given session, ET-aware."""
    t = to_et(ts).time()
    if session.start <= session.end:
        return session.start <= t < session.end
    # wraps midnight (Asia 20:00 → 24:00 isn't wrap, but handle generally)
    return t >= session.start or t < session.end


def current_session(ts) -> str | None:
    for key, s in SESSIONS.items():
        if in_session(ts, s):
            return key
    return None
