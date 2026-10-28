"""保存済み旧Screening cacheの明示整理。新規runの公開は行わない。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from baibai_engine.storage.sqlite.machine_analysis import (
    DEFAULT_RUN_STORE_PATH,
    connect_rw,
    initialize_run_store,
    run_store_path,
)


@dataclass(frozen=True, slots=True)
class PruneResult:
    kept_runs: int
    deleted_runs: int
    deleted_stock_analyses: int
    bytes_before: int
    bytes_after: int


class ScreeningRunStore:
    """旧runの保守だけを所有し、読み手は現在の候補算術をロードしない。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    def prune(self, *, keep: int = 3) -> PruneResult:
        if keep < 0:
            raise ValueError("keep must be zero or greater")
        initialize_run_store(self._path)
        path = run_store_path(self._path)
        bytes_before = path.stat().st_size
        deleted_stock_analyses = 0
        with closing(connect_rw(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                ordered = connection.execute(
                    """
                    SELECT run_revision_id FROM screening_run
                    ORDER BY asof_date DESC, run_clock(run_at) DESC, run_revision_id DESC
                    """
                ).fetchall()
                deleted_ids = [str(row[0]) for row in ordered[keep:]]
                if deleted_ids:
                    connection.execute(
                        """
                        CREATE TEMP TABLE prune_run_id (
                            run_revision_id TEXT PRIMARY KEY
                        ) WITHOUT ROWID
                        """
                    )
                    connection.executemany(
                        "INSERT INTO prune_run_id (run_revision_id) VALUES (?)",
                        ((run_revision_id,) for run_revision_id in deleted_ids),
                    )
                    deleted_stock_analyses = int(
                        connection.execute(
                            """
                            SELECT count(*) FROM stock_analysis
                            WHERE run_revision_id IN (SELECT run_revision_id FROM prune_run_id)
                            """
                        ).fetchone()[0]
                    )
                    connection.execute(
                        """
                        DELETE FROM stock_analysis
                        WHERE run_revision_id IN (SELECT run_revision_id FROM prune_run_id)
                        """
                    )
                    connection.execute(
                        """
                        DELETE FROM screening_run
                        WHERE run_revision_id IN (SELECT run_revision_id FROM prune_run_id)
                        """
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            try:
                connection.execute("VACUUM")
            except sqlite3.Error as error:
                raise sqlite3.OperationalError(
                    "run rows were pruned but VACUUM failed; rerun prune to compact the store"
                ) from error
        return PruneResult(
            kept_runs=min(keep, len(ordered)),
            deleted_runs=len(deleted_ids),
            deleted_stock_analyses=deleted_stock_analyses,
            bytes_before=bytes_before,
            bytes_after=path.stat().st_size,
        )


__all__ = [
    "DEFAULT_RUN_STORE_PATH",
    "PruneResult",
    "ScreeningRunStore",
    "initialize_run_store",
    "run_store_path",
]
