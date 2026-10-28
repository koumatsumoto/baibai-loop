"""Publication-vintage read policy for retained native inputs.

Vintage filtering is opt-in because most providers timestamp bulk history with
acquisition time rather than publication time. The cutoff uses the source date,
not proof of when each revision became available through the API. Read-only
consumers and fetch implementations share the same capability object.
"""

from __future__ import annotations

from baibai_engine.data_collection.provider_contracts import JQUANTS_FLOWS_SPEC

NATIVE_BULK_PROVIDERS = frozenset(
    {
        "fred_csv",
        "umich_sca",
        "boj_timeseries",
        "mof_jgb",
        "boj",
        "yahoo",
        "nikkei_indexes",
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


_POINT_IN_TIME_SPECS = (JQUANTS_FLOWS_SPEC,)


def point_in_time_providers() -> frozenset[str]:
    """Provider names whose source publication dates are filtered by the cutoff."""

    return frozenset(spec.name for spec in _POINT_IN_TIME_SPECS if spec.point_in_time_vintage)


__all__ = [
    "NATIVE_BULK_PROVIDERS",
    "NATIVE_PROVIDERS",
    "point_in_time_providers",
]
