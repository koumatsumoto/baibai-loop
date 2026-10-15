"""Source capabilities and native input identities without fetch implementations.

Vintage filtering is opt-in because most providers timestamp bulk history with
acquisition time rather than publication time. The cutoff uses the source date,
not proof of when each revision became available through the API. Read-only
consumers and fetch implementations share the same capability object.
"""

from __future__ import annotations

from datetime import date

from .definitions import SeriesDefinition
from .provider_specs import ProviderSpec

DERIVED_SPEC = ProviderSpec(name="derived", kind="local", all_history_start=date(1970, 1, 1))

JQUANTS_OPTIONS_SPEC = ProviderSpec(
    name="jquants_options",
    all_history_rolling_years=10,
    required_env=("JQUANTS_API_KEY",),
)

CFTC_SPEC = ProviderSpec(name="cftc", all_history_start=date(2000, 1, 1))
ECB_FX_SPEC = ProviderSpec(name="ecb_fx", all_history_start=date(1999, 1, 1))
NATIVE_BULK_PROVIDERS = frozenset({"cftc", "ecb_fx"})
NATIVE_PROVIDERS = NATIVE_BULK_PROVIDERS | {"jquants_options"}


def native_source_key(series: SeriesDefinition) -> str:
    if series.provider == "jquants_options":
        return "jquants_options:n225"
    if series.provider == "ecb_fx":
        return "ecb_fx:reference"
    if series.provider == "cftc":
        return f"cftc:{series.provider_series_id}"
    raise ValueError(f"not a native-record provider: {series.provider}")


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
    "CFTC_SPEC",
    "ECB_FX_SPEC",
    "NATIVE_BULK_PROVIDERS",
    "NATIVE_PROVIDERS",
    "native_source_key",
    "DERIVED_SPEC",
    "JQUANTS_FLOWS_SPEC",
    "JQUANTS_OPTIONS_SPEC",
    "point_in_time_providers",
]
