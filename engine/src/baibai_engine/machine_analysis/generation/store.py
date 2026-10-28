"""生成モデルの保存・読取。候補政策を実行せず、個社の読取に全社展開を要求しない。"""

from __future__ import annotations

import gzip
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.machine_analysis import (
    connect_rw,
    initialize_run_store,
)

from baibai_engine.machine_analysis.generation.query import read_generation, read_model_inputs
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    ModelGeneration,
    ModelGenerationStream,
    ModelInputSlice,
)


class GenerationConflictError(ValueError):
    """同じ生成IDを別の入力・計算・結果へ上書きできない。"""


@dataclass(frozen=True, slots=True)
class GenerationPublication:
    created: bool
    model_count: int


class ModelGenerationStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    def publish(self, generation: ModelGeneration) -> bool:
        """生成結果を一括commitする。同一内容の再送はFalse、変更された再送は拒否。"""
        if not isinstance(generation, ModelGeneration):
            raise TypeError("publication requires a complete model generation")
        metadata = GenerationMetadata.model_validate(generation.model_dump(exclude={"models"}))
        return self._publish(metadata, generation.models).created

    def publish_stream(self, generation: ModelGenerationStream) -> GenerationPublication:
        """全生成列を一回消費し、途中失敗ではheaderを含め一切公開しない。"""
        return self._publish(generation.metadata, generation.models)

    def _publish(
        self, metadata: GenerationMetadata, models: Iterable[GeneratedModel]
    ) -> GenerationPublication:
        header = canonical_json(
            metadata.model_dump(mode="json", include=set(GenerationMetadata.model_fields))
        )
        identity = metadata.generation_id
        initialize_run_store(self._path)
        with closing(connect_rw(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT header FROM model_generation WHERE generation_id=?", (identity,)
                ).fetchone()
                if existing is not None and header != existing[0]:
                    raise GenerationConflictError("generation identity has different content")
                if existing is None:
                    connection.execute(
                        "INSERT INTO model_generation VALUES (?,?,?,?)",
                        (
                            identity,
                            metadata.as_of.isoformat(),
                            metadata.evaluation_time.isoformat(),
                            header,
                        ),
                    )
                seen: set[tuple[str, str]] = set()
                for model in models:
                    key = (model.model_id, model.subject)
                    if key in seen:
                        raise GenerationConflictError("generation has duplicate model identity")
                    seen.add(key)
                    payload = canonical_json(model.payload)
                    if existing is None:
                        connection.execute(
                            "INSERT INTO generated_model VALUES (?,?,?,?,?)",
                            (
                                identity,
                                *key,
                                model.calculation_revision,
                                gzip.compress(payload.encode(), mtime=0),
                            ),
                        )
                    else:
                        saved = connection.execute(
                            "SELECT calculation_revision,payload_gzip FROM generated_model "
                            "WHERE generation_id=? AND model_id=? AND subject=?",
                            (identity, *key),
                        ).fetchone()
                        if (
                            saved is None
                            or saved[0] != model.calculation_revision
                            or gzip.decompress(saved[1]).decode() != payload
                        ):
                            raise GenerationConflictError(
                                "generation identity has different model content"
                            )
                count = connection.execute(
                    "SELECT count(*) FROM generated_model WHERE generation_id=?", (identity,)
                ).fetchone()[0]
                if count != len(seen):
                    raise GenerationConflictError("generation identity has different model count")
                connection.commit()
                return GenerationPublication(created=existing is None, model_count=count)
            except BaseException:
                connection.rollback()
                raise

    def read(self, generation_id: str) -> ModelGeneration | None:
        return read_generation(generation_id, path=self._path)

    def read_subjects(
        self, generation_id: str, *, subjects: Sequence[str]
    ) -> ModelInputSlice | None:
        return read_model_inputs(generation_id, subjects=subjects, path=self._path)


def delete_generations(connection: sqlite3.Connection, identities: Sequence[str]) -> None:
    """呼出元のtransaction内で指定世代だけを削除する。参照元の処理は所有しない。"""
    for identity in identities:
        connection.execute("DELETE FROM generated_model WHERE generation_id=?", (identity,))
        connection.execute("DELETE FROM model_generation WHERE generation_id=?", (identity,))
