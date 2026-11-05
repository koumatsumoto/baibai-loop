"""明示的なEDINET取得と保存原本の抽出を組み立てる。"""

from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol, TextIO

from baibai_engine.data_collection.providers.edinet.models import (
    EdinetAcquisitionError,
    EdinetRateLimitError,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.planning import (
    plan_fact_item,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.service import (
    XbrlReader,
    finish_edinet_facts,
    prepare_edinet_facts,
    process_edinet_fact_item,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.store import record_failure
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.service import (
    EdinetMetricReader,
    extract_edinet_metrics_from_plan,
    prepare_edinet_metric_plan,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError
from baibai_jobs.acquisition.edinet_archives import metric_archive_candidates
from baibai_jobs.acquisition.edinet_fact_plan import (
    enable_fact_acquisition,
    needs_archive,
    prepare_fact_acquisition,
)


class EdinetMetricAcquisition(Protocol):
    def list_documents(self, on_date: date) -> list[dict[str, Any]]: ...

    def download_csv_zip(self, doc_id: str) -> bytes: ...


def refresh_edinet_metrics_command(
    *,
    asof_date: date,
    lookback_days: int,
    reader: EdinetMetricReader,
    acquisition: EdinetMetricAcquisition,
    sqlite_path: Path,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    try:
        cursor = asof_date - timedelta(days=lookback_days)
        while cursor <= asof_date:
            acquisition.list_documents(cursor)
            cursor += timedelta(days=1)
    except (
        EdinetAcquisitionError,
        EDINETProviderError,
        SQLiteSchemaError,
        sqlite3.Error,
        OSError,
        ValueError,
    ) as exc:
        print(f"EDINET source preparation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    try:
        candidates, _ = metric_archive_candidates(
            sqlite_path, asof=asof_date, lookback_days=lookback_days
        )
        for doc_id in candidates:
            acquisition.download_csv_zip(doc_id)
    except (
        EdinetAcquisitionError,
        EDINETProviderError,
        SQLiteSchemaError,
        sqlite3.Error,
        OSError,
        ValueError,
    ) as exc:
        print(f"EDINET archive acquisition failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    plan = prepare_edinet_metric_plan(
        asof_date=asof_date,
        lookback_days=lookback_days,
        reader=reader,
        sqlite_path=sqlite_path,
        stdout=out,
    )
    if plan is None:
        return 1
    return extract_edinet_metrics_from_plan(
        asof_date=asof_date,
        plan=plan,
        reader=reader,
        sqlite_path=sqlite_path,
        stdout=out,
    )


class XbrlAcquisition(Protocol):
    def download_xbrl_zip(self, doc_id: str) -> bytes: ...


def refresh_edinet_facts_command(
    *,
    asof: date,
    sqlite_path: Path,
    reader: XbrlReader,
    acquisition: XbrlAcquisition,
    initialize: bool = False,
    tickers: tuple[str, ...] = (),
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    selected = prepare_fact_acquisition(
        sqlite_path, asof=asof, initialize=initialize, tickers=tickers
    )
    if selected is None:
        print(
            "EDINET research facts: skipped=not_initialized; run refresh-edinet-facts --initialize",
            file=out,
        )
        return 0
    failed: dict[str, str] = {}
    rate_limited: str | None = None
    for document in selected:
        if not needs_archive(document, asof=asof):
            continue
        try:
            acquisition.download_xbrl_zip(str(document["docID"]))
        except (EdinetAcquisitionError, EDINETProviderError, ValueError, OSError) as exc:
            doc_id = str(document["docID"])
            failed[doc_id] = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, EdinetRateLimitError):
                rate_limited = doc_id
                break
    if initialize and not tickers and selected and not failed:
        enable_fact_acquisition(sqlite_path, asof=asof)
    inputs = prepare_edinet_facts(
        asof=asof, sqlite_path=sqlite_path, initialize=initialize, tickers=tickers, stdout=out
    )
    if inputs is None:
        return 0
    counts: Counter[str] = Counter(selected=len(inputs.documents))
    reasons: Counter[str] = Counter()
    for document in inputs.documents:
        item = plan_fact_item(document, statuses=inputs.statuses, asof=asof)
        if item.doc_id in failed:
            assert item.identity is not None
            if item.action != "reused":
                record_failure(
                    sqlite_path,
                    doc_id=item.doc_id,
                    disclosed_on=item.identity.disclosed_on,
                    reason=failed[item.doc_id],
                )
            counts["failed"] += 1
            if item.doc_id == rate_limited:
                counts["rate_limited"] += 1
                break
            continue
        process_edinet_fact_item(
            item=item,
            reader=reader,
            sqlite_path=sqlite_path,
            counts=counts,
            reasons=reasons,
            stdout=out,
        )
    return finish_edinet_facts(
        counts=counts,
        reasons=reasons,
        stdout=out,
    )
