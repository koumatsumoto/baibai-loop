"""保存世代を同一断面で読み、経済経路の候補政策へ渡す。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from baibai_engine.machine_analysis.generation.query import stream_generation_subjects
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import project_company
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    CompanyCoordinates,
    EconomicPathPolicy,
    select_economic_paths,
)
from baibai_engine.primitives.stocks import normalize_ticker


def economic_selection_id(generation_id: str, *, policy: EconomicPathPolicy) -> str:
    """新規候補のidentityを固定世代と政策methodに結びつける。"""
    return f"selection-{generation_id}-{policy.method}"


def select_saved_economic_generation(
    generation_id: str,
    *,
    tickers: Sequence[str],
    policy: EconomicPathPolicy,
    path: Path | None = None,
) -> ScreeningSelection:
    """全社を一つの読取断面で走査し、個社payloadを順に小さな座標へ縮める。"""
    subjects = tuple(sorted(normalize_ticker(ticker) for ticker in tickers))
    if len(set(subjects)) != len(subjects):
        raise ValueError("screening universe must contain unique companies")
    with stream_generation_subjects(generation_id, subjects=subjects, path=path) as generation:
        if generation is None:
            raise ValueError("model generation not found")
        metadata, companies = generation
        coordinates: dict[str, CompanyCoordinates] = {}
        for subject, models in companies:
            coordinates[subject] = project_company(subject, models=models, metadata=metadata)
            # 次社の復元前に、この社のpayload参照を手放す。
            del models
        for subject in subjects:
            if subject not in coordinates:
                coordinates[subject] = project_company(subject, models=(), metadata=metadata)
    return ScreeningSelection(
        generation_id=generation_id,
        universe_tickers=subjects,
        result=select_economic_paths(tuple(coordinates[t] for t in subjects), policy=policy),
    )
