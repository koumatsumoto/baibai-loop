"""Compare a screening pass over the legacy store with one over a lake release.

The lake pilot publishes two datasets. Everything else screening reads still lives
only in ``market.sqlite``, so a full cutover is not available yet and this module
does not pretend otherwise: it runs a controlled comparison in which the migrated
tables come from the release projection, every other table is held constant from
the legacy store, and both lists are reported. ``legacy_sourced_tables`` is the
blocker made visible — it is what still has to move before a cutover means anything.

The comparison is deterministic. Both sides run with the same as-of, the same rules
and the same clock, into their own run store, so the only fields allowed to differ
are the per-publication identifiers named in ``_RUN_IDENTITY_PATHS`` and
``_SELECTION_IDENTITY_PATHS``. Every other value, including ordering, must match.
"""

from __future__ import annotations

import io
import os
import shutil
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, TextIO

import yaml

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.market.lake.datasets import PILOT_DATASETS, LakeDataset
from baibai_engine.market.lake.projection import (
    ProjectionDataset,
    ProjectionError,
    read_projection_identity,
)

from .cli.providers import ProviderBundle
from .cli.query import select_command
from .cli.run import run_command
from .config import ScreeningConfig
from .providers.edinet import EDINETProvider
from .providers.jpx import JPXProvider
from .providers.jquants import JQuantsProvider
from .rule_config import ScreeningRules

# Per-publication identifiers, named as the flattened paths they appear at. Two
# runs of the same inputs mint new ones by construction, so these are the only
# values allowed to differ — and listing them explicitly is what keeps "allowed to
# differ" from quietly growing to cover a real drift.
_RUN_IDENTITY_PATHS = frozenset({"run_revision_id"})
_SELECTION_IDENTITY_PATHS = frozenset({"selection_id", "selection.input_refs.candidates_ref"})
_MAX_REPORTED_DIFFERENCES = 50

type ParityScope = Literal["candidate", "metric", "selection"]


class LakeShadowError(RuntimeError):
    """The shadow store cannot be built from the projection and the legacy store."""


@dataclass(frozen=True, slots=True)
class ShadowStoreReport:
    path: Path
    release_id: str
    release_sourced_tables: tuple[str, ...]
    legacy_sourced_tables: tuple[str, ...]
    replaced_rows: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class ParityDifference:
    scope: ParityScope
    key: str
    field: str
    legacy: str
    lake: str

    def render(self) -> str:
        return f"{self.scope} {self.key} {self.field}: legacy={self.legacy} lake={self.lake}"


@dataclass(frozen=True, slots=True)
class ScreeningParityReport:
    asof: str
    release_id: str
    release_sourced_tables: tuple[str, ...]
    legacy_sourced_tables: tuple[str, ...]
    candidates_compared: int
    selection_entries_compared: int
    differences: tuple[ParityDifference, ...]
    difference_counts: Mapping[str, int]
    total_differences: int

    @property
    def compared_nothing(self) -> bool:
        """True when neither side produced a candidate, so nothing was proven."""

        return self.candidates_compared == 0

    @property
    def matched(self) -> bool:
        """Agreement counts only when there was something to disagree about.

        Two runs that both produce an empty universe agree trivially. Reporting that
        as a match would let a broken input — a store the screen cannot read, an
        as-of with no data — pass as evidence that the release reproduces the legacy
        result.
        """

        return self.total_differences == 0 and not self.compared_nothing

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "lake_shadow_parity",
            "asof": self.asof,
            "release_id": self.release_id,
            "matched": self.matched,
            "compared_nothing": self.compared_nothing,
            "release_sourced_tables": list(self.release_sourced_tables),
            "legacy_sourced_tables": list(self.legacy_sourced_tables),
            "candidates_compared": self.candidates_compared,
            "selection_entries_compared": self.selection_entries_compared,
            "total_differences": self.total_differences,
            "difference_counts": dict(self.difference_counts),
            "differences": [item.render() for item in self.differences],
        }


def build_shadow_store(
    *, legacy_sqlite: Path, projection: Path, destination: Path
) -> ShadowStoreReport:
    """Copy the legacy store and replace the migrated tables from the projection.

    Holding the un-migrated tables constant is what makes the comparison isolate
    the datasets the release actually publishes. The report names both sides so a
    reader never has to infer which tables the release is answering for.
    """

    identity = read_projection_identity(projection)
    if identity is None:
        raise ProjectionError(f"projection is absent or unreadable: {projection}")
    datasets = _projection_datasets(identity.datasets)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.part")
    replaced: dict[str, int] = {}
    try:
        shutil.copyfile(legacy_sqlite, temporary)
        with closing(sqlite3.connect(temporary, uri=True)) as connection:
            connection.execute(
                "ATTACH DATABASE ? AS lake", (f"{projection.resolve().as_uri()}?mode=ro",)
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                for dataset in datasets:
                    replaced[dataset.sqlite_table] = _replace_table(connection, dataset)
                legacy_tables = _remaining_tables(connection, replaced=tuple(replaced))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            connection.execute("DETACH DATABASE lake")
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return ShadowStoreReport(
        path=destination,
        release_id=identity.source_release_id,
        release_sourced_tables=tuple(sorted(replaced)),
        legacy_sourced_tables=legacy_tables,
        replaced_rows=replaced,
    )


def _projection_datasets(entries: Sequence[ProjectionDataset]) -> tuple[LakeDataset, ...]:
    names = tuple(entry.dataset for entry in entries)
    unknown = [name for name in names if name not in PILOT_DATASETS]
    if unknown:
        raise LakeShadowError(f"projection holds a dataset this shadow cannot place: {unknown[0]}")
    return tuple(PILOT_DATASETS[name] for name in sorted(names))


def _replace_table(connection: sqlite3.Connection, dataset: LakeDataset) -> int:
    columns = ", ".join(column.name for column in dataset.columns)
    connection.execute(f"DELETE FROM main.{dataset.sqlite_table}")  # nosec B608
    connection.execute(
        f"INSERT INTO main.{dataset.sqlite_table}({columns}) "  # nosec B608
        f"SELECT {columns} FROM lake.{dataset.sqlite_table}"
    )
    row = connection.execute(
        f"SELECT COUNT(*) FROM main.{dataset.sqlite_table}"  # nosec B608
    ).fetchone()
    return int(row[0])


def _remaining_tables(
    connection: sqlite3.Connection, *, replaced: Sequence[str]
) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT name FROM main.sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return tuple(str(row[0]) for row in rows if str(row[0]) not in set(replaced))


def cache_only_providers(config: ScreeningConfig, *, sqlite_path: Path) -> ProviderBundle:
    """Providers that may only read the given store — a miss is an error, not a fetch.

    ``cache_only`` is what keeps the comparison honest: if the projection were
    missing rows, a provider allowed to fall back would quietly refill them from
    the network and both sides would agree for the wrong reason.
    """

    return ProviderBundle(
        jquants=JQuantsProvider(
            config.jquants_api_key,
            config.cache_dir,
            sqlite_path=sqlite_path,
            cache_only=True,
        ),
        edinet=EDINETProvider(
            config.edinet_api_key,
            config.cache_dir,
            sqlite_path=sqlite_path,
            cache_only=True,
        ),
        jpx=JPXProvider(
            config.cache_dir,
            regulation_urls=config.jpx_regulation_urls,
            special_caution_index_url=config.jpx_special_caution_index_url,
            sqlite_path=sqlite_path,
            cache_only=True,
        ),
    )


@dataclass(frozen=True, slots=True)
class ScreeningSideResult:
    run: Mapping[str, object]
    selection: Mapping[str, object]


def run_lake_shadow_parity(
    *,
    asof: date,
    legacy_sqlite: Path,
    projection: Path,
    workspace: Path,
    config: ScreeningConfig,
    rules: ScreeningRules,
    now: datetime,
    app_db_path: Path,
    select_top: int,
    stdout: TextIO,
) -> ScreeningParityReport:
    """Run screening twice over the same as-of and report every allowed difference."""

    legacy_dir = workspace / "legacy"
    lake_dir = workspace / "lake"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    lake_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(legacy_sqlite, legacy_dir / "market.sqlite")
    shadow = build_shadow_store(
        legacy_sqlite=legacy_sqlite,
        projection=projection,
        destination=lake_dir / "market.sqlite",
    )
    print(
        f"lake shadow: release={shadow.release_id} "
        f"release_sourced={','.join(shadow.release_sourced_tables)} "
        f"legacy_sourced={len(shadow.legacy_sourced_tables)} table(s)",
        file=stdout,
        flush=True,
    )

    sides = {
        name: _run_side(
            directory=directory,
            asof=asof,
            config=config,
            rules=rules,
            now=now,
            app_db_path=app_db_path,
            select_top=select_top,
            stdout=stdout,
        )
        for name, directory in (("legacy", legacy_dir), ("lake", lake_dir))
    }
    differences = compare_sides(legacy=sides["legacy"], lake=sides["lake"])
    # The rendered list is capped so one broken input cannot produce an unreadable
    # report, and the per-scope counts carry what the cap leaves out.
    counts = {
        scope: sum(1 for item in differences if item.scope == scope)
        for scope in ("candidate", "metric", "selection")
    }
    return ScreeningParityReport(
        asof=asof.isoformat(),
        release_id=shadow.release_id,
        release_sourced_tables=shadow.release_sourced_tables,
        legacy_sourced_tables=shadow.legacy_sourced_tables,
        candidates_compared=len(_candidates(sides["legacy"].run)),
        selection_entries_compared=len(_recommendations(sides["legacy"].selection)),
        differences=tuple(differences[:_MAX_REPORTED_DIFFERENCES]),
        difference_counts=counts,
        total_differences=len(differences),
    )


def _run_side(
    *,
    directory: Path,
    asof: date,
    config: ScreeningConfig,
    rules: ScreeningRules,
    now: datetime,
    app_db_path: Path,
    select_top: int,
    stdout: TextIO,
) -> ScreeningSideResult:
    sqlite_path = directory / "market.sqlite"
    side_config = config.model_copy(update={"sqlite_cache_dir": directory})
    run_yaml = directory / "screening-run.yaml"
    runs_db = directory / "runs.sqlite"
    exit_code = run_command(
        asof,
        side_config,
        cache_only_providers(side_config, sqlite_path=sqlite_path),
        rules=rules,
        now=now,
        output_path=run_yaml,
        force=True,
        run_store_path=runs_db,
        stdout=stdout,
    )
    if exit_code not in (0, 2):
        raise LakeShadowError(f"screening run failed with exit {exit_code} in {directory}")
    run_payload = _load_mapping(run_yaml, label="screening run view")
    revision = run_payload.get("run_revision_id")
    if not isinstance(revision, str) or not revision:
        raise LakeShadowError(f"screening run view has no run_revision_id in {directory}")
    buffer = io.StringIO()
    select_exit = select_command(
        asof_date=asof,
        top=select_top,
        rules=rules,
        run_revision_id=revision,
        runs_db_path=runs_db,
        app_db_path=app_db_path,
        stdout=buffer,
    )
    if select_exit != 0:
        raise LakeShadowError(f"screening select failed with exit {select_exit} in {directory}")
    selection = safe_load(buffer.getvalue())
    if not isinstance(selection, dict):
        raise LakeShadowError(f"screening select produced no mapping in {directory}")
    return ScreeningSideResult(run=run_payload, selection=selection)


def _load_mapping(path: Path, *, label: str) -> Mapping[str, object]:
    try:
        payload = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LakeShadowError(f"{label} is unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise LakeShadowError(f"{label} is not a mapping: {path}")
    return payload


def compare_sides(
    *, legacy: ScreeningSideResult, lake: ScreeningSideResult
) -> list[ParityDifference]:
    differences = _compare_document(
        scope="candidate",
        legacy=legacy.run,
        lake=lake.run,
        identity_paths=_RUN_IDENTITY_PATHS,
        collection="candidates",
    )
    differences.extend(
        _compare_document(
            scope="selection",
            legacy=legacy.selection,
            lake=lake.selection,
            identity_paths=_SELECTION_IDENTITY_PATHS,
            collection="recommendations",
        )
    )
    differences.extend(
        _compare_metrics(
            legacy=_candidates(legacy.run),
            lake=_candidates(lake.run),
        )
    )
    return differences


def _compare_document(
    *,
    scope: ParityScope,
    legacy: Mapping[str, object],
    lake: Mapping[str, object],
    identity_paths: frozenset[str],
    collection: str,
) -> list[ParityDifference]:
    differences = _compare_flat(
        scope=scope,
        key="document",
        legacy=_document_fields(legacy, collection=collection, identity_paths=identity_paths),
        lake=_document_fields(lake, collection=collection, identity_paths=identity_paths),
    )
    differences.extend(
        _compare_by_ticker(
            scope=scope,
            collection=collection,
            legacy=_by_ticker(legacy.get(collection)),
            lake=_by_ticker(lake.get(collection)),
        )
    )
    return differences


def _document_fields(
    document: Mapping[str, object], *, collection: str, identity_paths: frozenset[str]
) -> dict[str, str]:
    """Flatten a document minus its keyed collection and its identity paths."""

    flattened = _flatten({key: value for key, value in document.items() if key != collection})
    return {path: value for path, value in flattened.items() if path not in identity_paths}


def _compare_metrics(
    *, legacy: Sequence[Mapping[str, object]], lake: Sequence[Mapping[str, object]]
) -> list[ParityDifference]:
    """Compare the derived metric block of each candidate on its own.

    The candidate comparison already covers these values, but reporting the metric
    block separately is what tells a reader whether a mismatch is a different
    universe or the same names carrying different numbers.
    """

    left = {ticker: row.get("metrics") for ticker, row in _by_ticker(legacy).items()}
    right = {ticker: row.get("metrics") for ticker, row in _by_ticker(lake).items()}
    differences: list[ParityDifference] = []
    for ticker in sorted(set(left) | set(right)):
        differences.extend(
            _compare_flat(
                scope="metric",
                key=ticker,
                legacy=_flatten(left.get(ticker)),
                lake=_flatten(right.get(ticker)),
            )
        )
    return differences


def _compare_by_ticker(
    *,
    scope: ParityScope,
    collection: str,
    legacy: Mapping[str, Mapping[str, object]],
    lake: Mapping[str, Mapping[str, object]],
) -> list[ParityDifference]:
    differences: list[ParityDifference] = []
    legacy_order = list(legacy)
    lake_order = list(lake)
    if legacy_order != lake_order:
        differences.append(
            ParityDifference(
                scope=scope,
                key=collection,
                field="order",
                legacy=",".join(legacy_order),
                lake=",".join(lake_order),
            )
        )
    for ticker in sorted(set(legacy) | set(lake)):
        differences.extend(
            _compare_flat(
                scope=scope,
                key=ticker,
                legacy=_flatten(legacy.get(ticker)),
                lake=_flatten(lake.get(ticker)),
            )
        )
    return differences


def _compare_flat(
    *, scope: ParityScope, key: str, legacy: Mapping[str, str], lake: Mapping[str, str]
) -> list[ParityDifference]:
    return [
        ParityDifference(
            scope=scope,
            key=key,
            field=field,
            legacy=legacy.get(field, "<absent>"),
            lake=lake.get(field, "<absent>"),
        )
        for field in sorted(set(legacy) | set(lake))
        if legacy.get(field, "<absent>") != lake.get(field, "<absent>")
    ]


def _candidates(document: Mapping[str, object]) -> list[Mapping[str, object]]:
    rows = document.get("candidates")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _recommendations(document: Mapping[str, object]) -> list[Mapping[str, object]]:
    rows = document.get("recommendations")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _by_ticker(rows: object) -> dict[str, Mapping[str, object]]:
    """Index rows by ticker, keeping the order they were published in."""

    if not isinstance(rows, list):
        return {}
    indexed: dict[str, Mapping[str, object]] = {}
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        ticker = row.get("ticker")
        key = ticker if isinstance(ticker, str) and ticker else f"#{position}"
        indexed[key] = row
    return indexed


def _flatten(value: object, prefix: str = "") -> dict[str, str]:
    """Render a nested value as dotted paths so a difference names its own field."""

    if isinstance(value, dict):
        flattened: dict[str, str] = {}
        for key, item in value.items():
            flattened.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return flattened
    if isinstance(value, list):
        flattened = {}
        for index, item in enumerate(value):
            flattened.update(_flatten(item, f"{prefix}[{index}]"))
        return flattened or {prefix or "<root>": "[]"}
    return {prefix or "<root>": repr(value)}
