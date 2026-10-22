"""The new source survives the existing private fixed-cohort handoff."""

import json
from datetime import UTC, datetime
from uuid import uuid4

from baibai_batch.jobs.external_assessment_cohorts import (
    load_plan,
    load_result,
    make_plan,
    new_result,
    plan_bytes,
    record_outcome,
    result_value,
)
from baibai_engine.batch_api import (
    ExternalAssessmentAnalystForecastObservation,
    ExternalAssessmentCollectionOutcome,
)


def test_default_plan_includes_four_sources_and_minkabu_roundtrips():
    plan = make_plan(
        run_id="synthetic-minkabu",
        tickers=("7203",),
        universe_binding="synthetic-universe",
        collector_binding="synthetic-code",
    )
    assert plan.sources == ("monex", "rakuten", "kabumap", "minkabu")
    assert load_plan(plan_bytes(plan)) == plan
    assert len(plan.cohorts[0].pairs) == 4
    row = ExternalAssessmentAnalystForecastObservation(
        ticker="7203",
        fetched_at_utc=datetime(2026, 10, 22, tzinfo=UTC),
        source_url="https://minkabu.jp/stock/7203/analyst_consensus",
        parser_version="synthetic-1",
        display_as_of_date="2026-10-22",
        target_price_yen=100,
    )
    outcome = ExternalAssessmentCollectionOutcome(
        "minkabu",
        "7203",
        "observed",
        None,
        200,
        str(uuid4()),
    )
    result = record_outcome(plan, new_result(plan, 0), outcome, row)
    restored = load_result(plan, json.loads(json.dumps(result_value(plan, result))))
    assert restored == result
    assert restored.observations == (row,)
    assert [entry.state for entry in restored.entries] == ["unattempted"] * 3 + ["success"]
