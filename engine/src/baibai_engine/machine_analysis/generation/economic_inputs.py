"""経済原本の固定選択。本文は既存L1に残し、cold計算で現在の採用状態を使わない。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

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
from baibai_engine.storage.objects.json import MAX_PRIVATE_OBJECT_BYTES, put_private
from baibai_engine.storage.objects.r2 import LakePublishError, ObjectStore
from baibai_engine.storage.sqlite.read import connect_read_only

_PREFIX = "fixed-inputs/economic/v1/"
_MAX_RETAINED_BYTES = 64 * 1024 * 1024
_MAX_RETAINED_OBJECTS = 4096
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
    # Scoped days need their own real clock; FRED keeps its shared collection clock.
    checked_at: str | None = Field(default=None, exclude_if=lambda value: value is None)


class _Block(_Fixed):
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
    # Keep the saved wire name: old monthly and new yearly record blocks have
    # identical receipts, and readers resolve their digests, not calendar labels.
    months: dict[str, Digest]
    # Valid complete FRED state has one shared clock; never repeat it in each block.
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
    return _retain_verified(store, value)[0]


def _retain_verified(store: ObjectStore, value: Any) -> tuple[str, bytes]:
    payload = canonical_json(value).encode()
    digest = hashlib.sha256(payload).hexdigest()
    key = _key(digest)
    saved = _find(store, digest)
    if saved is None:
        try:
            put_private(store, key, payload, previous_etag=None)
        except LakePublishError:
            # A lost acknowledgement or concurrent identical write is resolved
            # by the exact immutable bytes, never by another identity.
            if (saved := _find(store, digest)) != payload:
                raise
        else:
            saved = _find(store, digest)
    if saved != payload:
        raise LakePublishError("economic selection object failed integrity")
    return digest, saved


def _find(store: ObjectStore, digest: str) -> bytes | None:
    key = _key(digest)
    metadata = store.head(key)
    if metadata is None:
        return None
    if (
        not metadata.etag
        or not 0 < metadata.size <= MAX_PRIVATE_OBJECT_BYTES
        or metadata.metadata.get("sha256") != digest
    ):
        raise LakePublishError("economic selection object is missing or changed")
    payload = store.get_bytes(key)
    # These keys name exact bytes, not a mutable generation. A post-GET HEAD
    # cannot strengthen the expected digest; keep the pre-read size bound and
    # validate the bytes actually received, including concurrent replacements.
    if len(payload) != metadata.size or hashlib.sha256(payload).hexdigest() != digest:
        raise LakePublishError("economic selection object is missing or changed")
    return payload


def _read(store: ObjectStore, digest: str) -> bytes:
    payload = _find(store, digest)
    if payload is None:
        raise LakePublishError("economic selection object is missing or changed")
    return payload


class _RetainedObjects:
    """Hand verified immutable metadata to the next read on this exact store only."""

    def __init__(self, store: ObjectStore, *, max_bytes: int) -> None:
        self._store = store
        self._max_bytes = max_bytes
        self._payloads: dict[str, bytes] = {}
        self._bytes = 0
        self._lock = Lock()

    def retain(self, value: Any) -> str:
        digest, payload = _retain_verified(self._store, value)
        with self._lock:
            if (
                digest not in self._payloads
                and len(self._payloads) < _MAX_RETAINED_OBJECTS
                and self._bytes + len(payload) <= self._max_bytes
            ):
                self._payloads[digest] = payload
                self._bytes += len(payload)
        return digest

    def read(self, digest: str) -> bytes:
        _key(digest)
        with self._lock:
            payload = self._payloads.get(digest)
        if payload is None:
            # Do not evict retained later blocks while reading an uncached prefix.
            return _read(self._store, digest)
        if (
            not 0 < len(payload) <= MAX_PRIVATE_OBJECT_BYTES
            or hashlib.sha256(payload).hexdigest() != digest
        ):
            raise LakePublishError("retained economic selection object failed integrity")
        return payload

    def clear(self) -> None:
        with self._lock:
            self._payloads.clear()
            self._bytes = 0


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

    Regular and cold calculation share the same reconstruction of these receipts.
    The canonical native originals remain the owner.
    """
    return _retain_economic_input(
        lambda value: _retain(store, value),
        source,
        as_of=as_of,
        evaluation_time=evaluation_time,
        rules_path=rules_path,
    )


def _retain_economic_input(
    retain: Callable[[Any], str],
    source: Path,
    *,
    as_of: date,
    evaluation_time: datetime,
    rules_path: Path,
) -> InputSnapshot:
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

        def remember(
            kind: str,
            row: tuple[Any, ...],
            *,
            confirmed: bool = False,
            checked_at: str | None = None,
        ) -> None:
            key = tuple(row[:3])
            # A selected record has at most one receipt per native day. Year
            # blocks reduce round trips while keeping unchanged years reusable.
            # Documents keep their existing request grouping and receipt order.
            if kind == "records":
                period = key[1][:4]
            elif len(key[1]) >= 10 and key[1][4:5] == "-":
                period = key[1][:7]
            else:
                period = "documents"
            block = blocks.setdefault(key[0] + "/" + period, {"records": [], "documents": []})
            block[kind].append(
                _Receipt(
                    key=key, sha256=_row_digest(row), confirmed=confirmed, checked_at=checked_at
                )
            )

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
                    check = connection.execute(
                        "SELECT checked_at FROM economic_source_record_confirmations "
                        "WHERE source_id=? AND observed_at=?",
                        key[:2],
                    ).fetchone()
                    complete = confirmations[source_id] is not None
                    remember(
                        "records",
                        row,
                        confirmed=check is not None and complete,
                        checked_at=str(check[0]) if check is not None and not complete else None,
                    )

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

    def retain_block(item: tuple[str, dict[str, list[_Receipt]]]) -> tuple[str, str]:
        name, rows = item
        return name, retain(_Block(**rows).model_dump(mode="json"))

    # Independent immutable objects otherwise serialize thousands of
    # network round trips. Keep their order and all per-object integrity checks.
    with ThreadPoolExecutor(max_workers=8) as workers:
        retained = dict(workers.map(retain_block, sorted(blocks.items()), buffersize=8))

    selection = _Selection(
        as_of=as_of,
        evaluation_time=evaluation_time,
        recipe_sha256=recipe,
        sources=sources,
        record_ranges=record_ranges,
        months=retained,
        confirmations=confirmations,
        runs=runs,
    )
    descriptor = {
        "format": "economic-input-v2",
        "selection_sha256": retain(selection.model_dump(mode="json")),
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
    return _rebuild_economic_input(
        lambda digest: _read(store, digest), source, saved, rules_path=rules_path
    )


def _rebuild_economic_input(
    read: Callable[[str], bytes],
    source: Path,
    saved: InputSnapshot,
    *,
    rules_path: Path,
) -> EconomicModelInput:
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
    selection = _Selection.model_validate_json(read(digest))
    if selection.recipe_sha256 != _recipe(rules_path):
        raise ValueError("economic calculation recipe differs from the saved input")
    with (
        closing(connect_read_only(source)) as original,
        tempfile.TemporaryDirectory(prefix=".economic-fixed-projection-") as directory,
    ):
        original.execute("BEGIN")
        validate_current_schema(original)
        path = Path(directory) / "calculation.sqlite"
        with (
            closing(initialize_database(path)) as projection,
            projection,
            ThreadPoolExecutor(max_workers=8) as workers,
        ):

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

            def read_block(block_digest: str) -> _Block:
                return _Block.model_validate_json(read(block_digest))

            # Only metadata transport runs concurrently. SQLite reads/writes
            # and fixed receipt checks stay on this transaction's owning thread.
            for block in workers.map(read_block, selection.months.values(), buffersize=8):
                for reference in block.records:
                    row = copy("records", reference)
                    checked_at = reference.checked_at
                    if reference.confirmed:
                        if checked_at is not None:
                            raise ValueError(
                                "fixed period has both scoped and complete confirmation"
                            )
                        state = selection.confirmations.get(reference.key[0])
                        if state is None:
                            raise ValueError("fixed period has no complete confirmation")
                        checked_at = state[0]
                    if checked_at is not None:
                        if checked_at < reference.key[2]:
                            raise ValueError("fixed confirmation predates its source receipt")
                        projection.execute(
                            "INSERT INTO economic_source_record_confirmations VALUES (?,?,?,?,?)",
                            (
                                *reference.key[:2],
                                checked_at,
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


class EconomicInputSession:
    """One retain/rebuild pair on one store, with bounded verified metadata reuse.

    The handoff does not cache native originals or calculated context. Cold reads
    still verify remote objects; all calculations check the recipe and each receipt.
    """

    def __init__(self, store: ObjectStore) -> None:
        self._objects = _RetainedObjects(store, max_bytes=_MAX_RETAINED_BYTES)
        self._state: Literal["new", "retaining", "retained", "closed"] = "new"
        self._saved_json: str | None = None

    def _close(self) -> None:
        self._state = "closed"
        self._saved_json = None
        self._objects.clear()

    def retain(
        self,
        source: Path,
        *,
        as_of: date,
        evaluation_time: datetime,
        rules_path: Path = DEFAULT_RULES_PATH,
    ) -> InputSnapshot:
        if self._state != "new":
            self._close()
            raise ValueError("economic input session retains once")
        self._state = "retaining"
        try:
            saved = _retain_economic_input(
                self._objects.retain,
                source,
                as_of=as_of,
                evaluation_time=evaluation_time,
                rules_path=rules_path,
            )
            # InputSnapshot freezes fields, but its descriptor still contains dicts.
            self._saved_json = canonical_json(saved.model_dump(mode="json"))
            self._state = "retained"
            return saved
        finally:
            if self._state != "retained":
                self._close()

    def rebuild(
        self,
        source: Path,
        saved: InputSnapshot,
        *,
        rules_path: Path = DEFAULT_RULES_PATH,
    ) -> EconomicModelInput:
        try:
            if (
                self._state != "retained"
                or canonical_json(saved.model_dump(mode="json")) != self._saved_json
            ):
                raise ValueError("economic input session requires its retained identity once")
            self._state = "closed"
            return _rebuild_economic_input(self._objects.read, source, saved, rules_path=rules_path)
        finally:
            self._close()
