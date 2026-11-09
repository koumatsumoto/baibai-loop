"""明示的なEDINET取得と保存原本の抽出を組み立てる。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol, TextIO

from baibai_engine.data_collection.providers.edinet.archive_native import FixedEdinetArchives
from baibai_engine.data_collection.providers.edinet.archive_plan import select_metric_archives
from baibai_engine.data_collection.providers.edinet.models import (
    EdinetAcquisitionError,
    EDINETProviderError,
    EdinetResponse,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_document_store import (
    read_observed_documents,
    read_stored_documents,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.service import (
    extract_edinet_facts_command,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.service import (
    EdinetMetricReader,
    extract_edinet_metrics_from_plan,
    prepare_edinet_metric_plan,
)
from baibai_engine.storage.objects.r2 import Boto3R2Store, LakePublishError
from baibai_engine.storage.sqlite.source_cache.coverage import date_covered
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError


class EdinetMetricAcquisition(Protocol):
    def download_csv_zip(self, doc_id: str) -> bytes: ...

    def retained_archives(self) -> FixedEdinetArchives: ...


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
        from baibai_engine.data_collection.providers.edinet.list_native import EdinetListL1
        from baibai_jobs.integration.edinet_list_native import materialize_lists

        with closing(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        ) as store:
            fixed = EdinetListL1(store, sqlite_path.parent / "edinet-list-read").freeze()
            result = materialize_lists(fixed, sqlite_path)
            print(json.dumps({"native_list_input": result}, sort_keys=True), file=out)
    except (
        EdinetAcquisitionError,
        EDINETProviderError,
        SQLiteSchemaError,
        LakePublishError,
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
    archive_reader = acquisition.retained_archives() if plan.archive_candidates else reader
    return extract_edinet_metrics_from_plan(
        asof_date=asof_date,
        plan=plan,
        reader=archive_reader,
        sqlite_path=sqlite_path,
        stdout=out,
    )


class XbrlAcquisition(Protocol):
    def fetch_archive(self, doc_id: str, source_type: int) -> EdinetResponse: ...


def refresh_edinet_facts_command(
    *,
    asof: date,
    sqlite_path: Path,
    acquisition: XbrlAcquisition,
    initialize: bool = False,
    tickers: tuple[str, ...] = (),
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    from baibai_engine.data_collection.providers.edinet.archive_native import EdinetArchiveL1
    from baibai_engine.data_collection.providers.edinet.list_native import EdinetListL1
    from baibai_jobs.acquisition.edinet_archive_native import acquire_archives
    from baibai_jobs.integration.edinet_archive_native import materialize_archives
    from baibai_jobs.integration.edinet_list_native import materialize_lists

    try:
        with closing(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        ) as store:
            owner = EdinetArchiveL1(store, sqlite_path.parent / "edinet-archive-work")
            lists = EdinetListL1(store, sqlite_path.parent / "edinet-list-read").freeze()
            report = acquire_archives(
                owner,
                acquisition,
                lists,
                group="edinet-facts",
                asof=asof,
                initialize_facts=initialize,
                tickers=tickers,
                # The explicit annual refresh never requested the daily holder batch.
                include_holder_archives=False,
            )
            print(json.dumps({"native_archive_acquisition": report}, sort_keys=True), file=out)
            archives = owner.freeze()
            materialize_archives(archives, sqlite_path)
            materialize_lists(lists, sqlite_path)
            # Extraction must use this same head while its object store is open.
            return max(
                int(report["status"] == "failed"),
                extract_edinet_facts_command(
                    asof=asof,
                    sqlite_path=sqlite_path,
                    reader=archives,
                    initialize=initialize,
                    tickers=tickers,
                    stdout=out,
                ),
            )
    except (
        EdinetAcquisitionError,
        EDINETProviderError,
        LakePublishError,
        sqlite3.Error,
        OSError,
        ValueError,
    ) as error:
        print(f"EDINET native facts refresh failed: {type(error).__name__}", file=sys.stderr)
        return 1


def metric_archive_candidates(
    database: Path, *, asof: date, lookback_days: int = 540
) -> tuple[list[str], int]:
    """Plan CSV originals from complete saved lists, never extracted metrics.

    The requested origin window and native event selection stay fixed.
    Older origins resolve edits/withdrawals but cannot become new candidates.
    """
    start = asof - timedelta(days=lookback_days)
    with closing(connect_read_only(database)) as connection:
        connection.execute("BEGIN")
        day = start
        while day <= asof:
            if not date_covered(connection, "edinet_documents", day):
                raise EDINETProviderError("stored EDINET document list is unavailable")
            day += timedelta(days=1)
        documents = read_stored_documents(connection, through=asof, start=start)
        documents.extend(read_observed_documents(connection, through=start - timedelta(days=1)))
    return select_metric_archives(documents, origin_start=start)
