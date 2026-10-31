"""Actual SDK/urllib3 retry behavior, with synthetic bodies and no socket access."""

from __future__ import annotations

import gzip
import io
import json
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from urllib.parse import parse_qs, urlsplit

import jquantsapi
import pytest
from tests.engine.machine_analysis.observations.economic_indicators.test_option_iv import (
    ASOF,
    _chain_records,
    _registered,
)
from tests.helpers.network import _no_real_network as _no_real_network
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import ReadTimeoutError
from urllib3.response import HTTPResponse

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_engine.data_collection.providers.jquants.options as jquants_options
from baibai_engine.data_collection.catalog import require_source
from baibai_engine.data_collection.receipts.native_sources.documents import SourceDocument
from baibai_engine.data_collection.receipts.native_sources.records import (
    read_snapshots,
    store_snapshot,
)
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.data_collection.transport.context import (
    FetchContext,
    ProviderFetchStoppedError,
    ProviderRateLimitError,
)
from baibai_engine.data_collection.transport.retry import read_deadline
from baibai_engine.machine_analysis.observations.economic_indicators.option_iv import (
    option_iv_readings,
    quotes_from_records,
)
from baibai_engine.machine_analysis.observations.economic_indicators.options import (
    OptionSeriesReader,
)

BASE = datetime(2026, 10, 13, tzinfo=UTC).timestamp()
READINGS = ("jp.n225_iv_30d", "jp.n225_iv_skew", "jp.n225_iv_term")


class BodyTimeout(io.BytesIO):
    def read(self, *args, **kwargs):
        raise ReadTimeoutError(None, None, "synthetic body timeout")


@pytest.fixture
def wire(monkeypatch, tmp_path):
    class Wire:
        elapsed = 0.0
        calls = []
        waits = []
        timeouts = []
        reply = None

        def sleep(self, seconds):
            self.waits.append(seconds)
            self.elapsed += seconds

        def send(self, pool, connection, method, url, **kwargs):
            assert method == "GET"
            assert kwargs["headers"]["x-api-key"] == "synthetic-key"
            self.calls.append(url)
            self.timeouts.append(kwargs["timeout"])
            status, headers, payload = self.reply(url)
            body = (
                payload
                if hasattr(payload, "read")
                else io.BytesIO(
                    payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                )
            )
            return HTTPResponse(
                body=body,
                status=status,
                headers={"Content-Type": "application/json", **headers},
                preload_content=False,
            )

        def acquire(self, *, start=ASOF, end=ASOF):
            return acquisition._refresh_source(
                self.connection,
                require_source("jquants-options-n225"),
                start=start,
                end=end,
                context=self.context,
            )

        def read(self, series_id=READINGS[0], *, start=ASOF, end=ASOF):
            return OptionSeriesReader(self.connection).read(
                _registered(series_id), start=start, end=end
            )

    wire = Wire()
    wire.context = FetchContext()
    wire.connection = initialize_database(tmp_path / "wire.sqlite")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(jquantsapi.ClientV2, "_load_config", lambda self: {})
    monkeypatch.setattr(jquants_options, "_read_api_key", lambda: "synthetic-key")
    monkeypatch.setattr(
        HTTPSConnectionPool,
        "_make_request",
        lambda pool, connection, method, url, **kwargs: wire.send(
            pool, connection, method, url, **kwargs
        ),
    )
    monkeypatch.setattr("time.sleep", wire.sleep)
    monkeypatch.setattr("time.monotonic", lambda: wire.elapsed)
    monkeypatch.setattr("time.time", lambda: BASE + wire.elapsed)

    def no_socket(*args, **kwargs):
        pytest.fail("offline SDK transport must never connect")

    monkeypatch.setattr("socket.socket.connect", no_socket)
    yield wire
    wire.context.close()
    wire.connection.close()


def chain(day=ASOF):
    return [{**row, "Date": day.isoformat()} for row in _chain_records(day)]


def success(day=ASOF, *, rows=None, next_page=None):
    payload = {"data": chain(day) if rows is None else rows}
    if next_page is not None:
        payload["pagination_key"] = next_page
    return 200, {}, payload


@pytest.mark.parametrize("failures", [1, 5, 6])
def test_same_get_has_initial_plus_five_retries_and_keeps_success_value(wire, failures):
    wire.reply = lambda url: (429, {}, {}) if len(wire.calls) <= failures else success()
    if failures == 6:
        with pytest.raises(ProviderRateLimitError, match=r"HTTP 429.*retries exhausted"):
            wire.acquire()
    else:
        wire.acquire()
        records = wire.read()
        expected = option_iv_readings(quotes_from_records(_chain_records()), ASOF).iv_30d
        assert len(records) == 1
        assert records[0].value == expected
    assert len(wire.calls) == min(failures + 1, 6)
    assert len(set(wire.calls)) == 1
    assert wire.waits == [30, 60, 120, 300, 600][: min(failures, 5)]


def test_three_readings_do_not_restart_a_failed_days_http_budget(wire):
    wire.reply = lambda url: (429, {}, {})
    with pytest.raises(ProviderFetchStoppedError):
        wire.acquire()
    assert all(wire.read(series_id) == () for series_id in READINGS)
    assert len(wire.calls) == 6
    assert wire.waits == [30, 60, 120, 300, 600]


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_existing_transient_get_statuses_remain_bounded(wire, status):
    wire.reply = lambda url: (status, {}, {})
    with pytest.raises(ProviderFetchStoppedError, match="RetryError"):
        wire.acquire()
    assert len(wire.calls) == 6
    assert wire.waits == [30, 60, 120, 300, 600]


def test_read_timeouts_do_not_reset_the_transport_budget_at_service(wire):
    def reply(url):
        raise ReadTimeoutError(None, url, "synthetic timeout")

    wire.reply = reply
    with pytest.raises(ProviderFetchStoppedError, match="ConnectionError"):
        wire.acquire()
    assert len(wire.calls) == 6
    assert wire.waits == [30, 60, 120, 300, 600]


def test_http200_body_timeout_then_success_keeps_all_three_readings(wire):
    wire.reply = lambda url: (
        (200, {}, BodyTimeout(b"partial JSON")) if len(wire.calls) == 1 else success()
    )
    wire.acquire()
    for series_id in READINGS:
        assert len(wire.read(series_id)) == 1
    assert len(wire.calls) == 2
    assert wire.waits == [30]


def test_status_and_body_timeout_use_one_initial_plus_five_budget(wire):
    def reply(url):
        if len(wire.calls) <= 3:
            return 429, {}, {}
        return 200, {}, BodyTimeout(b"partial JSON")

    wire.reply = reply
    with pytest.raises(ProviderFetchStoppedError):
        wire.acquire()
    assert all(wire.read(series_id) == () for series_id in READINGS)
    assert len(wire.calls) == 6
    assert wire.waits == [30, 60, 120, 300, 600]


def test_body_timeout_consumes_the_same_elapsed_deadline(wire):
    def reply(url):
        wire.elapsed += 1190
        return 200, {}, BodyTimeout(b"partial JSON")

    wire.reply = reply
    with pytest.raises(ProviderFetchStoppedError, match="deadline exhausted"):
        wire.acquire()
    assert len(wire.calls) == 1
    assert wire.waits == []


def test_body_timeout_on_later_page_does_not_replay_successful_page(wire):
    counts = Counter()

    def reply(url):
        page = "second" if "pagination_key" in parse_qs(urlsplit(url).query) else "first"
        counts[page] += 1
        if page == "first":
            return success(rows=chain()[:1], next_page="second")
        if counts[page] == 1:
            return 200, {}, BodyTimeout(b"partial JSON")
        return success(rows=chain()[1:])

    wire.reply = reply
    wire.acquire()
    assert len(wire.read()) == 1
    assert counts == {"first": 1, "second": 2}
    assert wire.waits == [30]


@pytest.mark.parametrize("status", [401, 403, 404])
def test_body_timeout_cannot_turn_a_permanent_status_into_a_retry(wire, status):
    wire.reply = lambda url: (status, {}, BodyTimeout(b"partial error body"))
    with pytest.raises(ProviderFetchStoppedError, match=f"HTTP {status} body failed"):
        wire.acquire()
    assert len(wire.calls) == 1
    assert wire.waits == []


def test_rate_limit_with_incomplete_error_body_still_respects_retry_after(wire):
    wire.reply = lambda url: (
        (429, {"Retry-After": "17"}, BodyTimeout(b"partial error body"))
        if len(wire.calls) == 1
        else success()
    )
    wire.acquire()
    assert len(wire.read()) == 1
    assert len(wire.calls) == 2
    assert wire.waits == [17]


def test_preloaded_gzip_json_keeps_the_sdk_payload(wire):
    wire.reply = lambda url: (
        200,
        {"Content-Encoding": "gzip"},
        gzip.compress(json.dumps(success()[2]).encode()),
    )
    wire.acquire()
    assert len(wire.read()) == 1
    assert len(wire.calls) == 1
    assert wire.waits == []


def test_proxied_body_read_has_the_same_retry_and_no_global_pool_patch(wire, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:8080")
    # The proxy tunnel is outside the synthetic response; no socket may be opened.
    monkeypatch.setattr(HTTPSConnectionPool, "_prepare_proxy", lambda self, conn: None)
    wire.reply = lambda url: (
        (200, {}, BodyTimeout(b"partial JSON")) if len(wire.calls) == 1 else success()
    )
    wire.acquire()
    assert len(wire.read()) == 1
    assert len(wire.calls) == 2
    assert wire.waits == [30]
    other = (
        jquantsapi.ClientV2(api_key="other-synthetic-key")._request_session().adapters["https://"]
    )
    assert other.poolmanager.pool_classes_by_scheme["https"] is HTTPSConnectionPool
    assert (
        other.proxy_manager_for("http://proxy.invalid:8080").pool_classes_by_scheme["https"]
        is HTTPSConnectionPool
    )


@pytest.mark.parametrize(
    ("header", "delay"),
    [
        ("17", 17),
        ("0", 0),
        (format_datetime(datetime.fromtimestamp(BASE + 60, UTC), usegmt=True), 60),
        (format_datetime(datetime.fromtimestamp(BASE - 60, UTC), usegmt=True), 0),
        ("invalid", 30),
    ],
)
def test_retry_after_seconds_or_date_precedes_fallback(wire, header, delay):
    wire.reply = lambda url: (
        (429, {"Retry-After": header}, {}) if len(wire.calls) == 1 else success()
    )
    wire.acquire()
    assert len(wire.read()) == 1
    assert len(wire.calls) == 2
    assert wire.waits == ([] if delay == 0 else [delay])


def test_sdk_retry_after_wait_is_inside_one_elapsed_deadline(wire):
    wire.reply = lambda url: (429, {"Retry-After": "900"}, {})
    with pytest.raises(ProviderRateLimitError, match=r"HTTP 429.*deadline exhausted"):
        wire.acquire()
    assert all(wire.read(series_id) == () for series_id in READINGS)
    assert len(wire.calls) == 2
    assert wire.waits == [900]


def test_retry_after_beyond_deadline_is_not_shortened_or_retried(wire):
    wire.reply = lambda url: (429, {"Retry-After": "1201"}, {})
    with pytest.raises(ProviderRateLimitError, match=r"HTTP 429.*deadline exhausted"):
        wire.acquire()
    assert len(wire.calls) == 1
    assert wire.waits == []


def test_communication_time_counts_before_waiting(wire):
    def reply(url):
        wire.elapsed += 600
        return 429, {"Retry-After": "900"}, {}

    wire.reply = reply
    with pytest.raises(ProviderFetchStoppedError, match="deadline exhausted"):
        wire.acquire()
    assert len(wire.calls) == 1
    assert wire.waits == []


def test_days_share_one_deadline_and_completed_day_stays_readable(wire):
    counts = Counter()

    def reply(url):
        day = parse_qs(urlsplit(url).query)["date"][0]
        counts[day] += 1
        if day != ASOF.strftime("%Y%m%d") or counts[day] == 1:
            return 429, {"Retry-After": "900"}, {}
        return success()

    wire.reply = reply
    with pytest.raises(ProviderFetchStoppedError, match="deadline exhausted"):
        wire.acquire(end=ASOF + timedelta(days=1))
    for series_id in READINGS:
        assert len(wire.read(series_id)) == 1
    assert list(counts.values()) == [2, 1]
    assert wire.waits == [900]


def test_terminal_day_failure_is_not_carried_into_a_new_acquisition(wire):
    wire.reply = lambda url: (403, {}, {})
    with pytest.raises(ProviderFetchStoppedError):
        wire.acquire()
    wire.reply = lambda url: success()
    wire.acquire()
    assert len(wire.read()) == 1
    assert len(wire.calls) == 2
    assert wire.waits == []


def test_a_new_pass_can_recover_a_day_that_failed_in_the_same_provider(wire):
    wire.reply = lambda url: (403, {}, {})
    with pytest.raises(ProviderFetchStoppedError):
        wire.acquire()
    wire.elapsed += 3600
    wire.context.close()
    wire.context = FetchContext()
    wire.reply = lambda url: success()
    wire.acquire()
    assert len(wire.read()) == 1
    assert len(wire.calls) == 2
    assert wire.waits == []


@pytest.mark.parametrize("status", [401, 403, 404])
def test_permanent_http_failure_is_one_request_even_with_rate_limit_text(wire, status):
    wire.reply = lambda url: (
        status,
        {"Retry-After": "900"},
        {"message": "429 Too Many Requests synthetic-key"},
    )
    with pytest.raises(ProviderFetchStoppedError, match="HTTPError") as caught:
        wire.acquire()
    assert "synthetic-key" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert len(wire.calls) == 1
    assert wire.waits == []


@pytest.mark.parametrize(
    "body", [b"429 Too Many Requests", {"data": [123]}, {"data": chain(ASOF - timedelta(days=1))}]
)
def test_json_or_schema_failure_does_not_repeat_the_sdk_fetch(wire, body):
    wire.reply = lambda url: (200, {}, body)
    with pytest.raises(ProviderFetchStoppedError):
        wire.acquire()
    assert all(wire.read(series_id) == () for series_id in READINGS)
    assert len(wire.calls) == 1
    assert wire.waits == []


def test_successful_first_page_is_not_replayed_when_second_page_exhausts(wire):
    wire.reply = lambda url: (
        (429, {"Retry-After": "0"}, {})
        if "pagination_key" in parse_qs(urlsplit(url).query)
        else success(rows=chain()[:1], next_page="second")
    )
    with pytest.raises(ProviderFetchStoppedError):
        wire.acquire()
    counts = Counter(
        "second" if "pagination_key" in parse_qs(urlsplit(url).query) else "first"
        for url in wire.calls
    )
    assert counts == {"first": 1, "second": 6}
    assert wire.waits == []


def test_separate_pages_have_separate_get_budgets_and_the_same_calculation(wire):
    counts = Counter()

    def reply(url):
        page = "second" if "pagination_key" in parse_qs(urlsplit(url).query) else "first"
        counts[page] += 1
        if counts[page] <= 5:
            return 429, {"Retry-After": "0"}, {}
        return success(
            rows=chain()[1:] if page == "second" else chain()[:1],
            next_page=None if page == "second" else "second",
        )

    wire.reply = reply
    wire.acquire()
    for series_id, field in zip(READINGS, ("iv_30d", "iv_skew", "iv_term"), strict=True):
        records = wire.read(series_id)
        expected = getattr(option_iv_readings(quotes_from_records(_chain_records()), ASOF), field)
        assert len(records) == 1
        assert records[0].value == expected
    assert counts == {"first": 6, "second": 6}
    assert wire.waits == []


def test_next_page_is_not_started_after_communication_spends_the_deadline(wire):
    def reply(url):
        wire.elapsed += 1201
        return success(rows=chain()[:1], next_page="second")

    wire.reply = reply
    with pytest.raises(ProviderFetchStoppedError, match="deadline exhausted"):
        wire.acquire()
    assert len(wire.calls) == 1
    assert wire.waits == []


def test_separate_days_have_separate_get_budgets_and_reuse_all_three_readings(wire):
    counts = Counter()

    def reply(url):
        day = parse_qs(urlsplit(url).query)["date"][0]
        counts[day] += 1
        if counts[day] <= 5:
            return 429, {"Retry-After": "0"}, {}
        return success(ASOF if day == ASOF.strftime("%Y%m%d") else ASOF + timedelta(days=1))

    wire.reply = reply
    wire.acquire(end=ASOF + timedelta(days=1))
    for series_id in READINGS:
        records = wire.read(series_id, end=ASOF + timedelta(days=1))
        assert len(records) == 2
        assert [r.observed_at for r in records] == [ASOF, ASOF + timedelta(days=1)]
    assert list(counts.values()) == [6, 6]
    assert len(wire.calls) == 12


def test_normal_tls_proxy_pool_timeout_and_other_sdk_clients_are_unchanged(wire):
    client = jquants_options.jquants_read_client(
        "synthetic-key", deadline=read_deadline("jquants_options", wire.context)
    )
    session = client._request_session()
    assert session.verify is True
    assert session.trust_env
    assert session.adapters["https://"]._pool_maxsize == client.MAX_WORKERS + 10
    assert session.adapters["https://"].max_retries.allowed_methods == frozenset({"GET"})
    assert (
        jquantsapi.ClientV2(api_key="other-synthetic-key")
        ._request_session()
        .adapters["https://"]
        .max_retries.total
        == 3
    )
    wire.reply = lambda url: success()
    wire.acquire()
    assert all(t.connect_timeout == t.read_timeout == 30 for t in wire.timeouts)


def test_failed_options_keep_native_chain_readable_and_another_series_can_commit(
    wire, monkeypatch, tmp_path
):
    definition = _registered("jp.bankruptcies")
    body = json.dumps(
        [
            {
                "period_division": "月次",
                "title": f"{ASOF.year}年{ASOF.month}月 全国企業倒産 420件",
            }
        ],
        ensure_ascii=False,
    ).encode()
    document = SourceDocument(
        source_id=definition.source_id,
        request_key="history",
        source_url=definition.source_url,
        content_type="application/json",
        fetched_at=datetime.combine(ASOF, datetime.min.time(), UTC),
        payload=body,
        metadata={"params": {"data": "data"}},
    )
    monkeypatch.setattr(acquisition, "fetch_source_documents", lambda *a, **k: iter([document]))
    database = tmp_path / "indicators.sqlite"
    conn = initialize_database(database)
    store_snapshot(
        conn,
        source_id="jquants-options-n225",
        observed_at=ASOF,
        fetched_at=datetime.combine(ASOF, datetime.min.time(), UTC),
        records=_chain_records(ASOF),
    )
    conn.commit()
    before = tuple(read_snapshots(conn, ASOF, ASOF, source_id="jquants-options-n225"))
    reader = OptionSeriesReader(conn)
    before_values = {
        series_id: reader.read(_registered(series_id), start=ASOF, end=ASOF)
        for series_id in READINGS
    }
    assert all(before_values.values())
    conn.close()
    wire.reply = lambda url: (403, {}, {"message": "synthetic-key"})
    results = acquisition.refresh_sources(
        ["jquants-options-n225", definition.source_id], db_path=database, start=ASOF, end=ASOF
    )
    assert isinstance(results[0], acquisition.SourceRefreshFailure)
    assert isinstance(results[1], acquisition.SourceRefreshSuccess)
    conn = initialize_database(database)
    try:
        after = tuple(read_snapshots(conn, ASOF, ASOF, source_id="jquants-options-n225"))
        assert after == before
        reader = OptionSeriesReader(conn)
        assert {
            series_id: reader.read(_registered(series_id), start=ASOF, end=ASOF)
            for series_id in READINGS
        } == before_values
        documents = conn.execute(
            "SELECT payload FROM economic_source_documents WHERE source_id = ?",
            (definition.source_id,),
        ).fetchall()
        assert len(documents) == 1
        assert documents[0][0] == body
        assert conn.execute("SELECT COUNT(*) FROM economic_source_runs").fetchone()[0] == 2
    finally:
        conn.close()
    assert len(wire.calls) == 1
    assert wire.waits == []


@pytest.mark.parametrize("initially_empty", [False, True])
def test_new_pass_refreshes_successful_and_empty_chain_results(wire, initially_empty):
    wire.reply = lambda url: success(rows=[] if initially_empty else chain())
    wire.acquire()
    for series_id in READINGS:
        assert len(wire.read(series_id)) == (0 if initially_empty else 1)
    assert len(wire.calls) == 1

    wire.context.close()
    wire.context = FetchContext()
    wire.reply = lambda url: success(rows=chain() if initially_empty else [])
    wire.acquire()
    for series_id in READINGS:
        assert len(wire.read(series_id)) == (1 if initially_empty else 0)
    assert len(wire.calls) == 2


@pytest.mark.parametrize("payload", [{}, {"data": None}, {"data": {}}, {"data": [None]}])
def test_malformed_native_page_is_not_a_successful_empty_day(wire, payload):
    wire.reply = lambda url: (200, {}, payload)
    with pytest.raises(ProviderFetchStoppedError, match="data"):
        wire.acquire()
    assert len(wire.calls) == 1
    assert wire.waits == []


def test_native_pages_keep_all_fields_and_source_date_strings(wire):
    rows = [{**row, "EO": "", "extra_source_field": None} for row in chain()]
    wire.reply = lambda url: success(rows=rows)
    result = jquants_options.OptionChainSource(wire.context).fetch(ASOF)
    assert result == rows
    assert isinstance(result[0]["Date"], str)
    assert result[0]["EO"] == ""
    assert result[0]["extra_source_field"] is None


def test_later_malformed_page_does_not_return_a_partial_day(wire):
    wire.reply = lambda url: (
        (200, {}, {"data": None})
        if "pagination_key" in parse_qs(urlsplit(url).query)
        else success(rows=chain()[:1], next_page="second")
    )
    with pytest.raises(ProviderFetchStoppedError, match="data list"):
        wire.acquire()
    assert len(wire.calls) == 2
    assert wire.waits == []


def test_repeated_pagination_key_cannot_loop_or_complete_a_day(wire):
    wire.reply = lambda url: success(rows=[], next_page="same")
    with pytest.raises(ProviderFetchStoppedError, match=r"repeated.*pagination_key"):
        wire.acquire()
    assert len(wire.calls) == 2
    assert wire.waits == []


def test_public_refresh_deduplicates_repeated_source_ids_and_readings_stay_offline(wire, tmp_path):
    wire.reply = lambda url: success()
    database = tmp_path / "public-source.sqlite"
    outcomes = acquisition.refresh_sources(
        ["jquants-options-n225"] * 3, db_path=database, start=ASOF, end=ASOF
    )
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], acquisition.SourceRefreshSuccess)
    assert len(wire.calls) == 1
    with closing(initialize_database(database)) as connection:
        reader = OptionSeriesReader(connection)
        assert all(
            len(reader.read(_registered(series_id), start=ASOF, end=ASOF)) == 1
            for series_id in READINGS
        )
    assert len(wire.calls) == 1
