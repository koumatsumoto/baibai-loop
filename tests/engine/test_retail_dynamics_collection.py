from __future__ import annotations

import json
import signal
import stat
import time
from datetime import UTC, datetime

import pytest
import requests
from tests.engine.test_retail_dynamics_sources import monex_html, official_html, rakuten_html

from baibai_engine.market.retail_dynamics import cli, collection
from baibai_engine.market.retail_dynamics.sources import monex
from baibai_engine.market.sqlite.schema import open_connection


class Session:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        status, html = next(self.replies)
        response = requests.Response()
        response.status_code = status
        response._content = html.encode()
        response._content_consumed = True
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        return response


def test_live_collector_separates_acquisition_and_receipt_time():
    client = Session([(200, monex_html()), (200, official_html()), (200, rakuten_html())])
    before = datetime.now(UTC)
    result = collection.collect(["7203"], session=client)
    after = datetime.now(UTC)
    assert len(result.observations) == 2
    assert result.requests == 3
    assert result.stop_reason is None
    assert all(before <= row.fetched_at_utc <= after for row in result.observations)
    assert client.calls[-1][0].endswith("bcode=7203.T")
    assert all(call[1] == {"timeout": 10, "allow_redirects": False} for call in client.calls)


@pytest.mark.parametrize(("status", "category"), [(403, "access_denied"), (429, "rate_limited")])
def test_limit_response_stops_only_that_source(status, category):
    client = Session(
        [
            (status, "secret"),
            (200, official_html()),
            (200, rakuten_html()),
            (200, official_html("9432")),
            (200, rakuten_html("9432")),
        ]
    )
    result = collection.collect(["7203", "9432"], session=client)
    assert result.requests == 5
    assert len(result.observations) == 2
    assert [row.status for row in result.outcomes] == [
        category,
        "not_attempted",
        "observed",
        "observed",
    ]
    assert result.outcomes[1].reason == category
    assert sum("monex.co.jp" in url for url, _ in client.calls) == 1
    assert "secret" not in json.dumps(result.summary())


def test_iframe_403_stops_remaining_rakuten_tickers():
    client = Session([(200, official_html()), (403, "secret")])
    result = collection.collect(["7203", "9432"], sources=("rakuten",), session=client)
    assert result.requests == 2
    assert not result.observations
    assert result.outcomes[0].status == "access_denied"


def test_budget_counts_official_and_iframe_separately():
    client = Session([(200, monex_html()), (200, official_html())])
    result = collection.collect(["7203"], session=client, request_budget=2)
    assert result.requests == 2
    assert result.stop_reason == "request_budget"
    assert result.outcomes[-1].status == "incomplete"
    assert result.outcomes[-1].reason == "request_budget"
    assert len(result.observations) == 1


def test_actual_wall_clock_interrupts_a_transport_that_ignores_timeout(monkeypatch):
    def stall(*args, **kwargs):
        time.sleep(5)
        raise AssertionError("deadline did not interrupt transport")

    client = Session([])
    monkeypatch.setattr(client, "get", stall)
    start = time.monotonic()
    result = collection.collect(["7203"], session=client, time_limit_seconds=0.05)
    assert time.monotonic() - start < 1
    assert result.stop_reason == "wall_clock_limit"
    assert result.requests == 1
    assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)


def test_timeout_and_shell_are_failures_not_nullable_observations(monkeypatch):
    client = Session([(200, "<html>secret shell</html>")])
    result = collection.collect(["7203"], session=client, sources=("monex",))
    assert not result.observations
    assert result.outcomes[0].status == "parse_failure"
    monkeypatch.setattr(
        client, "get", lambda *args, **kwargs: (_ for _ in ()).throw(requests.Timeout("secret"))
    )
    result = collection.collect(["7203"], session=client, sources=("monex",))
    assert result.outcomes[0].status == "request_timeout"
    assert "secret" not in json.dumps(result.summary())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tickers": []},
        {"tickers": ["7203"] * 2},
        {"tickers": ["7203", "9432", "6758", "8306"]},
        {"request_budget": 10},
        {"request_budget": 0},
        {"time_limit_seconds": float("nan")},
        {"request_timeout_seconds": 31},
    ],
)
def test_invalid_expansion_never_calls_transport(kwargs):
    client = Session([])
    with pytest.raises(ValueError, match=r"requires|budget|limit|timeout"):
        collection.collect(session=client, **kwargs)
    assert not client.calls


def test_private_staging_roundtrip_preserves_original_receipt(tmp_path):
    timestamp = datetime(2024, 4, 1, 18, tzinfo=UTC)
    observed = monex.parse_html(
        monex_html(), ticker="7203", source_url=monex.public_url("7203"), fetched_at_utc=timestamp
    )
    path = tmp_path / "private.json"
    collection.save_staging(path, [observed])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    replay = collection.load_staging(path)
    assert replay == (observed,)
    assert replay[0].fetched_at_utc == timestamp
    assert "theory_summary_inner" not in path.read_text()


def test_cli_collect_stages_only_and_import_requires_explicit_db(monkeypatch, tmp_path, capsys):
    observed = monex.parse_html(
        monex_html(),
        ticker="7203",
        source_url=monex.public_url("7203"),
        fetched_at_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )
    result = collection.CollectionResult(
        (observed,), (collection.CollectionOutcome("monex", "7203", "observed"),), 1
    )
    monkeypatch.setattr(cli, "collect", lambda *args, **kwargs: result)
    writes = []
    monkeypatch.setattr(cli, "save_observations", lambda *args: writes.append(args) or 1)
    path = tmp_path / "private.json"
    assert cli.main(["collect", "--output", str(path)]) == 0
    assert writes == []
    with pytest.raises(SystemExit):
        cli.main(["import", "--input", str(path)])
    db = tmp_path / "copy.sqlite"
    open_connection(db).close()
    assert cli.main(["import", "--input", str(path), "--db", str(db)]) == 0
    assert writes == [(db, (observed,))]
    assert "2024" not in capsys.readouterr().out


def test_cli_import_reports_storage_failure_without_values_or_http(monkeypatch, tmp_path, capsys):
    def fail(*args):
        raise ValueError("secret licensed numeric data")

    monkeypatch.setattr(cli, "load_staging", fail)
    monkeypatch.setattr(
        cli, "collect", lambda *args, **kwargs: pytest.fail("import accessed provider")
    )
    assert (
        cli.main(
            [
                "import",
                "--input",
                str(tmp_path / "stage.json"),
                "--db",
                str(tmp_path / "copy.sqlite"),
            ]
        )
        == 1
    )
    assert "secret" not in capsys.readouterr().err


def test_cli_import_rejects_missing_or_unmigrated_db_without_bootstrap(
    monkeypatch, tmp_path, capsys
):
    import sqlite3

    observed = monex.parse_html(
        monex_html(),
        ticker="7203",
        source_url=monex.public_url("7203"),
        fetched_at_utc=datetime(2024, 1, 1, tzinfo=UTC),
    )
    stage = tmp_path / "stage.json"
    collection.save_staging(stage, [observed])
    missing = tmp_path / "missing.sqlite"
    assert cli.main(["import", "--input", str(stage), "--db", str(missing)]) == 1
    assert not missing.exists()
    assert json.loads(capsys.readouterr().err)["category"] == "storage_failure"
    old = tmp_path / "old.sqlite"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE old_store (value INTEGER)")
    assert cli.main(["import", "--input", str(stage), "--db", str(old)]) == 1
    assert json.loads(capsys.readouterr().err)["category"] == "schema_failure"
    with sqlite3.connect(old) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("old_store",)
        ]
