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
FRB_H15_PACKAGE_SERIES = "bf17364827e38702b42a58cf8eaa3f78"

MULTPL_SPEC = ProviderSpec(name="multpl", all_history_start=date(1871, 1, 1))

NATIVE_BULK_PROVIDERS = frozenset(
    {
        "fred_csv",
        "umich_sca",
        "boj_timeseries",
        "cftc",
        "ecb_fx",
        "jquants_flows",
        "jquants_indices",
        "estat_dashboard",
        "estat",
        "frb_h15",
        "multpl",
    }
)
NATIVE_PROVIDERS = NATIVE_BULK_PROVIDERS | {"jquants_options"}


def native_source_key(series: SeriesDefinition) -> str:
    if series.provider == "boj_timeseries":
        return f"boj_timeseries:{series.source_url}:{series.provider_series_id}"
    if series.provider == "umich_sca":
        return f"umich_sca:{series.source_url}"
    if series.provider == "fred_csv":
        return f"fred_csv:{series.source_url}:{series.provider_series_id}"
    if series.provider == "frb_h15":
        return f"frb_h15:{series.source_url}:{FRB_H15_PACKAGE_SERIES}"
    if series.provider == "estat":
        # Configured public endpoint/table/narrowing; runtime appId is not included.
        return f"estat:{series.source_url}:{series.provider_series_id}"
    if series.provider == "estat_dashboard":
        # The URL owns cycle, seasonal adjustment and region as well as indicator.
        return f"estat_dashboard:{series.source_url}"
    if series.provider == "jquants_options":
        return "jquants_options:n225"
    if series.provider == "ecb_fx":
        return "ecb_fx:reference"
    if series.provider == "jquants_flows":
        return "jquants_flows:TSEPrime"
    if series.provider == "jquants_indices":
        return f"jquants_indices:{series.provider_series_id}"
    if series.provider == "cftc":
        return f"cftc:{series.provider_series_id}"
    if series.provider == "multpl":
        return f"multpl:{series.source_url}"
    raise ValueError(f"not a native-record provider: {series.provider}")


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

_POINT_IN_TIME_SPECS = (JQUANTS_FLOWS_SPEC,)


def point_in_time_providers() -> frozenset[str]:
    """Provider names whose source publication dates are filtered by the cutoff."""

    return frozenset(spec.name for spec in _POINT_IN_TIME_SPECS if spec.point_in_time_vintage)


__all__ = [
    "CFTC_SPEC",
    "DERIVED_SPEC",
    "ECB_FX_SPEC",
    "JQUANTS_FLOWS_SPEC",
    "JQUANTS_INDICES_SPEC",
    "JQUANTS_OPTIONS_SPEC",
    "MULTPL_SPEC",
    "NATIVE_BULK_PROVIDERS",
    "NATIVE_PROVIDERS",
    "native_source_key",
    "point_in_time_providers",
]
