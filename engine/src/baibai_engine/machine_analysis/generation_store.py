"""生成モデルの保存・読取。候補政策を実行せず、個社の読取に全社展開を要求しない。"""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from baibai_engine.foundation.json import canonical_json
from baibai_engine.storage.machine_analysis import (
    connect_read_only,
    connect_rw,
    initialize_run_store,
)

from .model_generation import GeneratedModel, ModelGeneration, ModelInputSlice


class GenerationConflictError(ValueError):
    """同じ生成IDを別の入力・計算・結果へ上書きできない。"""


class ModelGenerationStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    def publish(self, generation: ModelGeneration) -> bool:
        """生成結果を一括commitする。同一内容の再送はFalse、変更された再送は拒否。"""
        if not isinstance(generation, ModelGeneration):
            raise TypeError("publication requires a complete model generation")
        header = canonical_json(generation.model_dump(mode="json", exclude={"models"}))
        initialize_run_store(self._path)
        with closing(connect_rw(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT header FROM model_generation WHERE generation_id=?",
                    (generation.generation_id,),
                ).fetchone()
                if existing is not None:
                    self._require_identical(connection, generation, header, str(existing[0]))
                    connection.commit()
                    return False
                connection.execute(
                    "INSERT INTO model_generation VALUES (?,?,?,?)",
                    (
                        generation.generation_id,
                        generation.as_of.isoformat(),
                        generation.evaluation_time.isoformat(),
                        header,
                    ),
                )
                connection.executemany(
                    "INSERT INTO generated_model VALUES (?,?,?,?,?)",
                    (
                        (
                            generation.generation_id,
                            model.model_id,
                            model.subject,
                            model.calculation_revision,
                            gzip.compress(canonical_json(model.payload).encode(), mtime=0),
                        )
                        for model in generation.models
                    ),
                )
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise

    def read(self, generation_id: str) -> ModelGeneration | None:
        return self._read(generation_id)

    def read_subjects(
        self, generation_id: str, *, subjects: Sequence[str]
    ) -> ModelInputSlice | None:
        """必要対象のみをL3等へ渡す。共通市場はsubject='market'で明示する。"""
        scope = tuple(sorted(set(subjects)))
        generation = self._read(generation_id, subjects=scope)
        return (
            None
            if generation is None
            else ModelInputSlice.model_validate({**generation.model_dump(), "subjects": scope})
        )

    def _read(
        self, generation_id: str, *, subjects: Sequence[str] | None = None
    ) -> ModelGeneration | None:
        with closing(connect_read_only(self._path)) as connection:
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

    @staticmethod
    def _require_identical(
        connection: sqlite3.Connection,
        generation: ModelGeneration,
        header: str,
        saved_header: str,
    ) -> None:
        count = connection.execute(
            "SELECT count(*) FROM generated_model WHERE generation_id=?",
            (generation.generation_id,),
        ).fetchone()[0]
        if header != saved_header or count != len(generation.models):
            raise GenerationConflictError("generation identity has different content")
        for model in generation.models:
            saved = connection.execute(
                "SELECT calculation_revision,payload_gzip FROM generated_model "
                "WHERE generation_id=? AND model_id=? AND subject=?",
                (generation.generation_id, model.model_id, model.subject),
            ).fetchone()
            if (
                saved is None
                or saved[0] != model.calculation_revision
                or gzip.decompress(saved[1]).decode() != canonical_json(model.payload)
            ):
                raise GenerationConflictError("generation identity has different model content")
