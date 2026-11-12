"""The native list cutover keeps explicit arbitrary-window CSV acquisition."""

from __future__ import annotations

import io
import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from tests.helpers.edinet_metrics import _edinet_csv_zip, _edinet_document
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.edinet.archive_native import EdinetArchiveL1
from baibai_engine.data_collection.providers.edinet.archives import read_source_archive
from baibai_engine.data_collection.providers.edinet.list_native import (
    EdinetListL1,
    NativeListReceipt,
)
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.machine_analysis.observations.financials.edinet_reader import EDINETReader
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.maintenance import edinet_extraction as refresh
from baibai_jobs.maintenance.source_refresh.edinet import EDINETRefresh

DAY = date(2026, 11, 2)
AT = datetime(2026, 11, 2, tzinfo=UTC)


def native_input(tmp_path, monkeypatch, lookback, *, include_bad_old=True):
    start = DAY - timedelta(days=lookback)
    document = {
        **_edinet_document(ticker="72030", doc_id="S100NATIVE"),
        "seqNumber": 1,
        "submitDateTime": start.isoformat() + " 12:00",
    }
    receipts = []
    for offset in range(lookback + 1):
        day = DAY - timedelta(days=offset)
        rows = [document] if day == start else []
        body = json.dumps(
            {
                "metadata": {
                    "parameter": {"date": day.isoformat()},
                    "resultset": {"count": len(rows)},
                },
                "results": rows,
            }
        ).encode()
        receipts.append(
            NativeListReceipt.from_response(
                day, EdinetResponse(AT + timedelta(seconds=offset), body)
            )
        )
    # Unrelated malformed old input cannot globally stop this requested window.
    if include_bad_old:
        receipts.append(
            NativeListReceipt.from_response(
                start - timedelta(days=1), EdinetResponse(AT, b"bad JSON")
            )
        )
    store = MemoryR2Store()
    owner = EdinetListL1(store, tmp_path / "native")
    owner.freeze(False)
    owner.publish(receipts)
    store.close = lambda: None
    monkeypatch.setattr(refresh, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(
        "baibai_jobs.maintenance.source_refresh.edinet.Boto3R2Store", lambda **_: store
    )
    EdinetArchiveL1(store, tmp_path / "archive-native").initialize()
    return store, owner


@pytest.mark.parametrize("lookback", [0, 2, 600])
@pytest.mark.parametrize("fail_l2", [False, True])
def test_native_refresh_preserves_window_csv_bytes_and_reuse(
    tmp_path, monkeypatch, lookback, fail_l2
):
    store, owner = native_input(tmp_path, monkeypatch, lookback)
    database = tmp_path / "market.sqlite"
    zip_content = _edinet_csv_zip()
    calls = []

    def fetch(doc_id, kind):
        calls.append((doc_id, kind))
        return EdinetResponse(AT, zip_content)

    acquisition = EDINETRefresh("synthetic", sqlite_path=database)
    acquisition._http = SimpleNamespace(fetch_archive=fetch)
    reader = EDINETReader(database)
    if fail_l2:
        monkeypatch.setattr(
            reader, "list_documents", lambda _: (_ for _ in ()).throw(ValueError("L2 broken"))
        )
    output = io.StringIO()
    assert refresh.refresh_edinet_metrics_command(
        asof_date=DAY,
        lookback_days=lookback,
        reader=reader,
        acquisition=acquisition,
        sqlite_path=database,
        stdout=output,
    ) == int(fail_l2)
    assert calls == [("S100NATIVE", 5)]
    fixed = EdinetArchiveL1(store, tmp_path / "read").freeze()
    assert fixed.read_csv_zip("S100NATIVE") == zip_content
    assert fixed.metadata("S100NATIVE", 5).received_at_utc == AT
    with closing(open_connection(database)) as conn:
        assert read_source_archive(conn, "S100NATIVE", 5) == zip_content
        assert (
            conn.execute("SELECT stored_on FROM edinet_source_archives").fetchone()[0]
            == DAY.isoformat()
        )
    # Same actual original is reused even while the downstream L2 remains broken.
    assert refresh.refresh_edinet_metrics_command(
        asof_date=DAY,
        lookback_days=lookback,
        reader=reader,
        acquisition=acquisition,
        sqlite_path=database,
        stdout=io.StringIO(),
    ) == int(fail_l2)
    assert calls == [("S100NATIVE", 5)]
    assert json.loads(output.getvalue().splitlines()[0])["native_list_input"]["failed_days"]
    assert len(tuple(owner.freeze().iter_receipts())) == lookback + 2


def test_native_refresh_missing_head_does_not_fall_back_to_legacy_list_writer(
    tmp_path, monkeypatch
):
    store = MemoryR2Store()
    store.close = lambda: None
    monkeypatch.setattr(refresh, "Boto3R2Store", lambda **_: store)
    calls = []
    acquisition = SimpleNamespace(
        list_documents=lambda day: calls.append(day),
        download_csv_zip=lambda value: calls.append(value),
    )
    assert (
        refresh.refresh_edinet_metrics_command(
            asof_date=DAY,
            lookback_days=600,
            reader=EDINETReader(tmp_path / "market.sqlite"),
            acquisition=acquisition,
            sqlite_path=tmp_path / "market.sqlite",
        )
        == 1
    )
    assert calls == []


@pytest.mark.parametrize("lookback", [1, 540])
@pytest.mark.parametrize("missing_kind", ["unreceived", "failed"])
def test_native_refresh_missing_list_keeps_known_csv_without_completing_l2(
    tmp_path, monkeypatch, lookback, missing_kind
):
    _store, owner = native_input(tmp_path, monkeypatch, 0, include_bad_old=False)
    if missing_kind == "failed":
        owner.publish(
            [
                NativeListReceipt.from_response(
                    DAY - timedelta(days=1), EdinetResponse(AT + timedelta(minutes=1), b"{")
                )
            ]
        )
    calls = []
    database = tmp_path / "market.sqlite"
    zip_content = _edinet_csv_zip()

    def fetch(doc_id, kind):
        calls.append((doc_id, kind))
        return EdinetResponse(AT, zip_content)

    acquisition = EDINETRefresh("synthetic", sqlite_path=database)
    acquisition._http = SimpleNamespace(fetch_archive=fetch)
    assert (
        refresh.refresh_edinet_metrics_command(
            asof_date=DAY,
            lookback_days=lookback,
            reader=EDINETReader(database),
            acquisition=acquisition,
            sqlite_path=database,
        )
        == 1
    )
    assert calls == [("S100NATIVE", 5)]
    original = acquisition.retained_archives().metadata("S100NATIVE", 5)
    assert original.received_at_utc == AT
    with closing(open_connection(database)) as connection:
        assert read_source_archive(connection, "S100NATIVE", 5) == zip_content
        assert connection.execute("SELECT * FROM edinet_metrics").fetchall() == []
        assert (
            connection.execute(
                "SELECT * FROM source_coverage WHERE source='edinet_metrics'"
            ).fetchall()
            == []
        )

    if lookback == 1:
        day = DAY - timedelta(days=1)
        owner.publish(
            [
                NativeListReceipt.from_response(
                    day,
                    EdinetResponse(
                        AT + timedelta(days=1),
                        json.dumps(
                            {
                                "metadata": {"resultset": {"count": 0}},
                                "results": [],
                            }
                        ).encode(),
                    ),
                )
            ]
        )
        assert (
            refresh.refresh_edinet_metrics_command(
                asof_date=DAY,
                lookback_days=lookback,
                reader=EDINETReader(database),
                acquisition=acquisition,
                sqlite_path=database,
                stdout=io.StringIO(),
            )
            == 0
        )
        assert calls == [("S100NATIVE", 5)]
        assert acquisition.retained_archives().metadata("S100NATIVE", 5) == original
        assert set(EDINETReader(database).load_metric_records(DAY)) == {"7203"}


def test_missing_list_preserves_good_metrics_while_retaining_another_known_original(
    tmp_path, monkeypatch
):
    _store, owner = native_input(tmp_path, monkeypatch, 0)
    database = tmp_path / "market.sqlite"
    calls = []
    zip_content = _edinet_csv_zip()

    def fetch(doc_id, kind):
        calls.append((doc_id, kind))
        return EdinetResponse(AT, zip_content)

    acquisition = EDINETRefresh("synthetic", sqlite_path=database)
    acquisition._http = SimpleNamespace(fetch_archive=fetch)

    def refresh_window(lookback):
        return refresh.refresh_edinet_metrics_command(
            asof_date=DAY,
            lookback_days=lookback,
            reader=EDINETReader(database),
            acquisition=acquisition,
            sqlite_path=database,
            stdout=io.StringIO(),
        )

    def l2_rows():
        with closing(open_connection(database)) as connection:
            return (
                [tuple(row) for row in connection.execute("SELECT * FROM edinet_metrics")],
                [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT * FROM source_coverage WHERE source='edinet_metrics'"
                    )
                ],
            )

    assert refresh_window(0) == 0
    baseline = l2_rows()
    assert baseline[0]
    documents = [
        {
            **_edinet_document(ticker=ticker, doc_id=doc_id),
            "seqNumber": index,
            "submitDateTime": DAY.isoformat() + " 12:00",
        }
        for index, (ticker, doc_id) in enumerate(
            [("72030", "S100NATIVE"), ("83060", "S100NEXT")], 1
        )
    ]
    owner.publish(
        [
            NativeListReceipt.from_response(
                DAY,
                EdinetResponse(
                    AT + timedelta(minutes=1),
                    json.dumps(
                        {"metadata": {"resultset": {"count": 2}}, "results": documents}
                    ).encode(),
                ),
            )
        ]
    )
    assert refresh_window(1) == 1
    assert calls == [("S100NATIVE", 5), ("S100NEXT", 5)]
    assert acquisition.retained_archives().read_csv_zip("S100NEXT") == zip_content
    assert l2_rows() == baseline
