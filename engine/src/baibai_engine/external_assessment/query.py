"""保存済み外部評価の読取入口。領域はUI合成read_apiへ依存しない。"""

from datetime import datetime
from pathlib import Path

from .analysis import analyze
from .models import ExternalAssessment
from .reader import read_observations


def external_assessment(
    path: Path, *, ticker: str, cutoff: datetime, evaluated_at: datetime
) -> ExternalAssessment:
    return analyze(read_observations(path, ticker, cutoff), evaluated_at=evaluated_at)


def observed_tickers(path: Path, *, cutoff: datetime) -> list[str]:
    """Expose saved identities for Web export, including null/unresolved receipts."""
    from contextlib import closing

    from baibai_engine.market.retail_dynamics.contract import (
        ANALYST_RATING_TABLE,
        MODEL_VALUATION_TABLE,
        QUANTS_SCORE_TABLE,
    )
    from baibai_engine.market.retail_dynamics.models import utc_datetime
    from baibai_engine.market.sqlite.read import connect_read_only
    from baibai_engine.market.tradingview.contract import TABLE

    cutoff = utc_datetime(cutoff)
    if not path.is_file():
        return []
    tickers: set[str] = set()
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        for table in (MODEL_VALUATION_TABLE, ANALYST_RATING_TABLE, QUANTS_SCORE_TABLE, TABLE):
            if (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                is not None
            ):
                rows = connection.execute(
                    f"SELECT DISTINCT ticker FROM {table} WHERE fetched_at_utc<=?",  # nosec B608
                    (cutoff.isoformat(timespec="microseconds"),),
                ).fetchall()
                tickers.update(str(row[0]) for row in rows)
    return sorted(tickers)
