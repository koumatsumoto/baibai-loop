"""生成結果の読取専用入口。個社の読取で全社を展開しない。"""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections.abc import Generator, Iterator, Sequence
from contextlib import closing, contextmanager
from itertools import groupby
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
    return _read(generation_id, collection=ModelGeneration, path=path)


def read_generation_metadata(
    generation_id: str, *, path: Path | None = None
) -> GenerationMetadata | None:
    """固定対象planをモデル本文の展開なしで読む。"""
    with closing(connect_read_only(path)) as connection:
        return _read_metadata(connection, generation_id)


def read_model_inputs(
    generation_id: str, *, subjects: Sequence[str], path: Path | None = None
) -> ModelInputSlice | None:
    return _read(
        generation_id,
        collection=ModelInputSlice,
        path=path,
        subjects=tuple(sorted(set(subjects))),
    )


def read_research_model_inputs(
    generation_id: str, *, targets: Sequence[str], path: Path | None = None
) -> ModelInputSlice | None:
    return _read(generation_id, collection=ModelInputSlice, path=path, research_targets=targets)


@contextmanager
def stream_generation_subjects(
    generation_id: str, *, subjects: Sequence[str], path: Path | None = None
) -> Iterator[tuple[GenerationMetadata, Iterator[tuple[str, tuple[GeneratedModel, ...]]]] | None]:
    """同一断面の対象別モデル列。with内で消費し、未生成の対象は返さない。"""
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        metadata = _read_metadata(connection, generation_id)
        if metadata is None:
            yield None
            return
        with closing(_subject_models(connection, generation_id, tuple(subjects))) as companies:
            yield metadata, companies


def _subject_models(
    connection: sqlite3.Connection, generation_id: str, subjects: Sequence[str]
) -> Generator[tuple[str, tuple[GeneratedModel, ...]]]:
    # 並べ替えるのは対象のキーだけ。payloadは同じ読取断面から主キーで個別に読む。
    keys = connection.execute(
        "SELECT subject,model_id FROM generated_model WHERE generation_id=? "
        "AND subject IN (SELECT value FROM json_each(?)) ORDER BY subject,model_id",
        (generation_id, canonical_json(tuple(subjects))),
    )
    for subject, group in groupby(keys, key=lambda item: item[0]):
        models: list[GeneratedModel] = []
        for _, model_id in group:
            saved = connection.execute(
                "SELECT calculation_revision,payload_gzip FROM generated_model "
                "WHERE generation_id=? AND model_id=? AND subject=?",
                (generation_id, model_id, subject),
            ).fetchone()
            models.append(_decode_model(model_id, subject, saved[0], saved[1]))
        yield subject, tuple(models)


def _read_metadata(connection: sqlite3.Connection, generation_id: str) -> GenerationMetadata | None:
    header = connection.execute(
        "SELECT header FROM model_generation WHERE generation_id=?", (generation_id,)
    ).fetchone()
    return None if header is None else GenerationMetadata.model_validate_json(header[0])


def _decode_model(
    model_id: str, subject: str, calculation_revision: str, payload_gzip: bytes
) -> GeneratedModel:
    return GeneratedModel(
        model_id=model_id,
        subject=subject,
        calculation_revision=calculation_revision,
        payload=json.loads(gzip.decompress(payload_gzip)),
    )


def _read[Collection: (ModelGeneration, ModelInputSlice)](
    generation_id: str,
    *,
    collection: type[Collection],
    path: Path | None,
    subjects: Sequence[str] | None = None,
    research_targets: Sequence[str] | None = None,
) -> Collection | None:
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        metadata = _read_metadata(connection, generation_id)
        if metadata is None:
            return None
        if research_targets is not None:
            subjects = research_subjects(metadata, research_targets)
        scope = None if subjects is None else canonical_json(tuple(subjects))
        rows = connection.execute(
            "SELECT model_id,subject,calculation_revision,payload_gzip FROM generated_model "
            "WHERE generation_id=? AND (? IS NULL OR subject IN "
            "(SELECT value FROM json_each(?))) ORDER BY model_id,subject",
            (generation_id, scope, scope),
        )
        models = tuple(_decode_model(*row) for row in rows)
        # Keep decoded models and nested metadata typed: dumping a temporary
        # generation would recursively copy every retained owner payload again.
        fields = dict(metadata)
        if subjects is not None:
            fields["subjects"] = tuple(subjects)
            if metadata.subject_plan is not None:
                fields["subject_plan"] = {
                    subject: definition
                    for subject, definition in metadata.subject_plan.items()
                    if subject in subjects
                }
        return collection(**fields, models=models)
