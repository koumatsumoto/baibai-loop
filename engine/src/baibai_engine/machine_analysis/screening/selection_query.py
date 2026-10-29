"""保存候補の読取専用入口。候補の再計算・保存は行わない。"""

from __future__ import annotations

import gzip
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from pydantic import TypeAdapter

from baibai_engine.machine_analysis.generation.contracts import GenerationMetadata
from baibai_engine.machine_analysis.screening.contracts import (
    ScreeningSelection,
)
from baibai_engine.storage.sqlite.machine_analysis import connect_read_only

_CODEC = TypeAdapter(ScreeningSelection)


def read_selection(selection_id: str, *, path: Path | None = None) -> ScreeningSelection | None:
    with closing(connect_read_only(path)) as connection:
        row = connection.execute(
            "SELECT payload_gzip FROM screening_selection WHERE selection_id=?", (selection_id,)
        ).fetchone()
        return None if row is None else _CODEC.validate_json(gzip.decompress(row[0]))


@dataclass(frozen=True, slots=True)
class ScreeningPublication:
    selection_id: str
    published_at: datetime
    generation: GenerationMetadata
    selection: ScreeningSelection


def read_publication(
    *,
    path: Path,
    selection_id: str | None = None,
    before_as_of: date | None = None,
    as_of: date | None = None,
    known_at: datetime | None = None,
) -> ScreeningPublication | None:
    """基準日の新しさを優先し、同日は実際の発行時刻で選ぶ。再発行は不変。"""
    if not path.is_file():
        return None
    with closing(connect_read_only(path)) as connection:
        row = connection.execute(
            "SELECT s.selection_id,s.published_at,g.header,s.payload_gzip "
            "FROM screening_selection s JOIN model_generation g USING(generation_id) "
            "WHERE (? IS NULL OR s.selection_id=?) AND (? IS NULL OR g.as_of<?) "
            "AND (? IS NULL OR g.as_of<=?) "
            "AND (? IS NULL OR (run_clock(g.evaluation_time)<=run_clock(?) "
            "AND run_clock(s.published_at)<=run_clock(?))) "
            "ORDER BY g.as_of DESC,run_clock(s.published_at) DESC,s.selection_id DESC LIMIT 1",
            (
                selection_id,
                selection_id,
                None if before_as_of is None else before_as_of.isoformat(),
                None if before_as_of is None else before_as_of.isoformat(),
                None if as_of is None else as_of.isoformat(),
                None if as_of is None else as_of.isoformat(),
                None if known_at is None else known_at.isoformat(),
                None if known_at is None else known_at.isoformat(),
                None if known_at is None else known_at.isoformat(),
            ),
        ).fetchone()
        if row is None:
            return None
        return ScreeningPublication(
            selection_id=row[0],
            published_at=datetime.fromisoformat(row[1]),
            generation=GenerationMetadata.model_validate_json(row[2]),
            selection=_CODEC.validate_json(gzip.decompress(row[3])),
        )
