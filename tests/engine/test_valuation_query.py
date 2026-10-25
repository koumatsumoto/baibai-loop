"""保存済み機械推計の対象集合・cutoff・副作用と、Valuation の依存境界。"""

import copy
import hashlib
import socket
import sqlite3
import subprocess
import sys
from datetime import date, datetime

import pytest
from tests.helpers.research_selection import selection
from tests.helpers.screening_run import screening_run_payload, stock_analysis

from baibai_engine.read_api import stored_machine_valuation
from baibai_engine.read_api.ticker_profile import _load_stock_analysis_context
from baibai_engine.research.inputs import collect_research_inputs, project_research_inputs
from baibai_engine.research.selection import InputGap
from baibai_engine.research.selection_query import load_analysis_snapshot
from baibai_engine.screening.discovery.candidates import build_candidate_contexts
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.run_store import ScreeningRunStore
from baibai_engine.screening.schema import DerivedMetrics
from baibai_engine.screening.stock_analysis_builder import (
    build_stock_analysis_metrics,
    expected_return_inputs,
)
from baibai_engine.valuation.estimates import estimate_expected_return
from baibai_engine.valuation.metrics.models import FinancialSnapshot
from baibai_engine.valuation.query import (
    anchor_gap_pct,
    balance_sheet_from_analysis,
    cash_flow_from_analysis,
    forecast_sources_from_analysis,
    machine_valuation_from_analysis,
    valuation_metrics_from_analysis,
)

CUTOFF = date(2026, 9, 19)


@pytest.mark.parametrize(
    ("jq_ocf", "edinet_ocf", "capex", "fcf"),
    [(120.0, -35.0, 20.0, -55.0), (0.0, 0.0, 0.0, 0.0), (None, None, None, None)],
)
def test_saved_cf_sources_reach_research_without_recomputing_or_changing_selection(
    tmp_path, jq_ocf, edinet_ocf, capex, fcf
):
    row = stock_analysis(
        "1234",
        per_forward=10.0,
        metrics={
            "per_forward_sector_gap": -0.2,
            "normalized_per_3fy": 12.0,
            "ocf_ttm": jq_ocf,
            "ocf_yield": None if jq_ocf is None else jq_ocf / 1000,
            "edinet_ocf_ttm": edinet_ocf,
            "capex_ttm": capex,
            "fcf_ttm": fcf,
            "fcf_yield": None if fcf is None else fcf / 1000,
            "fin_latest_disclosed_date": "2026-09-18",
            "edinet_snapshot_asof": "2026-09-11",
            "edinet_snapshot_recorded_at_utc": "2026-09-11T10:00:00+00:00",
            "edinet_source_doc_id": "S100SYNTHETIC",
            "edinet_document_type": "160",
            "edinet_source_submit_datetime": "2026-09-10 12:00",
            "edinet_source_period_start": "2026-04-01",
            # Half-year document metadata may contain a future fiscal year end.
            "edinet_source_period_end": "2027-03-31",
            "edinet_capex_source": "purchase_of_fixed_assets",
            "cash_eq": jq_ocf,
            "cash": edinet_ocf,
            "debt": capex,
            # Deliberately not cash - debt: projection preserves saved facts.
            "net_cash": fcf,
            "investment_securities": 75.0,
            "total_assets": 900.0,
            "same_state_equity_yen": 400.0,
            "equity_ratio": 0.4,
            "bs_carry_forward_fields": "cash_eq,total_assets",
            "bs_carry_forward_lag_days": 90,
            "capital_basis_failure_reason": "synthetic_basis_gap",
            "edinet_failure_reasons": "metric_unavailable:debt_breakdown",
        },
        ttm_quality={"ocf_yield": "exact", "fcf_yield": "approximated"},
    )
    original = copy.deepcopy(row)
    rules = load_screening_rules()
    selection = build_candidate_contexts(
        [row],
        rules=rules.candidate_discovery,
        required_jpx_flags=rules.universe.required_jpx_flags,
    )
    path = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(path)
    store.publish_run(
        screening_run_payload(as_of=CUTOFF.isoformat(), stock_analyses=[row]),
        run_revision_id="cf-fixed",
        rules=load_screening_rules(),
    )
    store.publish_run(
        screening_run_payload(
            as_of="2026-09-20",
            stock_analyses=[stock_analysis("1234", metrics={"fcf_ttm": 999.0})],
        ),
        run_revision_id="future",
        rules=load_screening_rules(),
    )
    before = path.read_bytes()
    now = datetime.fromisoformat("2026-09-19T18:00:00+09:00")
    inputs = _project_inputs(
        tickers=("1234", "9999"),
        as_of=CUTOFF,
        cutoff=now,
        known_at=now,
        market_path=tmp_path / "absent-market",
        runs_path=path,
        indicators_path=tmp_path / "absent-indicators",
        app_path=tmp_path / "absent-app",
        run_revision_id="cf-fixed",
    )
    security = inputs["securities"]["1234"]
    assert security["machine_valuation"]["run_revision_id"] == "cf-fixed"
    assert "not company-specific value" in security["machine_valuation"]["role"]
    assert security["valuation_metrics"]["ratios"]["per_forward"] == 10
    assert security["valuation_metrics"]["normalized_per_3fy"] == 12
    assert inputs["securities"]["9999"]["valuation_metrics"] is None
    assert security["cash_flow"] == {
        "jquants": {
            "ocf_ttm": jq_ocf,
            "ocf_yield": None if jq_ocf is None else jq_ocf / 1000,
            "ttm_quality": "exact",
            "fin_latest_disclosed_date": "2026-09-18",
        },
        "edinet": {
            "edinet_snapshot_asof": "2026-09-11",
            "edinet_snapshot_recorded_at_utc": "2026-09-11T10:00:00+00:00",
            "edinet_ocf_ttm": edinet_ocf,
            "capex_ttm": capex,
            "fcf_ttm": fcf,
            "fcf_yield": None if fcf is None else fcf / 1000,
            "ttm_quality": "approximated",
            "edinet_capex_source": "purchase_of_fixed_assets",
            "document_metadata": {
                "edinet_source_doc_id": "S100SYNTHETIC",
                "edinet_document_type": "160",
                "edinet_source_submit_datetime": "2026-09-10 12:00",
                "edinet_source_period_start": "2026-04-01",
                "edinet_source_period_end": "2027-03-31",
            },
        },
    }
    assert inputs["securities"]["9999"]["cash_flow"] is None
    assert inputs["securities"]["9999"]["forecast_sources"] is None
    bs = security["balance_sheet"]
    assert bs["amount_unit"] == "JPY"
    assert bs["jquants"] == {
        "cash_eq": jq_ocf,
        "total_assets": 900.0,
        "same_state_equity_yen": 400.0,
        "equity_ratio": 0.4,
        "fin_latest_disclosed_date": "2026-09-18",
        "measurement_date": None,
        "measurement_date_status": "field_measurement_dates_not_stored",
        "bs_carry_forward_fields": "cash_eq,total_assets",
        "bs_carry_forward_lag_days": 90,
        "capital_basis_failure_reason": "synthetic_basis_gap",
    }
    assert bs["edinet"]["cash"] == edinet_ocf
    assert bs["edinet"]["debt"] == capex
    assert bs["edinet"]["net_cash"] == fcf
    assert bs["edinet"]["investment_securities"] == 75.0
    assert bs["edinet"]["edinet_snapshot_asof"] == "2026-09-11"
    assert bs["edinet"]["edinet_snapshot_recorded_at_utc"] == "2026-09-11T10:00:00+00:00"
    assert bs["edinet"]["measurement_date"] is None
    assert bs["edinet"]["debt_scope"] is None
    assert bs["edinet"]["document_metadata"] == security["cash_flow"]["edinet"]["document_metadata"]
    assert inputs["securities"]["9999"]["balance_sheet"] is None
    assert row == original
    assert (
        build_candidate_contexts(
            [row],
            rules=rules.candidate_discovery,
            required_jpx_flags=rules.universe.required_jpx_flags,
        )
    ) == selection
    assert path.read_bytes() == before


def test_absent_cf_and_document_metadata_stay_missing():
    for analysis in ({}, {"metrics": {}}, {"metrics": None}):
        cf = cash_flow_from_analysis(analysis)
        assert all(value is None for value in cf["jquants"].values())
        assert all(value is None for value in cf["edinet"]["document_metadata"].values())
        assert cf["edinet"]["edinet_ocf_ttm"] is None
        assert cf["edinet"]["capex_ttm"] is None
        assert cf["edinet"]["fcf_ttm"] is None


def test_financial_forecast_sources_survive_stock_analysis_serialization():
    financial = FinancialSnapshot(
        latest_financial_disclosure_date=CUTOFF,
        per_forward=10.0,
        per_trailing=None,
        pbr=0.8,
        ev_ebitda=None,
        p_s=None,
        pcfr=None,
        eps=None,
        sales_ttm=None,
        ocf_ttm=None,
        market_price_yen=1000.0,
        dps_actual_annual=40.0,
        dps_forecast_annual=170.0,
        dividend_yield=0.04,
        dividend_basis="actual_reported",
        earnings_forecast_source={
            "status": "partially_unresolved",
            "disclosure_number": "income-unknown",
            "period_start": "2026-04-01",
            "period_end": "2027-03-31",
            "basis": "consolidated",
            "target_basis": "consolidated",
            "candidate_disclosure_numbers": "income-known,income-unknown",
            "forecast_eps_source_records": "income-known,income-unknown",
            "forecast_profit_source_records": None,
        },
        dividend_forecast_source={
            "status": "confirmed",
            "disclosure_number": "dividend-revision",
            "period_start": "2027-04-01",
            "period_end": "2028-03-31",
            "basis": "issuer_dividend",
            "target_basis": "issuer_dividend",
            "dps_forecast_annual_source_records": "dividend-revision",
        },
    )
    row = stock_analysis(
        "1234",
        metrics=build_stock_analysis_metrics(financial, derived=DerivedMetrics()),
    )
    assert forecast_sources_from_analysis(row) == {
        "earnings_forecast_source": {
            "status": "partially_unresolved",
            "disclosure_number": "income-unknown",
            "period_start": "2026-04-01",
            "period_end": "2027-03-31",
            "basis": "consolidated",
            "target_basis": "consolidated",
            "candidate_disclosure_numbers": "income-known,income-unknown",
            "forecast_eps_source_records": "income-known,income-unknown",
            "forecast_profit_source_records": None,
        },
        "dividend_forecast_source": {
            "status": "confirmed",
            "disclosure_number": "dividend-revision",
            "period_start": "2027-04-01",
            "period_end": "2028-03-31",
            "basis": "issuer_dividend",
            "target_basis": "issuer_dividend",
            "dps_forecast_annual_source_records": "dividend-revision",
        },
    }
    valuation = machine_valuation_from_analysis(row)
    assert valuation.dps_actual_annual == 40.0
    assert valuation.dps_forecast_annual == 170.0
    assert valuation.dividend_yield == 0.04
    assert valuation.dividend_basis == "actual_reported"


@pytest.mark.parametrize(
    "analysis",
    [
        {},
        {"metrics": None},
        {"metrics": {}},
        {"metrics": {"earnings_forecast_source": None, "dividend_forecast_source": None}},
    ],
)
def test_unrecorded_forecast_sources_stay_missing(analysis):
    original = copy.deepcopy(analysis)
    assert forecast_sources_from_analysis(analysis) == {
        "earnings_forecast_source": None,
        "dividend_forecast_source": None,
    }
    assert analysis == original


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("broken JSON", "Expecting value"),
        ("null", "saved earnings_forecast_source must be a JSON object string"),
        ("[]", "saved earnings_forecast_source must be a JSON object string"),
        ({}, "saved earnings_forecast_source must be a JSON object string"),
    ],
)
def test_malformed_saved_forecast_source_is_not_reported_as_unrecorded(source, message):
    with pytest.raises(ValueError, match=message):
        forecast_sources_from_analysis({"metrics": {"earnings_forecast_source": source}})


@pytest.mark.parametrize("bad", [None, True, "100", float("nan"), float("inf")])
def test_balance_sheet_projection_keeps_unknowns_and_never_derives_dates(bad):
    bs = balance_sheet_from_analysis({"metrics": {"cash": bad, "cash_eq": bad}})
    assert bs["edinet"]["cash"] is None
    assert bs["jquants"]["cash_eq"] is None
    assert bs["edinet"]["measurement_date"] is None
    assert bs["jquants"]["measurement_date"] is None
    assert bs["edinet"]["net_cash_quality"] is None
    assert all(value is None for value in bs["edinet"]["document_metadata"].values())


@pytest.mark.parametrize(
    ("dividend", "share_change", "basis", "carry"),
    [
        (None, None, "unavailable", 0.0),
        (0.0, 0.0, "actual_reported", 0.0),
        (0.03, -0.02, "forecast_annual", 0.05),
        (None, 0.03, "unavailable", -0.03),
    ],
)
def test_saved_carry_inputs_reach_valuation_candidates_and_research_without_changing_er(
    tmp_path, dividend, share_change, basis, carry
):
    financial = FinancialSnapshot(
        latest_financial_disclosure_date=CUTOFF,
        per_forward=10.0,
        per_trailing=None,
        pbr=0.8,
        ev_ebitda=None,
        p_s=None,
        pcfr=None,
        eps=None,
        sales_ttm=None,
        ocf_ttm=None,
        market_price_yen=100.0,
        dividend_yield=dividend,
        net_share_change_yoy=share_change,
        dividend_basis=basis,
    )
    derived = DerivedMetrics(
        sector_median_value={"pbr": 1.2, "per_forward": 15.0},
        self_range_median={"pbr": 1.0},
        sector_median_gap={"per_forward": -0.5},
    )
    estimate = estimate_expected_return(expected_return_inputs(financial, derived))
    assert estimate is not None
    assert estimate.carry_annual == pytest.approx(carry)
    assert estimate.er_annual == pytest.approx(0.0375 + carry)
    row = stock_analysis(
        "1234",
        per_forward=financial.per_forward,
        pbr=financial.pbr,
        metrics=build_stock_analysis_metrics(financial, derived=derived, estimate=estimate),
    )
    path = tmp_path / "runs.sqlite"
    ScreeningRunStore(path).publish_run(
        screening_run_payload(as_of=CUTOFF.isoformat(), stock_analyses=[row]),
        run_revision_id="carry-inputs",
        rules=load_screening_rules(),
    )
    before = path.read_bytes()
    valuation = stored_machine_valuation(path, ticker="1234", as_of=CUTOFF).valuation
    assert valuation.dividend_yield == dividend
    assert valuation.net_share_change_yoy == share_change
    assert valuation.dividend_basis == basis
    assert "not observed zero" in valuation.carry_missing_assumption
    assert valuation.assumptions == estimate.assumptions
    now = datetime.fromisoformat("2026-09-19T18:00:00+09:00")
    research = _project_inputs(
        tickers=("1234",),
        as_of=CUTOFF,
        cutoff=now,
        known_at=now,
        market_path=tmp_path / "absent-market",
        runs_path=path,
        indicators_path=tmp_path / "absent-indicators",
        app_path=tmp_path / "absent-app",
    )["securities"]["1234"]["machine_valuation"]["estimate"]
    for key in (
        "dividend_yield",
        "net_share_change_yoy",
        "dividend_basis",
        "carry_missing_assumption",
    ):
        assert research[key] == getattr(valuation, key)
    assert valuation.carry_annual == research["carry_annual"] == carry
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("actual", "forecast", "basis"),
    [
        (40.0, 170.0, "actual_reported"),
        (0.0, 12.0, "forecast_annual"),
        (None, 12.0, "forecast_annual"),
        (12.0, 0.0, "forecast_annual"),
        (None, None, "unavailable"),
    ],
)
def test_saved_dps_reaches_research_without_changing_candidates_or_raw_values(
    tmp_path, actual, forecast, basis
):
    row = stock_analysis(
        "1234",
        per_forward=10.0,
        metrics={
            "per_forward_sector_gap": -0.2,
            "dps_actual_annual": actual,
            "dps_forecast_annual": forecast,
            "dividend_basis": basis,
            "dividend_yield": 0.01,
            "er_annual": 0.05,
            "er_carry_annual": 0.01,
        },
    )
    rules = load_screening_rules()
    kwargs = {
        "rules": rules.candidate_discovery,
        "required_jpx_flags": rules.universe.required_jpx_flags,
    }
    before = build_candidate_contexts([row], **kwargs)
    row_without_dps = copy.deepcopy(row)
    del row_without_dps["metrics"]["dps_actual_annual"]
    del row_without_dps["metrics"]["dps_forecast_annual"]
    without_dps = build_candidate_contexts([row_without_dps], **kwargs)
    assert before == without_dps
    assert row["metrics"]["dps_actual_annual"] == actual
    assert row["metrics"]["dps_forecast_annual"] == forecast
    assert row["metrics"]["dividend_basis"] == basis
    assert row["metrics"]["er_annual"] == 0.05

    path = tmp_path / "runs.sqlite"
    selected = selection(
        now=datetime.fromisoformat("2026-09-19T18:00:00+09:00"),
        tickers=("1234", "5678"),
        runs=path,
        rows=[row, {**row_without_dps, "ticker": "5678"}],
    )
    saved = type(selected).model_validate_json(selected.model_dump_json())
    snapshot_before = saved.model_dump(mode="json")
    # Normal Research reads the frozen analysis, even after its mutable run store is gone.
    path.unlink()
    projected = project_research_inputs(saved)["securities"]
    recorded = projected["1234"]["machine_valuation"]["estimate"]
    assert recorded["dps_actual_annual"] == actual
    assert recorded["dps_forecast_annual"] == forecast
    assert recorded["dividend_basis"] == basis
    assert recorded["er_annual"] == 0.05
    unrecorded = projected["5678"]["machine_valuation"]["estimate"]
    assert unrecorded["dps_actual_annual"] is None
    assert unrecorded["dps_forecast_annual"] is None
    assert "dps_actual_annual" not in saved.inputs.analysis.analyses["5678"]["metrics"]
    assert "dps_forecast_annual" not in saved.inputs.analysis.analyses["5678"]["metrics"]
    assert saved.model_dump(mode="json") == snapshot_before


def publish(path, day, revision, *, price=1000, anchor=1500, run_at=None):
    ScreeningRunStore(path).publish_run(
        screening_run_payload(
            as_of=day,
            run_at=run_at,
            stock_analyses=[
                stock_analysis(
                    "1234",
                    metrics={
                        "market_price_yen": price,
                        "fv_sector_median_yen": anchor,
                        "fv_self_range_yen": 1300,
                        "er_annual": 0.075,
                        "er_reversion_annual": 0.025,
                        "er_carry_annual": 0.05,
                        "er_model_version": "expected-return-v1",
                        "er_unit": "annual_ratio",
                    },
                )
            ],
        ),
        run_revision_id=revision,
        rules=load_screening_rules(),
    )


def test_saved_non_candidate_and_profile_use_latest_run_before_cutoff(tmp_path):
    path = tmp_path / "runs.sqlite"
    publish(path, "2026-09-18", "old")
    publish(path, "2026-09-19", "early", anchor=1400, run_at="2026-09-19T09:00:00+09:00")
    publish(path, "2026-09-19", "last", anchor=1600)
    publish(path, "2026-09-20", "future", anchor=2000)
    # Candidate membership cannot authorize or suppress this saved-analysis read.
    result = stored_machine_valuation(path, ticker="1234", as_of=CUTOFF)
    assert result.run_revision_id == "last"
    assert result.as_of == CUTOFF
    assert result.valuation.sector_median_anchor_yen == 1600
    assert result.valuation.er_annual == 0.075
    assert anchor_gap_pct(result.valuation) == 60
    context = _load_stock_analysis_context(path, "1234", CUTOFF, run_revision_id=None)
    assert context["screening_run_revision_id"] == result.run_revision_id
    assert context["stock_analysis"]["metrics"]["fv_sector_median_yen"] == 1600
    assert stored_machine_valuation(path, ticker="1234", as_of=date(2026, 9, 17)) is None


@pytest.mark.parametrize("entry", [stored_machine_valuation, _load_stock_analysis_context])
def test_fixed_run_checks_cutoff_and_identity(tmp_path, entry):
    path = tmp_path / "runs.sqlite"
    publish(path, "2026-09-19", "fixed")
    publish(path, "2026-09-20", "future")

    def read(revision):
        if entry is stored_machine_valuation:
            return entry(path, ticker="1234", as_of=CUTOFF, run_revision_id=revision)
        return entry(path, "1234", CUTOFF, run_revision_id=revision)

    assert read("fixed") is not None
    with pytest.raises(ValueError, match="after cutoff"):
        read("future")
    with pytest.raises(ValueError, match="unknown run_revision_id"):
        read("absent")


def test_missing_store_scope_and_coordinates_are_distinct(tmp_path):
    path = tmp_path / "runs.sqlite"
    assert stored_machine_valuation(path, ticker="1234", as_of=CUTOFF) is None
    assert not path.exists()
    sqlite3.connect(path).close()
    assert stored_machine_valuation(path, ticker="1234", as_of=CUTOFF) is None
    publish(path, "2026-09-19", "run")
    result = stored_machine_valuation(path, ticker="9999", as_of=CUTOFF)
    assert result.run_revision_id == "run"
    assert result.valuation is None
    publish(path, "2026-09-20", "missing-anchor", anchor=None)
    result = stored_machine_valuation(path, ticker="1234", as_of=date(2026, 9, 20))
    assert result.valuation.sector_median_anchor_yen is None
    assert result.valuation.er_annual == 0.075
    assert anchor_gap_pct(result.valuation) is None
    for raw in ({}, {"metrics": {}}, {"metrics": None}):
        assert machine_valuation_from_analysis(raw).sector_median_anchor_yen is None
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE stock_analysis")
    connection.close()
    with pytest.raises(sqlite3.OperationalError):
        stored_machine_valuation(path, ticker="1234", as_of=CUTOFF)


def test_read_has_no_network_or_store_writes(tmp_path, monkeypatch):
    path = tmp_path / "runs.sqlite"
    publish(path, "2026-09-19", "run")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    original = sqlite3.connect
    connections = []

    def connect(database, *args, **kwargs):
        assert "mode=ro" in database
        connection = original(database, *args, **kwargs)
        connections.append(connection)

        # Assert that no INSERT/UPDATE/DELETE or schema mutation is even attempted.
        def authorize(action, _a, _b, _db, _trigger):
            assert action not in {
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_CREATE_TABLE,
                sqlite3.SQLITE_DROP_TABLE,
            }
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    def forbidden(*_args, **_kwargs):
        pytest.fail("valuation read attempted network I/O")

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(socket, "socket", forbidden)
    assert stored_machine_valuation(path, ticker="1234", as_of=CUTOFF).valuation.er_annual == 0.075
    assert connections
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_valuation_import_does_not_start_research_or_aggregate_read_api():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from baibai_engine.valuation.models import Valuation
from baibai_engine.valuation.estimates import estimate_expected_return
from baibai_engine.valuation.query import machine_valuation_from_analysis
assert not any(name.startswith(("baibai_engine.research", "baibai_engine.read_api", "baibai_engine.screening")) for name in sys.modules)
""",
        ],
        check=True,
    )


def _project_inputs(
    *,
    tickers,
    as_of,
    cutoff,
    known_at,
    market_path,
    runs_path,
    indicators_path,
    app_path,
    run_revision_id=None,
):
    revision = run_revision_id or "carry-inputs"
    gaps = tuple(
        InputGap(
            ticker=t, reason="synthetic analysis unavailable", attempted_sources=("saved source",)
        )
        for t in tickers
        if t == "9999"
    )
    snapshot = load_analysis_snapshot(
        run_revision_id=revision,
        research_as_of=as_of,
        tickers=tickers,
        gaps=gaps,
        known_at=known_at,
        runs_path=runs_path,
    )
    inputs = collect_research_inputs(
        snapshot=snapshot,
        input_gaps=gaps,
        tickers=tickers,
        research_as_of=as_of,
        input_known_at=cutoff,
        market_path=market_path,
        indicators_path=indicators_path,
        app_path=app_path,
    )
    value = selection(now=known_at, tickers=tickers)
    return project_research_inputs(value.model_copy(update={"inputs": inputs}))


def test_valuation_metrics_project_saved_coordinates_without_reconstructing_earnings():
    saved = {
        "per_forward": 10,
        "per_trailing": 0,
        "pbr": 0.8,
        "ev_ebitda": -2,
        "p_s": None,
        "pcfr": True,
        "metrics": {"normalized_per_3fy": 12, "market_price_yen": 1000},
        "ttm_quality": {"per_trailing": "exact", "ev_ebitda": "approximated"},
    }
    original = copy.deepcopy(saved)
    projected = valuation_metrics_from_analysis(saved)
    assert projected["ratios"] == {
        "per_forward": 10,
        "per_trailing": 0,
        "pbr": 0.8,
        "ev_ebitda": -2,
        "p_s": None,
        "pcfr": None,
    }
    assert projected["normalized_per_3fy"] == 12
    assert projected["ttm_quality"]["per_trailing"] == "exact"
    assert "forecast_eps" not in projected
    assert saved == original
