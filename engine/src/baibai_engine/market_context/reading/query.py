"""保存済み指標からReadingを計算する読取。取得・判断・公開・UI合成へ依存しない。"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import cast

from baibai_engine.foundation.sqlite_read import connect_read_only, is_unwritten_store
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.indicators.read_contracts import point_in_time_providers
from baibai_engine.market_context.reading.compute import compute_reading
from baibai_engine.market_context.reading.models import ReadingSnapshot, snapshot_payload
from baibai_engine.market_context.reading.reader import build_store_observation_reader
from baibai_engine.market_context.reading.rules import (
    DEFAULT_RULES_PATH as READING_RULES_PATH,
)
from baibai_engine.market_context.reading.rules import load_reading_rules, rules_revision


def reading_snapshot(
    path: Path,
    *,
    asof: date,
    rules_path: Path = READING_RULES_PATH,
) -> dict[str, object] | None:
    """Compute the L2 reading from the indicator store, or None when it is unavailable.

    The reading is a pure function of the store, the rules revision and the as-of date,
    so a read-only consumer recomputes it instead of depending on a stored snapshot. A
    store that has not been written yields None so a consumer can hide the panel.
    Rules are trusted configuration: read or validation failures propagate and stop
    materialization, because publishing a fresh generation with the reading silently
    absent is unsafe.
    """

    if not path.is_file():
        return None
    connection = connect_read_only(path)
    try:
        connection.execute("BEGIN")
        snapshot = store_snapshot_payload(
            read_store_snapshot(connection, asof=asof, rules_path=rules_path)
        )
    except sqlite3.OperationalError as error:
        if not is_unwritten_store(error, path):
            raise
        return None
    finally:
        connection.close()
    return snapshot


def store_snapshot_payload(snapshot: ReadingSnapshot) -> dict[str, object]:
    """Describe store-reader selection; the arbitrary-reader pure serializer makes no PIT claim."""
    payload = snapshot_payload(snapshot)
    semantics: dict[str, object] = {
        "mode": "recomputed_from_current_store",
        "point_in_time_guaranteed": False,
        "known_at_applied": False,
        "limitation": (
            "as_ofは観測の対象日です。公表vintageを持つ提供元だけを対象日で制限します。"
            "その他は現在の保存履歴から後日取得・改定済みの値を含めて再計算するため、"
            "当時知り得た値の再現を保証しません。Researchのknown_at/received_cutoffも適用しません。"
        ),
    }
    payload["read_semantics"] = semantics
    providers = {item.series_id: item.provider for item in load_definitions().series}
    publication_providers = point_in_time_providers()
    rows = cast(list[dict[str, object]], payload["series"])
    for row in rows:
        publication = providers[str(row["series_id"])] in publication_providers
        row["read_semantics"] = {
            **semantics,
            "vintage_selection": "publication_vintage_cutoff"
            if publication
            else "latest_stored_history",
            "vintage_cutoff": snapshot.asof.isoformat() if publication else None,
        }
    return payload


def read_store_snapshot(
    connection: sqlite3.Connection, *, asof: date, rules_path: Path
) -> ReadingSnapshot:
    definitions = load_definitions()
    return compute_reading(
        series=definitions.series,
        reader=build_store_observation_reader(connection, series=definitions.series),
        rules=load_reading_rules(rules_path),
        rules_revision=rules_revision(rules_path),
        asof=asof,
    )


__all__ = [
    "READING_RULES_PATH",
    "read_store_snapshot",
    "reading_snapshot",
    "store_snapshot_payload",
]
