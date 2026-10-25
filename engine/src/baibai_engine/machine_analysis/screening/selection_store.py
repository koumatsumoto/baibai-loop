"""候補政策の実行済み結果を保持する。保存時に再選定・丸め・補充しない。"""

from __future__ import annotations

import gzip
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from baibai_engine.foundation.json import canonical_json
from baibai_engine.storage.machine_analysis import connect_rw, initialize_run_store

from .model_projection import ScreeningSelection
from .selection_query import read_selection

_CODEC = TypeAdapter(ScreeningSelection)


class ScreeningSelectionStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    def publish(self, selection_id: str, selection: ScreeningSelection) -> bool:
        """本DBの運用結果として発行する。比較実験は別の作業DBへ保存する。"""
        if not selection_id:
            raise ValueError("selection_id must not be empty")
        if not set(selection.result.tickers).issubset(selection.universe_tickers):
            raise ValueError("selected companies must belong to the explicit universe")
        payload = canonical_json(_CODEC.dump_python(selection, mode="json")).encode()
        initialize_run_store(self._path)
        with closing(connect_rw(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT payload_gzip FROM screening_selection WHERE selection_id=?",
                    (selection_id,),
                ).fetchone()
                if existing is not None:
                    if gzip.decompress(existing[0]) != payload:
                        raise ValueError("selection identity has different content")
                    connection.commit()
                    return False
                connection.execute(
                    "INSERT INTO screening_selection VALUES (?,?,?,?)",
                    (
                        selection_id,
                        selection.generation_id,
                        datetime.now(UTC).isoformat(timespec="microseconds"),
                        gzip.compress(payload, mtime=0),
                    ),
                )
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise

    def read(self, selection_id: str) -> ScreeningSelection | None:
        return read_selection(selection_id, path=self._path)
