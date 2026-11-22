"""通常は索引を読み、索引のない発行物はheaderだけ返す。原本の明示読は別経路。"""

from __future__ import annotations

import gzip
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from pydantic import TypeAdapter

from baibai_engine.machine_analysis.screening.contracts import (
    ScreeningSelection,
    validate_selection_source,
)
from baibai_engine.machine_analysis.screening.discovery_contract import (
    DiscoveryIndexRead,
    DiscoverySelection,
    is_discovery_publication,
)
from baibai_engine.machine_analysis.screening.selection_query import find_publication_header
from baibai_engine.storage.sqlite.machine_analysis import connect_read_only


@dataclass(frozen=True, slots=True)
class _IndexedPublication(ScreeningSelection):
    result: DiscoverySelection


_CODEC = TypeAdapter(_IndexedPublication)


def read_discovery_index(
    *,
    path: Path,
    selection_id: str | None = None,
    as_of: date | None = None,
    known_at: datetime | None = None,
) -> DiscoveryIndexRead:
    if not path.is_file():
        return DiscoveryIndexRead("no_publication")
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        header = find_publication_header(
            connection, selection_id=selection_id, as_of=as_of, known_at=known_at
        )
        if header is None:
            return DiscoveryIndexRead("no_publication")
        identity, published, generation, day, evaluation = header
        clocks = (
            identity,
            generation,
            date.fromisoformat(day),
            datetime.fromisoformat(evaluation),
            datetime.fromisoformat(published),
        )
        if not is_discovery_publication(identity, generation):
            return DiscoveryIndexRead("publication_without_index", *clocks)
        (wire,) = connection.execute(
            "SELECT payload_gzip FROM screening_selection WHERE selection_id=?", (identity,)
        ).fetchone()
        saved = _CODEC.validate_json(gzip.decompress(wire))
        validate_selection_source(saved, selection_id=identity, generation_id=generation)
        return DiscoveryIndexRead("indexed", *clocks, saved.result.rows, saved.universe_tickers)
