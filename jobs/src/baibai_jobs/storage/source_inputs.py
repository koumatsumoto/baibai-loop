"""Disposable, generation-bound scoped inputs in the existing private staging bucket."""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

from baibai_engine.market.source_inputs import SOURCE_INPUT_GROUPS
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import FetchError as TradingViewFetchError
from baibai_engine.data_collection.economic.refresh_policy import acquisition_groups as economic_acquisition_groups
from baibai_engine.data_collection.economic.refresh_policy import source_policy as economic_source_policy
from baibai_engine.data_collection.economic.handoff import export_receipts as export_economic_receipts
from baibai_engine.market.source_inputs import export_source_input as export_market_source_input
from baibai_engine.data_collection.economic.sources import load_sources as load_economic_sources
from baibai_jobs.storage.economic_staging import load_handoff, save_handoff
from baibai_jobs.storage.external_assessment_staging import put_private, read_private
from baibai_jobs.storage.json_io import write_json_atomic
from baibai_jobs.storage.lake_publish import LakePublishError, ObjectStore


def input_key(group: str) -> str:
    allowed = set(SOURCE_INPUT_GROUPS) | {
        "economic-" + group for group in economic_acquisition_groups()
    }
    if group not in allowed:
        raise ValueError("unsupported source input group")
    return f"coordination/source-inputs/{group}.json"


def publish_inputs(
    store: ObjectStore,
    *,
    market: Path,
    economic: Path,
    identity: str,
    asof: date,
    generations: dict[str, str],
) -> dict[str, object]:
    """Keep failed groups' prior pointers while updating independent healthy inputs."""
    groups = list(SOURCE_INPUT_GROUPS) + [
        "economic-" + group for group in economic_acquisition_groups()
    ]
    updated: list[str] = []
    failed: list[dict[str, str]] = []
    for group in groups:
        try:
            _publish_one(
                store,
                market=market,
                economic=economic,
                identity=identity,
                asof=asof,
                generations=generations,
                group=group,
            )
        except (ValueError, TradingViewFetchError) as error:
            failed.append({"group": group, "reason": type(error).__name__})
        else:
            updated.append(group)
    return {"updated": updated, "failed": failed}


def _publish_one(
    store: ObjectStore,
    *,
    market: Path,
    economic: Path,
    identity: str,
    asof: date,
    generations: dict[str, str],
    group: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="baibai-source-input-") as temporary:
        directory = Path(temporary)
        path = directory / "receipts.sqlite"
        if group.startswith("economic-"):
            export_economic_receipts(
                economic,
                path,
                source_ids=sorted(
                    source.source_id
                    for source in load_economic_sources().values()
                    if economic_source_policy(source).acquisition_group
                    == group.removeprefix("economic-")
                ),
                since=datetime.min.replace(tzinfo=UTC),
            )
            report: dict[str, object] = {"group": group, "asof": asof.isoformat()}
        else:
            report = export_market_source_input(market, path, group=group, asof=asof)
        report["canonical_generations"] = generations
        report["created_at"] = datetime.now(UTC).isoformat()
        stage_id = f"{group}-{identity}"
        write_json_atomic(directory / "result.json", report)
        save_handoff(store, stage_id, directory, namespace="source-inputs")
        key = input_key(group)
        prior = read_private(store, key)
        pointer = json.dumps(
            {"stage_id": stage_id, "canonical_generations": generations}, sort_keys=True
        ).encode()
        if prior is None or prior.payload != pointer:
            put_private(store, key, pointer, previous_etag=prior.etag if prior else None)


def load_input(
    store: ObjectStore, group: str, database: Path, directory: Path
) -> dict[str, object]:
    """Download only this group's immutable input; never fall back to the full DB."""
    saved = read_private(store, input_key(group))
    if saved is None:
        raise LakePublishError(
            "scoped source input is missing; initialize through common integration"
        )
    pointer = json.loads(saved.payload)
    load_handoff(store, pointer["stage_id"], directory, namespace="source-inputs")
    report = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if (
        report["group"] != group
        or report["canonical_generations"] != pointer["canonical_generations"]
    ):
        raise LakePublishError("scoped source input differs from its generation binding")
    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        raise FileExistsError(database)
    shutil.copyfile(directory / "receipts.sqlite", database)
    return cast("dict[str, object]", report)
