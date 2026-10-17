"""Retain received J-Quants pages in a temporary SQLite market store only."""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import requests
from tests.helpers.l1_release import market_store
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.collection.jquants_contract import JQUANTS_SOURCE_PAGES_DDL
from baibai_engine.collection.jquants_http import JQuantsAcquisitionError, JQuantsHTTP
from baibai_engine.collection.jquants_receipts import (
    acquire_request,
    read_complete_request,
    read_request_pages,
)
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.hydrate import hydrate_market_store
from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.lake.reader import resolve_release
from baibai_engine.storage.lake.release import create_release
from baibai_engine.storage.lake.writer import (
    export_dataset_snapshot,
    sealed_sqlite_snapshot,
    validate_snapshot_parity,
)
from baibai_engine.storage.observations.schema import SQLITE_SCHEMA_VERSION, open_connection

FIRST_AT = datetime(2026, 10, 17, 12, 34, 56, 123456, tzinfo=UTC)
SECOND_AT = FIRST_AT + timedelta(seconds=9)


class Session(requests.Session):
    def __init__(self, *replies):
        super().__init__()
        self.replies = iter(replies)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        response = requests.Response()
        response.status_code = 200
        response._content = reply
        response._content_consumed = True
        return response


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "market.sqlite")
    connection.executescript(JQUANTS_SOURCE_PAGES_DDL)
    try:
        yield connection
    finally:
        connection.close()


def client(session):
    times = iter((FIRST_AT, SECOND_AT))
    return JQuantsHTTP("synthetic-test-key", session=session, clock=lambda: next(times))


def retained_id(conn):
    rows = conn.execute("SELECT DISTINCT request_id FROM jquants_source_pages").fetchall()
    assert len(rows) == 1
    return rows[0][0]


def two_pages():
    first = (
        b' {"data":[{"Code":"9682","Extra":{"raw":[null,"42"]},'
        b'"Price":"1.00"}],"pagination_key":"second"}\n'
    )
    second = b'{"data":[{"Code":"9709","Missing":null,"Number":1}]}'
    return first, second


def test_two_page_acquisition_and_read_preserve_bytes_fields_and_source_times(conn):
    first, second = two_pages()
    session = Session(first, second)
    params = {"date": "2026-10-16"}
    request_id = acquire_request(conn, client(session), "daily_bars", params)

    request = read_complete_request(conn, request_id)
    assert request.source == "daily_bars"
    assert dict(request.params) == params
    assert [page.body for page in request.pages] == [first, second]
    assert [page.received_at for page in request.pages] == [FIRST_AT, SECOND_AT]
    assert request.received_at == SECOND_AT
    assert request.records() == [
        {"Code": "9682", "Extra": {"raw": [None, "42"]}, "Price": "1.00"},
        {"Code": "9709", "Missing": None, "Number": 1},
    ]
    assert [dict(page.query) for page in request.pages] == [
        params,
        {**params, "pagination_key": "second"},
    ]
    rows = conn.execute(
        "SELECT received_at_utc, received_on, body_gzip FROM jquants_source_pages "
        "WHERE request_id = ? ORDER BY page_index",
        (request_id,),
    ).fetchall()
    assert [(at, on) for at, on, _ in rows] == [
        (FIRST_AT.isoformat(timespec="microseconds"), "2026-10-17"),
        (SECOND_AT.isoformat(timespec="microseconds"), "2026-10-17"),
    ]
    assert [gzip.decompress(body) for _, _, body in rows] == [first, second]
    assert not conn.in_transaction


def test_malformed_json_is_committed_before_parse_failure(conn):
    bad_body = b'{"data": [truncated'
    with pytest.raises(JQuantsAcquisitionError, match="not JSON") as caught:
        acquire_request(conn, client(Session(bad_body)), "master", {})

    request_id = retained_id(conn)
    assert caught.value.request_id == request_id
    assert [page.body for page in read_request_pages(conn, request_id)] == [bad_body]
    with pytest.raises(JQuantsAcquisitionError, match="not JSON"):
        read_complete_request(conn, request_id)
    assert not conn.in_transaction


def test_second_page_http_failure_leaves_first_page_explicitly_incomplete(conn):
    first, _ = two_pages()
    session = Session(first, requests.ConnectionError("synthetic-test-key"))
    with pytest.raises(JQuantsAcquisitionError, match="ConnectionError") as caught:
        acquire_request(conn, client(session), "daily_bars", {"date": "2026-10-16"})

    request_id = retained_id(conn)
    assert caught.value.request_id == request_id
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert "synthetic-test-key" not in str(caught.value)
    assert [page.body for page in read_request_pages(conn, request_id)] == [first]
    with pytest.raises(JQuantsAcquisitionError, match="incomplete"):
        read_complete_request(conn, request_id)
    assert len(session.calls) == 2
    assert not conn.in_transaction


def test_empty_data_is_a_complete_raw_observation(conn):
    body = b'{ "data": [] }'
    request_id = acquire_request(conn, client(Session(body)), "market_calendar", {})
    request = read_complete_request(conn, request_id)
    assert len(request.pages) == 1
    assert request.pages[0].body == body
    assert request.records() == []


def test_wrong_second_page_query_is_rejected(conn):
    request_id = acquire_request(conn, client(Session(*two_pages())), "daily_bars", {})
    with conn:
        conn.execute(
            "UPDATE jquants_source_pages SET query_json = ? "
            "WHERE request_id = ? AND page_index = 1",
            (json.dumps({"pagination_key": "wrong"}), request_id),
        )
    with pytest.raises(JQuantsAcquisitionError, match="pagination identity differs"):
        read_complete_request(conn, request_id)


def test_missing_page_index_is_rejected(conn):
    request_id = acquire_request(conn, client(Session(*two_pages())), "daily_bars", {})
    with conn:
        conn.execute(
            "DELETE FROM jquants_source_pages WHERE request_id = ? AND page_index = 0",
            (request_id,),
        )
    with pytest.raises(ValueError, match="missing page"):
        read_complete_request(conn, request_id)


def test_repeated_continuation_is_retained_but_rejected(conn):
    first = b'{"data":[],"pagination_key":"again"}'
    second = b'{"data":[],"pagination_key":"again"}'
    with pytest.raises(JQuantsAcquisitionError, match="repeated"):
        acquire_request(conn, client(Session(first, second)), "master", {})

    request_id = retained_id(conn)
    assert [page.body for page in read_request_pages(conn, request_id)] == [first, second]
    with pytest.raises(JQuantsAcquisitionError, match="repeated"):
        read_complete_request(conn, request_id)


def test_pending_caller_transaction_is_rejected_before_http(conn):
    session = Session(b'{"data":[]}')
    conn.execute("BEGIN")
    try:
        with pytest.raises(ValueError, match="pending transaction"):
            acquire_request(conn, client(session), "master", {})
        assert session.calls == []
        assert conn.execute("SELECT COUNT(*) FROM jquants_source_pages").fetchone()[0] == 0
    finally:
        conn.rollback()


def test_storage_failure_is_not_classified_as_source_failure(conn):
    conn.execute("DROP TABLE jquants_source_pages")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        acquire_request(conn, client(Session(b'{"data":[]}')), "master", {})


def test_later_l2_failure_cannot_roll_back_committed_source_page(conn):
    body = b'{"data":[{"Code":"9682"}]}'
    conn.execute("CREATE TABLE synthetic_l2(code TEXT)")
    request_id = acquire_request(conn, client(Session(body)), "master", {})

    def fail_l2():
        with conn:
            conn.execute("INSERT INTO synthetic_l2 VALUES ('9682')")
            raise RuntimeError("synthetic L2 failure")

    with pytest.raises(RuntimeError, match="synthetic L2 failure"):
        fail_l2()

    assert conn.execute("SELECT COUNT(*) FROM synthetic_l2").fetchone()[0] == 0
    assert [page.body for page in read_complete_request(conn, request_id).pages] == [body]


def test_current_market_schema_exports_and_hydrates_complete_and_incomplete_raw_pages(
    tmp_path, monkeypatch
):
    names = ("jquants.daily_bars", "jquants.source_pages")
    narrow_release_policy(monkeypatch, datasets=names, carries_history=None)
    source_path = market_store(tmp_path / "market.sqlite")
    connection = open_connection(source_path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SQLITE_SCHEMA_VERSION == 46
    first, second = two_pages()
    params = {"date": "2026-10-16"}
    completed_id = acquire_request(connection, client(Session(first, second)), "daily_bars", params)
    failed_body = b'{"data":[{"Code":"9682","Unknown":null}],"pagination_key":"next"}'
    with pytest.raises(JQuantsAcquisitionError, match="ConnectionError") as caught:
        acquire_request(
            connection,
            client(Session(failed_body, requests.ConnectionError("synthetic-test-key"))),
            "master",
            {},
        )
    incomplete_id = caught.value.request_id
    assert incomplete_id is not None
    connection.close()

    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=source_path, mirror_root=mirror) as snapshot:
        manifests = [
            export_dataset_snapshot(
                dataset_name=name,
                mirror_root=mirror,
                producer_git_commit="a" * 40,
                source_snapshot=snapshot,
                build_id=f"source-page-test-{name.replace('.', '-')}",
            )
            for name in names
        ]
    raw_manifest = manifests[1].manifest
    assert raw_manifest.layer == "l1_source"
    assert raw_manifest.coverage_status == "partial"
    assert raw_manifest.population_count is None
    assert raw_manifest.coverage_start.isoformat() == "2026-10-17"
    assert raw_manifest.data_as_of.isoformat() == "2026-10-17"
    assert raw_manifest.totals.rows == 3
    validate_snapshot_parity(sqlite_path=source_path, mirror_root=mirror, manifest=raw_manifest)

    manifest_path, _ = create_release(
        dataset_manifest_paths=[item.manifest_path for item in manifests],
        mirror_root=mirror,
        release_id="source-pages-test-release",
    )
    source = LocalMirrorSource(mirror)
    release = resolve_release(
        source,
        release_id="source-pages-test-release",
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    hydrated_path = tmp_path / "hydrated.sqlite"
    open_connection(hydrated_path).close()
    with lake_session() as session:
        report = hydrate_market_store(
            session,
            release=release,
            cache=LakeObjectCache(root=mirror, source=source),
            store=hydrated_path,
            dataset_names=names,
        )
    assert report.rows["jquants.source_pages"] == 3
    hydrated = open_connection(hydrated_path)
    try:
        complete = read_complete_request(hydrated, completed_id)
        assert [page.body for page in complete.pages] == [first, second]
        assert [page.received_at for page in complete.pages] == [FIRST_AT, SECOND_AT]
        assert [dict(page.query) for page in complete.pages] == [
            params,
            {**params, "pagination_key": "second"},
        ]
        assert complete.records()[0]["Extra"] == {"raw": [None, "42"]}
        assert [page.body for page in read_request_pages(hydrated, incomplete_id)] == [failed_body]
        with pytest.raises(JQuantsAcquisitionError, match="incomplete"):
            read_complete_request(hydrated, incomplete_id)
    finally:
        hydrated.close()
