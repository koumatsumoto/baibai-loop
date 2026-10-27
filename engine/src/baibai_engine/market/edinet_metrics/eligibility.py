"""Eligible saved EDINET metrics, independently of the latest acquisition result."""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import date
from pathlib import Path

from baibai_engine.market.edinet_document_store import read_observed_documents
from baibai_engine.market.edinet_metrics.planning import (
    can_reuse_edinet_metric,
    make_metric_extraction_plan,
)
from baibai_engine.market.edinet_metrics.revision import EDINET_METRIC_REVISION
from baibai_engine.market.edinet_metrics.store import read_edinet_metric_baseline
from baibai_engine.market.edinet_models import EdinetMetricRecord
from baibai_engine.market.edinet_selection import select_document_candidates
from baibai_engine.storage.observations.read import connect_read_only


def read_eligible_edinet_metrics(
    sqlite_path: Path, asof_date: date
) -> dict[str, EdinetMetricRecord]:
    """Select only compatible observations from the validated latest baseline.

    Failed later extraction/list coverage is not proof that saved values are bad.
    Known filing changes and quarantined events do invalidate affected values.
    Never search behind an inconsistent successful baseline or revive an older
    version for an ineligible ticker. This read does not update rows or coverage.
    """
    baseline = read_edinet_metric_baseline(sqlite_path, asof_date)
    if baseline is None:
        return {}
    with closing(connect_read_only(sqlite_path)) as connection:
        connection.execute("BEGIN")
        selection = select_document_candidates(
            read_observed_documents(connection, through=asof_date), origin_start=None
        )
    plan = make_metric_extraction_plan(
        selection=selection, baseline=baseline, extractor_revision=EDINET_METRIC_REVISION
    )
    return {
        ticker: replace(
            row.record,
            snapshot_asof=baseline.asof_date,
            snapshot_recorded_at_utc=baseline.recorded_at_utc,
        )
        for ticker, row in baseline.rows.items()
        if ticker not in plan.events_by_ticker
        and (candidate := selection.candidates.get(ticker)) is not None
        and can_reuse_edinet_metric(
            candidate=candidate, baseline_row=row, extractor_revision=EDINET_METRIC_REVISION
        )
    }
