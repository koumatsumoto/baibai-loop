"""External history readback contracts and acquisition/integration workflow boundaries."""

from __future__ import annotations

import shlex
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from tests.batch.external_assessment_publication_fixtures import publication as publication
from tools.quality.drift.check_workflow_trust import check_workflow

from baibai_jobs.jobs import external_assessment_integration as external_assessment
from baibai_jobs.storage import publish_market_lake as publisher
from baibai_engine.job_api import (
    ExternalAssessmentAnalystRatingObservation,
    LakeObjectCache,
    LocalMirrorSource,
    external_assessment_history_digests,
    external_assessment_verify_release_history,
    lake_session,
)

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/cloud-external-assessment.yml"


def test_acquire_workflow_timeout_can_start_a_harmless_command():
    commands = [
        shlex.split(line.strip())
        for step in yaml.safe_load(
            WORKFLOW.with_name("cloud-external-assessment-source.yml").read_text()
        )["jobs"]["acquire"]["steps"]
        for line in step.get("run", "").splitlines()
        if line.strip().startswith("timeout ")
    ]
    assert len(commands) == 1
    command = commands[0]
    # Exercise GNU timeout's actual duration parser without provider or R2 I/O.
    result = subprocess.run(
        [*command[: command.index("uv")], "true"],
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0


def fixed_external_assessment_readback(publication):
    store, _, _, database, _, tmp_path = publication
    mirror = tmp_path / "typed-readback"
    pointer = publisher._serving_pointer(store).pointer
    fixed = publisher._resolve_serving_release(store, mirror, pointer)
    for name in (external_assessment.VALUATION_DATASET, external_assessment.RATING_DATASET):
        for partition in fixed.dataset_manifests[name].partitions:
            for obj in partition.objects:
                publisher._fetch(store, mirror, obj.key, expected_sha256=obj.sha256)
    return fixed, LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror)), database


def test_missing_old_month_history_fails_even_without_current_receipts(publication):
    fixed, cache, database = fixed_external_assessment_readback(publication)
    manifests = dict(fixed.dataset_manifests)
    original = manifests[external_assessment.VALUATION_DATASET]
    assert len(original.partitions) == 2
    manifests[external_assessment.VALUATION_DATASET] = original.model_copy(
        update={"partitions": original.partitions[1:]}
    )
    with lake_session() as session, pytest.raises(ValueError, match="changed received history"):
        external_assessment_verify_release_history(
            session,
            release=replace(fixed, dataset_manifests=manifests),
            cache=cache,
            expected_history=external_assessment_history_digests(database),
            observations=(),
        )


@pytest.mark.parametrize("updates", [{"rating": 1}, {"rating_updated_label": None}])
def test_typed_null_and_metadata_receipts_must_match_all_columns(publication, updates):
    fixed, cache, database = fixed_external_assessment_readback(publication)
    original = ExternalAssessmentAnalystRatingObservation(
        ticker="6758",
        fetched_at_utc=datetime(2026, 7, 31, 15, tzinfo=UTC),
        source_url="https://example.test/synthetic",
        parser_version="synthetic-1",
        distributor="rakuten",
        rating=None,
        rating_updated_label="synthetic-label",
    )
    with lake_session() as session:
        external_assessment_verify_release_history(
            session,
            release=fixed,
            cache=cache,
            expected_history=external_assessment_history_digests(database),
            observations=(original,),
        )
        with pytest.raises(ValueError, match="receipt readback mismatch"):
            external_assessment_verify_release_history(
                session,
                release=fixed,
                cache=cache,
                expected_history=external_assessment_history_digests(database),
                observations=(original.model_copy(update=updates),),
            )


def test_missing_external_assessment_dataset_is_empty_only_when_expected_history_is_empty(
    publication,
):
    fixed, cache, database = fixed_external_assessment_readback(publication)
    manifests = dict(fixed.dataset_manifests)
    del manifests[external_assessment.RATING_DATASET]
    with lake_session() as session, pytest.raises(ValueError, match="changed received history"):
        external_assessment_verify_release_history(
            session,
            release=replace(fixed, dataset_manifests=manifests),
            cache=cache,
            expected_history=external_assessment_history_digests(database),
            observations=(),
        )


def test_workflow_is_daily_serial_private_and_keeps_publication_inside_existing_lease():
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch", "schedule"}
    acquire_job = workflow["jobs"]["source"]
    assert acquire_job["strategy"]["max-parallel"] == "4"
    assert acquire_job["strategy"]["fail-fast"] == "false"
    assert acquire_job["uses"] == "./.github/workflows/cloud-external-assessment-source.yml"
    child = yaml.load(
        WORKFLOW.with_name("cloud-external-assessment-source.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert child["concurrency"]["group"] == "external-assessment-acquire-${{ inputs.source }}"
    assert "include" in acquire_job["strategy"]["matrix"]
    assert int(child["jobs"]["acquire"]["timeout-minutes"]) < 360
    assert "publish" not in child["jobs"]
    handoff = child["jobs"]["handoff"]
    assert "always()" in handoff["if"]
    runs = [step.get("run", "") for step in handoff["steps"]]
    assert any("external_assessment_sources ready" in text for text in runs)
    assert any("--confirmed-partial" in text for text in runs)
    assert not any("hydrate-market" in text or "publication_lease" in text for text in runs)
    common = yaml.load(
        WORKFLOW.with_name("cloud-daily-batch.yml").read_text(), Loader=yaml.BaseLoader
    )
    daily = common["jobs"]["daily"]
    assert daily["concurrency"]["group"] == "cloud-publish"
    assert "needs" not in daily
    assert int(daily["timeout-minutes"]) < 120
    assert "refs/heads/main" in workflow["jobs"]["plan"]["if"]
    assert not any(
        "upload-artifact" in step.get("uses", "")
        for job in child["jobs"].values()
        for step in job["steps"]
    )
    assert check_workflow(WORKFLOW) == []


def test_selected_month_history_keeps_old_receipts_and_ignores_other_months(publication):
    fixed, cache, database = fixed_external_assessment_readback(publication)
    selected = {
        (external_assessment.VALUATION_DATASET, 2026, 9),
        (external_assessment.RATING_DATASET, 2026, 9),
    }
    expected = external_assessment_history_digests(database, partitions=selected)
    with lake_session() as session:
        external_assessment_verify_release_history(
            session,
            release=fixed,
            cache=cache,
            expected_history=expected,
            observations=(),
            partitions=selected,
        )
    # An old receipt in the selected month must still match all its metadata.
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE external_assessment_model_valuation_observations SET consensus_target_updated_label='changed' WHERE snapshot_date >= '2026-09-01' AND snapshot_date < '2026-10-01'"
        )
    changed = external_assessment_history_digests(database, partitions=selected)
    assert changed != expected
    with lake_session() as session, pytest.raises(ValueError, match="changed received history"):
        external_assessment_verify_release_history(
            session,
            release=fixed,
            cache=cache,
            expected_history=changed,
            observations=(),
            partitions=selected,
        )
    # The August partition is outside this readback scope.
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "DELETE FROM external_assessment_analyst_rating_observations WHERE snapshot_date < '2026-09-01'"
        )
    assert external_assessment_history_digests(database, partitions=selected) == changed


def test_selected_month_missing_old_receipt_is_detected(publication):
    fixed, cache, database = fixed_external_assessment_readback(publication)
    selected = {(external_assessment.VALUATION_DATASET, 2026, 9)}
    expected = external_assessment_history_digests(database, partitions=selected)
    manifests = dict(fixed.dataset_manifests)
    original = manifests[external_assessment.VALUATION_DATASET]
    manifests[external_assessment.VALUATION_DATASET] = original.model_copy(
        update={
            "partitions": tuple(part for part in original.partitions if part.values["month"] != 9)
        }
    )
    with lake_session() as session, pytest.raises(ValueError, match="changed received history"):
        external_assessment_verify_release_history(
            session,
            release=replace(fixed, dataset_manifests=manifests),
            cache=cache,
            expected_history=expected,
            observations=(),
            partitions=selected,
        )
