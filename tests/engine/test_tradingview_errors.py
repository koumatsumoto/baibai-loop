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


@pytest.mark.parametrize(
    ("status", "category"),
    [(401, "auth"), (403, "auth"), (429, "provider_rate_limit"), (503, "provider_response")],
)
def test_provider_http_category_through_collection_and_groups(status, category):
    from baibai_engine.market.tradingview.collector import CollectionFailure, CollectionProgress
    from baibai_engine.market.tradingview.observations import ProviderHTTPError

    error = ProviderHTTPError(status)
    assert cli.failure_category(error) == category
    error = CollectionFailure(error, CollectionProgress("2026-09-24", "fetch", 51, 2, 1, 2, 1))
    assert cli.failure_category(error) == category
    wrapped = ExceptionGroup("private-token", [ExceptionGroup("private-body", [error])])
    assert cli.failure_category(wrapped) == category
    assert "chunks_completed=1" in cli.failure_line(wrapped)
    assert "private" not in cli.failure_line(wrapped)


def test_cli_collection_error_prints_only_safe_diagnostics(monkeypatch, capsys):
    from baibai_engine.market.tradingview.collector import CollectionFailure, CollectionProgress
    from baibai_engine.market.tradingview.observations import ProviderPayloadError

    def fail(*args, **kwargs):
        error = ProviderPayloadError("invalid_numeric", "quote_close")
        error.__cause__ = RuntimeError("private-token TSE:7203 https://secret")
        raise ExceptionGroup(
            "private-body",
            [
                CollectionFailure(
                    error, CollectionProgress("2026-09-24", "normalize", 51, 2, 1, 2, 1)
                )
            ],
        )

    monkeypatch.setattr(cli.OAuthState, "model_validate_json", fail)
    assert cli.main(["smoke"]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert len(output.err.splitlines()) == 1
    assert "validation_reason=invalid_numeric; validation_field=quote_close" in output.err
    assert "chunk_index=2" in output.err
    for forbidden in ("private", "TSE:", "https://", "Traceback"):
        assert forbidden not in output.err


def test_progress_writer_atomic_filtered_and_no_fsync(tmp_path, monkeypatch):
    import json
    import os
    from pathlib import Path

    path = tmp_path / "progress.json"
    path.write_text('{"phase":"fetch"}')
    replace = Path.replace
    observed = []

    def inspect(source, target):
        assert json.loads(path.read_text()) == {"phase": "fetch"}
        observed.append(json.loads(source.read_text()))
        return replace(source, target)

    def forbidden(*args, **kwargs):
        pytest.fail("runner progress must not fsync")

    monkeypatch.setattr(Path, "replace", inspect)
    monkeypatch.setattr(os, "fsync", forbidden)
    cli._write_progress_file(
        path,
        {
            "phase": "complete",
            "oauth_rotations": 2,
            "symbols": ["TSE:7203"],
            "token": "private-token",
            "headers": "private-header",
            "response_bytes": {"body": "private-body"},
        },
    )
    assert observed == [{"phase": "complete", "oauth_rotations": 2}]
    assert json.loads(path.read_text()) == observed[0]
    assert [entry for entry in tmp_path.iterdir() if entry.is_file()] == [path]


def test_progress_writer_cleans_temp_on_replace_failure(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "progress.json"
    path.write_text('{"phase":"fetch"}')

    def fail(*args):
        raise OSError("private-path")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="private-path"):
        cli._write_progress_file(path, {"phase": "complete"})
    assert path.read_text() == '{"phase":"fetch"}'
    assert [entry for entry in tmp_path.iterdir() if entry.is_file()] == [path]


@pytest.mark.parametrize("write_failure", [False, True])
def test_snapshot_progress_rotation_and_best_effort_acquisition(
    tmp_path, monkeypatch, capsys, write_failure
):
    import asyncio
    import json
    from contextlib import asynccontextmanager
    from functools import partial

    from mcp.shared.auth import OAuthToken
    from tests.engine.test_tradingview_auth import state
    from tests.engine.test_tradingview_collector import DAY, NOW, payload, store, stored

    path = store(tmp_path / "market.sqlite")
    progress_path = tmp_path / "progress.json"
    writes = []
    storage_ref = []
    writer = cli._write_progress_file

    @asynccontextmanager
    async def connect(storage):
        storage_ref.append(storage)
        yield object()

    async def fetch(*args):
        await storage_ref[0].set_tokens(
            OAuthToken(access_token="private-token", token_type="Bearer")
        )
        return payload(args[1])

    def write(path, value):
        writes.append(value)
        if write_failure:
            raise OSError("private-token /private/path")
        writer(path, value)

    monkeypatch.setattr(cli, "connect", connect)
    monkeypatch.setattr(cli, "fetch_batch", fetch)
    monkeypatch.setattr(cli, "save_github_secret", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "collect", partial(cli.collect, clock=lambda: NOW))
    monkeypatch.setattr(cli, "_write_progress_file", write)
    result = asyncio.run(
        cli.snapshot(state(), "owner/repo", "writer-token", path, DAY, 0, progress_path)
    )
    assert result["rows"] == len(stored(path)) == 51
    assert result["oauth_rotations"] == 2
    output = capsys.readouterr()
    assert not output.out
    if write_failure:
        assert len(writes) == 1
        assert output.err == "TradingView progress output disabled; type=OSError\n"
    else:
        assert not output.err
        assert writes[0]["oauth_rotations"] == 0
        assert writes[-1]["oauth_rotations"] == 2
        assert json.loads(progress_path.read_text())["phase"] == "complete"
        assert "private-token" not in progress_path.read_text()
