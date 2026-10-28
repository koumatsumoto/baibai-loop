"""Acquire only needed EDINET archive originals; common integration extracts L2."""

from __future__ import annotations

import sys
from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.data_collection.providers.edinet.models import EdinetAcquisitionError, EdinetRateLimitError
from baibai_engine.market.edinet_facts.planning import plan_fact_item
from baibai_engine.market.edinet_facts.service import prepare_edinet_facts
from baibai_engine.market.edinet_metrics.service import prepare_edinet_metric_plan
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.market.edinet_reader import EDINETReader
from baibai_jobs.maintenance.source_refresh.edinet import EDINETRefresh


def acquire_archives(
    database: Path,
    *,
    group: str,
    asof: date,
    api_key: str,
    retained: set[tuple[str, int]],
) -> dict[str, object]:
    """Read saved list/baseline inputs and archive identities, without extracting facts."""
    if group not in {"edinet-metrics", "edinet-facts"}:
        raise ValueError("unsupported EDINET archive group")
    if group == "edinet-metrics":
        plan = prepare_edinet_metric_plan(
            asof_date=asof,
            lookback_days=540,
            reader=EDINETReader(database),
            sqlite_path=database,
        )
        if plan is None:
            return {"status": "failed", "failed": ["selection"]}
        documents = [(candidate.doc_id, 5) for candidate in plan.archive_candidates]
    else:
        inputs = prepare_edinet_facts(
            asof=asof,
            sqlite_path=database,
            initialize=False,
            tickers=(),
            stdout=sys.stdout,
        )
        if inputs is None:
            return {"status": "skipped_not_initialized", "downloaded": 0}
        documents = [
            (item.doc_id, 1)
            for document in inputs.documents
            if (item := plan_fact_item(document, statuses=inputs.statuses, asof=asof)).action
            == "extract"
        ]
    downloaded, reused, failed = 0, 0, []
    with closing(EDINETRefresh(api_key, sqlite_path=database)) as acquisition:
        for doc_id, source_type in documents:
            if (doc_id, source_type) in retained:
                reused += 1
                continue
            try:
                if source_type == 5:
                    acquisition.download_csv_zip(doc_id)
                else:
                    acquisition.download_xbrl_zip(doc_id)
            except (EdinetAcquisitionError, EDINETProviderError, ValueError, OSError) as error:
                failed.append(doc_id)
                if isinstance(error, EdinetRateLimitError):
                    break
            else:
                downloaded += 1
    return {
        "status": "failed" if failed else "received",
        "downloaded": downloaded,
        "retained_archives_reused": reused,
        "failed": failed,
    }
