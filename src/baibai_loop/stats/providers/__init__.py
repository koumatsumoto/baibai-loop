from __future__ import annotations

from datetime import date

import requests

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from .base import (
    FetchContext,
    HttpSession,
    MacroDataProvider,
    StatsProviderError,
)
from .boj import BojProvider, parse_boj_csv
from .ecb_fx import EcbFxProvider, parse_ecb_fx_csv
from .estat import EStatProvider, parse_estat_json
from .frb_h15 import FrbH15Provider, parse_h15_csv
from .fred import FredProvider, parse_fred_csv
from .jquants_flows import JQuantsFlowsProvider, parse_trades_spec
from .manual import ManualProvider, parse_manual_entries

__all__ = [
    "FetchContext",
    "MacroDataProvider",
    "StatsProviderError",
    "fetch_observations",
    "parse_boj_csv",
    "parse_ecb_fx_csv",
    "parse_estat_json",
    "parse_fred_csv",
    "parse_h15_csv",
    "parse_manual_entries",
    "parse_trades_spec",
]

_PROVIDERS: dict[str, MacroDataProvider] = {
    provider.name: provider
    for provider in (
        FredProvider(),
        FrbH15Provider(),
        EcbFxProvider(),
        ManualProvider(),
        BojProvider(),
        EStatProvider(),
        JQuantsFlowsProvider(),
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
        raise StatsProviderError(f"unsupported stats provider: {name}")
    return provider
