"""Explicit maintenance retains source evidence before planning or extracting L2."""

import io
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from tests.helpers.edinet_metrics import _edinet_csv_zip, _edinet_document

from baibai_engine.data_collection.providers.edinet.archives import retain_source_archive
from baibai_engine.data_collection.providers.edinet.models import (
    EdinetRateLimitError,
    EdinetResponse,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_selection import (
    select_document_candidates,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.revision import (
    EDINET_METRIC_REVISION,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.store import (
    read_edinet_metrics,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.write import (
    store_edinet_metrics,
)
from baibai_engine.machine_analysis.observations.financials.edinet_reader import EDINETReader
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.maintenance.edinet_extraction import refresh_edinet_metrics_command
from baibai_jobs.maintenance.source_refresh.edinet import EDINETRefresh

ASOF = date(2026, 4, 24)
AT = datetime(2026, 4, 24, 12, tzinfo=UTC)
DOC_ID = "S100CURRENT"


def response(documents):
    return EdinetResponse(
        received_at=AT,
        body=json.dumps(
            {"metadata": {"resultset": {"count": len(documents)}}, "results": documents}
        ).encode(),
    )


def document(doc_id=DOC_ID, ticker="72030"):
    return {"seqNumber": 1, **_edinet_document(ticker=ticker, doc_id=doc_id)}


def archives(path):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT doc_id, source_type, stored_on, zip_content FROM edinet_source_archives ORDER BY doc_id, source_type"
        ).fetchall()


def baseline(path, *, corrupt=False):
    candidate = select_document_candidates(
        EDINETReader(path).list_documents(ASOF), origin_start=ASOF
    ).candidates["7203"]
    store_edinet_metrics(
        path,
        ASOF - timedelta(days=1),
        [
            {
                "ticker": candidate.ticker,
                "sales_ttm": 77.0,
                "source_doc_id": candidate.doc_id,
                "document_type": candidate.doc_type_code,
                "source_submit_datetime": candidate.submit_datetime,
                "source_period_start": candidate.period_start.isoformat(),
                "source_period_end": candidate.period_end.isoformat(),
                "extractor_revision": EDINET_METRIC_REVISION,
                "source_document_revision": candidate.source_document_revision,
            }
        ],
    )
    if corrupt:
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE edinet_metrics SET failure_reasons='invalid-json'")


@pytest.mark.parametrize("metric_state", ["missing", "reusable", "corrupt"])
@pytest.mark.parametrize("retained_type", [None, 1, 5])
def test_actual_refresh_retains_zip_before_l2_and_reuses_exact_type(
    tmp_path, metric_state, retained_type, capsys
):
    path = tmp_path / "market.sqlite"
    body = _edinet_csv_zip()
    with patch("baibai_jobs.maintenance.source_refresh.edinet.EDINETHTTP") as http:
        client = http.return_value
        client.fetch_list.return_value = response([document()])
        client.fetch_archive.return_value = EdinetResponse(received_at=AT, body=body)
        acquisition = EDINETRefresh("synthetic", sqlite_path=path)
        acquisition.list_documents(ASOF)
        if metric_state != "missing":
            baseline(path, corrupt=metric_state == "corrupt")
        stored_on = ASOF - timedelta(days=2)
        if retained_type is not None:
            with closing(open_connection(path)) as connection:
                retain_source_archive(connection, DOC_ID, retained_type, body, stored_on=stored_on)
        reader = EDINETReader(path)
        parsed = Mock(wraps=reader.read_csv_zip)
        reader.read_csv_zip = parsed
        for _ in range(2):
            assert refresh_edinet_metrics_command(
                asof_date=ASOF,
                lookback_days=0,
                reader=reader,
                acquisition=acquisition,
                sqlite_path=path,
                stdout=io.StringIO(),
            ) == (1 if metric_state == "corrupt" else 0)
        client.fetch_list.assert_called_once_with(ASOF)
        if retained_type == 5:
            client.fetch_archive.assert_not_called()
        else:
            client.fetch_archive.assert_called_once_with(DOC_ID, 5)
        assert parsed.call_count == (1 if metric_state == "missing" else 0)
        expected = [(DOC_ID, 5, str(stored_on if retained_type == 5 else ASOF), body)]
        if retained_type == 1:
            expected.insert(0, (DOC_ID, 1, str(stored_on), body))
        assert archives(path) == expected
        if metric_state == "corrupt":
            assert "baseline is corrupt" in capsys.readouterr().err
            with sqlite3.connect(path) as connection:
                assert connection.execute(
                    "SELECT failure_reasons FROM edinet_metrics"
                ).fetchall() == [("invalid-json",)]
        elif metric_state == "reusable":
            assert read_edinet_metrics(path, ASOF)["7203"].sales_ttm == 77.0
        acquisition.close()
        client.close.assert_called_once()


def test_partial_archive_failure_keeps_prior_raw_and_does_not_plan_l2(tmp_path):
    path = tmp_path / "market.sqlite"
    body = _edinet_csv_zip()
    store_edinet_metrics(
        path, ASOF, [{"ticker": "7203", "sales_ttm": 77.0, "extractor_revision": "previous"}]
    )
    with patch("baibai_jobs.maintenance.source_refresh.edinet.EDINETHTTP") as http:
        client = http.return_value
        client.fetch_list.return_value = response(
            [
                document("S100FIRST", "72030"),
                {**document("S100NEXT", "83060"), "seqNumber": 2},
            ]
        )
        client.fetch_archive.side_effect = [
            EdinetResponse(received_at=AT, body=body),
            EdinetRateLimitError("synthetic 429"),
        ]
        acquisition = EDINETRefresh("synthetic", sqlite_path=path)
        with patch(
            "baibai_jobs.maintenance.edinet_extraction.prepare_edinet_metric_plan",
            side_effect=AssertionError("L2 entered"),
        ):
            assert (
                refresh_edinet_metrics_command(
                    asof_date=ASOF,
                    lookback_days=0,
                    reader=EDINETReader(path),
                    acquisition=acquisition,
                    sqlite_path=path,
                    stdout=io.StringIO(),
                )
                == 1
            )
        assert archives(path) == [("S100FIRST", 5, str(ASOF), body)]
        assert read_edinet_metrics(path, ASOF)["7203"].sales_ttm == 77.0
        acquisition.close()


@pytest.mark.parametrize("lookback", [0, 2, 540])
def test_explicit_window_and_default_scope_only_fetch_missing_selected_original(tmp_path, lookback):
    path = tmp_path / "market.sqlite"
    start = ASOF - timedelta(days=lookback)
    body = _edinet_csv_zip()
    with patch("baibai_jobs.maintenance.source_refresh.edinet.EDINETHTTP") as http:
        client = http.return_value
        # Real saved lists are used, including normal-empty days. No L2 fixture
        # or fabricated success coverage determines the source candidate range.
        client.fetch_list.side_effect = lambda day: response([document()] if day == start else [])
        client.fetch_archive.return_value = EdinetResponse(received_at=AT, body=body)
        acquisition = EDINETRefresh("synthetic", sqlite_path=path)
        assert (
            refresh_edinet_metrics_command(
                asof_date=ASOF,
                lookback_days=lookback,
                reader=EDINETReader(path),
                acquisition=acquisition,
                sqlite_path=path,
                stdout=io.StringIO(),
            )
            == 0
        )
        assert [call.args[0] for call in client.fetch_list.call_args_list] == [
            start + timedelta(days=n) for n in range(lookback + 1)
        ]
        client.fetch_archive.assert_called_once_with(DOC_ID, 5)
        assert archives(path) == [(DOC_ID, 5, str(ASOF), body)]
        acquisition.close()
