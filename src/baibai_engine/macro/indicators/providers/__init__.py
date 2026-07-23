from __future__ import annotations

from datetime import date

import requests

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from .base import (
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    MacroDataProvider,
)
from .boj import BojProvider, parse_boj_xlsx
from .boj_timeseries import BojTimeSeriesProvider, parse_boj_timeseries_json
from .ecb_fx import EcbFxProvider, parse_ecb_fx_csv
from .estat import EStatProvider, parse_estat_json
from .frb_h15 import FrbH15Provider, parse_h15_csv
from .fred import FredProvider, parse_fred_csv
from .jquants_flows import JQuantsFlowsProvider, parse_trades_spec
from .manual import ManualProvider, parse_manual_entries, parse_manual_seed
from .mof_jgb import MofJgbProvider, parse_mof_jgb_csv
from .multpl import MultplProvider, parse_multpl_current, parse_multpl_history
from .tsr_bankruptcies import (
    TsrBankruptciesProvider,
    parse_tsr_bankruptcies_json,
)
from .yahoo import YahooChartProvider, parse_yahoo_chart

__all__ = [
    "FetchContext",
    "IndicatorsProviderError",
    "MacroDataProvider",
    "fetch_observations",
    "parse_boj_timeseries_json",
    "parse_boj_xlsx",
    "parse_ecb_fx_csv",
    "parse_estat_json",
    "parse_fred_csv",
    "parse_h15_csv",
    "parse_manual_entries",
    "parse_manual_seed",
    "parse_mof_jgb_csv",
    "parse_multpl_current",
    "parse_multpl_history",
    "parse_trades_spec",
    "parse_tsr_bankruptcies_json",
    "parse_yahoo_chart",
]

_PROVIDERS: dict[str, MacroDataProvider] = {
    provider.name: provider
    for provider in (
        FredProvider(),
        FrbH15Provider(),
        EcbFxProvider(),
        ManualProvider(),
        BojProvider(),
        BojTimeSeriesProvider(),
        EStatProvider(),
        JQuantsFlowsProvider(),
        MofJgbProvider(),
        MultplProvider(),
        TsrBankruptciesProvider(),
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
    provider = _resolve_provider(series.provider)
    if context is not None:
        return provider.fetch(
            series, start=start, end=end, session=context.session, context=context
        )
    if session is not None:
        return provider.fetch(series, start=start, end=end, session=session)
    with requests.Session() as http:
        return provider.fetch(series, start=start, end=end, session=http)


def _resolve_provider(name: str) -> MacroDataProvider:
    provider = _PROVIDERS.get(name)
    if provider is None:
        raise IndicatorsProviderError(f"unsupported indicator provider: {name}")
    return provider
