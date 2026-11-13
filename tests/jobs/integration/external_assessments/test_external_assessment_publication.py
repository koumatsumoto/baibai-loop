"""External history readback contracts and acquisition/integration workflow boundaries."""

from __future__ import annotations

import shlex
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from tests.helpers.external_assessment_publication import publication as publication
from tests.helpers.workflow_schedule import declared_schedule
from tools.quality.drift.check_workflow_trust import check_workflow

import baibai_jobs.integration.external_assessments as external_assessment
import baibai_jobs.publication.snapshot_publication as publisher
from baibai_engine.machine_analysis.observations.external_assessments.models import (
    AnalystRatingObservation as ExternalAssessmentAnalystRatingObservation,
)
from baibai_engine.machine_analysis.observations.external_assessments.models import (
    ModelValuationObservation as ExternalAssessmentModelValuationObservation,
)
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_jobs.publication.external_assessment_verification import (
    history_digests as external_assessment_history_digests,
)
from baibai_jobs.publication.external_assessment_verification import (
    verify_release_history as external_assessment_verify_release_history,
)
from baibai_jobs.publication.lease import DAILY_LEASE_TTL

WORKFLOW = Path(__file__).resolve().parents[4] / ".github/workflows/cloud-external-assessment.yml"


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


@pytest.mark.parametrize(
    ("kind", "updates"),
    [
        ("rating", {"rating": 1}),
        ("rating", {"rating_updated_label": None}),
        ("rating", {"rating_scale_direction": "lower_is_positive"}),
        ("rating", {"rating_count": 5}),
        ("rating", {"fetched_at_utc": datetime(2026, 7, 31, 15, microsecond=1, tzinfo=UTC)}),
        ("model", {"fetched_at_utc": datetime(2026, 7, 31, 15, microsecond=1, tzinfo=UTC)}),
        ("model", {"source_url": "https://example.test/other"}),
        ("model", {"parser_version": "synthetic-2"}),
        ("model", {"currency": "USD"}),
        ("model", {"unit": "USD/share"}),
        ("model", {"share_basis": "other-share-basis"}),
        ("model", {"consensus_target_origin": "other-origin"}),
        ("model", {"per_consensus_origin": "other-origin"}),
        ("model", {"per_consensus_updated_label": "other-update"}),
        ("model", {"per_consensus_count": 5}),
    ],
)
def test_typed_null_and_metadata_receipts_must_match_all_columns(publication, kind, updates):
    fixed, cache, database = fixed_external_assessment_readback(publication)
    common = {
        "ticker": "6758",
        "fetched_at_utc": datetime(2026, 7, 31, 15, tzinfo=UTC),
        "source_url": "https://example.test/synthetic",
        "parser_version": "synthetic-1",
    }
    original = (
        ExternalAssessmentAnalystRatingObservation(
            **common, distributor="rakuten", rating=None, rating_updated_label="synthetic-label"
        )
        if kind == "rating"
        else ExternalAssessmentModelValuationObservation(
            **common,
            distributor="monex",
            consensus_target=0,
            consensus_target_origin="synthetic-origin",
            consensus_target_count=0,
        )
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
    raw = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(raw, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch", "schedule"}
    assert declared_schedule(raw) == [{"cron": "13 10,14,22 * * *"}]
    acquire_job = workflow["jobs"]["source"]
    assert acquire_job["strategy"]["max-parallel"] == "2"
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
    assert any("acquisition.external_assessments.sources ready" in text for text in runs)
    assert any("--confirmed-partial" in text for text in runs)
    assert not any("hydrate-market" in text or "publication_lease" in text for text in runs)
    common = yaml.load(
        WORKFLOW.with_name("cloud-daily-batch.yml").read_text(), Loader=yaml.BaseLoader
    )
    daily = common["jobs"]["daily"]
    assert daily["concurrency"]["group"] == "cloud-publish"
    assert "needs" not in daily
    assert timedelta(minutes=int(daily["timeout-minutes"])) < DAILY_LEASE_TTL
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
