"""Offline replay errors preserve missingness without exposing retained payloads."""

import sqlite3

import pytest

from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
    FetchError,
    ProviderHTTPError,
    ProviderPayloadError,
)
from baibai_jobs.observability.tradingview import (
    SourceDataError,
    TimeGuardError,
    failure_category,
    failure_line,
)


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (TimeGuardError("private"), "time_guard"),
        (SourceDataError("private"), "storage"),
        (sqlite3.OperationalError("private"), "storage"),
        (FetchError("private"), "provider_response"),
        (ProviderHTTPError(403), "auth"),
        (ProviderHTTPError(429), "provider_rate_limit"),
        (ProviderHTTPError(503), "provider_response"),
        (RuntimeError("private"), "internal"),
    ],
)
def test_replay_classifies_only_saved_failure_and_never_echoes_payload(error, category):
    assert failure_category(error) == category
    assert f"category={category}" in failure_line(error)
    assert "private" not in failure_line(error)


def test_saved_payload_diagnostic_uses_canonical_field():
    line = failure_line(ProviderPayloadError("invalid_numeric", "quote_close"))
    assert "validation_reason=invalid_numeric" in line
    assert "validation_field=quote_close" in line
