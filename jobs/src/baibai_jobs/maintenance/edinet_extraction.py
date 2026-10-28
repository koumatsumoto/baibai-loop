"""明示的なEDINET取得と保存原本の抽出を組み立てる。"""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol, TextIO

from baibai_engine.data_collection.edinet.models import EdinetAcquisitionError, EdinetRateLimitError
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.market.edinet_metrics.service import EdinetMetricReader, prepare_edinet_metric_plan, extract_edinet_metrics_from_plan
from baibai_engine.market.edinet_facts.service import XbrlReader, prepare_edinet_facts, process_edinet_fact_item, finish_edinet_facts
from baibai_engine.market.edinet_facts.planning import plan_fact_item
from baibai_engine.market.edinet_facts.store import record_failure
from baibai_engine.storage.observations.schema import SQLiteSchemaError


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
    plan = prepare_edinet_metric_plan(
        asof_date=asof_date,
        lookback_days=lookback_days,
        reader=reader,
        sqlite_path=sqlite_path,
        stdout=out,
    )
    if plan is None:
        return 1
    try:
        for candidate in plan.archive_candidates:
            acquisition.download_csv_zip(candidate.doc_id)
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
    inputs = prepare_edinet_facts(
        asof=asof, sqlite_path=sqlite_path, initialize=initialize, tickers=tickers, stdout=out
    )
    if inputs is None:
        return 0
    counts: Counter[str] = Counter(selected=len(inputs.documents))
    reasons: Counter[str] = Counter()
    for document in inputs.documents:
        item = plan_fact_item(document, statuses=inputs.statuses, asof=asof)
        if item.action == "extract":
            try:
                acquisition.download_xbrl_zip(item.doc_id)
            except (EdinetAcquisitionError, EDINETProviderError, ValueError, OSError) as exc:
                assert item.identity is not None
                record_failure(
                    sqlite_path,
                    doc_id=item.doc_id,
                    disclosed_on=item.identity.disclosed_on,
                    reason=f"{type(exc).__name__}: {exc}",
                )
                counts["failed"] += 1
                if isinstance(exc, EdinetRateLimitError):
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
        asof=asof,
        sqlite_path=sqlite_path,
        initialize=initialize,
        tickers=tickers,
        counts=counts,
        reasons=reasons,
        stdout=out,
    )
