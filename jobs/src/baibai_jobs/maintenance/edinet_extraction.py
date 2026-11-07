"""明示的なEDINET取得と保存原本の抽出を組み立てる。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol, TextIO

from baibai_engine.data_collection.providers.edinet.archive_native import FixedEdinetArchives
from baibai_engine.data_collection.providers.edinet.models import (
    EdinetAcquisitionError,
    EdinetResponse,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.service import (
    extract_edinet_facts_command,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.service import (
    EdinetMetricReader,
    extract_edinet_metrics_from_plan,
    prepare_edinet_metric_plan,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.storage.objects.r2 import Boto3R2Store, LakePublishError
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError
from baibai_jobs.acquisition.edinet_archives import metric_archive_candidates
from baibai_jobs.edinet_cutover import edinet_list_native_enabled


class EdinetMetricAcquisition(Protocol):
    def list_documents(self, on_date: date) -> list[dict[str, Any]]: ...

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
        if edinet_list_native_enabled():
            from baibai_engine.data_collection.providers.edinet.list_native import EdinetListL1
            from baibai_jobs.integration.edinet_list_native import materialize_lists

            with closing(
                Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
            ) as store:
                fixed = EdinetListL1(store, sqlite_path.parent / "edinet-list-read").freeze()
                result = materialize_lists(fixed, sqlite_path)
                print(json.dumps({"native_list_input": result}, sort_keys=True), file=out)
        else:
            cursor = asof_date - timedelta(days=lookback_days)
            while cursor <= asof_date:
                acquisition.list_documents(cursor)
                cursor += timedelta(days=1)
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
    archive_reader = (
        acquisition.retained_archives()
        if edinet_list_native_enabled() and plan.archive_candidates
        else reader
    )
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
