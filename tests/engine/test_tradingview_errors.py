import sqlite3

import httpx2
import pytest
from mcp.client.auth.exceptions import OAuthTokenError

from baibai_engine.market.tradingview import cli
from baibai_engine.market.tradingview.auth import AuthenticationError, SecretPersistenceError
from baibai_engine.market.tradingview.collector import SourceDataError, TimeGuardError
from baibai_engine.market.tradingview.observations import AllMissingError, FetchError


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (AuthenticationError("private"), "auth"),
        (OAuthTokenError("private"), "auth"),
        (SecretPersistenceError("private"), "secret_persistence"),
        (TimeGuardError("private"), "time_guard"),
        (SourceDataError("private"), "storage"),
        (sqlite3.OperationalError("private"), "storage"),
        (AllMissingError("private"), "provider_all_missing"),
        (FetchError("private"), "provider_response"),
        (httpx2.ReadTimeout("private"), "provider_transport"),
        (RuntimeError("private"), "internal"),
    ],
)
def test_safe_category_through_nested_sdk_groups(error, category):
    wrapped = ExceptionGroup(
        "private", [RuntimeError("private"), ExceptionGroup("private", [error])]
    )
    assert cli.failure_category(wrapped) == category


@pytest.mark.parametrize(
    ("status", "category"),
    [(401, "auth"), (403, "auth"), (429, "provider_rate_limit"), (503, "provider_response")],
)
def test_http_status_category(status, category):
    response = httpx2.Response(status, request=httpx2.Request("GET", "https://example.com"))
    error = httpx2.HTTPStatusError("private", request=response.request, response=response)
    assert cli.failure_category(error) == category


def test_cli_never_prints_exception_payload(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise ExceptionGroup("private-token", [SecretPersistenceError("private-token")])

    monkeypatch.setattr(cli.OAuthState, "model_validate_json", fail)
    assert cli.main(["smoke"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "TradingView acquisition failed; category=secret_persistence\n"


def test_historical_refresh_skips_before_credentials_or_connection(monkeypatch, capsys):
    from datetime import datetime

    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2026, 9, 22, 16, tzinfo=tz)

    def forbidden(*args, **kwargs):
        pytest.fail("historical refresh must not load credentials or connect")

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(cli.OAuthState, "model_validate_json", forbidden)
    monkeypatch.setattr(cli, "connect", forbidden)
    assert cli.main(["refresh", "--asof", "2026-09-21"]) == 0
    assert "skipped_historical_asof" in capsys.readouterr().out
    assert cli.main(["refresh", "--asof", "2026-09-23"]) == 1
    assert "category=time_guard" in capsys.readouterr().err
