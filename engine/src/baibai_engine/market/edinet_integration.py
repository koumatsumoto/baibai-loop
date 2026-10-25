"""Offline EDINET integration distinguishes absent inputs from broken inputs."""

from __future__ import annotations

import json
import sys
from collections import Counter
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.edinet.receipts import read_list_responses
from baibai_engine.market.edinet_document_store import read_observed_documents
from baibai_engine.market.edinet_facts.planning import plan_fact_item
from baibai_engine.market.edinet_facts.selection import annual_documents
from baibai_engine.market.edinet_facts.service import finish_edinet_facts, process_edinet_fact_item
from baibai_engine.market.edinet_facts.store import extraction_status
from baibai_engine.market.edinet_handoff import archive_keys
from baibai_engine.market.edinet_list_projection import project_document_list
from baibai_engine.market.edinet_metrics.planning import make_metric_extraction_plan
from baibai_engine.market.edinet_metrics.revision import EDINET_METRIC_REVISION
from baibai_engine.market.edinet_metrics.service import extract_edinet_metrics_from_plan
from baibai_engine.market.edinet_metrics.store import read_edinet_metric_baseline
from baibai_engine.market.edinet_reader import EDINETReader
from baibai_engine.market.edinet_selection import select_document_candidates
from baibai_engine.storage.observations.coverage import range_covered
from baibai_engine.storage.observations.read import connect_read_only


def extract_saved_edinet(
    database: Path, *, asof: date, lookback_days: int = 540
) -> dict[str, dict[str, Any]]:
    """Use saved source dates; never turn missing acquisition into extraction failure.

    The strict acquisition/manual extraction CLIs retain their requested-date
    contract. Only the independent integration selects available saved inputs.
    Validation/parse/storage failures are not classified as missing acquisition.
    """
    baseline = read_edinet_metric_baseline(database, asof)
    with closing(connect_read_only(database)) as conn:
        conn.execute("BEGIN")
        documents = read_observed_documents(conn, through=asof)
        failed_list_days: list[str] = []
        for (day,) in conn.execute(
            "SELECT DISTINCT r.doc_date FROM edinet_list_responses r WHERE r.doc_date <= ? "
            "AND EXISTS (SELECT 1 FROM source_coverage c WHERE c.source = 'edinet_documents' "
            "AND c.status = 'failed' AND c.coverage_start <= r.doc_date "
            "AND c.coverage_end >= r.doc_date) ORDER BY r.doc_date",
            (asof.isoformat(),),
        ):
            projection = project_document_list(
                read_list_responses(conn, doc_date=date.fromisoformat(day))
            )
            if projection.error is not None:
                failed_list_days.append(day)
        row = conn.execute(
            "SELECT MAX(coverage_end) FROM source_coverage "
            "WHERE source = 'edinet_documents' AND status = 'ok' AND coverage_end <= ?",
            (asof.isoformat(),),
        ).fetchone()
        available = date.fromisoformat(row[0]) if row[0] is not None else None
        covered = available is not None and range_covered(
            conn, "edinet_documents", available - timedelta(days=lookback_days), available
        )
    retained = archive_keys(database)
    reader = EDINETReader(database)
    metrics: dict[str, Any] = {
        "exit_code": 0,
        "status": "deferred_input",
        "input_asof": available.isoformat() if available else None,
        "snapshot_asof": baseline.asof_date.isoformat() if baseline else None,
        "deferred_inputs": [],
    }
    if available is None:
        metrics["deferred_inputs"].append("document_list_unavailable")
    elif not covered:
        metrics["deferred_inputs"].append("document_window_incomplete")
    elif baseline is not None and available < baseline.asof_date:
        metrics["deferred_inputs"].append("document_list_behind_saved_snapshot")
    else:
        assert available is not None
        window_start = available - timedelta(days=lookback_days)
        selection = select_document_candidates(
            [
                doc
                for doc in documents
                if window_start.isoformat() <= str(doc["doc_date"]) <= available.isoformat()
            ],
            origin_start=window_start,
        )
        plan = make_metric_extraction_plan(
            selection=selection, baseline=baseline, extractor_revision=EDINET_METRIC_REVISION
        )
        missing = [
            item.doc_id for item in plan.archive_candidates if (item.doc_id, 5) not in retained
        ]
        if not selection.candidates:
            metrics["deferred_inputs"].append("no_selected_filings")
        elif missing and len(missing) == len(plan.archive_candidates):
            metrics["deferred_inputs"].append("csv_archives_unavailable")
            metrics["missing_archive_count"] = len(missing)
        elif (
            baseline is not None
            and baseline.asof_date == available
            and not plan.archive_candidates
            and not plan.events_by_ticker
        ):
            metrics["status"] = "unchanged"
        else:
            if missing:
                metrics["deferred_inputs"].append("csv_archives_unavailable")
                metrics["missing_archive_count"] = len(missing)
            code = extract_edinet_metrics_from_plan(
                asof_date=available,
                plan=plan,
                reader=reader,
                sqlite_path=database,
                unavailable_doc_ids=frozenset(missing),
            )
            metrics.update(
                exit_code=code,
                status="failed" if code else "partial" if missing else "completed",
            )
            if not code:
                metrics["snapshot_asof"] = available.isoformat()
    if available is not None and available < asof:
        metrics["deferred_inputs"].append("latest_document_list_delayed")
    results = {
        "edinet-metrics": metrics,
        "edinet-facts": _extract_saved_facts(
            database, asof, available, documents, retained, reader
        ),
    }
    if failed_list_days:
        results["edinet-list"] = {
            "exit_code": 1,
            "status": "failed",
            "reason": "retained_document_list_validation_failed",
            "failed_days": failed_list_days,
        }
    return results


def _extract_saved_facts(
    database: Path,
    asof: date,
    available: date | None,
    documents: list[dict[str, Any]],
    retained: set[tuple[str, int]],
    reader: EDINETReader,
) -> dict[str, Any]:
    statuses = extraction_status(database)
    result: dict[str, Any] = {
        "exit_code": 0,
        "status": "skipped_not_initialized",
        "input_asof": available.isoformat() if available else None,
        "deferred_inputs": [],
    }
    if "initialized" not in statuses:
        return result
    since = json.loads(statuses["initialized"][1] or "{}").get("since")
    selected = annual_documents(documents, retained=set(statuses), since=since)
    counts: Counter[str] = Counter(selected=len(selected))
    reasons: Counter[str] = Counter()
    missing = 0
    for document in selected:
        item = plan_fact_item(document, statuses=statuses, asof=asof)
        if item.action == "extract" and (item.doc_id, 1) not in retained:
            missing += 1
            continue
        process_edinet_fact_item(
            item=item,
            reader=reader,
            sqlite_path=database,
            counts=counts,
            reasons=reasons,
            stdout=sys.stdout,
        )
    if not selected:
        result.update(status="deferred_input", deferred_inputs=["no_selected_filings"])
    else:
        code = finish_edinet_facts(
            asof=asof,
            sqlite_path=database,
            initialize=False,
            tickers=(),
            counts=counts,
            reasons=reasons,
            stdout=sys.stdout,
        )
        result.update(exit_code=code, status="failed" if code else "completed", counts=dict(counts))
    if missing:
        result["deferred_inputs"].append("xbrl_archives_unavailable")
        result["missing_archive_count"] = missing
    if available is None or available < asof:
        result["deferred_inputs"].append("latest_document_list_delayed")
    return result
