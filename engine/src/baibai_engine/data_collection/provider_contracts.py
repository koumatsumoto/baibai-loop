"""Economic source acquisition capabilities and publication-date metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderSpec:
    """A source provider's acquisition range and publication-date capabilities."""

    name: str
    # All-history refresh floor. ``all_history_start`` is the reproducible fixed
    # start a bulk source exposes; ``all_history_rolling_years`` derives the floor
    # from today instead (a licensed rolling window such as J-Quants' 10 years).
    all_history_start: date | None = None
    all_history_rolling_years: int | None = None
    # The source's publication date is used as a vintage cutoff. This does not
    # prove exact historical API availability: J-Quants flows can serve later
    # corrections, and older periods may retain only the latest revision.
    point_in_time_vintage: bool = False
    # Required credential variable names, never their values. The provider reads
    # values from its supplied environment; the registry does not open credentials.
    required_env: tuple[str, ...] = ()


# Publication-vintage selection is shared with the read-only L2 contract. Keep
# this capability pure so a reader does not import the acquisition implementation.
JQUANTS_FLOWS_SPEC = ProviderSpec(
    name="jquants_flows",
    all_history_rolling_years=10,
    point_in_time_vintage=True,
    required_env=("JQUANTS_API_KEY",),
)
