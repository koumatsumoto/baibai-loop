"""List acquisition capabilities from their owners, without constructing clients.

The catalog selects active source requests. A registered adapter alone does not
activate a source or give it permission to fetch.
"""

from __future__ import annotations

from baibai_engine.data_collection.provider_contracts import JQUANTS_FLOWS_SPEC, ProviderSpec
from baibai_engine.data_collection.providers.boj.timeseries import BojTimeSeriesSource
from baibai_engine.data_collection.providers.boj.workbooks import BojSource
from baibai_engine.data_collection.providers.cftc import CftcSource
from baibai_engine.data_collection.providers.ecb.fx import EcbFxSource
from baibai_engine.data_collection.providers.estat.api import EStatSource
from baibai_engine.data_collection.providers.estat.dashboard import EStatDashboardSource
from baibai_engine.data_collection.providers.federal_reserve.h15 import FrbH15Source
from baibai_engine.data_collection.providers.fred import FredSource
from baibai_engine.data_collection.providers.jquants.indices import JQuantsIndicesSource
from baibai_engine.data_collection.providers.jquants.options import OptionChainSource
from baibai_engine.data_collection.providers.matsui.margin import MatsuiMarginSource
from baibai_engine.data_collection.providers.mof.jgb import MofJgbSource
from baibai_engine.data_collection.providers.multpl import MultplSource
from baibai_engine.data_collection.providers.nikkei.indices import NikkeiIndexesSource
from baibai_engine.data_collection.providers.spglobal.pmi import SpGlobalPmiSource
from baibai_engine.data_collection.providers.tradingeconomics.pmi import TradingEconomicsPmiSource
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
        TradingEconomicsPmiSource.spec,
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
        OptionChainSource.spec,
        CftcSource.spec,
        MultplSource.spec,
        EcbFxSource.spec,
        JQUANTS_FLOWS_SPEC,
        JQuantsIndicesSource.spec,
    )
