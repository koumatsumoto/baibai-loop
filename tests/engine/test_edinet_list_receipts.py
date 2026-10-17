from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.http_doubles import FakeResponse, FakeSession, response
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.collection.edinet import http
from baibai_engine.collection.edinet.receipts import (
    read_list_response,
    read_list_responses,
    retain_list_response,
)
from baibai_engine.market.edinet_models import EDINETProviderError
from baibai_engine.market.providers.edinet import EDINETProvider
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.hydrate import hydrate_market_store
from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.lake.reader import resolve_release
from baibai_engine.storage.lake.release import create_release
from baibai_engine.storage.lake.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.observations.schema import open_connection

_DAY = date(2026, 10, 15)


def _wire(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _stored_ids(path):
    with closing(sqlite3.connect(path)) as conn:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT receipt_id FROM edinet_list_responses "
                "WHERE doc_date = ? ORDER BY received_at_utc, receipt_id",
                (_DAY.isoformat(),),
            )
        ]


def test_receipt_round_trips_exact_bytes_and_commits_before_return(tmp_path):
    path = tmp_path / "market.sqlite"
    body = b'{"results": [null], "unrecognized": "\xe6\x97\xa5"}\n'
    received_at = datetime(2026, 10, 15, 3, 4, 5, 123456, tzinfo=UTC)
    with closing(open_connection(path)) as conn:
        receipt_id = retain_list_response(conn, doc_date=_DAY, received_at=received_at, body=body)
        assert uuid.UUID(hex=receipt_id).hex == receipt_id
        assert not conn.in_transaction
        row = conn.execute(
            "SELECT doc_date, received_at_utc, body_gzip "
            "FROM edinet_list_responses WHERE receipt_id = ?",
            (receipt_id,),
        ).fetchone()
        assert row[0] == "2026-10-15"
        assert row[1] == "2026-10-15T03:04:05.123456+00:00"
        assert row[2][4:8] == b"\x00" * 4
        assert gzip.decompress(row[2]) == body
    with closing(sqlite3.connect(path)) as reader:
        saved = read_list_response(reader, receipt_id)
        assert saved is not None
        assert (saved.receipt_id, saved.doc_date, saved.received_at, saved.body) == (
            receipt_id,
            _DAY,
            received_at,
            body,
        )
        assert read_list_response(reader, "missing") is None


def test_read_list_responses_returns_only_requested_day_in_transport_order(tmp_path):
    path = tmp_path / "market.sqlite"
    stamp = datetime(2026, 10, 15, 3, tzinfo=UTC)
    with closing(open_connection(path)) as conn:
        first = retain_list_response(conn, doc_date=_DAY, received_at=stamp, body=b"first")
        second = retain_list_response(conn, doc_date=_DAY, received_at=stamp, body=b"second")
        last = retain_list_response(
            conn, doc_date=_DAY, received_at=stamp + timedelta(seconds=1), body=b"last"
        )
        retain_list_response(
            conn, doc_date=date(2026, 10, 16), received_at=stamp, body=b"other-day"
        )
        rows = read_list_responses(conn, doc_date=_DAY)
        assert [row.receipt_id for row in rows] == [*sorted((first, second)), last]
        assert {row.body for row in rows} == {b"first", b"second", b"last"}
        assert read_list_responses(conn, doc_date=date(2026, 10, 14)) == ()


def test_receipt_refuses_pending_transaction(tmp_path):
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        conn.execute("BEGIN")
        with pytest.raises(ValueError, match="pending transaction"):
            retain_list_response(conn, doc_date=_DAY, received_at=datetime.now(UTC), body=b"{}")
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM edinet_list_responses").fetchone() == (0,)


def test_same_day_refetch_retains_both_received_bodies(tmp_path):
    path = tmp_path / "market.sqlite"
    first = {"metadata": {"resultset": {"count": 0}}, "results": [], "revision": 1}
    second = {"metadata": {"resultset": {"count": 0}}, "results": [], "revision": 2}
    bodies = (_wire(first), _wire(second))
    provider = EDINETProvider(
        "test-key",
        tmp_path / "cache",
        session=FakeSession(
            response(bodies[0], payload=first), response(bodies[1], payload=second)
        ),
        sqlite_path=path,
    )
    assert provider.list_documents(_DAY) == []
    assert provider.list_documents(_DAY, force_refresh=True) == []
    ids = _stored_ids(path)
    assert len(ids) == len(set(ids)) == 2
    with closing(sqlite3.connect(path)) as conn:
        assert {read_list_response(conn, receipt_id).body for receipt_id in ids} == set(bodies)


def test_documents_are_decoded_from_retained_bytes_without_response_json(tmp_path):
    class NoJsonResponse(FakeResponse):
        def json(self):
            raise AssertionError("response.json must not be called")

    wire = _wire(
        {
            "metadata": {"resultset": {"count": 1}},
            "results": [{"seqNumber": 1, "docID": "S100WIRE", "futureField": None}],
        }
    )
    reply = NoJsonResponse(content=wire, _payload={"results": []})
    path = tmp_path / "market.sqlite"
    provider = EDINETProvider(
        "test-key", tmp_path / "cache", session=FakeSession(reply), sqlite_path=path
    )

    documents = provider.list_documents(_DAY)
    with closing(sqlite3.connect(path)) as conn:
        saved = read_list_response(conn, _stored_ids(path)[0])
        assert saved is not None
        assert saved.body == wire
        assert documents == [
            {**item, "doc_date": _DAY.isoformat()} for item in json.loads(saved.body)["results"]
        ]
        assert conn.execute("SELECT doc_id FROM edinet_documents").fetchone() == ("S100WIRE",)
    assert reply.close_count == 1


@pytest.mark.parametrize("body", [b"{not valid JSON test-key", b"\xff\xfe\xff"])
def test_invalid_json_or_encoding_is_retained_with_failed_coverage(tmp_path, body):
    path = tmp_path / "market.sqlite"
    provider = EDINETProvider(
        "test-key", tmp_path / "cache", session=FakeSession(response(body)), sqlite_path=path
    )
    with pytest.raises(EDINETProviderError) as caught:
        provider.list_documents(_DAY)
    assert str(caught.value).startswith("EDINET list projection failed:")
    assert "test-key" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    with closing(sqlite3.connect(path)) as conn:
        saved = read_list_response(conn, _stored_ids(path)[0])
        assert saved is not None
        assert saved.body == body
        assert conn.execute(
            "SELECT status FROM source_coverage WHERE source = 'edinet_documents'"
        ).fetchone() == ("failed",)


@pytest.mark.parametrize(
    "failure",
    ["malformed_json", "count_mismatch", "invalid_row", "missing_doc_id", "invalid_seq"],
)
def test_invalid_projection_still_retains_received_body(tmp_path, failure):
    if failure == "malformed_json":
        body = b"{not valid JSON"
        reply = response(body)
    else:
        results = {
            "count_mismatch": [],
            "invalid_row": [42],
            "missing_doc_id": [{"seqNumber": 1}],
            "invalid_seq": [{"docID": "S100TEST", "seqNumber": "invalid"}],
        }[failure]
        payload = {
            "metadata": {"resultset": {"count": 1}},
            "results": results,
        }
        body = _wire(payload)
        reply = response(body, payload=payload)
    path = tmp_path / "market.sqlite"
    provider = EDINETProvider(
        "test-key", tmp_path / "cache", session=FakeSession(reply), sqlite_path=path
    )
    with pytest.raises(EDINETProviderError) as caught:
        provider.list_documents(_DAY)
    if failure == "missing_doc_id":
        assert "invalid EDINET docID" in str(caught.value)
    elif failure == "invalid_seq":
        assert "EDINET seqNumber must be a positive integer" in str(caught.value)
    ids = _stored_ids(path)
    assert len(ids) == 1
    with closing(sqlite3.connect(path)) as conn:
        saved = read_list_response(conn, ids[0])
        assert saved is not None
        assert saved.body == body
        assert conn.execute("SELECT COUNT(*) FROM edinet_document_lists").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM edinet_documents").fetchone() == (0,)
        assert conn.execute(
            "SELECT status FROM source_coverage WHERE source = 'edinet_documents'"
        ).fetchone() == ("failed",)


def test_receipts_export_through_fixed_release_and_fresh_hydrate(tmp_path, monkeypatch):
    dataset = "edinet.list_responses"
    narrow_release_policy(monkeypatch, datasets=(dataset,), carries_history=None)
    source_path = tmp_path / "market.sqlite"
    first_at = datetime(2026, 10, 15, 3, 4, 5, 123456, tzinfo=UTC)
    bodies = (b'{"results":[{"docID":"S100ONE"}]}', b"{invalid JSON")
    expected = {}
    with closing(open_connection(source_path)) as conn:
        for index, body in enumerate(bodies):
            received_at = first_at + timedelta(seconds=index)
            receipt_id = retain_list_response(
                conn, doc_date=_DAY, received_at=received_at, body=body
            )
            expected[receipt_id] = (_DAY, received_at, body)

    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=source_path, mirror_root=mirror) as snapshot:
        exported = export_dataset_snapshot(
            dataset_name=dataset,
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            source_snapshot=snapshot,
            build_id="edinet-list-receipts-test",
        )
    assert exported.manifest.totals.rows == 2
    assert exported.manifest.layer == "l1_source"
    manifest_path, _ = create_release(
        dataset_manifest_paths=[exported.manifest_path],
        mirror_root=mirror,
        release_id="edinet-list-receipts-test-release",
    )
    source = LocalMirrorSource(mirror)
    release = resolve_release(
        source,
        release_id="edinet-list-receipts-test-release",
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
            dataset_names=(dataset,),
        )
    assert report.rows[dataset] == 2
    with closing(open_connection(hydrated_path)) as conn:
        rows = conn.execute(
            "SELECT receipt_id, doc_date, received_at_utc FROM edinet_list_responses"
        ).fetchall()
        assert len(rows) == 2
        for receipt_id, doc_date, received_at_utc in rows:
            saved = read_list_response(conn, receipt_id)
            assert saved is not None
            assert (saved.doc_date, saved.received_at, saved.body) == expected[receipt_id]
            assert doc_date == saved.doc_date.isoformat()
            assert received_at_utc == saved.received_at.isoformat(timespec="microseconds")


def test_received_time_is_recorded_once_before_decoding(tmp_path, monkeypatch):
    fixed = datetime(2026, 10, 15, 3, 4, 5, 123456, tzinfo=UTC)
    calls = []

    class Clock:
        @staticmethod
        def now(zone):
            calls.append(zone)
            return fixed

    monkeypatch.setattr(http, "datetime", Clock)
    path = tmp_path / "market.sqlite"
    payload = {"metadata": {"resultset": {"count": 0}}, "results": []}
    provider = EDINETProvider(
        "test-key",
        tmp_path / "cache",
        session=FakeSession(response(_wire(payload), payload=payload)),
        sqlite_path=path,
    )
    assert provider.list_documents(_DAY) == []
    assert calls == [UTC]
    with closing(sqlite3.connect(path)) as conn:
        saved = read_list_response(conn, _stored_ids(path)[0])
        assert saved is not None
        assert saved.received_at == fixed


def test_receipt_write_failure_stops_l2_and_is_not_success(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        conn.execute(
            "CREATE TRIGGER reject_edinet_receipt BEFORE INSERT ON edinet_list_responses "
            "BEGIN SELECT RAISE(ABORT, 'synthetic receipt write failure'); END"
        )
    payload = {"metadata": {"resultset": {"count": 0}}, "results": []}
    reply = response(_wire(payload), payload=payload)
    provider = EDINETProvider(
        "test-key", tmp_path / "cache", session=FakeSession(reply), sqlite_path=path
    )
    with pytest.raises(sqlite3.DatabaseError, match="synthetic receipt write failure"):
        provider.list_documents(_DAY)
    assert reply.close_count == 1
    assert _stored_ids(path) == []
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM edinet_document_lists").fetchone() == (0,)
