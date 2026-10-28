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
    # Credential env var names required to fetch (diagnostics only; values are
    # read from the environment by the provider, never stored in the registry).
    required_env: tuple[str, ...] = ()


JQUANTS_OPTIONS_SPEC = ProviderSpec(
    name="jquants_options",
    all_history_rolling_years=10,
    required_env=("JQUANTS_API_KEY",),
)

CFTC_SPEC = ProviderSpec(name="cftc", all_history_start=date(2000, 1, 1))
ECB_FX_SPEC = ProviderSpec(name="ecb_fx", all_history_start=date(1999, 1, 1))
FRB_H15_PACKAGE_SERIES = "bf17364827e38702b42a58cf8eaa3f78"
MULTPL_SPEC = ProviderSpec(name="multpl", all_history_start=date(1871, 1, 1))

JQUANTS_FLOWS_SPEC = ProviderSpec(
    name="jquants_flows",
    all_history_rolling_years=10,
    point_in_time_vintage=True,
    required_env=("JQUANTS_API_KEY",),
)

JQUANTS_INDICES_SPEC = ProviderSpec(
    name="jquants_indices",
    all_history_rolling_years=10,
    required_env=("JQUANTS_API_KEY",),
)
