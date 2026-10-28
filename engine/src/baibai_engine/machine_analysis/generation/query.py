"""生成結果の読取専用入口。個社の読取で全社を展開しない。"""

from __future__ import annotations

import gzip
import json
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    ModelGeneration,
    ModelInputSlice,
    research_subjects,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.machine_analysis import connect_read_only


def read_generation(generation_id: str, *, path: Path | None = None) -> ModelGeneration | None:
    return _read(generation_id, path=path)


def read_model_inputs(
    generation_id: str, *, subjects: Sequence[str], path: Path | None = None
) -> ModelInputSlice | None:
    scope = tuple(sorted(set(subjects)))
    generation = _read(generation_id, path=path, subjects=scope)
    return _slice(generation, scope)


def _slice(generation: ModelGeneration | None, subjects: Sequence[str]) -> ModelInputSlice | None:
    if generation is None:
        return None
    payload = generation.model_dump()
    if generation.subject_plan is not None:
        payload["subject_plan"] = {
            subject: definition.model_dump()
            for subject, definition in generation.subject_plan.items()
            if subject in subjects
        }
    return ModelInputSlice.model_validate({**payload, "subjects": tuple(subjects)})


def read_research_model_inputs(
    generation_id: str, *, targets: Sequence[str], path: Path | None = None
) -> ModelInputSlice | None:
    generation = _read(generation_id, path=path, research_targets=targets)
    return (
        None if generation is None else _slice(generation, research_subjects(generation, targets))
    )


def _read(
    generation_id: str,
    *,
    path: Path | None,
    subjects: Sequence[str] | None = None,
    research_targets: Sequence[str] | None = None,
) -> ModelGeneration | None:
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        header = connection.execute(
            "SELECT header FROM model_generation WHERE generation_id=?", (generation_id,)
        ).fetchone()
        if header is None:
            return None
        metadata = GenerationMetadata.model_validate_json(header[0])
        if research_targets is not None:
            subjects = research_subjects(metadata, research_targets)
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
