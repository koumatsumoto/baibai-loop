from __future__ import annotations

from baibai_engine.data_collection.provider_contracts import (
    CFTC_SPEC,
    ECB_FX_SPEC,
    JQUANTS_FLOWS_SPEC,
    JQUANTS_INDICES_SPEC,
    JQUANTS_OPTIONS_SPEC,
    MULTPL_SPEC,
    ProviderSpec,
)
from baibai_engine.data_collection.providers.boj.timeseries import BojTimeSeriesSource
from baibai_engine.data_collection.providers.boj.workbooks import BojSource
from baibai_engine.data_collection.providers.estat.api import EStatSource
from baibai_engine.data_collection.providers.estat.dashboard import EStatDashboardSource
from baibai_engine.data_collection.providers.federal_reserve.h15 import FrbH15Source
from baibai_engine.data_collection.providers.fred import FredSource
from baibai_engine.data_collection.providers.matsui.margin import MatsuiMarginSource
from baibai_engine.data_collection.providers.mof.jgb import MofJgbSource
from baibai_engine.data_collection.providers.nikkei.indices import NikkeiIndexesSource
from baibai_engine.data_collection.providers.spglobal.pmi import SpGlobalPmiSource
from baibai_engine.data_collection.providers.tsr.bankruptcies import TsrBankruptciesSource
from baibai_engine.data_collection.providers.university_of_michigan.sentiment import UmichScaSource
from baibai_engine.data_collection.providers.yahoo import YahooChartSource
from baibai_engine.data_collection.transport.context import IndicatorsProviderError


def provider_spec(name: str) -> ProviderSpec:
    for spec in registered_specs():
        if spec.name == name:
            return spec
    raise IndicatorsProviderError(f"unsupported indicator provider: {name}")


def registered_specs() -> tuple[ProviderSpec, ...]:
    return (
        SpGlobalPmiSource.spec,
        TsrBankruptciesSource.spec,
        MatsuiMarginSource.spec,
        EStatDashboardSource.spec,
        EStatSource.spec,
        FrbH15Source.spec,
        FredSource.spec,
        YahooChartSource.spec,
        NikkeiIndexesSource.spec,
        BojSource.spec,
        MofJgbSource.spec,
        BojTimeSeriesSource.spec,
        UmichScaSource.spec,
        JQUANTS_OPTIONS_SPEC,
        CFTC_SPEC,
        MULTPL_SPEC,
        ECB_FX_SPEC,
        JQUANTS_FLOWS_SPEC,
        JQUANTS_INDICES_SPEC,
    )
