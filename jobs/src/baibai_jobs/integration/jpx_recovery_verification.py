"""Exact recovery acceptance across source rows, saved generations and remote lake bytes."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from collections.abc import Iterable, Sequence
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, cast

from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.full_day_inputs import full_day_input_interval
from baibai_engine.machine_analysis.generation.query import stream_generation_subjects
from baibai_engine.machine_analysis.observations.external_assessments.snapshot_contracts import (
    EXTERNAL_ASSESSMENT_QUANTS_SCORE_OBSERVATIONS,
)
from baibai_engine.storage.objects.r2 import ObjectStore
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import (
    accepted_dataset,
    iter_partition_rows,
    materialize_partitions,
    selected_partitions,
)
from baibai_engine.storage.sqlite.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only as market_connection
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.snapshot_publication import (
    _fetch,
    _resolve_serving_release,
    _serving_pointer,
)

JPX_DATASETS = (
    "jpx.source_bundles",
    "jpx.source_responses",
    "jpx.earnings_calendar",
    "jpx.regulation_flags",
    "jpx.regulation_sources",
)
EDINET_DATASETS = tuple(
    name
    for name in LAKE_DATASETS
    if name.startswith("edinet.")
    and name not in {"edinet.list_responses", "edinet.source_archives"}
)
MARKET_DATASETS = {
    name: LAKE_DATASETS[name]
    for name in (
        *JPX_DATASETS,
        *EDINET_DATASETS,
        EXTERNAL_ASSESSMENT_QUANTS_SCORE_OBSERVATIONS.name,
    )
}
_GENERATION_TABLES = ("model_generation", "generated_model", "screening_selection")
_NATIVE_INPUTS = (
    "financial",
    "calendar",
    "master",
    "price",
    "margin_alerts",
    "margin_interest",
    "short_sale_reports",
)


def row_fingerprint(rows: Iterable[Sequence[object]]) -> dict[str, Any]:
    hashes = []
    for row in rows:
        values = [
            {"bytes_sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
            if isinstance(value, bytes)
            else value
            for value in row
        ]
        hashes.append(
            hashlib.sha256(
                json.dumps(values, separators=(",", ":"), ensure_ascii=False).encode()
            ).hexdigest()
        )
    hashes.sort()
    return {
        "count": len(hashes),
        "sha256": hashlib.sha256("".join(value + "\n" for value in hashes).encode()).hexdigest(),
    }


def snapshot_market(path: Path) -> dict[str, dict[str, Any]]:
    with closing(market_connection(path)) as connection:
        connection.execute("BEGIN")
        if connection.execute("PRAGMA user_version").fetchone()[0] != 51:
            raise ValueError("recovery requires market schema 51")
        return {
            name: row_fingerprint(
                connection.execute(
                    f"SELECT {','.join(column.name for column in dataset.columns)} "  # nosec B608
                    f"FROM {dataset.sqlite_table}"
                )
            )
            for name, dataset in MARKET_DATASETS.items()
        }


def coverage(path: Path, source: Literal["jpx", "edinet"]) -> dict[str, Any]:
    if source not in {"jpx", "edinet"}:
        raise ValueError("unsupported recovery coverage source")
    with closing(market_connection(path)) as connection:
        rows = connection.execute(
            "SELECT * FROM source_coverage WHERE source IN (?,?)"
            if source == "jpx"
            else "SELECT * FROM source_coverage WHERE source LIKE ?",
            ("jpx_earnings_calendar", "jpx_regulation_flags") if source == "jpx" else ("edinet%",),
        )
        return row_fingerprint(rows)


def edinet_coverage_rows(path: Path) -> list[dict[str, Any]]:
    with closing(market_connection(path)) as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM source_coverage WHERE source LIKE 'edinet%'"
            )
        ]


def verify_edinet_coverage(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    started_at: datetime,
    finished_at: datetime,
) -> int:
    def keyed(rows: list[dict[str, Any]]) -> dict[str, datetime]:
        result = {}
        for row in rows:
            semantic = {name: value for name, value in row.items() if name != "fetched_at_utc"}
            key = json.dumps(semantic, sort_keys=True, separators=(",", ":"))
            if key in result:
                raise ValueError("duplicate EDINET semantic coverage row")
            result[key] = datetime.fromisoformat(row["fetched_at_utc"])
        return result

    old, new = keyed(before), keyed(after)
    if old.keys() != new.keys():
        raise ValueError("EDINET semantic coverage changed")
    for key, clock in new.items():
        if clock != old[key] and not (old[key] <= clock and started_at <= clock <= finished_at):
            raise ValueError("EDINET projection clock is not an actual forward application clock")
    return sum(clock != old[key] for key, clock in new.items())


def prior_generations(path: Path, identities: Sequence[str] | None = None) -> dict[str, Any]:
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        present = {
            row[0] for row in connection.execute("SELECT generation_id FROM model_generation")
        }
        selected = present if identities is None else set(identities)
        if not selected or not selected <= present:
            raise ValueError("prior generation is missing")
        result = {}
        for identity in sorted(selected):
            result[identity] = {
                table: row_fingerprint(
                    connection.execute(
                        f"SELECT * FROM {table} WHERE generation_id=?",  # nosec B608
                        (identity,),
                    )
                )
                for table in _GENERATION_TABLES
            }
        return {"generation_ids": sorted(selected), "generations": result}


def _native_inputs(metadata: Any, store: ObjectStore) -> dict[str, Any]:
    result = {}
    for name in _NATIVE_INPUTS:
        saved = InputSnapshot.model_validate_json(metadata.inputs[name].model_dump_json())
        descriptor = saved.descriptor
        owner = importlib.import_module(
            "baibai_engine.machine_analysis.generation." + name + "_inputs"
        )
        if (
            saved.kind != "native_l1"
            or descriptor is None
            or descriptor.get("format") != owner.INPUT_FORMAT
        ):
            raise ValueError("normal native input format differs")
        if any(
            re.fullmatch("[0-9a-f]{64}", str(descriptor.get(key, ""))) is None
            for key in ("head_sha256", "selection_sha256")
        ):
            raise ValueError("normal native input digest differs")
        summary_value = descriptor["summary"]
        if not isinstance(summary_value, dict):
            raise ValueError("normal native input summary differs")
        summary = cast(dict[str, Any], summary_value)
        if name in {"margin_alerts", "margin_interest", "short_sale_reports"}:
            start, end = (
                date.fromisoformat(summary["selection_start"]),
                date.fromisoformat(summary["selection_end"]),
            )
            if descriptor["as_of"] != end.isoformat() or start > end or end < metadata.as_of:
                raise ValueError("normal retained native interval differs")
        elif descriptor["as_of"] != metadata.as_of.isoformat():
            raise ValueError("normal native target date differs")
        for key in ("evaluated_at", "latest_confirmed_at", "latest_changed_at"):
            if summary.get(key) and datetime.fromisoformat(summary[key]) > metadata.evaluation_time:
                raise ValueError("native receipt is later than model evaluation")
        if (
            name == "master"
            and summary.get("snapshot_date")
            and date.fromisoformat(summary["snapshot_date"]) > metadata.as_of
        ):
            raise ValueError("future master selected as current universe")
        first, last = full_day_input_interval(store, descriptor, contract=owner._CONTRACT)
        result[name] = {
            "input": saved.model_dump(mode="json"),
            "start": first.isoformat(),
            "end": last.isoformat(),
        }
    return result


def verify_models(
    path: Path,
    prior: dict[str, Any],
    notice: dict[str, Any],
    edinet_input: dict[str, Any],
    started_at: datetime,
    finished_at: datetime,
    store: ObjectStore,
) -> dict[str, Any]:
    identity = notice["generation_id"]
    if prior_generations(path, prior["generation_ids"]) != prior:
        raise ValueError("prior generation, model or Selection rows changed")
    with closing(connect_read_only(path)) as connection:
        if [tuple(row) for row in connection.execute("PRAGMA quick_check")] != [("ok",)]:
            raise ValueError("runs integrity check failed")
        present = {
            row[0] for row in connection.execute("SELECT generation_id FROM model_generation")
        }
        if identity in prior["generation_ids"] or present != set(prior["generation_ids"]) | {
            identity
        }:
            raise ValueError("recovery must add exactly one new generation")
        tables = prior_generations(path, [identity])["generations"][identity]
        selections = list(
            connection.execute(
                "SELECT selection_id FROM screening_selection WHERE generation_id=?", (identity,)
            )
        )
        if (
            tables["model_generation"]["count"] != 1
            or tables["generated_model"]["count"] != notice["model_count"]
            or [tuple(row) for row in selections] != [(notice["selection_id"],)]
        ):
            raise ValueError("new model or Selection membership differs")
    with stream_generation_subjects(identity, subjects=(), path=path) as pair:
        if pair is None:
            raise ValueError("new generation metadata missing")
        metadata, subjects = pair
        if list(subjects):
            raise ValueError("unexpected requested model subjects")
        lake = notice["lake"]
        if (
            metadata.generation_id != "models-" + lake["release_id"]
            or metadata.as_of.isoformat() != notice["asof"]
            or not started_at <= metadata.evaluation_time <= finished_at
            or metadata.inputs["market"].kind != "lake_release"
            or metadata.inputs["market"].reference != lake["release_id"]
            or metadata.inputs["market"].sha256 != lake["release_manifest_sha256"]
            or metadata.inputs["edinet"].model_dump(mode="json") != edinet_input
        ):
            raise ValueError("new generation fixed inputs or evaluation clock differ")
        native = _native_inputs(metadata, store)
    return {"generation_id": identity, "tables": tables, "native_inputs": native}


def verify_lake(
    store: ObjectStore,
    directory: Path,
    expected_generation: dict[str, str],
    expected_tables: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if set(expected_tables) != set(MARKET_DATASETS):
        raise ValueError("recovery readback dataset contract differs")
    directory.mkdir(parents=True, exist_ok=False)
    pointer = _serving_pointer(store).pointer
    if (
        pointer is None
        or pointer.release_id != expected_generation["lake_release_id"]
        or pointer.manifest_sha256 != expected_generation["lake_manifest_sha256"]
    ):
        raise ValueError("published current differs from recovered generation")
    release = _resolve_serving_release(store, directory, pointer)
    if {name for name in release.dataset_names() if name.startswith("edinet.")} != set(
        EDINET_DATASETS
    ):
        raise ValueError("published EDINET dataset membership differs")
    cache = LakeObjectCache(root=directory, source=LocalMirrorSource(directory))
    result = {}
    for name, wanted in expected_tables.items():
        dataset = accepted_dataset(release, MARKET_DATASETS[name])
        if dataset.contract_version != MARKET_DATASETS[name].contract_version:
            raise ValueError("recovery requires the current dataset contract")
        partitions = selected_partitions(release, name)
        for partition in partitions:
            for obj in partition.objects:
                _fetch(store, directory, obj.key, expected_sha256=obj.sha256)
        paths = materialize_partitions(cache, dataset=dataset, partitions=partitions)
        with lake_session() as session:
            observed = row_fingerprint(
                row
                for batch in iter_partition_rows(session, dataset=dataset, paths=paths)
                for row in batch
            )
        if observed != wanted:
            raise ValueError("published recovery dataset values differ")
        result[name] = observed
    if _serving_pointer(store).pointer != pointer:
        raise ValueError("published current changed during recovery readback")
    return result
