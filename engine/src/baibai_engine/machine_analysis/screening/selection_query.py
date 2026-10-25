"""保存候補の読取専用入口。候補の再計算・保存は行わない。"""

from __future__ import annotations

import gzip
from contextlib import closing
from pathlib import Path

from pydantic import TypeAdapter

from baibai_engine.storage.machine_analysis import connect_read_only

from .model_projection import ScreeningSelection

_CODEC = TypeAdapter(ScreeningSelection)


def read_selection(selection_id: str, *, path: Path | None = None) -> ScreeningSelection | None:
    with closing(connect_read_only(path)) as connection:
        row = connection.execute(
            "SELECT payload_gzip FROM screening_selection WHERE selection_id=?", (selection_id,)
        ).fetchone()
        return None if row is None else _CODEC.validate_json(gzip.decompress(row[0]))
