"""経済原本の固定選択。本文は既存L1に残し、cold計算で現在の採用状態を使わない。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import sqlite3
import sys
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from baibai_engine.data_collection.catalog import DOCUMENT_PROVIDERS
from baibai_engine.data_collection.providers.tradingview.market_models import MARKET_SOURCE_ID
from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    validate_collection_confirmations,
)
from baibai_engine.data_collection.receipts.native_sources.documents import read_document_rows
from baibai_engine.data_collection.receipts.native_sources.records import read_snapshot_rows
from baibai_engine.data_collection.receipts.native_sources.store import (
    initialize_database,
    validate_current_schema,
)
from baibai_engine.machine_analysis.generation.contracts import Digest, InputSnapshot
from baibai_engine.machine_analysis.generation.runners.market_reading import (
    reading_observation_ranges,
    reading_snapshot,
)
from baibai_engine.machine_analysis.generation.runners.reading_rules import (
    DEFAULT_RULES_PATH,
    load_reading_rules,
)
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)
from baibai_engine.machine_analysis.observations.economic_indicators.reader import (
    source_record_ranges,
)
from baibai_engine.machine_analysis.observations.prices.tradingview_market import (
    stored_market_context,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.primitives.time import JST
from baibai_engine.storage.objects.json import put_private, read_private
from baibai_engine.storage.objects.r2 import LakePublishError, ObjectStore
from baibai_engine.storage.sqlite.read import connect_read_only

_PREFIX = "fixed-inputs/economic/v1/"
# Only these native columns can be fetched. Run error messages are never copied.
_COLUMNS = {
    "records": "source_id,observed_at,fetched_at,record_count,payload",
    "documents": "source_id,request_key,fetched_at,source_url,content_type,payload,metadata",
    "runs": "run_id,source_id,range_start,range_end,started_at,finished_at,status,record_count",
}
_KEYS = {
    "records": ("source_id", "observed_at", "fetched_at"),
    "documents": ("source_id", "request_key", "fetched_at"),
    "runs": ("run_id",),
}


@dataclass(frozen=True, slots=True)
class EconomicModelInput:
    """The contexts recalculated from one saved native selection and its clocks."""

    identity: InputSnapshot
    as_of: date
    evaluation_time: datetime
    context: dict[str, Any]


class _Fixed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Receipt(_Fixed):
    key: tuple[str, ...]
    sha256: Digest
    confirmed: bool = False


class _Month(_Fixed):
    records: tuple[_Receipt, ...] = ()
    documents: tuple[_Receipt, ...] = ()


class _Selection(_Fixed):
    format: Literal["economic-selection-v2"] = "economic-selection-v2"
    as_of: date
    evaluation_time: AwareDatetime
    recipe_sha256: Digest
    sources: tuple[str, ...]
    # Native bounds come from the Reading/observation owners. Documents retain
    # every latest request, and flows still start at date.min (publication days).
    record_ranges: dict[str, tuple[date, date]]
    months: dict[str, Digest]
    # Valid complete FRED state has one shared clock; never repeat it in each month.
    confirmations: dict[str, tuple[str, Digest] | None]
    runs: dict[str, _Receipt | None]


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _row_digest(row: tuple[Any, ...]) -> str:
    return _digest(
        [
            {"blob_sha256": hashlib.sha256(value).hexdigest()}
            if isinstance(value, bytes)
            else value
            for value in row
        ]
    )


def _recipe(rules_path: Path) -> str:
    """Require the same installed engine/config and runtime; never fetch old code.

    A conservative whole-package fingerprint also notices changes in parser helpers
    and source bindings. Unrelated engine edits can require the old installation;
    it is not a promise that a label or a Git branch can reconstruct that environment.
    """
    package = Path(__file__).resolve().parents[2]
    files = {
        str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(package.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".yaml", ".sql"}
    }
    return _digest(
        {
            "engine": files,
            "rules": hashlib.sha256(rules_path.read_bytes()).hexdigest(),
            "rules_name": rules_path.stem,
            "python": sys.version,
            "dependencies": sorted(
                (item.metadata["Name"], item.version) for item in importlib.metadata.distributions()
            ),
        }
    )


def _key(digest: str) -> str:
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("invalid economic selection digest")
    return _PREFIX + digest + ".json"


def _retain(store: ObjectStore, value: Any) -> str:
    payload = canonical_json(value).encode()
    digest = hashlib.sha256(payload).hexdigest()
    key = _key(digest)
    saved = read_private(store, key)
    if saved is None:
        try:
            put_private(store, key, payload, previous_etag=None)
        except LakePublishError:
            # A lost acknowledgement or concurrent identical write is resolved
            # by the exact immutable bytes, never by another identity.
            if (saved := read_private(store, key)) is None or saved.payload != payload:
                raise
        saved = read_private(store, key)
    if saved is None or saved.payload != payload:
        raise LakePublishError("economic selection object failed integrity")
    return digest


def _read(store: ObjectStore, digest: str) -> bytes:
    saved = read_private(store, _key(digest))
    if saved is None or hashlib.sha256(saved.payload).hexdigest() != digest:
        raise LakePublishError("economic selection object is missing or changed")
    return saved.payload


def _row(connection: sqlite3.Connection, kind: str, key: tuple[str, ...]) -> tuple[Any, ...]:
    names = _KEYS[kind]
    if len(key) != len(names):
        raise ValueError("invalid economic receipt identity")
    where = " AND ".join(f"{name}=?" for name in names)
    row = connection.execute(
        f"SELECT {_COLUMNS[kind]} FROM economic_source_{kind} WHERE {where}",  # nosec B608
        key,
    ).fetchone()
    if row is None:
        raise ValueError("fixed economic original is unavailable")
    return tuple(row)


def retain_economic_input(
    store: ObjectStore,
    source: Path,
    *,
    as_of: date,
    evaluation_time: datetime,
    rules_path: Path = DEFAULT_RULES_PATH,
) -> InputSnapshot:
    """Save native membership and acquisition state, never a daily SQLite/body copy.

    Both regular calculation and cold calculation use ``rebuild_economic_input``
    with the returned identity. The canonical native originals remain the owner.
    """
    if evaluation_time.utcoffset() is None:
        raise ValueError("economic input requires a timezone-aware evaluation time")
    definitions = load_definitions()
    record_ranges = source_record_ranges(
        series=definitions.series,
        requested=reading_observation_ranges(
            series=definitions.series, rules=load_reading_rules(rules_path), asof=as_of
        ),
    )
    sources = tuple(sorted({s.source_id for s in definitions.series if s.provider != "derived"}))
    document_sources = {s.source_id for s in definitions.series if s.provider in DOCUMENT_PROVIDERS}
    blocks: dict[str, dict[str, list[_Receipt]]] = {}
    confirmations = {}
    runs = {}
    recipe = _recipe(rules_path)
    with closing(connect_read_only(source)) as connection:
        connection.execute("BEGIN")
        validate_current_schema(connection)
        # This integrity check still reads complete FRED anchors. Narrowing the
        # projection is not a bounded-capture-I/O guarantee for these sources.
        validate_collection_confirmations(connection)

        def remember(kind: str, row: tuple[Any, ...], *, confirmed: bool = False) -> None:
            key = tuple(row[:3])
            # Native period or request month. Non-date request identities share
            # one source block; bodies still stay in the canonical receipt store.
            month = key[1][:7] if len(key[1]) >= 10 and key[1][4:5] == "-" else "documents"
            block = blocks.setdefault(key[0] + "/" + month, {"records": [], "documents": []})
            block[kind].append(_Receipt(key=key, sha256=_row_digest(row), confirmed=confirmed))

        for source_id in sources:
            state = connection.execute(
                "SELECT checked_at,content_sha256 FROM economic_source_collection_confirmations "
                "WHERE source_id=?",
                (source_id,),
            ).fetchone()
            confirmations[source_id] = tuple(state) if state is not None else None
            latest = connection.execute(
                "SELECT run_id FROM economic_source_runs WHERE source_id=? "
                "ORDER BY julianday(finished_at) DESC,finished_at DESC,run_id DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            runs[source_id] = (
                _Receipt(
                    key=tuple(latest), sha256=_row_digest(_row(connection, "runs", tuple(latest)))
                )
                if latest is not None
                else None
            )
            if source_id in document_sources:
                for document_row in read_document_rows(connection, source_id):
                    remember("documents", document_row)
            else:
                for row in read_snapshot_rows(
                    connection, *record_ranges[source_id], source_id=source_id
                ):
                    key = tuple(row[:3])
                    confirmed = (
                        connection.execute(
                            "SELECT 1 FROM economic_source_record_confirmations "
                            "WHERE source_id=? AND observed_at=?",
                            key[:2],
                        ).fetchone()
                        is not None
                    )
                    remember("records", row, confirmed=confirmed)

        # This is the same pre-interpretation boundary as stored_market_context:
        # 201st receipt proves truncation; failed/invalid/tied receipts are inputs.
        end = datetime.combine(as_of + timedelta(days=1), time.min, JST).astimezone(UTC)
        for key in connection.execute(
            "SELECT source_id,request_key,fetched_at FROM economic_source_documents "
            "WHERE source_id=? AND fetched_at<=? AND fetched_at<? "
            "ORDER BY fetched_at DESC,request_key DESC LIMIT 201",
            (
                MARKET_SOURCE_ID,
                evaluation_time.astimezone(UTC).isoformat(timespec="microseconds"),
                end.isoformat(timespec="microseconds"),
            ),
        ):
            remember("documents", _row(connection, "documents", tuple(key)))

    selection = _Selection(
        as_of=as_of,
        evaluation_time=evaluation_time,
        recipe_sha256=recipe,
        sources=sources,
        record_ranges=record_ranges,
        months={
            name: _retain(store, _Month(**rows).model_dump(mode="json"))
            for name, rows in sorted(blocks.items())
        },
        confirmations=confirmations,
        runs=runs,
    )
    descriptor = {
        "format": "economic-input-v2",
        "selection_sha256": _retain(store, selection.model_dump(mode="json")),
    }
    digest = _digest(descriptor)
    return InputSnapshot(
        kind="native_l1", reference="economic-" + digest, sha256=digest, descriptor=descriptor
    )


def rebuild_economic_input(
    store: ObjectStore,
    source: Path,
    saved: InputSnapshot,
    *,
    rules_path: Path = DEFAULT_RULES_PATH,
) -> EconomicModelInput:
    """Recalculate from exact original rows, not current adoption or an old L2 result.

    Canonical schema-13 originals must still exist. The temporary projection is
    calculation-only and never returned, published, or merged into an L1 store.
    """
    saved = InputSnapshot.model_validate_json(saved.model_dump_json())
    descriptor = saved.descriptor
    if (
        saved.kind != "native_l1"
        or descriptor is None
        or descriptor.get("format") != "economic-input-v2"
    ):
        raise ValueError("saved economic fixed selection is required")
    digest = descriptor.get("selection_sha256")
    if not isinstance(digest, str):
        raise ValueError("saved economic selection digest is required")
    selection = _Selection.model_validate_json(_read(store, digest))
    if selection.recipe_sha256 != _recipe(rules_path):
        raise ValueError("economic calculation recipe differs from the saved input")
    with (
        closing(connect_read_only(source)) as original,
        tempfile.TemporaryDirectory(prefix=".economic-fixed-projection-") as directory,
    ):
        original.execute("BEGIN")
        validate_current_schema(original)
        path = Path(directory) / "calculation.sqlite"
        with closing(initialize_database(path)) as projection, projection:

            def copy(kind: str, reference: _Receipt) -> tuple[Any, ...]:
                row = _row(original, kind, reference.key)
                if _row_digest(row) != reference.sha256:
                    raise ValueError("fixed economic original failed integrity")
                placeholders = ",".join("?" for _ in row)
                projection.execute(
                    f"INSERT INTO economic_source_{kind} ({_COLUMNS[kind]}) "  # nosec B608
                    f"VALUES ({placeholders})",
                    row,
                )
                return row

            for month_digest in selection.months.values():
                block = _Month.model_validate_json(_read(store, month_digest))
                for reference in block.records:
                    row = copy("records", reference)
                    if reference.confirmed:
                        state = selection.confirmations.get(reference.key[0])
                        if state is None:
                            raise ValueError("fixed period has no complete confirmation")
                        projection.execute(
                            "INSERT INTO economic_source_record_confirmations VALUES (?,?,?,?,?)",
                            (
                                *reference.key[:2],
                                state[0],
                                reference.key[2],
                                hashlib.sha256(row[4]).hexdigest(),
                            ),
                        )
                for reference in block.documents:
                    copy("documents", reference)
            # Whole-source confirmation is saved provenance, not a claim that
            # this selected-period calculation projection is a complete source.
            for run_reference in selection.runs.values():
                if run_reference is not None:
                    copy("runs", run_reference)
        return EconomicModelInput(
            identity=saved,
            as_of=selection.as_of,
            evaluation_time=selection.evaluation_time,
            context={
                "reading": reading_snapshot(path, asof=selection.as_of, rules_path=rules_path),
                "tradingview_market": stored_market_context(
                    path,
                    asof=selection.as_of,
                    known_at=selection.evaluation_time,
                ),
            },
        )
