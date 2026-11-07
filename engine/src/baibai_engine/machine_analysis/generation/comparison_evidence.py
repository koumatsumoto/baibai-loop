"""既に取得済みのserving世代から、原本比較の任意seedだけを逐次読む。"""

from __future__ import annotations

import sqlite3
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.query import stream_generation_subjects


@dataclass(frozen=True, slots=True)
class SavedComparisonEvidence:
    """原本や新たなcacheではない。callerが固定した前回公開releaseの任意読取。"""

    market: InputSnapshot
    runs: Path

    def observations(
        self, *, tickers: Sequence[str], as_of: date, known_at: datetime
    ) -> Iterator[dict[str, JsonValue]]:
        if self.market.kind != "lake_release" or not self.runs.is_file():
            return
        generation_id = f"models-{self.market.reference}"
        try:
            with stream_generation_subjects(
                generation_id,
                subjects=tickers,
                model_ids=("company_financials",),
                path=self.runs,
            ) as saved:
                if saved is None:
                    return
                metadata, companies = saved
                if (
                    metadata.generation_id != generation_id
                    or metadata.inputs.get("market") != self.market
                    or metadata.as_of > as_of
                    or metadata.evaluation_time > known_at
                ):
                    return
                for ticker, models in companies:
                    for model in models:
                        financial = model.payload.get("financial")
                        reported = (
                            financial.get("reported_comparisons")
                            if isinstance(financial, dict)
                            else None
                        )
                        if not isinstance(reported, list):
                            continue
                        for item in reported:
                            observation = (
                                item.get("observation") if isinstance(item, dict) else None
                            )
                            if (
                                isinstance(observation, dict)
                                and observation.get("ticker") == ticker
                            ):
                                yield observation
        except (OSError, EOFError, ValueError, RuntimeError, sqlite3.Error, zlib.error):
            # Missing/legacy/damaged optional L2 never blocks regeneration from L1.
            return
