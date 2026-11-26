"""Disposable, generation-bound scoped inputs in the existing private staging bucket."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

from baibai_engine.data_collection.acquisition.policy import (
    acquisition_groups as economic_acquisition_groups,
)
from baibai_engine.data_collection.acquisition.policy import source_policy as economic_source_policy
from baibai_engine.data_collection.catalog import load_sources as load_economic_sources
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    export_pmi_input,
    export_tsr_input,
)
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    export_receipts as export_economic_receipts,
)
from baibai_engine.data_collection.receipts.native_sources.planning_inputs import export_day_input
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.json import put_private, read_private
from baibai_engine.storage.objects.r2 import (
    ImmutableObjectDeleter,
    LakePublishError,
    ObjectStore,
    RemoteObject,
)
from baibai_jobs.acquisition.economic_input_retirement import (
    EconomicRetirementDeferred,
    outstanding_retirement,
    retire_confirmed_predecessor,
    verified_predecessor,
    verify_retirement_selection,
)
from baibai_jobs.acquisition.economic_staging import (
    ScopedInputMissing,
    load_handoff,
    save_handoff,
    stage_prefix,
)
from baibai_jobs.acquisition.inputs import SOURCE_INPUT_GROUPS
from baibai_jobs.acquisition.inputs import export_source_input as export_market_source_input


class GuardedInputStore:
    """Check fixed canonical ownership immediately before every source-state PUT."""

    def __init__(self, store: ObjectStore, before_put: Callable[[], None]) -> None:
        self.store, self.before_put = store, before_put

    def head(self, key: str) -> RemoteObject | None:
        return self.store.head(key)

    def get_bytes(self, key: str) -> bytes:
        return self.store.get_bytes(key)

    def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None:
        self.store.download_file(key, path, expect_bytes=expect_bytes)

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
        content_md5: str,
        content_type: str,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> RemoteObject:
        self.before_put()
        return self.store.put_file(
            key,
            path,
            sha256=sha256,
            content_md5=content_md5,
            content_type=content_type,
            if_match=if_match,
            if_none_match=if_none_match,
        )


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
    only_group: str | None = None,
) -> dict[str, object]:
    """Keep failed groups' prior pointers while updating independent healthy inputs."""
    groups = list(SOURCE_INPUT_GROUPS) + [
        "economic-" + group for group in economic_acquisition_groups()
    ]
    if only_group is not None:
        if only_group not in groups:
            raise ValueError("unsupported source input group")
        groups = [only_group]
    updated: list[str] = []
    failed: list[dict[str, str]] = []
    for group in groups:
        try:
            stage = publish_input(
                store,
                market=market,
                economic=economic,
                identity=identity,
                asof=asof,
                generations=generations,
                group=group,
            )
        except ValueError as error:
            failed.append({"group": group, "reason": type(error).__name__})
        else:
            if stage == f"{group}-{identity}":
                updated.append(group)
    return {"updated": updated, "failed": failed}


def publish_input(
    store: ObjectStore,
    *,
    market: Path,
    economic: Path,
    identity: str,
    asof: date,
    generations: dict[str, str],
    group: str,
    economic_readback: Path | None = None,
) -> str:
    guarded = store if isinstance(store, GuardedInputStore) else None
    reader = guarded.store if guarded is not None else store
    deleter = (
        reader
        if economic_readback is not None and isinstance(reader, ImmutableObjectDeleter)
        else None
    )
    if economic_readback is not None and (
        not group.startswith("economic-") or guarded is None or deleter is None
    ):
        raise ValueError("economic input retirement requires the economic publication owner")
    with tempfile.TemporaryDirectory(prefix="baibai-source-input-") as temporary:
        directory = Path(temporary)
        path = directory / "receipts.sqlite"
        report: dict[str, object] = {"group": group, "asof": asof.isoformat()}
        if group == "economic-tsr_bankruptcies":
            export_tsr_input(economic, path)
        elif group in {
            "economic-boj",
            "economic-umich_sca",
            "economic-ecb_fx",
            "economic-estat",
            "economic-jquants",
            "economic-mof_jgb",
            "economic-frb_h15",
            "economic-yahoo",
            "economic-multpl",
            "economic-cftc",
            "economic-nikkei_indexes",
        }:
            export_day_input(economic, path, group=group)
        elif group.startswith("economic-"):
            source_ids = sorted(
                source.source_id
                for source in load_economic_sources().values()
                if economic_source_policy(source).acquisition_group
                == group.removeprefix("economic-")
            )
            if group in {"economic-matsui_margin", "economic-fred_csv"}:
                # Current-page and complete FRED requests need no prior bodies.
                # Keep the generation binding; fresh canonical acceptance owns
                # same-body, disappearance and late-response comparisons.
                source_ids = []
            if group == "economic-spglobal_pmi":
                export_pmi_input(economic, path, source_ids=source_ids)
            else:
                export_economic_receipts(
                    economic,
                    path,
                    source_ids=source_ids,
                    since=datetime.min.replace(tzinfo=UTC),
                )
        else:
            report = export_market_source_input(market, path, group=group, asof=asof)
        report["canonical_generations"] = generations
        issued = datetime.now(UTC)
        report["created_at"] = issued.isoformat()
        stage_id = f"{group}-{identity}"
        write_json_atomic(directory / "result.json", report)
        key = input_key(group)
        # The lease guards mutations; the underlying GET owns returned metadata.
        prior = read_private(reader, key)
        reusable = None
        current_stage = None
        old_pointer: dict[str, Any] = {}
        old_stage = None
        predecessor = None
        if prior is not None:
            try:
                value = json.loads(prior.payload)
                if isinstance(value, dict):
                    old_pointer = value
                old_stage = old_pointer.get("stage_id")
                if isinstance(old_stage, str) and old_stage.startswith(group + "-"):
                    old_prefix = stage_prefix(old_stage, namespace="source-inputs")
                    old_handoff = read_private(reader, old_prefix + "/handoff.json")
                    if old_handoff is not None:
                        candidate = json.loads(old_handoff.payload)
                        result = candidate.get("result") if isinstance(candidate, dict) else None
                        if isinstance(result, dict) and result.get("group") == group:
                            reusable = candidate
                            unchanged: dict[str, object] = {
                                "stage_id": old_stage,
                                "canonical_generations": generations,
                            }
                            comparable_report: dict[str, object] | None = report
                            if economic_readback is not None:
                                old_generations = old_pointer.get("canonical_generations")
                                predecessor = verified_predecessor(
                                    group, old_stage, old_handoff, generations=old_generations
                                )
                                unchanged["canonical_generations"] = old_generations
                                if "retired" in old_pointer:
                                    unchanged["retired"] = old_pointer["retired"]
                                # Only the issued date and canonical ETag may differ;
                                # save_handoff still compares this group's exact body and wire.
                                issued_day = result.get("asof")
                                comparable_report = (
                                    {
                                        **report,
                                        "asof": issued_day,
                                        "canonical_generations": old_generations,
                                    }
                                    if predecessor is not None
                                    and isinstance(issued_day, str)
                                    and date.fromisoformat(issued_day).isoformat() == issued_day
                                    else None
                                )
                            if (
                                comparable_report is not None
                                and group.startswith("economic-")
                                and set(generations) == {"economic_etag"}
                                and generations["economic_etag"]
                                and old_pointer == unchanged
                                and isinstance(result.get("created_at"), str)
                                and result
                                == {**comparable_report, "created_at": result["created_at"]}
                                and datetime.fromisoformat(result["created_at"]) <= issued
                            ):
                                current_stage = old_stage
            except (ValueError, TypeError):
                # Unusable old metadata must not prevent publishing a fresh input.
                pass
        if prior is not None and group.startswith("economic-") and "retired" in old_pointer:
            if economic_readback is None or guarded is None or deleter is None:
                raise LakePublishError("economic input cleanup requires its publication owner")
            prior_directory = economic_readback.with_suffix(".prior")

            def confirm_current() -> None:
                if not isinstance(old_stage, str):
                    raise LakePublishError("economic cleanup needs the selected generation")
                _confirm_economic_input(
                    reader,
                    group,
                    prior_directory,
                    stage=old_stage,
                    generations=old_pointer["canonical_generations"],
                )

            try:
                pending = outstanding_retirement(deleter, group, old_pointer)
            except EconomicRetirementDeferred:
                # A temporarily unavailable detached manifest is not proof that
                # the previous CAS and its exact selected input were accepted.
                confirm_current()
                verify_retirement_selection(deleter, key, prior, guarded.before_put)
                raise
            if pending is not None:
                confirm_current()
                if not retire_confirmed_predecessor(
                    deleter,
                    group,
                    key,
                    prior,
                    pending,
                    before_delete=guarded.before_put,
                ):
                    raise EconomicRetirementDeferred(accepted_stage=None)
        stage_id = save_handoff(
            store,
            stage_id,
            directory,
            namespace="source-inputs",
            reusable_receipt=reusable,
            current_input_stage=current_stage,
            input_group=group,
        )
        if economic_readback is not None and stage_id == current_stage:
            # save_handoff confirmed the same wire and body. Read back the
            # existing selection as issued; never claim a newer canonical ETag.
            _confirm_economic_input(
                reader,
                group,
                economic_readback,
                stage=stage_id,
                generations=old_pointer["canonical_generations"],
            )
            return stage_id
        pointer_value: dict[str, object] = {
            "stage_id": stage_id,
            "canonical_generations": generations,
        }
        if predecessor is not None and stage_id != old_stage:
            pointer_value["retired"] = predecessor.record()
        pointer = json.dumps(pointer_value, sort_keys=True).encode()
        published = prior
        if prior is None or prior.payload != pointer:
            published = put_private(
                store, key, pointer, previous_etag=prior.etag if prior else None
            )
        if economic_readback is not None:
            _confirm_economic_input(
                reader, group, economic_readback, stage=stage_id, generations=generations
            )
            if predecessor is not None and stage_id != old_stage:
                assert deleter is not None
                assert guarded is not None
                assert published is not None
                if not retire_confirmed_predecessor(
                    deleter,
                    group,
                    key,
                    published,
                    predecessor,
                    before_delete=guarded.before_put,
                ):
                    raise EconomicRetirementDeferred(accepted_stage=stage_id)
        return stage_id


def _confirm_economic_input(
    store: ObjectStore,
    group: str,
    directory: Path,
    *,
    stage: str,
    generations: object,
) -> None:
    metadata = load_input(store, group, None, directory, expected_stage=stage)
    if metadata["canonical_generations"] != generations:
        raise LakePublishError("economic scoped input generation changed during readback")


def load_input(
    store: ObjectStore,
    group: str,
    database: Path | None,
    directory: Path,
    *,
    expected_stage: str | None = None,
) -> dict[str, object]:
    """Download only this group's immutable input; never fall back to the full DB."""
    key = input_key(group)
    saved = read_private(store, key)
    if saved is None:
        raise LakePublishError(
            "scoped source input is missing; initialize through common integration"
        )

    def selection(payload: bytes) -> tuple[str, dict[str, object]]:
        pointer = json.loads(payload)
        if not isinstance(pointer, dict):
            raise LakePublishError("invalid scoped source input pointer")
        stage_id, generations = pointer.get("stage_id"), pointer.get("canonical_generations")
        if (
            not isinstance(stage_id, str)
            or not stage_id.startswith(group + "-")
            or not isinstance(generations, dict)
        ):
            raise LakePublishError("invalid scoped source input pointer")
        return stage_id, generations

    stage_id, generations = selection(saved.payload)
    if expected_stage is not None and stage_id != expected_stage:
        raise LakePublishError("economic scoped input stage changed during readback")
    for retry in (False, True):
        try:
            load_handoff(
                store,
                stage_id,
                directory,
                namespace="source-inputs",
                input_group=group,
                input_generations=generations,
            )
        except ScopedInputMissing:
            # Only latest consumers may repin, and only when a selected member disappeared.
            if retry or expected_stage is not None:
                raise
            latest = read_private(store, key)
            if latest is None or latest.etag == saved.etag:
                raise
            next_stage, next_generations = selection(latest.payload)
            if next_stage == stage_id:
                raise
            stage_id, generations = next_stage, next_generations
        else:
            break
    report = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if database is not None:
        database.parent.mkdir(parents=True, exist_ok=True)
        if database.exists():
            raise FileExistsError(database)
        shutil.copyfile(directory / "receipts.sqlite", database)
    return cast("dict[str, object]", report)
