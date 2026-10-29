"""固定releaseを持つ実行コピーから企業成長を読む。外部取得・保存をしない。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.machine_analysis.models.company_growth.generation import generate_company_growth
from baibai_engine.machine_analysis.models.company_growth.models import CompanyGrowth, InputRelease
from baibai_engine.machine_analysis.observations.financials.fixed_financial_reader import (
    read_fixed_financial_documents,
)
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.storage.snapshots.objects import LakeObjectError
from baibai_engine.storage.snapshots.reader import LakeReadError
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOriginError,
    read_lake_store_origin_from_connection,
)
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError


def company_growth(
    path: Path,
    *,
    ticker: str,
    as_of: date,
    start: date | None = None,
    end: date | None = None,
    mirror: Path = Path("stores"),
) -> CompanyGrowth:
    """Bounds select accounting period ends; all retained disclosure dates remain inputs.

    The release identifies the acquired input membership. Neither ``as_of`` nor
    Research's separate input_known_at rewinds acquisition to a past receipt cutoff.
    A later-disclosed correction of a displayed old period is intentionally included.
    """
    ticker = normalize_ticker(ticker)
    end = end or as_of
    if end > as_of or (start is not None and start > end):
        raise ValueError("company growth requires start <= end <= as_of")
    result = CompanyGrowth(
        ticker=ticker, status="unavailable", requested_start=start, requested_end=end
    )
    if not path.is_file():
        return result.model_copy(update={"reason": "market_store_unavailable"})
    try:
        with closing(connect_read_only(path)) as connection:
            connection.execute("BEGIN")
            origin = read_lake_store_origin_from_connection(connection)
            if origin is None:
                return result.model_copy(update={"reason": "fixed_release_unavailable"})
    except (sqlite3.Error, SQLiteSchemaError, LakeStoreOriginError):
        return result.model_copy(update={"reason": "market_store_unreadable"})
    # The SQLite marker chooses a release; it does not certify this working store's rows.
    try:
        fixed = read_fixed_financial_documents(mirror, origin=origin, ticker=ticker, as_of=as_of)
    except (LakeReadError, LakeObjectError, JQuantsProviderError, OSError):
        return result.model_copy(update={"reason": "fixed_release_inputs_unavailable"})
    return generate_company_growth(
        fixed.documents,
        ticker=ticker,
        sector=fixed.sector,
        as_of=as_of,
        input_release=InputRelease(
            release_id=origin.release_id, manifest_sha256=origin.release_manifest_sha256
        ),
        start=start,
        end=end,
    )
