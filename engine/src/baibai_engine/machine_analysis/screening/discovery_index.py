"""通常の探索では全社カードを開かない。旧発行物の明示読は別経路。"""

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
    validate_selection_source,
)
from baibai_engine.machine_analysis.screening.discovery_contract import (
    DISCOVERY_V2,
    DiscoveryIndexRead,
    DiscoverySelection,
    DiscoverySelectionV2,
    discovery_method_for_id,
)
from baibai_engine.machine_analysis.screening.selection_query import find_publication_header
from baibai_engine.storage.sqlite.machine_analysis import connect_read_only


@dataclass(frozen=True, slots=True)
class _IndexedPublication(ScreeningSelection):
    result: DiscoverySelection


# The publication identity already selects this format; do not validate old full-card unions.
@dataclass(frozen=True, slots=True)
class _IndexedPublicationV2(ScreeningSelection):
    result: DiscoverySelectionV2


_V1_CODEC = TypeAdapter(_IndexedPublication)
_V2_CODEC = TypeAdapter(_IndexedPublicationV2)


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
        method = discovery_method_for_id(identity, generation)
        if method is None:
            return DiscoveryIndexRead("unindexed_legacy", *clocks)
        metadata = None
        saved: _IndexedPublication | _IndexedPublicationV2
        if method == DISCOVERY_V2:
            wire, generation_header = connection.execute(
                "SELECT s.payload_gzip,g.header FROM screening_selection s "
                "JOIN model_generation g USING (generation_id) WHERE s.selection_id=?",
                (identity,),
            ).fetchone()
            metadata = GenerationMetadata.model_validate_json(generation_header)
            saved = _V2_CODEC.validate_json(gzip.decompress(wire))
        else:
            (wire,) = connection.execute(
                "SELECT payload_gzip FROM screening_selection WHERE selection_id=?", (identity,)
            ).fetchone()
            saved = _V1_CODEC.validate_json(gzip.decompress(wire))
        validate_selection_source(
            saved, selection_id=identity, generation_id=generation, metadata=metadata
        )
        return DiscoveryIndexRead(
            "indexed",
            *clocks,
            saved.result.rows,
            saved.universe_tickers,
            saved.result.price_session if isinstance(saved.result, DiscoverySelectionV2) else None,
        )
