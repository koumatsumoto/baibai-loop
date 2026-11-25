import sqlite3

import httpx2
import pytest
from mcp.client.auth.exceptions import OAuthTokenError
from mcp.shared.exceptions import MCPError

import baibai_jobs.observability.tradingview as diagnostics
from baibai_engine.data_collection.providers.tradingview.auth import (
    AuthenticationError,
    SecretPersistenceError,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
    FetchError,
)
from baibai_jobs.observability.tradingview import SourceDataError, TimeGuardError


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (AuthenticationError("private"), "auth"),
        (OAuthTokenError("private"), "auth"),
        (SecretPersistenceError("private"), "secret_persistence"),
        (TimeGuardError("private"), "time_guard"),
        (SourceDataError("private"), "storage"),
        (sqlite3.OperationalError("private"), "storage"),
        (FetchError("private"), "provider_response"),
        (httpx2.ReadTimeout("private"), "provider_transport"),
        (RuntimeError("private"), "internal"),
    ],
)
def test_safe_category_through_nested_sdk_groups(error, category):
    wrapped = ExceptionGroup(
        "private", [RuntimeError("private"), ExceptionGroup("private", [error])]
    )
    assert diagnostics.failure_category(wrapped) == category


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (RuntimeError("private-token https://private-response"), "exception_type=RuntimeError"),
        (
            MCPError(-32000, "private-token", {"access_token": "private-token"}),
            "exception_type=MCPError; mcp_error_code=-32000",
        ),
    ],
)
def test_internal_diagnostics_identify_leaf_without_exposing_payload(error, detail):
    wrapped = ExceptionGroup("private-group", [ExceptionGroup("private-child", [error])])
    line = diagnostics.failure_line(wrapped)
    assert diagnostics.failure_category(wrapped) == "internal"
    assert line.endswith(detail)
    for forbidden in ("private", "https://", "access_token", "Traceback"):
        assert forbidden not in line


@pytest.mark.parametrize(
    ("status", "category"),
    [(401, "auth"), (403, "auth"), (429, "provider_rate_limit"), (503, "provider_response")],
)
def test_http_status_category(status, category):
    response = httpx2.Response(status, request=httpx2.Request("GET", "https://example.com"))
    error = httpx2.HTTPStatusError("private", request=response.request, response=response)
    assert diagnostics.failure_category(error) == category
    line = diagnostics.failure_line(ExceptionGroup("private", [error]))
    assert f"mcp_http_status={status}" in line
    assert "private" not in line
    assert "https://" not in line


@pytest.mark.parametrize(
    ("status", "category"),
    [(401, "auth"), (403, "auth"), (429, "provider_rate_limit"), (503, "provider_response")],
)
def test_provider_http_category_through_nested_groups(status, category):
    from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
        ProviderHTTPError,
    )

    error = ProviderHTTPError(status)
    error.__cause__ = RuntimeError("private-token private-body")
    assert diagnostics.failure_category(error) == category
    assert f"provider_http_status={status}" in diagnostics.failure_line(error)
    wrapped = ExceptionGroup("private-token", [ExceptionGroup("private-body", [error])])
    assert diagnostics.failure_category(wrapped) == category
    assert "private" not in diagnostics.failure_line(wrapped)
    assert f"provider_http_status={status}" in diagnostics.failure_line(wrapped)


@pytest.mark.parametrize("value", ["0", "10801", "Sun, 11 Oct 2026 18:00:00 GMT"])
def test_retry_after_is_reported_as_safe_deadline_only(value):
    response = httpx2.Response(
        429,
        headers={"Retry-After": value, "Secret": "private"},
        request=httpx2.Request("GET", "https://example.com"),
    )
    error = httpx2.HTTPStatusError("private", request=response.request, response=response)
    line = diagnostics.failure_line(error)
    assert "retry_not_before_utc=" in line
    assert "private" not in line
    assert "Retry-After" not in line


@pytest.mark.parametrize("value", ["private\nvalue", "-3", "9" * 100, "not-a-date"])
def test_invalid_retry_after_is_not_echoed(value):
    response = httpx2.Response(
        503, headers={"Retry-After": value}, request=httpx2.Request("GET", "https://example.com")
    )
    assert diagnostics._retry_not_before(response) is None
