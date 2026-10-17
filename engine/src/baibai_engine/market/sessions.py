"""Cash-equity session date boundaries, separate from source observation times."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from baibai_engine.foundation.time import JST


def latest_eligible_close_date(at: datetime) -> date:
    """Apply the JST session cutoff without inventing a source observation timestamp."""
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("market instant requires a timezone")
    local = at.astimezone(JST)
    return local.date() - timedelta(days=local.time() < time(15, 30))
