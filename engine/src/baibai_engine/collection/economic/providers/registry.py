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
from .mof_jgb import MofJgbSource
from .nikkei_indexes import NikkeiIndexesSource
from .spglobal_pmi import SpGlobalPmiSource
from .tsr_bankruptcies import TsrBankruptciesSource
from .umich_sca import UmichScaSource
from .yahoo import YahooChartSource


def provider_spec(name: str) -> ProviderSpec:
    if name == "nikkei_indexes":
        return NikkeiIndexesSource.spec
    if name == "yahoo":
        return YahooChartSource.spec
    if name == "boj":
        return BojSource.spec
    if name == "mof_jgb":
        return MofJgbSource.spec
    if name == "boj_timeseries":
        return BojTimeSeriesSource.spec
    if name == "umich_sca":
        return UmichScaSource.spec
    if name == "fred_csv":
        return FredSource.spec
    if name == "frb_h15":
        return FrbH15Source.spec
    if name == "estat":
        return EStatSource.spec
    if name == "estat_dashboard":
        return EStatDashboardSource.spec
    if name == "multpl":
        return MULTPL_SPEC
    if name == "jquants_options":
        return JQUANTS_OPTIONS_SPEC
    if name == "jquants_flows":
        return JQUANTS_FLOWS_SPEC
    if name == "jquants_indices":
        return JQUANTS_INDICES_SPEC
    if name == "cftc":
        return CFTC_SPEC
    if name == "ecb_fx":
        return ECB_FX_SPEC
    if name == "spglobal_pmi":
        return SpGlobalPmiSource.spec
    if name == "tsr_bankruptcies":
        return TsrBankruptciesSource.spec
    raise IndicatorsProviderError(f"unsupported indicator provider: {name}")


def registered_specs() -> tuple[ProviderSpec, ...]:
    return (
        SpGlobalPmiSource.spec,
        TsrBankruptciesSource.spec,
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
