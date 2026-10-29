"""Safe diagnostics for explicit TradingView receipt replay."""

from __future__ import annotations

import sqlite3

from baibai_engine.machine_analysis.observations.external_assessments.tradingview import (
    observations as observations_owner,
)
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError


class TimeGuardError(observations_owner.FetchError):
    """Original reception is outside its recorded date or observation window."""


class SourceDataError(observations_owner.FetchError):
    """Required saved source context is unavailable or invalid."""


def failure_category(exc: BaseException) -> str:
    if isinstance(exc, TimeGuardError):
        return "time_guard"
    if isinstance(exc, (SourceDataError, SQLiteSchemaError, JQuantsProviderError, sqlite3.Error)):
        return "storage"
    if isinstance(exc, observations_owner.ProviderHTTPError):
        if exc.status_code in (401, 403):
            return "auth"
        return "provider_rate_limit" if exc.status_code == 429 else "provider_response"
    if isinstance(exc, observations_owner.FetchError):
        return "provider_response"
    return "internal"


def failure_line(exc: BaseException) -> str:
    line = f"TradingView replay failed; category={failure_category(exc)}"
    if isinstance(exc, observations_owner.ProviderHTTPError):
        line += f"; provider_http_status={exc.status_code}"
    if isinstance(exc, observations_owner.ProviderPayloadError):
        line += f"; validation_reason={exc.reason}; validation_field={exc.field or '-'}"
    return line
