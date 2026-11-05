import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from baibai_engine.data_collection.receipts.native_sources.documents import (
    SourceDocument,
    SourceDocumentMetadata,
    read_document_metadata,
    read_documents,
    store_document,
)
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database


def document(**changes):
    return replace(
        SourceDocument(
            source_id="test-publication",
            request_key="2026-08-01",
            source_url="https://example.com/release.pdf",
            content_type="application/pdf",
            fetched_at=datetime(2026, 10, 17, tzinfo=UTC),
            payload=b"%PDF-1.7 original bytes",
            metadata={"observed_at": "2026-08-01", "release_observed_at": "2026-09-01"},
        ),
        **changes,
    )


def test_original_body_and_metadata_round_trip(tmp_path):
    value = document(
        metadata=MappingProxyType(
            {"observed_at": "2026-08-01", "release_observed_at": "2026-09-01"}
        )
    )
    with closing(initialize_database(tmp_path / "indicators.sqlite")) as conn:
        with conn:
            assert store_document(conn, value)
        assert list(read_documents(conn, value.source_id)) == [value]
        assert list(read_documents(conn, "other-source")) == []


def test_unchanged_document_is_not_another_revision(tmp_path):
    first = document()
    later = replace(first, fetched_at=first.fetched_at + timedelta(days=1))
    with closing(initialize_database(tmp_path / "indicators.sqlite")) as conn:
        with conn:
            assert store_document(conn, first)
            assert not store_document(conn, later)
        assert list(read_documents(conn, first.source_id)) == [first]


def test_unreadable_new_body_does_not_restore_an_older_usable_body(tmp_path):
    first = document()
    later = replace(first, fetched_at=first.fetched_at + timedelta(days=1), payload=b"%PDF damaged")
    with closing(initialize_database(tmp_path / "indicators.sqlite")) as conn:
        with conn:
            store_document(conn, first)
            store_document(conn, later)
        assert list(read_documents(conn, first.source_id)) == [later]
        assert conn.execute("SELECT COUNT(*) FROM economic_source_documents").fetchone()[0] == 2


def test_document_receipt_identity_replay_and_conflict_leave_newer_body_intact(tmp_path):
    old_jst = datetime.fromisoformat("2026-10-17T09:00:00+09:00")
    old = document(fetched_at=old_jst)
    new = document(fetched_at=old_jst + timedelta(hours=1), payload=b"new body")
    with closing(initialize_database(tmp_path / "indicators.sqlite")) as conn, conn:
        assert store_document(conn, old)
        assert store_document(conn, new)
        before = conn.execute(
            "SELECT fetched_at, source_url, content_type, payload, metadata "
            "FROM economic_source_documents ORDER BY fetched_at"
        ).fetchall()
        replay = document(
            fetched_at=old_jst.astimezone(UTC),
            metadata=dict(reversed(list(old.metadata.items()))),
        )
        assert not store_document(conn, replay)
        with pytest.raises(ValueError, match="conflicting source receipt identity"):
            store_document(conn, document(fetched_at=old_jst, payload=b"different body"))
        assert (
            conn.execute(
                "SELECT fetched_at, source_url, content_type, payload, metadata "
                "FROM economic_source_documents ORDER BY fetched_at"
            ).fetchall()
            == before
        )
        assert list(read_documents(conn, old.source_id)) == [new]


def test_document_write_belongs_to_caller_transaction(tmp_path):
    value = document()
    with closing(initialize_database(tmp_path / "indicators.sqlite")) as conn:
        conn.execute("BEGIN")
        store_document(conn, value)
        conn.rollback()
        assert list(read_documents(conn, value.source_id)) == []


def test_document_requires_timezone(tmp_path):
    with (
        closing(initialize_database(tmp_path / "indicators.sqlite")) as conn,
        pytest.raises(ValueError, match="include a timezone"),
    ):
        store_document(conn, document(fetched_at=document().fetched_at.replace(tzinfo=None)))


def test_document_bodies_participate_in_store_merge(tmp_path):
    from baibai_jobs.integration.native_source_merge import merge_stores

    source, target = tmp_path / "source.sqlite", tmp_path / "target.sqlite"
    value = document()
    with closing(initialize_database(source)) as conn, conn:
        store_document(conn, value)
    with closing(initialize_database(target)):
        pass
    merge_stores(source, target)
    with closing(initialize_database(target)) as conn:
        assert list(read_documents(conn, value.source_id)) == [value]


def test_metadata_reader_uses_the_same_latest_request_receipts_without_payload(tmp_path):
    old = document()
    latest = replace(
        old,
        fetched_at=old.fetched_at + timedelta(days=1),
        payload=b"%PDF changed",
        metadata={"observed_at": "2026-08-01", "phase": "flash"},
    )
    other_request = document(request_key="2026-09-01", metadata={"observed_at": "2026-09-01"})
    other_source = document(source_id="other-source", metadata={"other": True})
    with closing(initialize_database(tmp_path / "indicators.sqlite")) as conn, conn:
        for value in (other_request, latest, old, other_source):
            store_document(conn, value)
        expected = [
            SourceDocumentMetadata(
                source_id=row.source_id,
                request_key=row.request_key,
                source_url=row.source_url,
                content_type=row.content_type,
                fetched_at=row.fetched_at,
                metadata=row.metadata,
            )
            for row in read_documents(conn, old.source_id)
        ]
        assert [row.metadata for row in expected] == [latest.metadata, other_request.metadata]
        conn.set_authorizer(
            lambda action, table, column, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ
                and table == "economic_source_documents"
                and column == "payload"
                else sqlite3.SQLITE_OK
            )
        )
        assert list(read_document_metadata(conn, old.source_id)) == expected
        assert list(read_document_metadata(conn, "absent-source")) == []


@pytest.mark.parametrize("reader", [read_documents, read_document_metadata])
@pytest.mark.parametrize(
    ("metadata", "fetched_at", "match"),
    [
        ("[]", None, "must be a mapping"),
        ("null", None, "must be a mapping"),
        ("not-json", None, "Expecting value"),
        ("{}", "invalid", "Invalid isoformat string"),
    ],
)
def test_latest_invalid_document_metadata_does_not_fall_back_to_prior_receipt(
    tmp_path, reader, metadata, fetched_at, match
):
    old = document()
    latest = replace(old, fetched_at=old.fetched_at + timedelta(days=1), payload=b"changed")
    with closing(initialize_database(tmp_path / "indicators.sqlite")) as conn, conn:
        store_document(conn, old)
        store_document(conn, latest)
        timestamp = latest.fetched_at.isoformat(timespec="microseconds")
        conn.execute(
            "UPDATE economic_source_documents SET metadata=?, fetched_at=? WHERE fetched_at=?",
            (metadata, fetched_at or timestamp, timestamp),
        )
        with pytest.raises(ValueError, match=match):
            list(reader(conn, old.source_id))
