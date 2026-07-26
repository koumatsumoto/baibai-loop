"""Point-in-time provider specs shared without importing fetch implementations.

Point-in-time vintage handling is opt-in because most providers timestamp bulk
history with acquisition time rather than publication time. A provider that can
support historical replay declares its ProviderSpec here so read-only consumers
and the fetch implementation use the same capability object.
"""

from __future__ import annotations

from .provider_specs import ProviderSpec

JQUANTS_FLOWS_SPEC = ProviderSpec(
    name="jquants_flows",
    all_history_rolling_years=5,
    range_replacement="through_end_vintage",
    point_in_time_vintage=True,
    required_env=("JQUANTS_API_KEY",),
)

_POINT_IN_TIME_SPECS = (JQUANTS_FLOWS_SPEC,)


def point_in_time_providers() -> frozenset[str]:
    """Provider names whose publication vintages support historical replay."""

    return frozenset(spec.name for spec in _POINT_IN_TIME_SPECS if spec.point_in_time_vintage)


__all__ = [
    "JQUANTS_FLOWS_SPEC",
    "point_in_time_providers",
]
