"""GET status/body retry and real SQLite I/O using synthetic provider responses."""

from __future__ import annotations

import gzip
import io
import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from urllib.parse import parse_qs, urlsplit

import jquantsapi
import pytest
from tests.helpers.indicator_store import observation
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import ReadTimeoutError
from urllib3.response import HTTPResponse

from baibai_batch.validation.market_context_stores import main as validate_stores
from baibai_engine.market_context.indicator_service import (
    IndicatorsService,
    RefreshFailure,
    RefreshSuccess,
    _fetch_with_retry,
)
from baibai_engine.market_context.indicators import db
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.indicators.providers.base import (
    FetchContext,
    ProviderFetchStoppedError,
)
from baibai_engine.market_context.indicators.providers.fred import parse_fred_csv
from baibai_engine.market_context.indicators.providers.native import fetch_native_records
from baibai_engine.market_context.reading.reader import build_store_observation_reader
from baibai_engine.storage.application.write import initialize_database as initialize_application

IDS = ("jp.foreign_flows", "jp.topix", "vix")
DAY = date(2026, 7, 24)
START = DAY - timedelta(days=14)
END = DAY + timedelta(days=7)
BASE = datetime(2026, 10, 13, tzinfo=UTC).timestamp()


class BodyTimeout(io.BytesIO):
    def read(self, *args, **kwargs):
        raise ReadTimeoutError(None, None, "synthetic body timeout")


def payload(series_id, *, day=DAY, value=None):
    if series_id == "vix":
        return f"observation_date,VIXCLS\n{day.isoformat()},{20 if value is None else value}\n".encode()
    if series_id == "jp.topix":
        return {"data": [{"Date": day.isoformat(), "C": 3000 if value is None else value}]}
    row = dict.fromkeys(jquantsapi.constants.EQ_INVESTOR_TYPES_COLUMNS_V2)
    row.update(
        Section="TSEPrime",
        PubDate=(day + timedelta(days=6)).isoformat(),
        StDate=(day - timedelta(days=4)).isoformat(),
        EnDate=day.isoformat(),
        FrgnBal=500 if value is None else value,
    )
    return {"data": [row]}


@pytest.fixture
def wire(monkeypatch):
    class Wire:
        elapsed = 0.0
        calls = []
        waits = []
        reply = None

        def sleep(self, seconds):
            self.waits.append(seconds)
            self.elapsed += seconds

        def send(self, pool, conn, method, url, **kwargs):
            assert method == "GET"
            series_id = (
                "jp.foreign_flows"
                if "investor-types" in url
                else "jp.topix"
                if "topix" in url
                else "vix"
            )
            if series_id != "vix":
                assert kwargs["headers"]["x-api-key"] == "synthetic-key"
            self.calls.append((series_id, url))
            status, headers, value = self.reply(series_id, url)
            if hasattr(value, "read"):
                body = value
            else:
                body = io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())
            return HTTPResponse(
                body=body, status=status, headers=headers, preload_content=False, pool=pool
            )

        def fetch(self, series_id, *, context=None):
            selected_context = self.context if context is None else context
            native = _fetch_with_retry(
                lambda: fetch_native_records(
                    load_definitions().by_id()[series_id],
                    start=START,
                    end=END,
                    context=selected_context,
                ),
                context=selected_context,
            )
            return [row for records in native.values() for row in records]

    wire = Wire()
    wire.context = FetchContext()
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(jquantsapi.ClientV2, "_load_config", lambda self: {})
    for name in ["jquants_flows", "jquants_indices"]:
        module = __import__(
            f"baibai_engine.market_context.indicators.providers.{name}", fromlist=["*"]
        )
        monkeypatch.setattr(module, "_read_api_key", lambda: "synthetic-key")
    monkeypatch.setattr(
        HTTPConnectionPool,
        "_make_request",
        lambda pool, conn, method, url, **kwargs: wire.send(pool, conn, method, url, **kwargs),
    )
    monkeypatch.setattr("time.sleep", wire.sleep)
    monkeypatch.setattr("time.monotonic", lambda: wire.elapsed)
    monkeypatch.setattr("time.time", lambda: BASE + wire.elapsed)

    def no_socket(*args, **kwargs):
        pytest.fail("synthetic transport must not open a provider connection")

    monkeypatch.setattr("socket.socket.connect", no_socket)
    yield wire
    wire.context.close()


@pytest.mark.parametrize("series_id", IDS)
@pytest.mark.parametrize("failures", [1, 5, 6])
def test_one_get_owns_the_initial_plus_five_attempts(wire, series_id, failures):
    wire.reply = lambda sid, url: (
        (429, {}, {}) if len(wire.calls) <= failures else (200, {}, payload(sid))
    )
    if failures == 6:
        with pytest.raises(ProviderFetchStoppedError):
            wire.fetch(series_id)
    else:
        records = wire.fetch(series_id)
        series = load_definitions().by_id()[series_id]
        data = payload(series_id)
        if series_id != "vix":
            assert records == data["data"]
        else:
            expected = parse_fred_csv(series, data.decode(), start=START, end=END)
            assert records == [row for rows in expected.values() for row in rows]
    assert len(wire.calls) == min(failures + 1, 6)
    assert len(set(wire.calls)) == 1
    assert wire.waits == [30, 60, 120, 300, 600][: min(failures, 5)]


@pytest.mark.parametrize("series_id", IDS)
@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("17", 17),
        ("0", 0),
        (format_datetime(datetime.fromtimestamp(BASE + 60, UTC), usegmt=True), 60),
        ("invalid", 30),
    ],
)
def test_retry_after_is_respected_by_sdk_and_plain_http(wire, series_id, header, seconds):
    wire.reply = lambda sid, url: (
        (429, {"Retry-After": header}, {}) if len(wire.calls) == 1 else (200, {}, payload(sid))
    )
    assert len(wire.fetch(series_id)) == 1
    assert len(wire.calls) == 2
    assert wire.waits == ([] if seconds == 0 else [seconds])


@pytest.mark.parametrize("series_id", IDS)
def test_long_sdk_or_http_wait_cannot_bypass_the_process_deadline(wire, series_id):
    wire.reply = lambda sid, url: (429, {"Retry-After": "900"}, {})
    with pytest.raises(ProviderFetchStoppedError, match="deadline exhausted"):
        wire.fetch(series_id)
    assert len(wire.calls) == 2
    assert wire.waits == [900]


@pytest.mark.parametrize("series_id", IDS)
def test_a_later_fetch_pass_does_not_inherit_an_expired_deadline(wire, series_id):
    wire.reply = lambda sid, url: (200, {}, payload(sid))
    assert len(wire.fetch(series_id)) == 1
    wire.elapsed += 3600
    wire.reply = lambda sid, url: (200, {}, payload(sid, value=25 if sid == "vix" else 3500))
    with FetchContext() as next_pass:
        records = wire.fetch(series_id, context=next_pass)
    if series_id == "vix":
        assert records[0]["VIXCLS"] == "25"
    else:
        assert records[0]["C" if series_id == "jp.topix" else "FrgnBal"] == 3500
    assert len(wire.calls) == 2


@pytest.mark.parametrize("series_id", IDS)
@pytest.mark.parametrize("status", [401, 403, 404])
def test_permanent_status_is_not_reissued_by_service(wire, series_id, status):
    wire.reply = lambda sid, url: (status, {"Retry-After": "900"}, {"message": "429 synthetic-key"})
    with pytest.raises(ProviderFetchStoppedError) as error:
        wire.fetch(series_id)
    assert "synthetic-key" not in str(error.value)
    assert len(wire.calls) == 1
    assert wire.waits == []


@pytest.mark.parametrize("series_id", IDS)
def test_schema_failure_is_not_reissued_or_retained_in_http_cache(wire, series_id):
    wire.reply = lambda sid, url: (200, {}, b"429 schema failure")
    with FetchContext() as context:
        with pytest.raises(ProviderFetchStoppedError):
            wire.fetch(series_id, context=context)
        assert context.bytes_cache == {}
    assert len(wire.calls) == 1
    assert wire.waits == []


@pytest.mark.parametrize("series_id", IDS)
def test_http200_body_timeout_then_success_uses_the_same_get(wire, series_id):
    wire.reply = lambda sid, url: (
        (200, {}, BodyTimeout(b"partial body")) if len(wire.calls) == 1 else (200, {}, payload(sid))
    )
    assert len(wire.fetch(series_id)) == 1
    assert len(wire.calls) == 2
    assert wire.waits == [30]


@pytest.mark.parametrize("series_id", IDS)
def test_broken_gzip_then_valid_body_recovers_within_the_same_get(wire, series_id):
    def reply(sid, url):
        value = payload(sid)
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        return (
            200,
            {"Content-Encoding": "gzip"},
            (b"not a gzip body" if len(wire.calls) == 1 else gzip.compress(body)),
        )

    wire.reply = reply
    assert len(wire.fetch(series_id)) == 1
    assert len(wire.calls) == 2
    assert wire.waits == [30]


@pytest.mark.parametrize("series_id", IDS)
def test_continuous_gzip_failure_is_bounded_by_the_same_six_attempts(wire, series_id):
    wire.reply = lambda sid, url: (200, {"Content-Encoding": "gzip"}, b"not a gzip body")
    with pytest.raises(ProviderFetchStoppedError):
        wire.fetch(series_id)
    assert len(wire.calls) == 6
    assert wire.waits == [30, 60, 120, 300, 600]


def test_valid_gzip_with_invalid_csv_is_not_retried(wire):
    wire.reply = lambda sid, url: (
        200,
        {"Content-Encoding": "gzip"},
        gzip.compress(b"observation_date,OTHER\n2026-07-24,20\n"),
    )
    with pytest.raises(ProviderFetchStoppedError, match="missing column"):
        wire.fetch("vix")
    assert len(wire.calls) == 1
    assert wire.waits == []


@pytest.mark.parametrize("series_id", IDS)
def test_status_and_body_failures_share_the_six_attempt_limit(wire, series_id):
    wire.reply = lambda sid, url: (
        (429, {}, {}) if len(wire.calls) <= 3 else (200, {}, BodyTimeout(b"partial body"))
    )
    with pytest.raises(ProviderFetchStoppedError):
        wire.fetch(series_id)
    assert len(wire.calls) == 6
    assert wire.waits == [30, 60, 120, 300, 600]


@pytest.mark.parametrize("series_id", IDS[:2])
def test_body_failure_on_later_sdk_page_does_not_replay_first_page(wire, series_id):
    counts = Counter()

    def reply(sid, url):
        page = "second" if "pagination_key" in parse_qs(urlsplit(url).query) else "first"
        counts[page] += 1
        if page == "first":
            data = payload(sid, day=DAY - timedelta(days=7))
            data["pagination_key"] = "second"
            return 200, {}, data
        return (
            (200, {}, BodyTimeout(b"partial JSON"))
            if counts[page] == 1
            else (200, {}, payload(sid))
        )

    wire.reply = reply
    records = wire.fetch(series_id)
    assert len(records) == 2
    assert counts == {"first": 1, "second": 2}
    assert wire.waits == [30]


def test_fred_reuses_session_pool_and_cached_bytes_without_affecting_other_hosts(wire):
    wire.reply = lambda sid, url: (200, {}, payload(sid))
    series = load_definitions().by_id()["vix"]
    origin = f"{urlsplit(series.source_url).scheme}://{urlsplit(series.source_url).netloc}/"
    with FetchContext() as context:
        assert len(wire.fetch("vix", context=context)) == 1
        adapter = context.session.get_adapter(origin)
        assert context.session.verify is True
        assert context.session.trust_env
        assert len(wire.fetch("vix", context=context)) == 1
        assert context.session.get_adapter(origin) is adapter
        assert context.session.get_adapter("https://unrelated.invalid/").max_retries.total == 0
    assert len(wire.calls) == 1


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({"Content-Length": "8000001"}, b"x"),
        ({}, b"x" * 8000001),
        ({"Content-Encoding": "gzip"}, gzip.compress(b"x" * 8000001)),
    ],
)
def test_fred_keeps_the_response_size_guard_inside_body_reception(wire, headers, body):
    wire.reply = lambda sid, url: (200, headers, body)
    with pytest.raises(ProviderFetchStoppedError, match="response too large"):
        wire.fetch("vix")
    assert len(wire.calls) == 1
    assert wire.waits == []


def test_service_writes_and_reads_real_temporary_stores_without_losing_prior_rows(
    wire, tmp_path, capsys
):
    definitions = load_definitions()
    by_id = definitions.by_id()
    database = tmp_path / "indicators.sqlite"
    application = tmp_path / "application.sqlite"
    initialize_application(application)
    conn = db.initialize_database(database)
    db.insert_observations(conn, [observation(by_id["vix"], DAY - timedelta(days=7), 19)])
    conn.commit()
    conn.close()

    def full_response(sid, url):
        current = payload(sid)
        if sid == "vix":
            current = (
                f"observation_date,VIXCLS\n{(DAY - timedelta(days=7)).isoformat()},19\n"
                f"{DAY.isoformat()},20\n"
            ).encode()
        else:
            previous = payload(
                sid, day=DAY - timedelta(days=7), value=400 if sid == "jp.foreign_flows" else 2900
            )
            current["data"] = previous["data"] + current["data"]
        return 200, {}, current

    wire.reply = full_response
    results = IndicatorsService(database).refresh_series(IDS, start=START, end=END)
    assert all(isinstance(r, RefreshSuccess) for r in results)
    conn = db.open_read_only_connection(database)
    try:
        before = conn.execute(
            "SELECT * FROM observations ORDER BY series_id,observed_at"
        ).fetchall()
        native_before = conn.execute(
            "SELECT * FROM indicator_source_snapshots ORDER BY source_key,observed_at,fetched_at"
        ).fetchall()
        reader = build_store_observation_reader(conn, series=definitions.series)
        for sid, value in zip(IDS, (500, 3000, 20), strict=True):
            rows = reader(sid, START, END)
            assert len(rows) == 2
            assert rows[-1].value == value
        before_publication = build_store_observation_reader(
            conn, series=definitions.series, vintage_cutoff=DAY + timedelta(days=5)
        )
        after_publication = build_store_observation_reader(
            conn, series=definitions.series, vintage_cutoff=DAY + timedelta(days=6)
        )
        assert before_publication(IDS[0], DAY, DAY) == ()
        assert len(after_publication(IDS[0], DAY, DAY)) == 1
    finally:
        conn.close()
    wire.reply = lambda sid, url: (403, {}, {})
    failures = IndicatorsService(database).refresh_series(IDS, start=START, end=END)
    assert all(isinstance(r, RefreshFailure) for r in failures)
    conn = db.open_read_only_connection(database)
    try:
        assert (
            conn.execute("SELECT * FROM observations ORDER BY series_id,observed_at").fetchall()
            == before
        )
        assert (
            conn.execute(
                "SELECT * FROM indicator_source_snapshots ORDER BY source_key,observed_at,fetched_at"
            ).fetchall()
            == native_before
        )
        assert conn.execute("SELECT COUNT(*) FROM provider_runs").fetchone()[0] == 6
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    assert validate_stores(["--db", str(database), "--app-db", str(application)]) == 0
    assert "ok:" in capsys.readouterr().out
    assert len(wire.calls) == 6
