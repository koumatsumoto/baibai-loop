"""新規計算は廃止値を採用せず、保存済みモデルと有限L3投影は読める。"""

from datetime import timedelta

from tests.helpers.external_assessment_observations import RECEIVED, rating, valuation

from baibai_engine.investment_judgment.workflow.screening_brief import _external
from baibai_engine.machine_analysis.generation.runners.external_assessment import (
    generate_external_assessment,
)
from baibai_engine.machine_analysis.models.external_assessment.models import ExternalAssessment
from baibai_engine.machine_analysis.observations.external_assessments.history import (
    AnalystForecastHistory,
    AnalystRatingHistory,
    ModelValuationHistory,
    ObservationHistory,
    QuantsScoreHistory,
)
from baibai_engine.machine_analysis.observations.external_assessments.models import (
    AnalystForecastObservation,
    QuantsScoreObservation,
)
from baibai_engine.machine_analysis.screening.policies.economic_path_context import (
    _external_context,
)


def test_new_generation_drops_retired_values_without_altering_inputs_or_survivors():
    cutoff = RECEIVED + timedelta(days=30)
    forecast = AnalystForecastObservation(
        ticker="1234",
        fetched_at_utc=RECEIVED,
        source_url="https://example.invalid/minkabu",
        parser_version="saved-1",
        display_as_of_date="2026-10-10",
        target_price_yen=1500,
        strong_buy_count=0,
        buy_count=2,
        analyst_eps_yen=0,
        origin=None,
        share_basis=None,
    )
    legacy_ratings = AnalystRatingHistory(latest=rating(origin="IFIS", rating_count=0))
    history = ObservationHistory(
        ticker="1234",
        cutoff=cutoff,
        model_valuations=(ModelValuationHistory(latest=valuation(consensus_target_origin="IFIS")),),
        quants_scores=(
            QuantsScoreHistory(
                latest=QuantsScoreObservation(
                    ticker="1234",
                    fetched_at_utc=RECEIVED,
                    score_as_of_date="2026-10-09",
                    source_url="https://example.invalid/kabumap",
                    parser_version="saved-1",
                    growth_score=0,
                )
            ),
        ),
        analyst_ratings=(legacy_ratings,),
        minkabu=AnalystForecastHistory(latest=forecast),
    )
    before = history.model_dump_json()
    fresh = generate_external_assessment(history, evaluated_at=cutoff)
    no_retired = generate_external_assessment(
        history.model_copy(update={"analyst_ratings": (), "minkabu": AnalystForecastHistory()}),
        evaluated_at=cutoff,
    )
    assert fresh == no_retired
    assert fresh.method_version == "same-series-v7"
    assert fresh.analyst_ratings == ()
    assert fresh.minkabu.latest is fresh.minkabu.previous is None
    assert fresh.model_valuations[0].inputs == history.model_valuations[0]
    assert fresh.quants_scores[0].inputs == history.quants_scores[0]
    assert {
        (ref.distributor, ref.metric)
        for group in fresh.origin_groups
        for ref in group.references
        if group.origin == "IFIS"
    } == {("monex", "consensus_target")}
    context = _external_context(fresh.model_dump(mode="json"))
    brief = _external(context)
    assert context["minkabu_latest"] is None
    assert brief["minkabu"] == {}
    assert all(row.get("distributor") not in {"rakuten", "minkabu"} for row in brief["sources"])
    assert history.model_dump_json() == before

    # A saved v6 result is decoded, never regenerated with today's source policy.
    old_payload = fresh.model_dump(mode="json")
    old_payload.update(
        method_version="same-series-v6",
        analyst_ratings=[
            {
                "distributor": "rakuten",
                "inputs": legacy_ratings.model_dump(mode="json"),
                "comparison": {
                    "metric": "rating",
                    "state": "first_observation",
                    "latest_value": 3.5,
                    "latest_count": 0,
                },
            }
        ],
        minkabu=history.minkabu.model_dump(mode="json"),
    )
    saved = ExternalAssessment.model_validate(old_payload)
    original = saved.model_dump_json()
    assert ExternalAssessment.model_validate_json(original).model_dump_json() == original
    old_context = _external_context(saved.model_dump(mode="json"))
    old_brief = _external(old_context)
    assert old_brief["minkabu"]["strong_buy_count"] == 0
    assert old_brief["minkabu"]["buy_count"] == 2
    assert old_brief["minkabu"]["origin"] is None
    assert old_brief["minkabu"]["share_basis"] is None
    assert any(row.get("distributor") == "rakuten" for row in old_brief["sources"])
    # Older payloads omitted the revision/forecast slot. Their old defaults stay old.
    old_payload.pop("method_version")
    old_payload.pop("minkabu")
    oldest = ExternalAssessment.model_validate(old_payload)
    assert oldest.method_version == "same-series-v4"
    assert oldest.minkabu.latest is None
    assert oldest.analyst_ratings == saved.analyst_ratings


def test_current_reads_do_not_query_retired_tables_while_history_reads_do(tmp_path, monkeypatch):
    import baibai_engine.machine_analysis.observations.external_assessments.reader as reader
    from baibai_engine.machine_analysis.generation.runners.external_assessment import (
        external_assessment,
    )
    from baibai_engine.machine_analysis.observations.external_assessments.contract import (
        ANALYST_FORECAST_TABLE,
        ANALYST_RATING_TABLE,
    )
    from baibai_engine.machine_analysis.observations.external_assessments.store import (
        save_observations,
    )

    path = tmp_path / "saved.sqlite"
    save_observations(path, [valuation(), rating()])
    before = path.read_bytes()
    calls = []
    original = reader._latest_rows

    def record(conn, table, *args):
        calls.append(table)
        return original(conn, table, *args)

    monkeypatch.setattr(reader, "_latest_rows", record)
    current = external_assessment(path, ticker="1234", cutoff=RECEIVED, evaluated_at=RECEIVED)
    assert current.model_valuations[0].inputs.latest == valuation()
    assert current.analyst_ratings == ()
    assert set(calls).isdisjoint({ANALYST_RATING_TABLE, ANALYST_FORECAST_TABLE})
    population = reader.read_observation_population(
        path, tickers=("1234",), cutoff=RECEIVED, include_retired=False
    )
    assert population["1234"].analyst_ratings == ()
    assert set(calls).isdisjoint({ANALYST_RATING_TABLE, ANALYST_FORECAST_TABLE})
    saved = reader.read_observations(path, "1234", RECEIVED)
    assert saved.analyst_ratings[0].latest == rating()
    assert {ANALYST_RATING_TABLE, ANALYST_FORECAST_TABLE} <= set(calls)
    assert path.read_bytes() == before
