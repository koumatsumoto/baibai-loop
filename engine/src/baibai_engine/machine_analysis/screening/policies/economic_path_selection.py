"""保存世代を同一断面で読み、経済経路の候補政策へ渡す。"""

from __future__ import annotations

import gzip
import json
from collections.abc import Sequence
from contextlib import closing
from itertools import groupby
from pathlib import Path

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel, GenerationMetadata
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import project_company
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    CompanyCoordinates,
    EconomicPathPolicy,
    select_economic_paths,
)
from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.storage.sqlite.machine_analysis import connect_read_only


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
    universe = set(subjects)
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT header FROM model_generation WHERE generation_id=?", (generation_id,)
        ).fetchone()
        if row is None:
            raise ValueError("model generation not found")
        metadata = GenerationMetadata.model_validate_json(row[0])
        rows = connection.execute(
            "SELECT subject,model_id,calculation_revision,payload_gzip FROM generated_model "
            "WHERE generation_id=? ORDER BY subject,model_id",
            (generation_id,),
        )
        coordinates: dict[str, CompanyCoordinates] = {}
        for subject, group in groupby(rows, key=lambda item: item[0]):
            if subject not in universe:
                continue
            models = tuple(
                GeneratedModel(
                    subject=subject,
                    model_id=item[1],
                    calculation_revision=item[2],
                    payload=json.loads(gzip.decompress(item[3])),
                )
                for item in group
            )
            coordinates[subject] = project_company(subject, models=models, metadata=metadata)
        for subject in subjects:
            if subject not in coordinates:
                coordinates[subject] = project_company(subject, models=(), metadata=metadata)
    return ScreeningSelection(
        generation_id,
        subjects,
        select_economic_paths(tuple(coordinates[t] for t in subjects), policy=policy),
    )
