"""保存済み外部値だけで候補外の銘柄detailとserving exportへ到達できる。"""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from tests.engine.test_external_assessment_core import valuation
from tests.engine.test_external_assessment_sources import (
    rakuten_html,
    rakuten_no_estimates_html,
    rakuten_peer_only_html,
)
from tests.helpers.research_selection import selection

from baibai_batch.jobs.external_assessment_cohorts import (
    load_result,
    make_plan,
    new_result,
    record_outcome,
    result_value,
)
from baibai_engine.external_assessment.acquisition.collection import (
    CollectionOutcome,
    load_staging,
    save_staging,
)
from baibai_engine.external_assessment.acquisition.sources import ParseError, rakuten
from baibai_engine.external_assessment.query import external_assessment
from baibai_engine.external_assessment.store import save_observations
from baibai_engine.read_api import (
    APPLICATION_DB_PATH,
    INDICATORS_DB_PATH,
    MARKET_DB_PATH,
    RUNS_DB_PATH,
)
from baibai_engine.research.inputs import collect_research_inputs, project_research_inputs
from baibai_web.api.server import create_app
from baibai_web.materialize import export_read_models
from baibai_web.sources.db_sources import DbExternalAssessmentSource


def test_external_only_ticker_is_available_in_api_and_materialized_export(
    app_method_root, tmp_path, monkeypatch
):
    observation = valuation(
        datetime(2026, 10, 10, 7, tzinfo=UTC),
        ticker="9999",
        consensus_target=1500,
        share_basis=None,
    )
    save_observations(app_method_root / MARKET_DB_PATH, [observation])
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/securities/9999")
    assert response.status_code == 200
    detail = response.json()
    assert detail["stock_analysis"] is None
    assert (
        detail["external_assessment"]["model_valuations"][0]["inputs"]["latest"]["consensus_target"]
        == 1500
    )
    assert (
        detail["external_assessment"]["model_valuations"][0]["inputs"]["latest"]["share_basis"]
        is None
    )
    out = tmp_path / "export"
    cutoffs = []
    original_tickers = DbExternalAssessmentSource.observed_tickers
    original_assessment = DbExternalAssessmentSource.assessment

    def observed_tickers(self, *, cutoff):
        cutoffs.append(cutoff)
        return original_tickers(self, cutoff=cutoff)

    def assessment(self, ticker, *, cutoff):
        assert cutoffs
        assert cutoff == cutoffs[0]
        return original_assessment(self, ticker, cutoff=cutoff)

    monkeypatch.setattr(DbExternalAssessmentSource, "observed_tickers", observed_tickers)
    monkeypatch.setattr(DbExternalAssessmentSource, "assessment", assessment)
    export_read_models(app_method_root, out)
    saved = json.loads((out / "views/security--9999.json").read_text())
    assert (
        saved["external_assessment"]["model_valuations"]
        == detail["external_assessment"]["model_valuations"]
    )
    assert (
        saved["external_assessment"]["origin_groups"]
        == detail["external_assessment"]["origin_groups"]
    )


@pytest.mark.parametrize(
    ("html", "checkpoint_state", "comparison_state", "reason"),
    [
        (
            rakuten_html("9999", value="0"),
            "success",
            "first_observation",
            "no_previous_observation",
        ),
        (rakuten_peer_only_html("9999"), "normal_null", "missing", "latest_missing"),
        (rakuten_no_estimates_html(), "failure", "not_observed", "no_observation"),
    ],
    ids=("value-zero", "received-null", "identity-unconfirmed"),
)
def test_rakuten_missingness_survives_checkpoint_store_api_export_and_research(
    app_method_root, tmp_path, html, checkpoint_state, comparison_state, reason
):
    received = datetime(2026, 10, 12, 7, tzinfo=UTC)
    market = app_method_root / MARKET_DB_PATH
    # The other distributor's IFIS target must not fill Rakuten's missing rating.
    save_observations(market, [valuation(received, ticker="9999", consensus_target_origin="IFIS")])

    def parse():
        return rakuten.parse_html(
            html,
            ticker="9999",
            source_url=rakuten.IFRAME_BASE + rakuten.IFRAME_PARAMETER + "9999.T",
            fetched_at_utc=received,
        )

    if checkpoint_state == "failure":
        with pytest.raises(ParseError, match=r"^no_estimates_identity_unconfirmed$"):
            parse()
        observation = None
        outcome = CollectionOutcome(
            "rakuten", "9999", "parse_failure", "no_estimates_identity_unconfirmed", 200
        )
    else:
        observation = parse()
        outcome = CollectionOutcome("rakuten", "9999", "observed", http_status=200)
    plan = make_plan(
        run_id="synthetic-missingness",
        tickers=("9999",),
        universe_binding="synthetic",
        collector_binding="synthetic",
        sources=("rakuten",),
    )
    journal = record_outcome(plan, new_result(plan, 0), outcome, observation)
    restored = load_result(plan, result_value(plan, journal))
    assert restored.entries[0].state == checkpoint_state
    if observation is None:
        assert restored.entries[0].reason == "no_estimates_identity_unconfirmed"
    stage = tmp_path / "stage.json"
    save_staging(stage, restored.observations)
    observations = load_staging(stage)
    assert save_observations(market, observations) == int(observation is not None)
    assert save_observations(market, observations) == 0
    before = market.read_bytes()
    expected = external_assessment(market, ticker="9999", cutoff=received, evaluated_at=received)
    source = expected.analyst_ratings[0]
    assert source.distributor == "rakuten"
    assert source.inputs.latest == observation
    assert source.comparison.state == comparison_state
    assert source.comparison.reason == reason
    assert source.comparison.delta is None
    assert len(expected.origin_groups[0].references) == (2 if checkpoint_state == "success" else 1)
    if observation is not None:
        earlier = external_assessment(
            market, ticker="9999", cutoff=received.replace(hour=6), evaluated_at=received
        )
        assert earlier.analyst_ratings[0].comparison.state == "not_observed"
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        detail = client.get("/api/securities/9999").json()["external_assessment"]
    out = tmp_path / "export"
    export_read_models(app_method_root, out)
    saved = json.loads((out / "views/security--9999.json").read_text())["external_assessment"]
    selected = selection(now=received, tickers=("9999",))
    snapshot = collect_research_inputs(
        snapshot=selected.inputs.analysis,
        input_gaps=selected.inputs.input_gaps,
        tickers=("9999",),
        research_as_of=received.date(),
        input_known_at=received,
        market_path=market,
        indicators_path=app_method_root / INDICATORS_DB_PATH,
        app_path=app_method_root / APPLICATION_DB_PATH,
    )
    inputs = project_research_inputs(selected.model_copy(update={"inputs": snapshot}))[
        "securities"
    ]["9999"]["external_assessment"]
    for result in (detail, saved, inputs):
        assert result["analyst_ratings"] == expected.model_dump(mode="json")["analyst_ratings"]
    assert market.read_bytes() == before


def test_known_security_keeps_unknown_rating_without_any_external_observation(
    app_method_root, tmp_path
):
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/securities/2331")
    assert response.status_code == 200
    rating = response.json()["external_assessment"]["analyst_ratings"][0]
    assert rating["inputs"]["latest"] is None
    assert rating["comparison"]["state"] == "not_observed"
    out = tmp_path / "export"
    export_read_models(app_method_root, out)
    saved = json.loads((out / "views/security--2331.json").read_text())
    assert saved["external_assessment"]["analyst_ratings"] == [rating]


@pytest.mark.parametrize("without_run", [False, True])
def test_all_external_sources_are_searchable_without_machine_analysis(
    app_method_root, tmp_path, without_run
):
    from datetime import date

    from tests.engine.test_external_assessment import quants, tv_row
    from tests.engine.test_external_assessment_core import rating

    from baibai_engine.storage.observations.schema import open_connection

    market = app_method_root / MARKET_DB_PATH
    received = datetime(2026, 10, 10, 7, tzinfo=UTC)
    save_observations(
        market,
        [
            valuation(received, ticker="9999"),
            rating(received, ticker="9999"),  # 同一tickerを別媒体で重複表示しない。
            rating(received, ticker="1111", rating=None),
            quants(received, date(2026, 10, 9), growth_score=0).model_copy(
                update={"ticker": "2222"}
            ),
            valuation(received, ticker="2331"),  # 既存Security Analysisに重複追加しない。
            valuation(datetime(2099, 1, 1, tzinfo=UTC), ticker="4444"),
        ],
    )
    tv_row(market, received, ticker="3333", status="unresolved")
    with open_connection(market) as conn:
        conn.executemany(
            "INSERT INTO jquants_master_snapshots(snapshot_date,ticker,name,sector_33) "
            "VALUES (?,?,?,?)",
            [
                ("2026-10-09", "9999", "合成検索企業", "サービス業"),
                ("2026-10-09", "2222", "合成零点企業", "情報・通信業"),
                ("2026-10-09", "1111", None, None),
                ("2026-10-09", "3333", "", None),
                ("2099-01-01", "9999", "未来の名称", "小売業"),
            ],
        )
        conn.commit()
    if without_run:
        (app_method_root / RUNS_DB_PATH).unlink()
    before = market.read_bytes()
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/screening/latest")
        assert response.status_code == 200
        view = response.json()
        index = {row["ticker"]: row for row in view["external_assessment_securities"]}
        assert set(index) == {"1111", "2222", "3333", "9999"} | ({"2331"} if without_run else set())
        assert index["9999"]["name"] == "合成検索企業"
        assert index["1111"]["name"] is None
        assert index["3333"]["name"] is None
        assert "er_annual" not in index["9999"]
        for ticker, identity in index.items():
            detail = client.get(f"/api/securities/{ticker}")
            assert detail.status_code == 200
            assert detail.json()["company_name"] == identity["name"]
            assert detail.json()["stock_analysis"] is None
    out = tmp_path / "export"
    export_read_models(app_method_root, out)
    saved = json.loads((out / "views/screening_latest.json").read_text())
    assert saved["external_assessment_securities"] == view["external_assessment_securities"]
    for ticker, identity in index.items():
        detail = json.loads((out / f"views/security--{ticker}.json").read_text())
        assert detail["company_name"] == identity["name"]
    assert not (out / "views/security--4444.json").exists()
    assert market.read_bytes() == before
