"""固定releaseを持つ実行コピーから企業成長を読む。外部取得・保存をしない。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from itertools import pairwise
from pathlib import Path

from baibai_engine.collection.identifiers import normalize_ticker
from baibai_engine.market.financial_periods import period_days
from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.market.fixed_financial_reader import read_fixed_financial_documents
from baibai_engine.storage.lake.objects import LakeObjectError
from baibai_engine.storage.lake.reader import LakeReadError
from baibai_engine.storage.observations.lake_origin import (
    read_lake_store_origin_from_connection,
)
from baibai_engine.storage.observations.read import connect_read_only
from baibai_engine.storage.observations.schema import SQLiteSchemaError

from .calculations import build_changes, build_period_states
from .models import CompanyGrowth, Coverage, InputRelease


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
    except (sqlite3.Error, JQuantsProviderError, SQLiteSchemaError) as exc:
        reason = (
            "financial_documents_unreadable"
            if isinstance(exc, JQuantsProviderError)
            else "market_store_unreadable"
        )
        return result.model_copy(update={"reason": reason})
    # The SQLite marker chooses a release; it does not certify this working store's rows.
    try:
        fixed = read_fixed_financial_documents(mirror, origin=origin, ticker=ticker, as_of=as_of)
    except (LakeReadError, LakeObjectError, JQuantsProviderError):
        return result.model_copy(update={"reason": "fixed_release_inputs_unavailable"})
    documents = fixed.documents
    states = build_period_states(documents, sector=fixed.sector)
    changes = build_changes(states)
    observed = tuple(sorted({state.period_end for state in states}))
    displayed = tuple(
        s for s in states if (start is None or start <= s.period_end) and s.period_end <= end
    )
    return result.model_copy(
        update={
            "status": "available" if displayed else "unavailable",
            "reason": None if displayed else "accounting_period_not_observed",
            "input_release": InputRelease(
                release_id=origin.release_id, manifest_sha256=origin.release_manifest_sha256
            ),
            "periods": displayed,
            "changes": tuple(
                c for c in changes if (start is None or start <= c.to_end) and c.to_end <= end
            ),
            "coverage": Coverage(
                observed_start=observed[0] if observed else None,
                observed_end=observed[-1] if observed else None,
                observed_period_ends=observed,
                gaps_between_observations=tuple(
                    (a, b) for a, b in pairwise(observed) if (b - a).days > 110
                ),
                excluded_document_count=sum(period_days(row) is None for row in documents),
            ),
        }
    )
