"""Public HTML receipt identity, byte retention and fixed lake release behavior."""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.data_collection.equity_web.models import EquityWebSourceResponse
from baibai_engine.data_collection.equity_web.receipts import (
    EquityWebReceiptError,
    read_responses,
    retain_response,
)
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.equity_web_verification import (
    history_digests,
    response_digest,
    verify_release_history,
)
from baibai_engine.storage.lake.hydrate import hydrate_market_store
from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.lake.reader import resolve_release
from baibai_engine.storage.lake.release import create_release
from baibai_engine.storage.lake.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.observations.equity_web_import import import_responses
from baibai_engine.storage.observations.schema import SQLiteSchemaError, open_connection

DAY = datetime(2026, 9, 30, 23, 59, tzinfo=UTC)
DATASET = "equity_web.source_responses"


def _retain(conn, *, attempt_id=None, **overrides):
    fields = {
        "attempt_id": str(uuid.uuid4()) if attempt_id is None else attempt_id,
        "source": "monex",
        "requested_ticker": "9682",
        "role": "page",
        "parent_receipt_id": None,
        "requested_url": "https://scouter.monex.co.jp/report/theoryDps/9682",
        "received_at_utc": DAY,
        "selected_encoding": "shift_jis",
        "body": b"\x82\xa0\xff\x00",
    }
    fields.update(overrides)
    return retain_response(conn, **fields)


def _received(*, source="monex", attempt_id=None, role="page", parent=None, body=b"original"):
    return EquityWebSourceResponse(
        receipt_id=str(uuid.uuid4()),
        attempt_id=attempt_id or str(uuid.uuid4()),
        source=source,
        requested_ticker="9682",
        role=role,
        parent_receipt_id=parent,
        requested_url=f"https://synthetic.invalid/{source}/{role}",
        received_at_utc=DAY,
        selected_encoding="utf-8",
        body=body,
    )


def test_import_existing_store_preserves_ids_bytes_and_page_first_lineage(tmp_path):
    market = tmp_path / "market.sqlite"
    open_connection(market).close()
    page = _received(source="rakuten")
    iframe = _received(
        source="rakuten", attempt_id=page.attempt_id, role="iframe", parent=page.receipt_id
    )
    assert import_responses(market, (iframe, page)) == 2
    assert import_responses(market, (page, iframe)) == 0
    with closing(sqlite3.connect(market)) as conn:
        assert read_responses(conn, (iframe.receipt_id, page.receipt_id)) == (iframe, page)
        assert conn.execute("SELECT COUNT(*) FROM equity_web_source_responses").fetchone() == (2,)


@pytest.mark.parametrize("conflict", ["changed_receipt", "changed_role_identity", "bad_parent"])
def test_import_conflict_rolls_back_whole_supplied_set(tmp_path, conflict):
    market = tmp_path / "market.sqlite"
    open_connection(market).close()
    existing = _received()
    assert import_responses(market, (existing,)) == 1
    fresh = _received()
    if conflict == "changed_receipt":
        invalid = replace(existing, body=b"changed")
    elif conflict == "changed_role_identity":
        invalid = replace(existing, receipt_id=str(uuid.uuid4()))
    else:
        invalid = _received(
            source="rakuten",
            attempt_id=existing.attempt_id,
            role="iframe",
            parent=existing.receipt_id,
        )
    with pytest.raises(EquityWebReceiptError):
        import_responses(market, (fresh, invalid))
    with closing(sqlite3.connect(market)) as conn:
        assert read_responses(conn, (existing.receipt_id,)) == (existing,)
        assert conn.execute("SELECT COUNT(*) FROM equity_web_source_responses").fetchone() == (1,)


def test_import_refuses_missing_or_obsolete_store_without_creating_it(tmp_path):
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        import_responses(missing, (_received(),))
    assert not missing.exists()
    obsolete = tmp_path / "obsolete.sqlite"
    sqlite3.connect(obsolete).close()
    with pytest.raises(SQLiteSchemaError, match="obsolete market SQLite schema"):
        import_responses(obsolete, (_received(),))
    with closing(sqlite3.connect(obsolete)) as conn:
        assert conn.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall() == []


def test_original_non_utf8_bytes_commit_independently_and_duplicate_rolls_back(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn:
        attempt = str(uuid.uuid4())
        receipt_id = _retain(conn, attempt_id=attempt)
        assert str(uuid.UUID(receipt_id)) == receipt_id
        assert not conn.in_transaction
        with closing(sqlite3.connect(path)) as other:
            stored = other.execute(
                "SELECT body_gzip, selected_encoding, received_on "
                "FROM equity_web_source_responses WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            assert stored is not None
            assert gzip.decompress(stored[0]) == b"\x82\xa0\xff\x00"
            assert stored[0] == gzip.compress(b"\x82\xa0\xff\x00", mtime=0)
            assert stored[1:] == ("shift_jis", "2026-09-30")
            assert other.execute(
                "SELECT COUNT(*) FROM tradingview_forecast_snapshots"
            ).fetchone() == (0,)
        with pytest.raises(EquityWebReceiptError, match="could not be retained"):
            _retain(conn, attempt_id=attempt)
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM equity_web_source_responses").fetchone() == (1,)
        response = read_responses(conn, [receipt_id])[0]
        assert response.body == b"\x82\xa0\xff\x00"
        assert response.selected_encoding == "shift_jis"
        assert response.body.__repr__() not in repr(response)


def test_rakuten_page_iframe_lineage_and_incomplete_attempts(tmp_path):
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        attempt = str(uuid.uuid4())
        page = _retain(
            conn,
            attempt_id=attempt,
            source="rakuten",
            role="page",
            requested_url="https://www.rakuten-sec.co.jp/web/market/search/forecast.html?ric=9682",
        )
        iframe = _retain(
            conn,
            attempt_id=attempt,
            source="rakuten",
            role="iframe",
            parent_receipt_id=page,
            requested_url="https://rakuten-sec.ifis.co.jp/index.php?code=9682",
            received_at_utc=DAY + timedelta(minutes=2),
        )
        assert [item.role for item in read_responses(conn, [iframe, page])] == ["iframe", "page"]
        assert read_responses(conn, [iframe])[0].parent_receipt_id == page
        with pytest.raises(EquityWebReceiptError, match="parent"):
            _retain(conn, source="rakuten", role="iframe", parent_receipt_id=page)
        with pytest.raises(EquityWebReceiptError, match="parent"):
            _retain(
                conn,
                attempt_id=attempt,
                source="rakuten",
                requested_ticker="9709",
                role="iframe",
                parent_receipt_id=page,
            )
        with pytest.raises(EquityWebReceiptError, match="only a Rakuten iframe"):
            _retain(conn, source="monex", role="iframe", parent_receipt_id=page)
        incomplete = str(uuid.uuid4())
        _retain(conn, attempt_id=incomplete)
        _retain(
            conn,
            source="kabumap",
            requested_url="https://jp.kabumap.com/servlets/kabumap/Action?codetext=9682",
        )
        assert conn.execute("SELECT COUNT(*) FROM equity_web_source_responses").fetchone() == (4,)


def test_parent_and_attempt_reads_share_the_immediate_write_transaction(tmp_path):
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        attempt = str(uuid.uuid4())
        page = _retain(conn, attempt_id=attempt, source="rakuten")
        statements = []
        conn.set_trace_callback(lambda sql: statements.append((sql, conn.in_transaction)))
        try:
            _retain(
                conn,
                attempt_id=attempt,
                source="rakuten",
                role="iframe",
                parent_receipt_id=page,
            )
        finally:
            conn.set_trace_callback(None)
        reads = [
            active
            for sql, active in statements
            if sql.startswith("SELECT") and "equity_web_source_responses" in sql
        ]
        assert len(reads) == 2
        assert all(reads)
        assert statements[0][0] == "BEGIN IMMEDIATE"
        assert not conn.in_transaction


def test_insert_failure_rolls_back_only_new_receipt(tmp_path):
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        committed = _retain(conn)
        conn.execute(
            "CREATE TRIGGER reject_equity_web_insert "
            "BEFORE INSERT ON equity_web_source_responses "
            "BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END"
        )
        with pytest.raises(EquityWebReceiptError, match="could not be retained"):
            _retain(conn)
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM equity_web_source_responses").fetchone() == (1,)
        assert read_responses(conn, [committed])[0].body == b"\x82\xa0\xff\x00"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"attempt_id": "bad"}, "UUID"),
        ({"attempt_id": ""}, "UUID"),
        ({"attempt_id": str(uuid.uuid4()).upper()}, "UUID"),
        ({"source": "unknown"}, "source"),
        ({"role": "other"}, "role"),
        ({"requested_ticker": "9682 "}, "canonical"),
        ({"requested_ticker": "ab12"}, "canonical"),
        ({"requested_url": " "}, "nonempty"),
        ({"selected_encoding": ""}, "nonempty"),
        ({"received_at_utc": DAY.replace(tzinfo=None)}, "UTC aware"),
        ({"received_at_utc": DAY.astimezone(timezone(timedelta(hours=9)))}, "UTC aware"),
        ({"body": "not-bytes"}, "bytes"),
        ({"parent_receipt_id": str(uuid.uuid4())}, "parent"),
    ],
)
def test_invalid_receipts_do_not_mutate_store(tmp_path, overrides, message):
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        with pytest.raises(EquityWebReceiptError, match=message):
            _retain(conn, **overrides)
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM equity_web_source_responses").fetchone() == (0,)


def test_idle_connection_and_explicit_missing_ids(tmp_path):
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        conn.execute(
            "INSERT INTO jquants_market_calendar(day,holiday_division) VALUES ('2026-09-30','1')"
        )
        with pytest.raises(EquityWebReceiptError, match="pending transaction"):
            _retain(conn)
        assert conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM jquants_market_calendar").fetchone() == (1,)
        conn.rollback()
        first = _retain(conn)
        second = _retain(conn)
        assert [item.receipt_id for item in read_responses(conn, [second, first, second])] == [
            second,
            first,
            second,
        ]
        with pytest.raises(EquityWebReceiptError, match="not retained"):
            read_responses(conn, [str(uuid.uuid4())])
        with pytest.raises(EquityWebReceiptError, match="UUID"):
            read_responses(conn, ["bad"])
        with pytest.raises(EquityWebReceiptError, match="sequence"):
            read_responses(conn, "not-a-sequence-of-IDs")
        assert not conn.in_transaction


def test_complete_and_incomplete_attempts_roundtrip_through_fixed_lake_release(tmp_path, monkeypatch):
    narrow_release_policy(monkeypatch, datasets=(DATASET,))
    market = tmp_path / "market.sqlite"
    with closing(open_connection(market)) as conn:
        attempt = str(uuid.uuid4())
        page = _retain(conn, attempt_id=attempt, source="rakuten", role="page")
        iframe = _retain(
            conn,
            attempt_id=attempt,
            source="rakuten",
            role="iframe",
            parent_receipt_id=page,
            received_at_utc=DAY + timedelta(minutes=2),
        )
        incomplete = _retain(conn, received_at_utc=DAY + timedelta(minutes=3))
        original = read_responses(conn, [iframe, incomplete, page])
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=mirror) as snapshot:
        manifest = export_dataset_snapshot(
            dataset_name=DATASET,
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            source_snapshot=snapshot,
            build_id="equity-web-l1-test",
        ).manifest_path
    assert [part["values"] for part in json.loads(manifest.read_text())["partitions"]] == [
        {"year": 2026, "month": 9},
        {"year": 2026, "month": 10},
    ]
    manifest_path, _ = create_release(
        dataset_manifest_paths=[manifest], mirror_root=mirror, release_id="equity-web-fixed-test"
    )
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    source = LocalMirrorSource(mirror)
    release = resolve_release(source, release_id="equity-web-fixed-test", manifest_sha256=digest)
    assert len(release.manifest.datasets) == 1
    cache = LakeObjectCache(root=mirror, source=source)
    restored = tmp_path / "restored.sqlite"
    open_connection(restored).close()
    with lake_session() as session:
        hydrate_market_store(
            session, release=release, cache=cache, store=restored, dataset_names=(DATASET,)
        )
    with closing(sqlite3.connect(restored)) as conn:
        assert read_responses(conn, [iframe, incomplete, page]) == original
        assert conn.execute(
            "SELECT received_on, COUNT(*) FROM equity_web_source_responses "
            "GROUP BY received_on ORDER BY received_on"
        ).fetchall() == [("2026-09-30", 1), ("2026-10-01", 2)]
    expected = history_digests(market, months={(2026, 9), (2026, 10)})
    selected = {item.receipt_id: response_digest(item) for item in original}
    with lake_session() as session:
        verify_release_history(
            session,
            release=release,
            cache=cache,
            expected_history=expected,
            selected_digests=selected,
        )
        with pytest.raises(ValueError, match="selected receipt readback mismatch"):
            verify_release_history(
                session,
                release=release,
                cache=cache,
                expected_history=expected,
                selected_digests={**selected, page: "0" * 64},
            )
        with pytest.raises(ValueError, match="changed received history"):
            verify_release_history(
                session,
                release=release,
                cache=cache,
                expected_history={**expected, (2026, 9): "0" * 64},
                selected_digests=selected,
            )
