"""未計算も原本・最新null・readbackまで保持し、旧成功stageも反映できる。"""

from datetime import timedelta

import pytest
from tests.helpers.external_assessment_html import kabumap_html, kabumap_not_computed_html
from tests.helpers.external_assessment_publication import RECEIVED, _plan, _stage
from tests.helpers.external_assessment_publication import publication as publication
from tests.helpers.external_assessment_responses import response

from baibai_engine.machine_analysis.observations.external_assessments.normalization import kabumap
from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import (
    decode_attempt,
)
from baibai_engine.machine_analysis.observations.external_assessments.reader import (
    read_observations,
)
from baibai_engine.machine_analysis.observations.external_assessments.store import save_observations
from baibai_jobs.acquisition.external_assessments.staging import save_new_plan
from baibai_jobs.integration.external_assessment_ready import (
    load_ready,
    prepare_plan,
    read_acknowledgement,
    save_ready,
)
from baibai_jobs.integration.external_assessments import merge_ready, verify_and_seal
from baibai_jobs.publication.snapshot_publication import publish_market_lake


@pytest.mark.parametrize("not_computed", [False, True])
def test_old_success_and_new_not_computed_survive_integration_and_readback(
    publication, not_computed
):
    store, previous, handle, database, mirror, directory = publication
    received = RECEIVED + timedelta(days=11)
    plan = _plan(previous, run_id="synthetic-kabumap-cutover", sources=("kabumap",))
    save_new_plan(store, plan)
    earlier = kabumap.parse_html(
        kabumap_html("7203"),
        ticker="7203",
        source_url=kabumap.public_url("7203"),
        fetched_at_utc=received - timedelta(days=1),
    )
    save_observations(database, [earlier])
    original = response(
        "kabumap",
        "7203",
        (kabumap_not_computed_html("7203") if not_computed else kabumap_html("7203")).encode(
            "shift_jis"
        ),
        url=kabumap.public_url("7203"),
        received_at_utc=received,
        encoding="shift_jis",
    )
    observation = decode_attempt(
        (original,),
        expected_parser_version=None if not_computed else "kabumap-quants-3",
    )
    _stage(
        store,
        plan,
        0,
        [("kabumap", "7203", "observed", original.attempt_id, observation, 200)],
        (original,),
    )
    reference = save_ready(store, prepare_plan(store, plan, cumulative=True))
    state = merge_ready(store, [reference], database=database)
    assert state["failed"] == []
    assert state["plans"][0]["rows_added"] == 1
    assert state["plans"][0]["source_responses_added"] == 1
    history = read_observations(database, "7203", received).quants_scores[0]
    assert history.latest == observation
    assert history.previous == earlier
    assert history.latest.availability == ("not_computed" if not_computed else "available")
    assert history.latest.composite_score == (None if not_computed else 42)
    publish_market_lake(sqlite_path=database, mirror_root=mirror, store=store)
    verify_and_seal(store, state, handle=handle, directory=directory / "kabumap")
    saved = load_ready(store, reference)
    ack = read_acknowledgement(store, saved, reference)
    assert ack["readback"] == "verified"
    assert ack["observations"] == 1
    assert ack["state_counts"] == {
        "success": 0 if not_computed else 1,
        "normal_null": 1 if not_computed else 0,
        "failure": 0,
        "unattempted": 0,
    }
    assert merge_ready(store, [reference], database=database)["plans"] == []
