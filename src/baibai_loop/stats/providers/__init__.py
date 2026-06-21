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
from .ecb_fx import EcbFxProvider, parse_ecb_fx_csv
from .frb_h15 import FrbH15Provider, parse_h15_csv
from .fred import FredProvider, parse_fred_csv

__all__ = [
    "FetchContext",
    "MacroDataProvider",
    "StatsProviderError",
    "fetch_observations",
    "parse_ecb_fx_csv",
    "parse_fred_csv",
    "parse_h15_csv",
]

_PROVIDERS: dict[str, MacroDataProvider] = {
    provider.name: provider for provider in (FredProvider(), FrbH15Provider(), EcbFxProvider())
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
