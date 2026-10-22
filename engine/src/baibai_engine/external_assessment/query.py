"""保存済み外部評価の読取入口。領域はUI合成read_apiへ依存しない。"""

from datetime import datetime
from pathlib import Path

from .analysis import analyze
from .models import ExternalAssessment
from .reader import read_observations, tradingview_rows_at_cutoff


def external_assessment(
    path: Path, *, ticker: str, cutoff: datetime, evaluated_at: datetime
) -> ExternalAssessment:
    return analyze(read_observations(path, ticker, cutoff), evaluated_at=evaluated_at)


def observed_tickers(path: Path, *, cutoff: datetime) -> list[str]:
    """Expose saved identities for Web export, including null/unresolved receipts."""
    from contextlib import closing

    from baibai_engine.external_assessment.observations.contract import (
        ANALYST_FORECAST_TABLE,
        ANALYST_RATING_TABLE,
        MODEL_VALUATION_TABLE,
        QUANTS_SCORE_TABLE,
    )
    from baibai_engine.external_assessment.observations.models import utc_datetime
    from baibai_engine.external_assessment.tradingview.contract import COLUMNS as TV_COLUMNS
    from baibai_engine.storage.observations.read import connect_read_only

    cutoff = utc_datetime(cutoff)
    if not path.is_file():
        return []
    tickers: set[str] = set()
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        for table in (
            MODEL_VALUATION_TABLE,
            ANALYST_RATING_TABLE,
            QUANTS_SCORE_TABLE,
            ANALYST_FORECAST_TABLE,
        ):
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
        tickers.update(
            str(row["ticker"])
            for row in tradingview_rows_at_cutoff(
                connection,
                cutoff,
                columns=tuple(
                    column for column in TV_COLUMNS if column[0] in {"ticker", "fetched_at_utc"}
                ),
            )
        )
    return sorted(tickers)
