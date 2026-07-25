"""L2 macro reading: machine-computed descriptive statistics over the L1 store.

Every registered series gets the same treatment every business day — level,
direction, position in its own history, threshold notes and how old the reading is
— so an L3 analysis starts from one measured frame instead of re-deciding what
"high" means each time. Descriptive only: no classification, no composite score,
no signal.
"""

from __future__ import annotations

from .compute import compute_reading
from .models import ReadingSnapshot, SeriesReading, SeriesTrend
from .rules import DEFAULT_RULES_PATH, ReadingRules, ReadingRulesError, load_reading_rules

__all__ = [
    "DEFAULT_RULES_PATH",
    "ReadingRules",
    "ReadingRulesError",
    "ReadingSnapshot",
    "SeriesReading",
    "SeriesTrend",
    "compute_reading",
    "load_reading_rules",
]
