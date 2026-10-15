from __future__ import annotations

from datetime import date

from ..definitions import SeriesDefinition
from ..observations import ObservationRecord
from ..read_contracts import DERIVED_SPEC, JQUANTS_OPTIONS_SPEC
from .base import (
    FetchContext,
    HttpSession,
    IndicatorDataProvider,
    IndicatorsProviderError,
    ProviderSpec,
)
from .boj import BojProvider
from .boj_timeseries import BojTimeSeriesProvider
from .cftc import CftcProvider
from .ecb_fx import EcbFxProvider
from .estat import EStatProvider
from .estat_dashboard import EStatDashboardProvider
from .frb_h15 import FrbH15Provider
from .fred import FredProvider
from .jquants_flows import JQuantsFlowsProvider
from .jquants_indices import JQuantsIndicesProvider
from .mof_jgb import MofJgbProvider
from .multpl import MultplProvider
from .nikkei_indexes import NikkeiIndexesProvider
from .spglobal_pmi import SpGlobalPmiProvider
from .tsr_bankruptcies import TsrBankruptciesProvider
from .umich_sca import UmichScaProvider
from .yahoo import YahooChartProvider

# Scalar external acquisitions. Native option chains have their own source boundary;
# application orchestration persists them before L2 computes the registered readings.
_PROVIDERS: dict[str, IndicatorDataProvider] = {
    provider.name: provider
    for provider in (
        FredProvider(),
        FrbH15Provider(),
        EcbFxProvider(),
        BojProvider(),
        BojTimeSeriesProvider(),
        CftcProvider(),
        EStatProvider(),
        EStatDashboardProvider(),
        JQuantsFlowsProvider(),
        JQuantsIndicesProvider(),
        MofJgbProvider(),
        MultplProvider(),
        NikkeiIndexesProvider(),
        SpGlobalPmiProvider(),
        TsrBankruptciesProvider(),
        UmichScaProvider(),
        YahooChartProvider(),
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


def resolve_provider(name: str) -> IndicatorDataProvider:
    provider = _PROVIDERS.get(name)
    if provider is None:
        raise IndicatorsProviderError(f"unsupported indicator provider: {name}")
    return provider


def provider_spec(name: str) -> ProviderSpec:
    if name == "derived":
        return DERIVED_SPEC
    if name == "jquants_options":
        return JQUANTS_OPTIONS_SPEC
    return resolve_provider(name).spec


def registered_specs() -> tuple[ProviderSpec, ...]:
    return (
        *tuple(provider.spec for provider in _PROVIDERS.values()),
        DERIVED_SPEC,
        JQUANTS_OPTIONS_SPEC,
    )
