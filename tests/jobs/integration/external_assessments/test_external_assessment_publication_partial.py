"""Explicit partial handoffs preserve access stops, normal-null receipts and unattempted pairs."""

import uuid

import pytest
from tests.helpers.external_assessment_publication import (
    RECEIVED,
    _plan,
    _stage,
    integrate_ready_plans,
    pair_responses,
)
from tests.helpers.external_assessment_publication import publication as publication

import baibai_jobs.acquisition.external_assessments.staging as staging
from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import (
    decode_attempt as external_assessment_decode_attempt,
)
from baibai_jobs.integration.external_assessment_ready import prepare_plan


@pytest.mark.parametrize("normal_null", [False, True])
def test_partial_common_integration_preserves_unattempted_pairs_and_fixed_checkpoint(
    publication, normal_null
):
    store, previous, _, _, _, _ = publication
    plan = _plan(
        previous,
        run_id="monex-explicit-partial",
        tickers=("6758", "7203", "9432"),
        cohort_size=2,
    )
    responses = pair_responses(
        "monex", "6758", received_at_utc=RECEIVED, value="--" if normal_null else "1,234円"
    )
    observation = external_assessment_decode_attempt(responses)
    _stage(
        store,
        plan,
        0,
        [
            ("monex", "6758", "observed", responses[0].attempt_id, observation, 200),
            ("monex", "7203", "access_denied", str(uuid.uuid4()), None, 403),
        ],
        responses,
    )
    before = store.values[staging.stage_key(plan, 0)].body
    with pytest.raises(ValueError, match="explicit confirmed partial"):
        prepare_plan(store, plan)
    item = prepare_plan(store, plan, confirmed_partial=True)
    assert item.accounting["state_counts"]["unattempted"] == 1
    assert item.accounting["state_counts"]["normal_null"] == int(normal_null)
    results = integrate_ready_plans(publication, [plan], "partial-readback", confirmed_partial=True)
    assert len(results) == 1
    assert results[0]["readback"] == "verified"
    terminal = staging.read_terminal(store, plan)
    assert terminal["observations"] == 1
    assert terminal["expected_pairs"] == 3
    assert terminal["state_counts"]["failure"] == 1
    assert terminal["state_counts"]["unattempted"] == 1
    assert store.values[staging.stage_key(plan, 0)].body == before
    assert staging.read_stage(store, plan, 1) is None


def test_partial_handoff_requires_a_confirmed_typed_observation(publication):
    store, previous, *_ = publication
    plan = _plan(previous, run_id="monex-empty-partial")
    _stage(store, plan, 0, [("monex", "7203", "http_failure", str(uuid.uuid4()), None, 503)])
    with pytest.raises(ValueError, match="confirmed typed observations"):
        prepare_plan(store, plan, confirmed_partial=True)
