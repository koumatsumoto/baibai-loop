from __future__ import annotations

import gzip
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from baibai_engine.collection.jpx import http as jpx_http
from baibai_engine.collection.jpx import receipts as jpx_receipts
from baibai_engine.collection.jpx.models import JPXAcquisitionError, JPXResponse
from baibai_engine.collection.jpx.receipts import (
    JPXBundleError,
    complete_bundle,
    create_bundle,
    declare_bundle_resources,
    read_bundle,
    read_complete_bundle,
    retain_bundle_response,
)
from baibai_engine.storage.observations.schema import open_connection

INDEX = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html"
FILE = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/kessan.xlsx"
START = datetime(2026, 10, 14, 23, 59, tzinfo=UTC)
RECEIVED = datetime(2026, 10, 15, 0, 1, tzinfo=UTC)


def _response(url: str = INDEX, body: bytes = b"\x00\xffmalformed") -> JPXResponse:
    return JPXResponse(url, RECEIVED, "application/vnd.ms-excel", body)


class _HTTPResponse:
    def __init__(self, events: list[str], *, status: int = 200, fail_body: bool = False) -> None:
        self.events = events
        self.status_code = status
        self.headers = {"content-type": "application/vnd.ms-excel"}
        self.fail_body = fail_body

    @property
    def content(self) -> bytes:
        self.events.append("body")
        if self.fail_body:
            raise requests.exceptions.ContentDecodingError("body read failed")
        return b"\x00\xffnot-an-excel-file"

    def close(self) -> None:
        self.events.append("response-close")


def test_http_validates_origin_before_lazy_session_and_closes_owned_session() -> None:
    events: list[str] = []
    response = _HTTPResponse(events)
    session = Mock()
    session.get.return_value = response
    with patch.object(jpx_http.requests, "Session", return_value=session) as factory:
        client = jpx_http.JPXHTTP()
        factory.assert_not_called()
        with pytest.raises(JPXAcquisitionError, match=r"https://www\.jpx\.co\.jp"):
            client.fetch("https://example.com/x")
        factory.assert_not_called()
        result = client.fetch(FILE)
        factory.assert_called_once_with()
        session.get.assert_called_once_with(FILE, timeout=30)
        client.close()
        client.close()
    assert result.requested_url == FILE
    assert result.body == b"\x00\xffnot-an-excel-file"
    assert result.content_type == "application/vnd.ms-excel"
    assert events == ["body", "response-close"]
    session.close.assert_called_once_with()


def test_http_captures_utc_after_body_read_and_closes_injected_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    response = _HTTPResponse(events)
    session = Mock()
    session.get.return_value = response

    def now(zone: object) -> datetime:
        assert zone is UTC
        events.append("capture")
        return RECEIVED

    monkeypatch.setattr(jpx_http, "datetime", SimpleNamespace(now=now))
    client = jpx_http.JPXHTTP(session)
    result = client.fetch(INDEX)
    client.close()
    assert result.received_at == RECEIVED
    assert events == ["body", "capture", "response-close"]
    session.close.assert_not_called()


@pytest.mark.parametrize("status", [302, 404, 503])
def test_http_non_200_closes_without_reading_body(status: int) -> None:
    events: list[str] = []
    session = Mock()
    session.get.return_value = _HTTPResponse(events, status=status)
    with pytest.raises(JPXAcquisitionError, match=f"HTTP {status}"):
        jpx_http.JPXHTTP(session).fetch(INDEX)
    assert events == ["response-close"]


def test_http_body_read_failure_closes_response() -> None:
    events: list[str] = []
    session = Mock()
    session.get.return_value = _HTTPResponse(events, fail_body=True)
    with pytest.raises(JPXAcquisitionError, match="source request failed"):
        jpx_http.JPXHTTP(session).fetch(INDEX)
    assert events == ["body", "response-close"]


def test_http_get_failure_is_acquisition_error() -> None:
    session = Mock()
    session.get.side_effect = requests.Timeout("no response")
    with pytest.raises(JPXAcquisitionError, match="source request failed"):
        jpx_http.JPXHTTP(session).fetch(INDEX)


def test_declared_bundle_requires_exact_set_and_preserves_malformed_body(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "market.sqlite")
    try:
        bundle_id = create_bundle(
            conn,
            "earnings_calendar",
            date(2026, 10, 15),
            START,
            expected_resources=(("index", INDEX), ("workbook", FILE)),
        )
        retain_bundle_response(conn, bundle_id, "index", _response())
        with pytest.raises(JPXBundleError, match="missing or unexpected"):
            complete_bundle(conn, bundle_id)
        with pytest.raises(JPXBundleError, match="incomplete"):
            read_complete_bundle(conn, bundle_id)
        with pytest.raises(JPXBundleError, match="not declared"):
            retain_bundle_response(conn, bundle_id, "extra", _response(FILE))
        with pytest.raises(JPXBundleError, match="already retained"):
            retain_bundle_response(conn, bundle_id, "index", _response())
        retain_bundle_response(
            conn,
            bundle_id,
            "workbook",
            JPXResponse(
                FILE,
                RECEIVED + timedelta(seconds=17),
                "application/vnd.ms-excel",
                b"\x00\xffmalformed",
            ),
        )
        complete_bundle(conn, bundle_id)
        bundle = read_complete_bundle(conn, bundle_id)
        assert bundle.started_at == START
        assert bundle.completed_at == RECEIVED + timedelta(seconds=17)
        assert bundle.expected_resources == (("index", INDEX), ("workbook", FILE))
        assert [item.response.body for item in bundle.responses] == [
            b"\x00\xffmalformed",
            b"\x00\xffmalformed",
        ]
        rows = conn.execute(
            "SELECT started_on FROM jpx_source_bundles WHERE bundle_id = ?", (bundle_id,)
        ).fetchone()
        assert rows == ("2026-10-14",)
        response_days = conn.execute(
            "SELECT DISTINCT received_on FROM jpx_source_responses WHERE bundle_id = ?",
            (bundle_id,),
        ).fetchall()
        assert response_days == [("2026-10-15",)]
        compressed = conn.execute(
            "SELECT body_gzip FROM jpx_source_responses WHERE bundle_id = ? AND resource_name = 'index'",
            (bundle_id,),
        ).fetchone()[0]
        assert compressed == gzip.compress(b"\x00\xffmalformed", mtime=0)
        with pytest.raises(JPXBundleError, match="immutable"):
            retain_bundle_response(conn, bundle_id, "index", _response())
        with pytest.raises(JPXBundleError, match="immutable"):
            complete_bundle(conn, bundle_id)
        with pytest.raises(JPXBundleError, match="cannot be changed"):
            declare_bundle_resources(conn, bundle_id, (("index", INDEX),))
    finally:
        conn.close()


def test_dynamic_bundle_requires_index_and_declaration_before_completion(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "market.sqlite")
    try:
        bundle_id = create_bundle(conn, "earnings_calendar", date(2026, 10, 15), START)
        with pytest.raises(JPXBundleError, match="no declared resources"):
            complete_bundle(conn, bundle_id)
        with pytest.raises(JPXBundleError, match="only the index"):
            retain_bundle_response(conn, bundle_id, "workbook", _response(FILE))
        retain_bundle_response(conn, bundle_id, "index", _response())
        assert read_bundle(conn, bundle_id).expected_resources is None
        with pytest.raises(JPXBundleError, match="include retained index"):
            declare_bundle_resources(conn, bundle_id, (("workbook", FILE),))
        with pytest.raises(JPXBundleError, match="include retained index"):
            declare_bundle_resources(conn, bundle_id, (("index", FILE),))
        declare_bundle_resources(conn, bundle_id, (("index", INDEX), ("workbook", FILE)))
        with pytest.raises(JPXBundleError, match="cannot be changed"):
            declare_bundle_resources(conn, bundle_id, (("index", INDEX),))
        retain_bundle_response(conn, bundle_id, "workbook", _response(FILE))
        complete_bundle(conn, bundle_id)
        assert read_complete_bundle(conn, bundle_id).completed_at == RECEIVED
    finally:
        conn.close()


def test_bundle_rejects_pending_transaction_times_and_manifest_duplicates(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "market.sqlite")
    try:
        conn.execute("INSERT INTO jpx_regulation_sources VALUES ('2026-10-15', 'x', 'now')")
        with pytest.raises(JPXBundleError, match="pending transaction"):
            create_bundle(conn, "regulations", date(2026, 10, 15), START)
        conn.rollback()
        with pytest.raises(JPXBundleError, match="must be UTC"):
            create_bundle(conn, "regulations", date(2026, 10, 15), START.replace(tzinfo=None))
        for resources in ((), (("a", INDEX), ("a", FILE)), (("a", INDEX), ("b", INDEX))):
            with pytest.raises(JPXBundleError, match=r"nonempty|unique"):
                create_bundle(conn, "regulations", date(2026, 10, 15), START, resources)
        bundle_id = create_bundle(
            conn, "regulations", date(2026, 10, 15), START, (("index", INDEX),)
        )
        conn.execute("INSERT INTO jpx_regulation_sources VALUES ('2026-10-15', 'y', 'now')")
        for operation in (
            lambda: retain_bundle_response(conn, bundle_id, "index", _response()),
            lambda: complete_bundle(conn, bundle_id),
            lambda: declare_bundle_resources(conn, bundle_id, (("index", INDEX),)),
        ):
            with pytest.raises(JPXBundleError, match="pending transaction"):
                operation()
        conn.rollback()
        with pytest.raises(JPXBundleError, match="precedes bundle start"):
            retain_bundle_response(
                conn, bundle_id, "index", JPXResponse(INDEX, START - timedelta(seconds=1), "", b"")
            )
        with pytest.raises(JPXBundleError, match="must be UTC"):
            retain_bundle_response(
                conn, bundle_id, "index", JPXResponse(INDEX, RECEIVED.replace(tzinfo=None), "", b"")
            )
    finally:
        conn.close()


def test_write_error_rolls_back_and_read_rejects_corrupt_or_missing(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "market.sqlite")
    try:
        with pytest.raises(JPXBundleError, match="not retained"):
            read_bundle(conn, "missing")
        bundle_id = create_bundle(
            conn, "regulations", date(2026, 10, 15), START, (("index", INDEX),)
        )
        conn.execute(
            "CREATE TRIGGER block_jpx_receipt BEFORE INSERT ON jpx_source_responses "
            "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="blocked"):
            retain_bundle_response(conn, bundle_id, "index", _response())
        assert not conn.in_transaction
        assert read_bundle(conn, bundle_id).responses == ()
        conn.execute("DROP TRIGGER block_jpx_receipt")
        conn.commit()
        retain_bundle_response(conn, bundle_id, "index", _response())
        conn.execute(
            "UPDATE jpx_source_responses SET requested_url = ? WHERE bundle_id = ?",
            (FILE, bundle_id),
        )
        conn.commit()
        with pytest.raises(JPXBundleError, match="differs from declared"):
            read_complete_bundle(conn, bundle_id)
        conn.execute(
            "UPDATE jpx_source_responses SET requested_url = ? WHERE bundle_id = ?",
            (INDEX, bundle_id),
        )
        conn.commit()
        complete_bundle(conn, bundle_id)
        conn.execute("DELETE FROM jpx_source_responses WHERE bundle_id = ?", (bundle_id,))
        conn.commit()
        with pytest.raises(JPXBundleError, match="missing or unexpected"):
            read_complete_bundle(conn, bundle_id)
    finally:
        conn.close()


def test_offline_jpx_models_and_receipts_do_not_import_requests() -> None:
    import subprocess
    import sys

    script = (
        "import sys; "
        "import baibai_engine.collection.jpx.models; "
        "import baibai_engine.collection.jpx.receipts; "
        "assert 'requests' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", script], check=True)


def test_declaration_and_completion_from_another_connection_cannot_be_replaced(
    tmp_path: Path,
) -> None:
    path = tmp_path / "market.sqlite"
    first = open_connection(path)
    second = sqlite3.connect(path)
    try:
        bundle_id = create_bundle(first, "earnings_calendar", date(2026, 10, 15), START)
        retain_bundle_response(second, bundle_id, "index", _response())
        declare_bundle_resources(second, bundle_id, (("index", INDEX),))
        assert not second.in_transaction
        with pytest.raises(JPXBundleError, match="cannot be changed"):
            declare_bundle_resources(first, bundle_id, (("index", INDEX), ("workbook", FILE)))
        assert not first.in_transaction
        complete_bundle(second, bundle_id)
        assert not second.in_transaction
        with pytest.raises(JPXBundleError, match="cannot be changed"):
            declare_bundle_resources(first, bundle_id, (("index", INDEX),))
        with pytest.raises(JPXBundleError, match="immutable"):
            retain_bundle_response(first, bundle_id, "index", _response())
        with pytest.raises(JPXBundleError, match="immutable"):
            complete_bundle(first, bundle_id)
        assert not first.in_transaction
    finally:
        first.close()
        second.close()


def test_writer_lock_prevents_manifest_change_between_receipt_read_and_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "market.sqlite"
    first = open_connection(path)
    second = sqlite3.connect(path, timeout=0)
    try:
        bundle_id = create_bundle(first, "earnings_calendar", date(2026, 10, 15), START)
        original = jpx_receipts._read_bundle
        raced = False

        def read_with_competing_declaration(conn: sqlite3.Connection, item_id: str):
            nonlocal raced
            if conn is first and not raced:
                raced = True
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    declare_bundle_resources(second, item_id, (("index", FILE),))
                assert not second.in_transaction
            return original(conn, item_id)

        monkeypatch.setattr(jpx_receipts, "_read_bundle", read_with_competing_declaration)
        retain_bundle_response(first, bundle_id, "index", _response())
        assert raced
        assert not first.in_transaction
        with pytest.raises(JPXBundleError, match="include retained index"):
            declare_bundle_resources(second, bundle_id, (("index", FILE),))
        assert not second.in_transaction
        declare_bundle_resources(second, bundle_id, (("index", INDEX),))
        assert not second.in_transaction
    finally:
        first.close()
        second.close()


def test_read_bundle_owns_only_its_own_sqlite_read_transaction(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "market.sqlite")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        bundle_id = create_bundle(conn, "regulations", date(2026, 10, 15), START)
        statements.clear()
        assert read_bundle(conn, bundle_id).bundle_id == bundle_id
        assert not conn.in_transaction
        assert "BEGIN" in statements
        assert "COMMIT" in statements

        statements.clear()
        with pytest.raises(JPXBundleError, match="not retained"):
            read_bundle(conn, "missing")
        assert not conn.in_transaction
        assert "BEGIN" in statements
        assert "ROLLBACK" in statements

        conn.execute("INSERT INTO jpx_regulation_sources VALUES ('2026-10-15', 'held', 'now')")
        assert conn.in_transaction
        statements.clear()
        assert read_bundle(conn, bundle_id).bundle_id == bundle_id
        with pytest.raises(JPXBundleError, match="not retained"):
            read_bundle(conn, "missing")
        assert conn.in_transaction
        assert "BEGIN" not in statements
        assert "COMMIT" not in statements
        assert "ROLLBACK" not in statements
        assert conn.execute(
            "SELECT COUNT(*) FROM jpx_regulation_sources WHERE source_name = 'held'"
        ).fetchone() == (1,)
        conn.rollback()
        assert not conn.in_transaction
    finally:
        conn.close()
