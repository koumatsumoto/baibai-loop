"""保存候補の読取専用入口。候補の再計算・保存は行わない。"""

from __future__ import annotations

import gzip
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path

from pydantic import TypeAdapter

from baibai_engine.machine_analysis.generation.contracts import GenerationMetadata
from baibai_engine.machine_analysis.screening.contracts import (
    ScreeningSelection,
    validate_selection_source,
)
from baibai_engine.storage.sqlite.machine_analysis import connect_read_only

_CODEC = TypeAdapter(ScreeningSelection)


def _decode_selection(payload: bytes) -> ScreeningSelection:
    # 全社結果の直接JSON型検証による一時メモリ増を避ける。検証schemaは同じ。
    return _CODEC.validate_python(json.loads(gzip.decompress(payload).decode("utf-8")))


def read_selection(selection_id: str, *, path: Path | None = None) -> ScreeningSelection | None:
    with closing(connect_read_only(path)) as connection:
        row = connection.execute(
            "SELECT payload_gzip,generation_id FROM screening_selection WHERE selection_id=?",
            (selection_id,),
        ).fetchone()
        if row is None:
            return None
        selection = _decode_selection(row[0])
        validate_selection_source(
            selection,
            selection_id=selection_id,
            generation_id=row[1],
        )
        return selection


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
        connection.execute("BEGIN")
        row = _publication_data(
            connection,
            selection_id=selection_id,
            before_as_of=before_as_of,
            as_of=as_of,
            known_at=known_at,
        )
    if row is None:
        return None
    identity, published_at, generation, compressed = row
    selection = _decode_selection(compressed)
    validate_selection_source(
        selection,
        selection_id=identity,
        generation_id=generation.generation_id,
    )
    return ScreeningPublication(identity, published_at, generation, selection)


def read_case_publication(
    *, path: Path, selection_id: str, known_at: datetime
) -> tuple[datetime, str, ScreeningSelection] | None:
    """有限Caseを固定する原本の時計・保存JSONの指紋・同一本文を一回の読取から返す。"""
    if not path.is_file():
        return None
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        return read_case_publication_from_connection(
            connection, selection_id=selection_id, known_at=known_at
        )


def read_case_publication_from_connection(
    connection: sqlite3.Connection, *, selection_id: str, known_at: datetime
) -> tuple[datetime, str, ScreeningSelection] | None:
    row = _publication_data(connection, selection_id=selection_id, known_at=known_at)
    if row is None:
        return None
    _, actual_time, generation, compressed = row
    wire = gzip.decompress(compressed)
    digest = sha256(wire).hexdigest()
    parsed = json.loads(wire)
    del wire
    selection = _CODEC.validate_python(parsed)
    validate_selection_source(
        selection,
        selection_id=selection_id,
        generation_id=generation.generation_id,
    )
    return actual_time, digest, selection


def _publication_data(
    connection: sqlite3.Connection,
    *,
    selection_id: str | None = None,
    before_as_of: date | None = None,
    as_of: date | None = None,
    known_at: datetime | None = None,
) -> tuple[str, datetime, GenerationMetadata, bytes] | None:
    header = find_publication_header(
        connection,
        selection_id=selection_id,
        before_as_of=before_as_of,
        as_of=as_of,
        known_at=known_at,
    )
    if header is None:
        return None
    identity, published, generation, _, _ = header
    metadata, payload = connection.execute(
        "SELECT g.header,s.payload_gzip FROM screening_selection s "
        "JOIN model_generation g USING(generation_id) WHERE s.selection_id=?",
        (identity,),
    ).fetchone()
    saved_header = GenerationMetadata.model_validate_json(metadata)
    if saved_header.generation_id != generation:
        raise ValueError("publication differs from the chosen generation header")
    return (
        identity,
        datetime.fromisoformat(published),
        saved_header,
        payload,
    )


def find_publication_header(
    connection: sqlite3.Connection,
    *,
    selection_id: str | None = None,
    before_as_of: date | None = None,
    as_of: date | None = None,
    known_at: datetime | None = None,
) -> tuple[str, str, str, str, str] | None:
    """一覧と明示詳細で同じ時計・同じ発行物を選ぶ。本文は読まない。"""
    if known_at is not None and known_at.utcoffset() is None:
        raise ValueError("publication cutoff requires a UTC offset")
    row = connection.execute(
        "SELECT s.selection_id,s.published_at,g.generation_id,g.as_of,g.evaluation_time "
        "FROM screening_selection s JOIN model_generation g USING(generation_id) "
        "WHERE (:id IS NULL OR s.selection_id=:id) AND (:before IS NULL OR g.as_of<:before) "
        "AND (:as_of IS NULL OR g.as_of<=:as_of) "
        "AND (:cutoff IS NULL OR (run_clock(g.evaluation_time)<=run_clock(:cutoff) "
        "AND run_clock(s.published_at)<=run_clock(:cutoff))) "
        "ORDER BY g.as_of DESC,run_clock(s.published_at) DESC,s.selection_id DESC LIMIT 1",
        {
            "id": selection_id,
            "before": before_as_of.isoformat() if before_as_of else None,
            "as_of": as_of.isoformat() if as_of else None,
            "cutoff": known_at.isoformat() if known_at else None,
        },
    ).fetchone()
    return None if row is None else (row[0], row[1], row[2], row[3], row[4])
