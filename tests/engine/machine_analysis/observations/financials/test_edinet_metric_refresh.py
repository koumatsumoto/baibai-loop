"""Explicit metric source acquisition feeds one offline extraction plan."""

from __future__ import annotations

import io
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.helpers.edinet_metrics import (
    FakeEDINETMetricReader,
    _edinet_csv_zip,
    _edinet_document,
)

from baibai_engine.data_collection.providers.edinet.archives import retain_source_archive
from baibai_engine.data_collection.providers.edinet.models import (
    EdinetAcquisitionError,
    EdinetRateLimitError,
)
from baibai_engine.data_collection.providers.edinet.receipts import retain_list_response
from baibai_engine.machine_analysis.observations.disclosures.edinet_list_rebuild import (
    rebuild_edinet_document_day,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_selection import (
    select_document_candidates,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.revision import (
    EDINET_METRIC_REVISION,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.service import (
    extract_edinet_metrics_command,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.store import (
    read_edinet_metrics,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.write import (
    store_edinet_metrics,
)
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError, open_connection
from baibai_jobs.maintenance.edinet_extraction import (
    refresh_edinet_metrics_command,
)


@dataclass
class _Acquisition:
    database: Path
    reader: FakeEDINETMetricReader
    documents: list[dict[str, object]]
    archives: dict[str, bytes]
    fail_at: str | None = None
    list_calls: list[date] = field(default_factory=list)
    download_calls: list[str] = field(default_factory=list)

    def list_documents(self, on_date: date) -> list[dict[str, object]]:
        self.list_calls.append(on_date)
        if self.fail_at == "list":
            raise EdinetAcquisitionError("document source unavailable")
        # Match production's retained-list and projection contract; the memory
        # reader still tracks the independent L2 read/parse calls below.
        body = json.dumps(
            {
                "metadata": {"resultset": {"count": len(self.documents)}},
                "results": [
                    {"seqNumber": i, **document} for i, document in enumerate(self.documents, 1)
                ],
            }
        ).encode()
        with closing(open_connection(self.database)) as connection:
            retain_list_response(
                connection,
                doc_date=on_date,
                received_at=datetime(2026, 4, 24, tzinfo=UTC),
                body=body,
            )
        rebuild_edinet_document_day(self.database, on_date)
        self.reader.documents = self.documents
        return self.documents

    def download_csv_zip(self, doc_id: str) -> bytes:
        self.download_calls.append(doc_id)
        if self.fail_at == "archive":
            raise EdinetRateLimitError("too many requests")
        content = self.archives[doc_id]
        with closing(open_connection(self.database)) as connection:
            retain_source_archive(connection, doc_id, 5, content, stored_on=date(2026, 4, 24))
        if self.reader.zip_by_doc_id is None:
            self.reader.zip_by_doc_id = {}
        self.reader.zip_by_doc_id[doc_id] = content
        return content


def test_refresh_acquires_source_selected_archives_before_l2_reuse_and_quarantine(tmp_path) -> None:
    asof = date(2026, 4, 24)
    db = tmp_path / "market.sqlite"
    documents = [
        _edinet_document(ticker="96820", doc_id="S100REUSE"),
        _edinet_document(ticker="83060", doc_id="S100NEEDED"),
        _edinet_document(ticker="13010", doc_id="S100CURRENT1"),
        _edinet_document(ticker="72030", doc_id="S100CURRENT2"),
        {
            "docID": "S100EDIT",
            "secCode": "13010",
            "docTypeCode": "120",
            "docInfoEditStatus": "1",
            "withdrawalStatus": "0",
            "disclosureStatus": "0",
        },
        {
            "docID": "S100WITHDRAW",
            "parentDocID": "S100OLD",
            "docInfoEditStatus": "0",
            "withdrawalStatus": "1",
            "disclosureStatus": "0",
            "legalStatus": "0",
        },
    ]
    reuse = select_document_candidates(documents, origin_start=asof).candidates["9682"]
    store_edinet_metrics(
        db,
        date(2026, 4, 23),
        [
            {
                "ticker": "9682",
                "sales_ttm": 123.0,
                "source_doc_id": reuse.doc_id,
                "document_type": reuse.doc_type_code,
                "source_submit_datetime": reuse.submit_datetime,
                "source_period_start": reuse.period_start.isoformat(),
                "source_period_end": reuse.period_end.isoformat(),
                "extractor_revision": EDINET_METRIC_REVISION,
                "source_document_revision": reuse.source_document_revision,
            },
            {
                "ticker": "7203",
                "sales_ttm": 456.0,
                "source_doc_id": "S100OLD",
                "document_type": "120",
                "extractor_revision": EDINET_METRIC_REVISION,
            },
        ],
    )
    reader = FakeEDINETMetricReader()
    acquisition = _Acquisition(
        database=db,
        reader=reader,
        documents=documents,
        archives={
            doc_id: _edinet_csv_zip() for doc_id in ("S100NEEDED", "S100REUSE", "S100CURRENT2")
        },
    )

    assert (
        refresh_edinet_metrics_command(
            asof_date=asof,
            lookback_days=0,
            reader=reader,
            acquisition=acquisition,
            sqlite_path=db,
            stdout=io.StringIO(),
        )
        == 0
    )
    assert acquisition.list_calls == [asof]
    assert reader.list_calls == [asof]
    assert acquisition.download_calls == ["S100CURRENT2", "S100NEEDED", "S100REUSE"]
    assert reader.read_calls == ["S100NEEDED"]
    saved = read_edinet_metrics(db, asof)
    assert saved is not None
    assert saved["9682"].sales_ttm == 123.0
    assert saved["8306"].sales_ttm is not None
    assert saved["1301"].failure_reasons == ("document_event_quarantined:edit:S100EDIT",)
    assert saved["7203"].failure_reasons == ("document_event_quarantined:withdrawal:S100WITHDRAW",)


@pytest.mark.parametrize("fail_at", ["list", "archive"])
def test_refresh_source_failure_preserves_previous_metrics(tmp_path, capsys, fail_at) -> None:
    asof = date(2026, 4, 24)
    db = tmp_path / "market.sqlite"
    store_edinet_metrics(
        db,
        asof,
        [{"ticker": "9682", "sales_ttm": 77.0, "extractor_revision": "previous"}],
    )
    reader = FakeEDINETMetricReader()
    acquisition = _Acquisition(
        database=db,
        reader=reader,
        documents=[_edinet_document(ticker="83060", doc_id="S100NEEDED")],
        archives={"S100NEEDED": _edinet_csv_zip()},
        fail_at=fail_at,
    )

    assert (
        refresh_edinet_metrics_command(
            asof_date=asof,
            lookback_days=0,
            reader=reader,
            acquisition=acquisition,
            sqlite_path=db,
            stdout=io.StringIO(),
        )
        == 1
    )
    error = capsys.readouterr().err
    if fail_at == "list":
        assert "EDINET source preparation failed: EdinetAcquisitionError" in error
        assert reader.list_calls == []
        assert acquisition.download_calls == []
    else:
        assert "EDINET archive acquisition failed: EdinetRateLimitError" in error
        assert reader.list_calls == []
        assert acquisition.download_calls == ["S100NEEDED"]
    assert reader.read_calls == []
    assert read_edinet_metrics(db, asof)["9682"].sales_ttm == 77.0


@pytest.mark.parametrize(
    "error_type", [SQLiteSchemaError, sqlite3.OperationalError, OSError, ValueError]
)
def test_offline_local_listing_failure_preserves_previous_metrics(
    tmp_path, capsys, error_type
) -> None:
    class FailingReader(FakeEDINETMetricReader):
        def list_documents(self, on_date: date) -> list[dict[str, object]]:
            del on_date
            raise error_type("stored documents unreadable")

    asof = date(2026, 4, 24)
    db = tmp_path / "market.sqlite"
    store_edinet_metrics(
        db,
        asof,
        [{"ticker": "9682", "sales_ttm": 77.0, "extractor_revision": "previous"}],
    )
    assert (
        extract_edinet_metrics_command(
            asof_date=asof,
            lookback_days=0,
            reader=FailingReader(),
            sqlite_path=db,
            stdout=io.StringIO(),
        )
        == 1
    )
    assert "EDINET document listing failed" in capsys.readouterr().err
    assert read_edinet_metrics(db, asof)["9682"].sales_ttm == 77.0
