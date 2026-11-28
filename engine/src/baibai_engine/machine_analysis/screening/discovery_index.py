"""現行索引を読み、旧索引は公表IDの明示時だけ開く。索引のない原本の明示読は別経路。"""

from __future__ import annotations

import gzip
import sqlite3
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
    DISCOVERY_METHOD,
    DiscoveryIndexRead,
    DiscoverySelection,
    discovery_publication_method,
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
        return read_discovery_index_from_connection(None)
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        return read_discovery_index_from_connection(
            connection, selection_id=selection_id, as_of=as_of, known_at=known_at
        )


def read_discovery_index_from_connection(
    connection: sqlite3.Connection | None,
    *,
    selection_id: str | None = None,
    as_of: date | None = None,
    known_at: datetime | None = None,
    expected_generation_id: str | None = None,
) -> DiscoveryIndexRead:
    """呼出元の読取transactionで公表物を一度だけ選ぶ。Noneは未作成。"""
    if (
        connection is None
        or (
            header := find_publication_header(
                connection,
                selection_id=selection_id,
                as_of=as_of,
                known_at=known_at,
                expected_generation_id=expected_generation_id,
            )
        )
        is None
    ):
        return DiscoveryIndexRead("no_publication")
    identity, published, generation, day, evaluation = header
    clocks = (
        identity,
        generation,
        date.fromisoformat(day),
        datetime.fromisoformat(evaluation),
        datetime.fromisoformat(published),
    )
    method = discovery_publication_method(identity, generation)
    if method is None:
        return DiscoveryIndexRead("publication_without_index", *clocks)
    # 最新が旧版なら、本文を開いたり別の現行版へ遡ったりしない。
    if selection_id is None and method != DISCOVERY_METHOD:
        return DiscoveryIndexRead("noncurrent_index", *clocks)
    (wire,) = connection.execute(
        "SELECT payload_gzip FROM screening_selection WHERE selection_id=?", (identity,)
    ).fetchone()
    saved = _CODEC.validate_json(gzip.decompress(wire))
    validate_selection_source(saved, selection_id=identity, generation_id=generation)
    return DiscoveryIndexRead("indexed", *clocks, saved.result.rows, saved.universe_tickers)
