"""GET status/body retry and real SQLite I/O using synthetic provider responses."""

from __future__ import annotations

import gzip
import json
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from tests.helpers.economic_wire import BASE, DAY, END, IDS, START, payload
from tests.helpers.economic_wire import wire as wire
from tests.helpers.http_doubles import BodyTimeout
from tests.helpers.network import _no_real_network as _no_real_network

from baibai_engine.data_collection.acquisition.refresh import (
    SourceRefreshFailure,
    SourceRefreshSuccess,
    refresh_sources,
)
from baibai_engine.data_collection.catalog import require_source
from baibai_engine.data_collection.providers.fred import parse_fred_csv
from baibai_engine.data_collection.receipts.native_sources.store import open_read_only_connection
from baibai_engine.data_collection.transport.context import (
    FetchContext,
    ProviderFetchStoppedError,
    ProviderRateLimitError,
)
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)

SHARED_QUOTA_CASES = (
    pytest.param(
        ("fred-vixcls", "fred-bamlc0a0cm"),
        ("vix", "credit"),
        "jquants-indices-topix",
        "jp.topix",
        id="fred-sibling-series",
    ),
    pytest.param(
        ("jquants-indices-topix", "jquants-trades-spec"),
        ("jp.topix", "jp.foreign_flows"),
        "fred-vixcls",
        "vix",
        id="jquants-distinct-providers",
    ),
)


@pytest.mark.parametrize("series_id", IDS)
@pytest.mark.parametrize("failures", [1, 5, 6])
def test_one_get_owns_the_initial_plus_five_attempts(wire, series_id, failures):
    wire.reply = lambda sid, url: (
        (429, {}, {}) if len(wire.calls) <= failures else (200, {}, payload(sid))
    )
    if failures == 6:
        with pytest.raises(ProviderRateLimitError, match=r"HTTP 429.*retries exhausted"):
            wire.fetch(series_id)
    else:
        records = wire.fetch(series_id)
        series = load_definitions().by_id()[series_id]
        data = payload(series_id)
        if series_id != "vix":
            assert records == data["data"]
        else:
            expected = parse_fred_csv(
                require_source(series.source_id), data.decode(), start=START, end=END
            )
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
    with pytest.raises(ProviderRateLimitError, match=r"HTTP 429.*deadline exhausted"):
        wire.fetch(series_id)
    assert len(wire.calls) == 2
    assert wire.waits == [900]


@pytest.mark.parametrize(
    ("group_sources", "group_series", "unrelated_source", "unrelated_series"), SHARED_QUOTA_CASES
)
@pytest.mark.parametrize("stop", ["provider_deadline", "job_deadline", "retries_exhausted"])
def test_public_refresh_stops_only_the_rate_limited_group_for_this_pass(
    wire,
    tmp_path,
    monkeypatch,
    group_sources,
    group_series,
    unrelated_source,
    unrelated_series,
    stop,
):
    database = tmp_path / "rate-limit.sqlite"
    wire.reply = lambda sid, url: (200, {}, payload(sid))
    seeded = refresh_sources(group_sources, db_path=database, start=START, end=END)
    assert all(isinstance(outcome, SourceRefreshSuccess) for outcome in seeded)

    def retained_records():
        with closing(open_read_only_connection(database)) as connection:
            return connection.execute(
                "SELECT * FROM economic_source_records WHERE source_id IN (?, ?) "
                "ORDER BY source_id, observed_at, fetched_at",
                group_sources,
            ).fetchall()

    before = retained_records()
    assert before
    wire.calls.clear()
    delay = "0" if stop == "retries_exhausted" else "1201"
    if stop == "job_deadline":
        monkeypatch.setenv("BAIBAI_ACQUISITION_DEADLINE", str(BASE + 60))
        delay = "61"
    wire.reply = lambda sid, url: (
        (429, {"Retry-After": delay}, {"message": "synthetic-key"})
        if sid == group_series[0]
        else (200, {}, payload(sid))
    )

    outcomes = refresh_sources(
        [*group_sources, unrelated_source], db_path=database, start=START, end=END
    )
    assert isinstance(outcomes[0], SourceRefreshFailure)
    assert "HTTP 429" in outcomes[0].message
    assert "synthetic-key" not in outcomes[0].message
    assert isinstance(outcomes[1], SourceRefreshFailure)
    assert "acquisition group stopped" in outcomes[1].message
    assert isinstance(outcomes[2], SourceRefreshSuccess)
    attempts = 6 if stop == "retries_exhausted" else 1
    assert [sid for sid, _ in wire.calls] == [group_series[0]] * attempts + [unrelated_series]
    assert wire.waits == []
    assert retained_records() == before

    wire.calls.clear()
    wire.reply = lambda sid, url: (200, {}, payload(sid))
    recovered = refresh_sources(group_sources, db_path=database, start=START, end=END)
    assert all(isinstance(outcome, SourceRefreshSuccess) for outcome in recovered)
    assert [sid for sid, _ in wire.calls] == list(group_series)


@pytest.mark.parametrize(
    ("group_sources", "group_series", "unrelated_source", "unrelated_series"), SHARED_QUOTA_CASES
)
@pytest.mark.parametrize("failure", ["schema", "403", "503"])
def test_public_refresh_does_not_stop_quota_siblings_for_non_rate_limit_failures(
    wire, tmp_path, group_sources, group_series, unrelated_source, unrelated_series, failure
):
    wire.reply = lambda sid, url: (
        (200 if failure == "schema" else int(failure), {"Retry-After": "0"}, b"429 synthetic-key")
        if sid == group_series[0]
        else (200, {}, payload(sid))
    )
    outcomes = refresh_sources(
        [*group_sources, unrelated_source],
        db_path=tmp_path / "non-rate-limit.sqlite",
        start=START,
        end=END,
    )
    assert isinstance(outcomes[0], SourceRefreshFailure)
    assert all(isinstance(outcome, SourceRefreshSuccess) for outcome in outcomes[1:])
    attempts = 6 if failure == "503" else 1
    assert [sid for sid, _ in wire.calls] == (
        [group_series[0]] * attempts + [group_series[1], unrelated_series]
    )
    assert wire.waits == []


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


@pytest.mark.parametrize("provider_budget", [False, True])
def test_shared_backoff_keeps_each_receivers_deadline_contract(provider_budget):
    from urllib3.exceptions import ProtocolError

    from baibai_engine.data_collection.transport.http import ReadRetry as JobReadRetry
    from baibai_engine.data_collection.transport.retry import ReadDeadline, ReadRetry

    deadline = ReadDeadline("synthetic-provider")
    retry = ReadRetry(deadline=deadline, total=6) if provider_budget else JobReadRetry(total=6)
    assert retry.get_backoff_time() == 0
    for seconds in (30, 60, 120, 300, 600):
        retry = retry.increment(method="GET", error=ProtocolError("synthetic failure"))
        assert retry.get_backoff_time() == seconds
        if provider_budget:
            assert retry.deadline is deadline
