"""通常モデルのfixtureへ日別の財務原本を固定する。"""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.financial_native import (
    FinancialAttempt,
    FinancialL1,
    FinancialObjectStore,
)
from baibai_engine.machine_analysis.generation.inputs import FinancialModelInput
from baibai_jobs.integration.financial_native import model_input
from tests.helpers.r2_store import MemoryR2Store


def seed_financial(
    store: FinancialObjectStore,
    directory: Path,
    *,
    by_day: Mapping[date, Sequence[Mapping[str, Any]]],
    received_at: datetime,
) -> FinancialL1:
    owner = FinancialL1(store, directory)
    owner.initialize()
    owner.commit(
        [
            FinancialAttempt(
                f"{'fixture' if rows else 'empty'}-financial-{day}",
                day,
                received_at,
                (
                    JQuantsPage(
                        "financial_summaries",
                        {"date": day.isoformat()},
                        received_at,
                        json.dumps({"data": rows}, separators=(",", ":")).encode(),
                    ),
                ),
            )
            for day, rows in sorted(by_day.items())
        ]
    )
    return owner


def seed_empty_financial(store: FinancialObjectStore, directory: Path, day: date) -> FinancialL1:
    at = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    return seed_financial(store, directory, by_day={day: ()}, received_at=at)


def fixed_empty_financial_input(directory: Path, day: date) -> FinancialModelInput:
    store = MemoryR2Store()
    owner = seed_empty_financial(store, directory, day)
    at = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    return model_input(store, owner.fixed(), as_of=day, evaluated_at=at)
