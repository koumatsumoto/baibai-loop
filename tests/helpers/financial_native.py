"""通常モデルのfixtureへ、正常空の日別財務原本を明示する。"""

from datetime import UTC, date, datetime
from pathlib import Path

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.financial_native import (
    FinancialAttempt,
    FinancialL1,
    FinancialObjectStore,
)


def seed_empty_financial(store: FinancialObjectStore, directory: Path, day: date) -> FinancialL1:
    owner = FinancialL1(store, directory)
    owner.initialize()
    at = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    owner.commit(
        [
            FinancialAttempt(
                f"empty-financial-{day}",
                day,
                at,
                (
                    JQuantsPage(
                        "financial_summaries", {"date": day.isoformat()}, at, b'{"data":[]}'
                    ),
                ),
            )
        ]
    )
    return owner
