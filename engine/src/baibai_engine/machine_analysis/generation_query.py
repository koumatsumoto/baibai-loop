"""生成結果の読取専用入口。個社の読取で全社を展開しない。"""

from __future__ import annotations

import gzip
import json
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from baibai_engine.foundation.json import canonical_json
from baibai_engine.storage.machine_analysis import connect_read_only

from .model_generation import GeneratedModel, ModelGeneration, ModelInputSlice


def read_generation(generation_id: str, *, path: Path | None = None) -> ModelGeneration | None:
    return _read(generation_id, path=path)


def read_model_inputs(
    generation_id: str, *, subjects: Sequence[str], path: Path | None = None
) -> ModelInputSlice | None:
    scope = tuple(sorted(set(subjects)))
    generation = _read(generation_id, path=path, subjects=scope)
    return (
        None
        if generation is None
        else ModelInputSlice.model_validate({**generation.model_dump(), "subjects": scope})
    )


def _read(
    generation_id: str, *, path: Path | None, subjects: Sequence[str] | None = None
) -> ModelGeneration | None:
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        header = connection.execute(
            "SELECT header FROM model_generation WHERE generation_id=?", (generation_id,)
        ).fetchone()
        if header is None:
            return None
        scope = None if subjects is None else canonical_json(tuple(subjects))
        rows = connection.execute(
            "SELECT model_id,subject,calculation_revision,payload_gzip FROM generated_model "
            "WHERE generation_id=? AND (? IS NULL OR subject IN "
            "(SELECT value FROM json_each(?))) ORDER BY model_id,subject",
            (generation_id, scope, scope),
        )
        models = tuple(
            GeneratedModel(
                model_id=row[0],
                subject=row[1],
                calculation_revision=row[2],
                payload=json.loads(gzip.decompress(row[3])),
            )
            for row in rows
        )
        return ModelGeneration.model_validate({**json.loads(header[0]), "models": models})
