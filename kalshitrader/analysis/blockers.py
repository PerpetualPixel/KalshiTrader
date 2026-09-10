"""Group the reasons the strategy passed, so "nothing traded" is an answer, not a shrug.

Every pass carries a rationale written for a human - "dip 3c < 6c (high 48c, ask 45c)".
Thousands of those are unreadable, but they collapse to a handful of *kinds* once the
numbers are blanked out, and the counts say which gate is actually doing the blocking.

The live loop shows this on the dashboard. The backtester needs exactly the same thing,
because a replay that returns no trades is otherwise indistinguishable from a replay
that is broken.
"""
from __future__ import annotations

import re

_NUMBERS = re.compile(r"[-+]?\d+(?:\.\d+)?")


def tally(blockers: dict[str, dict], reason: str, ticker: str = "") -> None:
    """Fold one pass rationale into the running tally, keyed by its shape."""
    reason = (reason or "").strip()
    if not reason:
        return
    key = _NUMBERS.sub("#", reason)
    row = blockers.get(key)
    if row is None:
        blockers[key] = {"reason": key, "count": 1, "example": reason[:120], "ticker": ticker}
    else:
        row["count"] += 1


def ranked(blockers: dict[str, dict], limit: int = 12) -> list[dict]:
    """The tally, commonest first."""
    return sorted(blockers.values(), key=lambda r: -r["count"])[:limit]
