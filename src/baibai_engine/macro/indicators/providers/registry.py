from __future__ import annotations

from datetime import date

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from .base import (
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    MacroDataProvider,
    ProviderSpec,
)
from .boj import BojProvider
from .boj_timeseries import BojTimeSeriesProvider
from .cftc import CftcProvider
from .derived import DerivedProvider
from .ecb_fx import EcbFxProvider
from .estat import EStatProvider
from .frb_h15 import FrbH15Provider
from .fred import FredProvider
from .jquants_flows import JQuantsFlowsProvider
from .jquants_indices import JQuantsIndicesProvider
from .mof_jgb import MofJgbProvider
from .multpl import MultplProvider
from .nikkei_indexes import NikkeiIndexesProvider
from .spglobal_pmi import SpGlobalPmiProvider
from .tsr_bankruptcies import TsrBankruptciesProvider
from .yahoo import YahooChartProvider

# The single registration point. Adding a provider means importing its class and
# adding one line here; the service and store read capabilities from each
# provider's ProviderSpec, never from a name branch.
_PROVIDERS: dict[str, MacroDataProvider] = {
    provider.name: provider
    for provider in (
        FredProvider(),
        FrbH15Provider(),
        EcbFxProvider(),
        BojProvider(),
        BojTimeSeriesProvider(),
        CftcProvider(),
        EStatProvider(),
        JQuantsFlowsProvider(),
        JQuantsIndicesProvider(),
        MofJgbProvider(),
        MultplProvider(),
        NikkeiIndexesProvider(),
        SpGlobalPmiProvider(),
        TsrBankruptciesProvider(),
        YahooChartProvider(),
        DerivedProvider(),
    )
}


def fetch_observations(
    series: SeriesDefinition,
    *,
    start: date,
    end: date,
    session: HttpSession | None = None,
    context: FetchContext | None = None,
) -> list[ObservationRecord]:
    provider = resolve_provider(series.provider)
    if context is not None:
        return provider.fetch(
            series, start=start, end=end, session=context.session, context=context
        )
    if session is not None:
        return provider.fetch(series, start=start, end=end, session=session)
    # No caller-supplied session/context: run inside a fresh FetchContext so a
    # browser-backed provider can lazily launch (and always close) its browser.
    with FetchContext() as own_context:
        return provider.fetch(
            series, start=start, end=end, session=own_context.session, context=own_context
        )


def resolve_provider(name: str) -> MacroDataProvider:
    provider = _PROVIDERS.get(name)
    if provider is None:
        raise IndicatorsProviderError(f"unsupported indicator provider: {name}")
    return provider


def provider_spec(name: str) -> ProviderSpec:
    return resolve_provider(name).spec


def registered_specs() -> tuple[ProviderSpec, ...]:
    return tuple(provider.spec for provider in _PROVIDERS.values())


def point_in_time_providers() -> frozenset[str]:
    """Provider names whose reads clamp to observations published on/before the cutoff."""

    return frozenset(spec.name for spec in registered_specs() if spec.point_in_time_vintage)
