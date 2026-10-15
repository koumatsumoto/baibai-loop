"""Publication-date cutoff specs shared without importing fetch implementations.

Vintage filtering is opt-in because most providers timestamp bulk history with
acquisition time rather than publication time. The cutoff uses the source date,
not proof of when each revision became available through the API. Read-only
consumers and fetch implementations share the same capability object.
"""

from __future__ import annotations

from datetime import date

from .provider_specs import ProviderSpec

DERIVED_SPEC = ProviderSpec(name="derived", kind="local", all_history_start=date(1970, 1, 1))

JQUANTS_FLOWS_SPEC = ProviderSpec(
    name="jquants_flows",
    all_history_rolling_years=10,
    range_replacement="through_end_vintage",
    point_in_time_vintage=True,
    required_env=("JQUANTS_API_KEY",),
)

_POINT_IN_TIME_SPECS = (JQUANTS_FLOWS_SPEC,)


def point_in_time_providers() -> frozenset[str]:
    """Provider names whose source publication dates are filtered by the cutoff."""

    return frozenset(spec.name for spec in _POINT_IN_TIME_SPECS if spec.point_in_time_vintage)


__all__ = [
    "DERIVED_SPEC",
    "JQUANTS_FLOWS_SPEC",
    "point_in_time_providers",
]
