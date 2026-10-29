"""公開済み候補政策を優先して再生成cacheを有限に保つ。"""

from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from baibai_engine.machine_analysis.generation.store import delete_generations
from baibai_engine.machine_analysis.screening.selection_store import delete_generation_selections
from baibai_engine.storage.sqlite.machine_analysis import connect_rw, initialize_run_store


def prune_model_cache(
    path: Path | None = None, *, keep: int = 3, preserve: Sequence[str] = ()
) -> tuple[str, ...]:
    """候補とモデルを同じtransactionで整理し、固定済みL3入力には触れない。

    bare生成は政策付き世代の後に置く。モデル生成自体は候補政策を知らず、
    この実行合成だけが両者の公開順と参照削除順を所有する。
    """
    if keep < 1:
        raise ValueError("keep must be positive")
    initialize_run_store(path)
    with closing(connect_rw(path)) as connection:
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
            delete_generation_selections(connection, deleted)
            delete_generations(connection, deleted)
            connection.commit()
            return deleted
        except BaseException:
            connection.rollback()
            raise
