from __future__ import annotations

from baibai_engine.collection.economic.provider_specs import (
    CFTC_SPEC,
    ECB_FX_SPEC,
    JQUANTS_FLOWS_SPEC,
    JQUANTS_INDICES_SPEC,
    JQUANTS_OPTIONS_SPEC,
    MULTPL_SPEC,
    ProviderSpec,
)

from .base import IndicatorsProviderError
from .boj import BojSource
from .boj_timeseries import BojTimeSeriesSource
from .estat import EStatSource
from .estat_dashboard import EStatDashboardSource
from .frb_h15 import FrbH15Source
from .fred import FredSource
from .matsui_margin import MatsuiMarginSource
from .mof_jgb import MofJgbSource
from .nikkei_indexes import NikkeiIndexesSource
from .spglobal_pmi import SpGlobalPmiSource
from .tsr_bankruptcies import TsrBankruptciesSource
from .umich_sca import UmichScaSource
from .yahoo import YahooChartSource


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
