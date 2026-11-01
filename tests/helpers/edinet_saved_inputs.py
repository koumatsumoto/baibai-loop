"""取得延期と保存metricの有効性を区別する合成入力。"""

from contextlib import closing
from datetime import date, timedelta

from baibai_engine.machine_analysis.observations.disclosures.edinet_document_store import (
    read_observed_documents,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_selection import (
    select_document_candidates,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.revision import (
    EDINET_METRIC_REVISION,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.write import (
    store_edinet_metrics,
)
from baibai_engine.storage.sqlite.source_cache.coverage import record_source_coverage
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from tests.helpers.edinet_documents import store_edinet_documents

BASE = date(2026, 4, 24)
TARGET = date(2026, 4, 27)
RECORDED = "2026-04-24T12:34:56+00:00"


def _seed(path):
    documents = [
        {
            "seqNumber": sequence,
            "docID": doc_id,
            "secCode": ticker,
            "docTypeCode": "120",
            "csvFlag": "1",
            "xbrlFlag": "1",
            "legalStatus": "1",
            "disclosureStatus": "0",
            "withdrawalStatus": "0",
            "docInfoEditStatus": "0",
            "periodStart": "2025-04-01",
            "periodEnd": "2026-03-31",
            "submitDateTime": "2026-04-24 12:00",
        }
        for sequence, (doc_id, ticker) in enumerate(
            [("S100SAVED", "72030"), ("S100OTHER", "83060")], 1
        )
    ]
    store_edinet_documents(path, BASE, documents)
    _write_snapshot(path, BASE, cash=123.0)
    with closing(open_connection(path)) as conn, conn:
        conn.execute(
            "UPDATE source_coverage SET fetched_at_utc = ? WHERE source = 'edinet_metrics'",
            (RECORDED,),
        )
        for source in ("edinet_documents", "edinet_metrics"):
            record_source_coverage(
                conn,
                source=source,
                coverage_key=TARGET.isoformat(),
                coverage_start=TARGET.isoformat(),
                coverage_end=TARGET.isoformat(),
                record_count=0,
                status="failed",
                error="source delayed",
            )
    return documents


def _write_snapshot(path, asof, *, cash):
    with closing(open_connection(path)) as conn:
        candidates = select_document_candidates(
            read_observed_documents(conn, through=asof), origin_start=None
        ).candidates
    store_edinet_metrics(
        path,
        asof,
        [
            {
                "ticker": candidate.ticker,
                "cash": cash,
                "source_doc_id": candidate.doc_id,
                "document_type": candidate.doc_type_code,
                "source_submit_datetime": candidate.submit_datetime,
                "source_period_start": candidate.period_start,
                "source_period_end": candidate.period_end,
                "extractor_revision": EDINET_METRIC_REVISION,
                "source_document_revision": candidate.source_document_revision,
            }
            for candidate in candidates.values()
        ],
    )


def _next_listing(path):
    documents = _seed(path)
    for offset in (1, 2):
        store_edinet_documents(path, BASE + timedelta(days=offset), [])
    updated = dict(documents[0])
    updated.update(docID="S100NEXT", submitDateTime="2026-04-27 12:00")
    store_edinet_documents(path, TARGET, [updated])
