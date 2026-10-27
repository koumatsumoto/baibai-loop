"""生成モデルの保存・読取。候補政策を実行せず、個社の読取に全社展開を要求しない。"""

from __future__ import annotations

import gzip
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from baibai_engine.foundation.json import canonical_json
from baibai_engine.storage.machine_analysis import (
    connect_rw,
    initialize_run_store,
)

from .generation_query import read_generation, read_model_inputs
from .model_generation import (
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

    def prune(self, *, keep: int = 3, preserve: Sequence[str] = ()) -> tuple[str, ...]:
        """政策付き世代を基準日・公開順で優先し、再生成cacheだけを有限に保つ。

        bare生成は政策付き世代の後に置く。保存済みL3入力はapplication側の複写であり、
        本操作は原本・application・旧run・較正へ触れない。
        """
        if keep < 1:
            raise ValueError("keep must be positive")
        initialize_run_store(self._path)
        with closing(connect_rw(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    "SELECT g.generation_id FROM model_generation g "
                    "LEFT JOIN screening_selection s USING(generation_id) "
                    "ORDER BY (s.selection_id IS NULL),g.as_of DESC,"
                    "run_clock(COALESCE(s.published_at,g.evaluation_time)) DESC,"
                    "s.selection_id DESC,g.generation_id DESC"
                ).fetchall()
                ordered = list(dict.fromkeys(str(row[0]) for row in rows))
                retained = set(ordered[:keep]) | set(preserve)
                deleted = tuple(identity for identity in ordered if identity not in retained)
                for identity in deleted:
                    connection.execute(
                        "DELETE FROM screening_selection WHERE generation_id=?", (identity,)
                    )
                    connection.execute(
                        "DELETE FROM generated_model WHERE generation_id=?", (identity,)
                    )
                    connection.execute(
                        "DELETE FROM model_generation WHERE generation_id=?", (identity,)
                    )
                connection.commit()
                return deleted
            except BaseException:
                connection.rollback()
                raise
