"""候補政策の実行済み結果を保持する。保存時に再選定・丸め・補充しない。"""

from __future__ import annotations

import gzip
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from baibai_engine.machine_analysis.generation.contracts import GenerationMetadata
from baibai_engine.machine_analysis.screening.contracts import (
    ScreeningSelection,
    validate_selection_source,
)
from baibai_engine.machine_analysis.screening.discovery_contract import (
    DiscoverySelection,
    require_discovery_stock_plan,
    require_publishable_rows,
)
from baibai_engine.primitives.json import encode_json
from baibai_engine.storage.sqlite.machine_analysis import connect_rw, initialize_run_store

from .selection_query import read_selection

_CODEC = TypeAdapter(ScreeningSelection)


class ScreeningSelectionStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    def publish(self, selection_id: str, selection: ScreeningSelection) -> bool:
        """本DBの運用結果として発行する。比較実験は別の作業DBへ保存する。"""
        if not selection_id:
            raise ValueError("selection_id must not be empty")
        if isinstance(selection.result, DiscoverySelection):
            require_publishable_rows(selection.result)
        # model_copyは制約を迂回する。全社snapshotを再構築せず政策設定だけ再検証する。
        policy = selection.result.policy
        type(policy).model_validate(policy.model_dump(mode="python"))
        if not set(selection.result.tickers).issubset(selection.universe_tickers):
            raise ValueError("selected companies must belong to the explicit universe")
        validate_selection_source(
            selection, selection_id=selection_id, generation_id=selection.generation_id
        )
        payload = encode_json(_CODEC.dump_python(selection, mode="json")).encode()
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
                if isinstance(selection.result, DiscoverySelection):
                    # Use the saved plan, not model availability or a new universe.
                    row = connection.execute(
                        "SELECT header FROM model_generation WHERE generation_id=?",
                        (selection.generation_id,),
                    ).fetchone()
                    if row is not None:
                        metadata = GenerationMetadata.model_validate_json(row[0])
                        if metadata.generation_id != selection.generation_id:
                            raise ValueError(
                                "discovery publication differs from its generation header"
                            )
                        require_discovery_stock_plan(metadata, selection.universe_tickers)
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
