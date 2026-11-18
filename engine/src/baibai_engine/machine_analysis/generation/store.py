"""生成モデルの保存。候補政策を実行しない。"""

from __future__ import annotations

import gzip
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from baibai_engine.machine_analysis.generation.contracts import (
    GenerationMetadata,
    ModelGenerationStream,
    validate_full_plan,
    validate_model_scope,
)
from baibai_engine.machine_analysis.generation.growth_storage import (
    GrowthWriter,
    model_bytes,
    preserve_growth_before_deleting,
)
from baibai_engine.primitives.json import canonical_json, encode_json
from baibai_engine.storage.sqlite.machine_analysis import (
    SHARED_GROWTH_STORE_VERSION,
    connect_rw,
    initialize_run_store,
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

    def publish_stream(self, generation: ModelGenerationStream) -> GenerationPublication:
        """全生成列を一回消費し、途中失敗ではheaderを含め一切公開しない。"""
        # Typed copies and mutable nested inputs can invalidate a once-checked header.
        metadata = GenerationMetadata.model_validate(
            generation.metadata.model_dump(
                mode="python", include=set(GenerationMetadata.model_fields)
            )
        )
        validate_full_plan(metadata)
        header = canonical_json(
            metadata.model_dump(mode="json", include=set(GenerationMetadata.model_fields))
        )
        identity = metadata.generation_id
        version = initialize_run_store(self._path)
        with closing(connect_rw(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT header FROM model_generation WHERE generation_id=?", (identity,)
                ).fetchone()
                if existing is not None and header != existing[0]:
                    raise GenerationConflictError("generation identity has different content")
                growth = None
                if existing is None:
                    # The physical body must already have been fixed at this cutoff.
                    previous = connection.execute(
                        "SELECT generation_id FROM model_generation WHERE as_of<=? "
                        "AND run_clock(evaluation_time)<=run_clock(?) "
                        "ORDER BY as_of DESC,run_clock(evaluation_time) DESC, "
                        "generation_id DESC LIMIT 1",
                        (metadata.as_of.isoformat(), metadata.evaluation_time.isoformat()),
                    ).fetchone()
                    growth = None if previous is None else GrowthWriter(previous[0])
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
                for model in generation.models:
                    validate_model_scope(metadata, model)
                    key = (model.model_id, model.subject)
                    if key in seen:
                        raise GenerationConflictError("generation has duplicate model identity")
                    seen.add(key)
                    # GeneratedModel has already validated this tree as JsonValue.
                    # Keep canonical bytes without normalizing/copying it again.
                    if existing is None:
                        if growth is not None and model.model_id == "company_growth":
                            full = growth.store(
                                connection,
                                model.subject,
                                model.calculation_revision,
                                model.payload,
                            )
                            if full.startswith(b"\0") and version != SHARED_GROWTH_STORE_VERSION:
                                connection.execute(
                                    f"PRAGMA user_version={SHARED_GROWTH_STORE_VERSION}"
                                )
                                version = SHARED_GROWTH_STORE_VERSION
                        else:
                            full = gzip.compress(encode_json(model.payload).encode(), mtime=0)
                        connection.execute(
                            "INSERT INTO generated_model VALUES (?,?,?,?,?)",
                            (
                                identity,
                                *key,
                                model.calculation_revision,
                                full,
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
                            or model_bytes(connection, *key, saved[0], saved[1]).decode()
                            != encode_json(model.payload)
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


def delete_generations(connection: sqlite3.Connection, identities: Sequence[str]) -> None:
    """呼出元のtransaction内で指定世代だけを削除する。参照元の処理は所有しない。"""
    preserve_growth_before_deleting(connection, set(identities))
    for identity in identities:
        connection.execute("DELETE FROM generated_model WHERE generation_id=?", (identity,))
        connection.execute("DELETE FROM model_generation WHERE generation_id=?", (identity,))
