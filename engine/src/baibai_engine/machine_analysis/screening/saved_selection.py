"""保存済み生成から政策が使う二モデルだけを同一読取断面で選ぶ。"""

from __future__ import annotations

import gzip
import json
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel, GenerationMetadata
from baibai_engine.storage.sqlite.machine_analysis import connect_read_only

from .model_projection import ScreeningSelection, _select_models
from .views import TwoViewPolicy


def select_saved_generation(
    generation_id: str, *, tickers: Sequence[str], policy: TwoViewPolicy, path: Path | None = None
) -> ScreeningSelection:
    """対象母集団を明示し、Growth等の大きな未使用payloadを展開しない。"""
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        header = connection.execute(
            "SELECT header FROM model_generation WHERE generation_id=?", (generation_id,)
        ).fetchone()
        if header is None:
            raise ValueError("model generation not found")
        metadata = GenerationMetadata.model_validate_json(header[0])
        rows = connection.execute(
            "SELECT model_id,subject,calculation_revision,payload_gzip FROM generated_model "
            "WHERE generation_id=? AND model_id IN ('company_valuation','technical_analysis') "
            "ORDER BY model_id,subject",
            (generation_id,),
        )
        models = (
            GeneratedModel(
                model_id=row[0],
                subject=row[1],
                calculation_revision=row[2],
                payload=json.loads(gzip.decompress(row[3])),
            )
            for row in rows
        )
        return _select_models(metadata.generation_id, models, tickers=tickers, policy=policy)
