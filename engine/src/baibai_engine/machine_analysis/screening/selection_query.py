"""保存候補の読取専用入口。候補の再計算・保存は行わない。"""

from __future__ import annotations

import gzip
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from pydantic import TypeAdapter

from baibai_engine.machine_analysis.generation.contracts import GenerationMetadata
from baibai_engine.machine_analysis.screening.contracts import (
    ScreeningSelection,
)
from baibai_engine.primitives.json import encode_json
from baibai_engine.storage.sqlite.machine_analysis import connect_read_only

_CODEC = TypeAdapter(ScreeningSelection)


def _decode_selection(payload: bytes) -> ScreeningSelection:
    # 全社結果の直接JSON型検証による一時メモリ増を避ける。検証schemaは同じ。
    return _CODEC.validate_python(json.loads(gzip.decompress(payload).decode("utf-8")))


def read_selection(selection_id: str, *, path: Path | None = None) -> ScreeningSelection | None:
    with closing(connect_read_only(path)) as connection:
        row = connection.execute(
            "SELECT payload_gzip FROM screening_selection WHERE selection_id=?", (selection_id,)
        ).fetchone()
        return None if row is None else _decode_selection(row[0])


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
    row = _publication_data(
        path=path,
        selection_id=selection_id,
        before_as_of=before_as_of,
        as_of=as_of,
        known_at=known_at,
    )
    if row is None:
        return None
    identity, published_at, generation, compressed = row
    return ScreeningPublication(identity, published_at, generation, _decode_selection(compressed))


def matches_validated_publication(
    *,
    path: Path,
    selection_id: str,
    known_at: datetime,
    published_at: datetime | None,
    selection: ScreeningSelection,
) -> bool:
    """型検証済みのOperation入力を同時点の発行原本と照合する。"""
    row = _publication_data(path=path, selection_id=selection_id, known_at=known_at)
    if row is None:
        return False
    _, actual_time, _, compressed = row
    incoming = _canonical_bytes(selection)
    # 通常writerの正規JSONとの完全一致は、検証済み入力と値・型が同じ証拠。
    # 表記や省略が異なる旧保存のみ、従来の型読取・再出力を経て比較する。
    same = (
        gzip.decompress(compressed) == incoming
        or _canonical_bytes(_decode_selection(compressed)) == incoming
    )
    return same and actual_time == published_at


def _canonical_bytes(selection: ScreeningSelection) -> bytes:
    return encode_json(_CODEC.dump_python(selection, mode="json")).encode()


def _publication_data(
    *,
    path: Path,
    selection_id: str | None = None,
    before_as_of: date | None = None,
    as_of: date | None = None,
    known_at: datetime | None = None,
) -> tuple[str, datetime, GenerationMetadata, bytes] | None:
    if not path.is_file():
        return None
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        header = find_publication_header(
            connection,
            selection_id=selection_id,
            before_as_of=before_as_of,
            as_of=as_of,
            known_at=known_at,
        )
        if header is None:
            return None
        identity, published, _, _, _ = header
        metadata, payload = connection.execute(
            "SELECT g.header,s.payload_gzip FROM screening_selection s "
            "JOIN model_generation g USING(generation_id) WHERE s.selection_id=?",
            (identity,),
        ).fetchone()
        return (
            identity,
            datetime.fromisoformat(published),
            GenerationMetadata.model_validate_json(metadata),
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
    return connection.execute(
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
