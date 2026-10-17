"""Explicit acquisition followed by offline EDINET metric extraction."""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import TextIO

from baibai_engine.collection.edinet.models import EdinetAcquisitionError
from baibai_engine.market.edinet_metrics.service import (
    extract_edinet_metrics_from_plan,
    prepare_edinet_metric_plan,
)
from baibai_engine.market.edinet_models import EDINETProviderError
from baibai_engine.storage.observations.schema import SQLiteSchemaError

from .providers import EDINETMaintenance, EDINETRead


def refresh_edinet_metrics_command(
    *,
    asof_date: date,
    lookback_days: int,
    reader: EDINETRead,
    acquisition: EDINETMaintenance,
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
