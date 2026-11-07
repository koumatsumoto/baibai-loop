"""小fixtureで本文入りSQLを使わない候補を比較する。通常配線はまだ変更しない。"""

import io
import sqlite3
import struct
import zipfile
import zlib
from contextlib import closing
from datetime import UTC, date, datetime

import pytest
from tests.engine.machine_analysis.observations.financials.test_edinet_holder_capital import (
    original,
)
from tests.engine.machine_analysis.observations.financials.test_edinet_holder_native import (
    DAY,
    KNOWN,
    native_for,
    seed,
)

from baibai_engine.data_collection.providers.edinet.archive_native import FixedEdinetArchives
from baibai_engine.machine_analysis.observations.financials import edinet_holder_capital as holder
from baibai_engine.primitives.time import JST
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def prepare(monkeypatch, connection, archives, *, as_of=DAY, at=None):
    prepared_at = at or datetime(2026, 11, 3, 6, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return prepared_at

    with monkeypatch.context() as patch:
        patch.setattr(holder, "datetime", Clock)
        return holder.prepare_holder_capital(connection, as_of=as_of, archives=archives)


def read(connection, *, archives=None, prepared=None, as_of=DAY, known_at=KNOWN, seal=None):
    return holder.read_holder_capital(
        connection,
        as_of=as_of,
        known_at=known_at,
        tickers=("1234",),
        snapshot_sealed_at=seal,
        archives=archives,
        prepared_capital=prepared,
    )


@pytest.mark.parametrize(
    ("stored_on", "as_of", "known", "seal", "accepted"),
    [
        ("2026-11-02", "2026-11-03", "2026-11-03T12:00Z", "2026-11-03T07:00Z", True),
        ("2026-11-03", "2026-11-03", "2026-11-03T12:00Z", "2026-11-03T07:00Z", True),
        ("2026-11-03", "2026-11-03", "2026-11-03T12:00Z", "2026-11-03T12:00Z", True),
        ("2026-11-03", "2026-11-03", "2026-11-03T12:00Z", "2026-11-03T13:00Z", False),
        ("2026-11-03", "2026-11-03", "2026-11-04T01:00Z", "2026-11-03T16:00Z", False),
        ("2026-11-02", "2026-11-02", "2026-11-03T12:00Z", "2026-11-03T07:00Z", False),
    ],
)
def test_rawless_read_has_the_existing_day_and_seal_boundary(
    monkeypatch, stored_on, as_of, known, seal, accepted
):
    as_of = date.fromisoformat(as_of)
    known = datetime.fromisoformat(known)
    seal = datetime.fromisoformat(seal)
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection, stored_on=stored_on)
        store, archives = native_for({"S100TEST": body})
        value = archives.value()
        value["archives"][0]["stored_on"] = stored_on
        archives = FixedEdinetArchives(store, value)
        expected = read(connection, as_of=as_of, known_at=known, seal=seal)
        prepared = prepare(monkeypatch, connection, archives, as_of=as_of)
        # Fixture only: the new path must not depend on a SQL BLOB or its table.
        connection.execute("DROP TABLE edinet_source_archives")
        queries = []
        connection.set_trace_callback(queries.append)
        actual = read(
            connection,
            archives=archives,
            prepared=prepared,
            as_of=as_of,
            known_at=known,
            seal=seal,
        )
        assert actual == expected
        assert bool(actual[0]) is accepted
        assert len(store.get_keys) == 1
        assert store.put_keys == []
        assert not any("edinet_source_archives" in query for query in queries)


@pytest.mark.parametrize("mode", ["partial", "all_missing", "bad_zip"])
def test_normal_partial_does_not_become_unavailable(monkeypatch, mode):
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection, body=b"not a ZIP" if mode == "bad_zip" else None)
        seed(connection, doc_id="S100MISS", seq=2, retained=False)
        if mode == "all_missing":
            connection.execute("DELETE FROM edinet_source_archives")
        store, archives = native_for({} if mode == "all_missing" else {"S100TEST": body})
        seal = datetime(2026, 11, 3, 7, tzinfo=UTC)
        expected = read(connection, seal=seal)
        prepared = prepare(monkeypatch, connection, archives)
        connection.execute("DROP TABLE edinet_source_archives")
        assert read(connection, archives=archives, prepared=prepared, seal=seal) == expected
        assert expected[1] == "partial"
        assert len(store.get_keys) == (0 if mode == "all_missing" else 1)


@pytest.mark.parametrize("mismatch", ["archive", "projection", "late", "no_seal"])
def test_preparation_is_bound_to_the_actual_evidence_and_precedes_seal(monkeypatch, mismatch):
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection)
        store, archives = native_for({"S100TEST": body})
        prepared = prepare(monkeypatch, connection, archives)
        seal = datetime(2026, 11, 3, 7, tzinfo=UTC)
        if mismatch == "archive":
            changed = archives.value()
            changed["archives"][0]["stored_on"] = "2026-11-01"
            archives = FixedEdinetArchives(store, changed)
        elif mismatch == "projection":
            connection.execute(
                "UPDATE edinet_document_lists SET selected_received_at_utc='2026-10-31T06:00:00Z'"
            )
        elif mismatch == "late":
            seal = datetime(2026, 11, 3, 5, tzinfo=UTC)
        else:
            seal = None
        with pytest.raises(ValueError, match="prepared holder originals differ"):
            read(connection, archives=archives, prepared=prepared, seal=seal)


@pytest.mark.parametrize("eligible", [True, False])
def test_prefetch_damage_is_only_raised_if_final_selection_uses_the_document(monkeypatch, eligible):
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection)
        if not eligible:
            connection.execute("UPDATE edinet_documents SET submit_datetime='2026-11-04 10:00'")
        store, archives = native_for({"S100TEST": body})
        store.values.clear()
        prepared = prepare(monkeypatch, connection, archives)
        connection.execute("DROP TABLE edinet_source_archives")
        args = dict(archives=archives, prepared=prepared, seal=datetime(2026, 11, 3, 7, tzinfo=UTC))
        if eligible:
            with pytest.raises(LakePublishError):
                read(connection, **args)
        else:
            assert read(connection, **args) == ({}, "all_retained_considered")


def test_prepared_content_needs_no_second_get_and_is_bounded_to_holder_candidates(monkeypatch):
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection)
        store, archives = native_for({"S100TEST": body, "S100OTHER": b"unrelated annual ZIP"})
        prepared = prepare(monkeypatch, connection, archives)
        store.values.clear()
        connection.execute("DROP TABLE edinet_source_archives")
        observed, coverage = read(
            connection,
            archives=archives,
            prepared=prepared,
            seal=datetime(2026, 11, 3, 7, tzinfo=UTC),
        )
        assert observed["1234"]
        assert coverage == "all_retained_considered"
        assert len(store.get_keys) == 1
        assert set(prepared.results) == {"S100TEST"}
        assert prepared.results["S100TEST"][1].total_stocks == 2500000


def test_earlier_native_day_still_makes_intervening_edit_unproven(monkeypatch):
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection, stored_on="2026-11-04", edit_day="2026-11-03")
        as_of = date(2026, 11, 5)
        known_at = datetime(2026, 11, 5, 8, tzinfo=UTC)
        seal = datetime(2026, 11, 5, 7, tzinfo=UTC)
        assert read(connection, as_of=as_of, known_at=known_at, seal=seal)[0]
        _store, archives = native_for({"S100TEST": body})
        prepared = prepare(monkeypatch, connection, archives, as_of=as_of)
        connection.execute("DROP TABLE edinet_source_archives")
        assert read(
            connection,
            archives=archives,
            prepared=prepared,
            as_of=as_of,
            known_at=known_at,
            seal=seal,
        ) == ({}, "partial")


def test_preparation_and_read_accept_the_generation_read_only_row_factory(tmp_path, monkeypatch):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as connection:
        body = seed(connection)
    _store, archives = native_for({"S100TEST": body})
    with closing(connect_read_only(path)) as connection:
        assert connection.row_factory is sqlite3.Row
        prepared = prepare(monkeypatch, connection, archives)
        observed, coverage = read(
            connection,
            archives=archives,
            prepared=prepared,
            seal=datetime(2026, 11, 3, 7, tzinfo=UTC),
        )
        assert observed["1234"]
        assert coverage == "all_retained_considered"


def test_real_seal_can_hold_only_the_list_projection_not_archive_bytes(tmp_path):
    path = tmp_path / "market.sqlite"
    now = datetime.now(UTC)
    as_of = now.astimezone(JST).date()
    with closing(open_connection(path)) as connection:
        body = seed(connection, stored_on=now.date().isoformat())
        store, archives = native_for({"S100TEST": body})
        value = archives.value()
        value["archives"][0]["stored_on"] = now.date().isoformat()
        archives = FixedEdinetArchives(store, value)
        prepared = holder.prepare_holder_capital(connection, as_of=as_of, archives=archives)
        # Only this fixture copy is changed. The native original remains intact.
        connection.execute("DELETE FROM edinet_source_archives")
        connection.commit()
    with (
        sealed_sqlite_snapshot(sqlite_path=path, mirror_root=tmp_path / "mirror") as snapshot,
        closing(sqlite3.connect(snapshot.path)) as connection,
    ):
        assert connection.execute("SELECT count(*) FROM edinet_source_archives").fetchone() == (0,)
        observed, coverage = read(
            connection,
            archives=archives,
            prepared=prepared,
            as_of=as_of,
            known_at=snapshot.sealed_at,
            seal=snapshot.sealed_at,
        )
        assert observed["1234"][0].total_stocks == 2500000
        assert coverage == "all_retained_considered"
        assert prepared.completed_at <= snapshot.sealed_at
    assert len(store.values) == 1
    assert len(store.get_keys) == 1
    assert store.put_keys == []


class TrackedBytes(bytes):
    """Track only the reader-returned payload lifetime, not process RSS."""

    def __new__(cls, content, tracker):
        result = super().__new__(cls, content)
        result.tracker = tracker
        tracker["count"] += 1
        tracker["bytes"] += len(result)
        tracker["peak_count"] = max(tracker["peak_count"], tracker["count"])
        tracker["peak_bytes"] = max(tracker["peak_bytes"], tracker["bytes"])
        return result

    def __del__(self):
        self.tracker["count"] -= 1
        self.tracker["bytes"] -= len(self)


def track_payloads(monkeypatch, archives):
    tracker = dict.fromkeys(("count", "bytes", "peak_count", "peak_bytes"), 0)
    read_original = archives.read_xbrl_zip
    monkeypatch.setattr(
        archives, "read_xbrl_zip", lambda doc_id: TrackedBytes(read_original(doc_id), tracker)
    )
    return tracker


def test_multiple_preparsed_originals_retain_one_payload_then_only_small_results(monkeypatch):
    first = original()
    padded = io.BytesIO(first)
    with zipfile.ZipFile(padded, "a") as archive:
        archive.writestr("unused.bin", b"x" * 4096)
    bodies = {"S100AAAA": first, "S100BBBB": padded.getvalue(), "S100CCCC": first}
    with closing(sqlite3.connect(":memory:")) as connection:
        for seq, (doc_id, body) in enumerate(bodies.items(), 1):
            seed(connection, doc_id=doc_id, seq=seq, body=body)
        seal = datetime(2026, 11, 3, 7, tzinfo=UTC)
        expected = read(connection, seal=seal)
        store, archives = native_for(bodies)
        tracker = track_payloads(monkeypatch, archives)
        prepared = prepare(monkeypatch, connection, archives)
        assert tracker == {
            "count": 0,
            "bytes": 0,
            "peak_count": 1,
            "peak_bytes": max(map(len, bodies.values())),
        }
        assert all(
            isinstance(value, holder._HolderCapitalContent)
            for _stored_on, value in prepared.results.values()
        )
        connection.execute("DROP TABLE edinet_source_archives")
        assert read(connection, archives=archives, prepared=prepared, seal=seal) == expected
        assert len(store.get_keys) == 3
        assert store.put_keys == []
        assert len(store.values) == 2


@pytest.mark.parametrize("kind", [LakePublishError, OSError, ValueError, holder.SourceFormatError])
def test_preparsed_failure_keeps_no_reader_buffer_or_traceback(monkeypatch, kind):
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection)
        _store, archives = native_for({"S100TEST": body})
        tracker = track_payloads(monkeypatch, archives)

        def fail(content):
            assert isinstance(content, TrackedBytes)
            raise kind("fixture failure")

        monkeypatch.setattr(holder, "_extract_holder_content", fail)
        prepared = prepare(monkeypatch, connection, archives)
        assert tracker["bytes"] == tracker["count"] == 0
        result = prepared.results["S100TEST"][1]
        args = dict(archives=archives, prepared=prepared, seal=datetime(2026, 11, 3, 7, tzinfo=UTC))
        if kind is holder.SourceFormatError:
            assert result is None
            assert read(connection, **args) == ({}, "partial")
        else:
            assert type(result) is kind
            with pytest.raises(kind, match="fixture failure"):
                read(connection, **args)
            assert result.__traceback__ is result.__context__ is result.__cause__ is None


def test_preparsed_reference_date_is_still_bounded_by_the_final_submission(monkeypatch):
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection, body=original(period="2026-11-01"))
        _store, archives = native_for({"S100TEST": body})
        seal = datetime(2026, 11, 3, 7, tzinfo=UTC)
        expected = read(connection, seal=seal)
        prepared = prepare(monkeypatch, connection, archives)
        assert prepared.results["S100TEST"][1].reference_date == date(2026, 11, 1)
        assert read(connection, archives=archives, prepared=prepared, seal=seal) == expected
        assert expected == ({}, "partial")


@pytest.mark.parametrize("excluded_by", ["submitted", "withdrawn", "receipt"])
def test_preparsed_unneeded_failure_does_not_hide_another_good_document(monkeypatch, excluded_by):
    with closing(sqlite3.connect(":memory:")) as connection:
        good = seed(connection, doc_id="S100GOOD")
        bad = seed(connection, doc_id="S100FAIL", seq=2, issuer="5678", retained=False)
        if excluded_by == "submitted":
            connection.execute(
                "UPDATE edinet_documents SET submit_datetime='2026-11-04 10:00' "
                "WHERE doc_id='S100FAIL'"
            )
        elif excluded_by == "withdrawn":
            connection.execute(
                "UPDATE edinet_documents SET withdrawal_status='1' WHERE doc_id='S100FAIL'"
            )
        else:
            connection.execute(
                "UPDATE edinet_documents SET doc_date='2026-11-01' WHERE doc_id='S100FAIL'"
            )
            connection.execute(
                "INSERT INTO edinet_document_lists "
                "(doc_date,result_count,selected_received_at_utc,latest_received_at_utc) "
                "VALUES ('2026-11-01',1,'2026-11-04T06:00:00Z','2026-11-04T06:00:00Z')"
            )
        seal = datetime(2026, 11, 3, 7, tzinfo=UTC)
        expected = read(connection, seal=seal)
        store, archives = native_for({"S100GOOD": good, "S100FAIL": bad})
        metadata = archives.metadata("S100FAIL", 1)
        del store.values[f"l1/edinet/archives/v1/objects/{metadata.sha256}.zip"]
        prepared = prepare(monkeypatch, connection, archives)
        assert isinstance(prepared.results["S100FAIL"][1], LakePublishError)
        connection.execute("DROP TABLE edinet_source_archives")
        assert read(connection, archives=archives, prepared=prepared, seal=seal) == expected
        assert expected[0]["1234"]


def damaged_original(kind):
    body = bytearray(original())
    local = body.index(b"PK\x03\x04")
    central = body.index(b"PK\x01\x02")
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        info = archive.infolist()[0]
        xml = archive.read(info)
        offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    if kind == "crc":
        body[offset + 20] ^= 1
    elif kind == "encrypted":
        struct.pack_into("<H", body, local + 6, 1)
        struct.pack_into("<H", body, central + 8, 1)
    elif kind == "compression":
        struct.pack_into("<H", body, local + 8, 99)
        struct.pack_into("<H", body, central + 10, 99)
    else:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(info.filename, xml)
        body = bytearray(out.getvalue())
        body[30 + len(info.filename.encode())] = 255
    return bytes(body)


@pytest.mark.parametrize(
    ("damage", "failure"),
    [
        ("crc", zipfile.BadZipFile),
        ("encrypted", RuntimeError),
        ("compression", NotImplementedError),
        ("deflate", zlib.error),
    ],
)
@pytest.mark.parametrize("eligible", [True, False])
def test_zip_read_errors_wait_for_eligibility_without_retaining_failed_payload(
    monkeypatch, damage, failure, eligible
):
    with closing(sqlite3.connect(":memory:")) as connection:
        good = seed(connection, doc_id="S100GOOD")
        bad = seed(connection, doc_id="S100FAIL", seq=2, body=damaged_original(damage))
        if not eligible:
            connection.execute(
                "UPDATE edinet_documents SET withdrawal_status='1' WHERE doc_id='S100FAIL'"
            )
        seal = datetime(2026, 11, 3, 7, tzinfo=UTC)
        if eligible:
            with pytest.raises(failure) as expected_error:
                read(connection, seal=seal)
        else:
            expected = read(connection, seal=seal)
        store, archives = native_for({"S100GOOD": good, "S100FAIL": bad})
        tracker = track_payloads(monkeypatch, archives)
        prepared = prepare(monkeypatch, connection, archives)
        result = prepared.results["S100FAIL"][1]
        assert type(result) is failure
        assert tracker["count"] == tracker["bytes"] == 0
        assert tracker["peak_count"] == 1
        connection.execute("DROP TABLE edinet_source_archives")
        if eligible:
            with pytest.raises(failure) as actual_error:
                read(connection, archives=archives, prepared=prepared, seal=seal)
            assert str(actual_error.value) == str(expected_error.value)
        else:
            assert read(connection, archives=archives, prepared=prepared, seal=seal) == expected
            assert expected[0]["1234"]
        assert result.__traceback__ is result.__context__ is result.__cause__ is None
        assert len(store.get_keys) == 2


@pytest.mark.parametrize("period", ["2026-10-28", "2026-11-01"])
@pytest.mark.parametrize("unit", ["xbrli:shares", "xbrli:pure"])
def test_stock_conversion_error_waits_for_the_reference_date_guard(monkeypatch, period, unit):
    facts = (
        '<h:SecurityCodeOfIssuer contextRef="FilingDateInstant">1234</h:SecurityCodeOfIssuer>'
        '<h:TotalNumberOfOutstandingStocksEtc contextRef="FilingDateInstant" '
        'unitRef="u1" decimals="0">' + "1" * 4400 + "</h:TotalNumberOfOutstandingStocksEtc>"
    )
    with closing(sqlite3.connect(":memory:")) as connection:
        body = seed(connection, body=original(period=period, unit=unit, facts=facts))
        _store, archives = native_for({"S100TEST": body})
        tracker = track_payloads(monkeypatch, archives)
        prepared = prepare(monkeypatch, connection, archives)
        result = prepared.results["S100TEST"][1]
        assert isinstance(result.total_stocks, ValueError)
        assert tracker["count"] == tracker["bytes"] == 0
        args = dict(archives=archives, prepared=prepared, seal=datetime(2026, 11, 3, 7, tzinfo=UTC))
        if period == "2026-11-01":
            assert read(connection, **args) == ({}, "partial")
        else:
            with pytest.raises(ValueError, match="integer string conversion"):
                read(connection, **args)
        assert result.total_stocks.__traceback__ is None
