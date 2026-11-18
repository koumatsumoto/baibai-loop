from datetime import date

import pytest
from tests.engine.investment_judgment.workflow.test_generated_research_inputs import AS_OF, NOW
from tests.helpers.research_selection import selection
from tests.helpers.retained_model_generation import seed_generation

from baibai_engine.investment_judgment.cash_flow_use import cash_flow_limit
from baibai_engine.investment_judgment.workflow.generated_inputs import project_generated_inputs
from baibai_engine.investment_judgment.workflow.inputs import project_research_inputs
from baibai_engine.investment_judgment.workflow.selection import parse_selection, selection_sha256
from baibai_engine.machine_analysis.generation.query import read_model_inputs


@pytest.mark.parametrize(
    ("sector", "reason"),
    [
        ("銀行業", "bank_funding_flows"),
        ("保険業", "insurer_funding_flows"),
        ("証券･商品先物取引業", "financing_business_review"),
        ("その他金融業", "financing_business_review"),
        ("その他", "business_model_unverified"),
        (None, "business_model_unverified"),
        ("機械", None),
        ("情報･通信業", None),
    ],
)
def test_classification_is_instrument_and_date_bound(sector, reason):
    master = {"snapshot_date": "2026-05-12", "sector_33": sector}
    sector_use = cash_flow_limit({"master": master}, ticker="1234", as_of=date(2026, 5, 12))
    assert (sector_use.reason if sector_use else None) == reason
    for data in [
        {"ticker": "5678", "master": master},
        {"master": {**master, "ticker": "5678"}},
        {"master": {**master, "snapshot_date": "2026-05-13"}},
        {"master": {"sector_33": sector}},
    ]:
        unknown = cash_flow_limit(data, ticker="1234", as_of=date(2026, 5, 12))
        assert unknown.reason == "business_model_unverified"
        assert unknown.sector is None


def test_new_and_legacy_readers_preserve_raw_and_keep_zero_separate(tmp_path):
    path = tmp_path / "runs"
    seed_generation(path, now=NOW, as_of=AS_OF)
    saved = read_model_inputs("fixed-synthetic", subjects=("1234", "market"), path=path)
    payload = saved.result("company_financials", "1234").payload
    payload["financial"] = {
        "fcf_yield": 0.0,
        "ttm_quality_fcf_yield": "exact",
        "edinet_reported_flows": {"amounts": {"free_cash_flow": 0.0}},
    }
    market = saved.result("market_context", "1234").payload
    market["master"] = {"snapshot_date": AS_OF.isoformat(), "sector_33": "銀行業"}
    original = saved.model_dump_json()
    full = project_generated_inputs(saved)["securities"]["1234"]["company_financials"]["financial"]
    assert full["reported_cash_flow_basis"]["reported_cfo_less_capex_to_market_cap"] == 0
    assert full["reported_cash_flow_basis"]["reported_fcf_period_quality"] == "exact"
    assert full["reported_cash_flow_basis"]["reason"] == "bank_funding_flows"
    assert "fcf_yield" not in full
    assert "CET1" in full["reported_cash_flow_basis"]["interpretation"]
    assert full["edinet_reported_flows"] == payload["financial"]["edinet_reported_flows"]
    proposal = project_generated_inputs(saved, proposal_view=True)["securities"]["1234"]
    assert "company_financials" not in proposal
    assert proposal["reported_cash_flow_basis"]["reason"] == "bank_funding_flows"
    assert saved.model_dump_json() == original
    market["master"]["sector_33"] = "化学"
    assert (
        project_generated_inputs(saved)["securities"]["1234"]["company_financials"]["financial"]
        == payload["financial"]
    )
    legacy = selection(now=NOW, tickers=("1234",), runs=tmp_path / "old-runs").model_dump(
        mode="json"
    )
    c = legacy["inputs"]["securities_context"]["1234"]
    c["market_context"] = {
        "master": {"snapshot_date": legacy["research_as_of"], "sector_33": "銀行業"}
    }
    ana = legacy["inputs"]["analysis"]["analyses"]["1234"]
    ana.setdefault("metrics", {}).update({"fcf_ttm": 0, "fcf_yield": 0, "ocf_yield": None})
    ana.setdefault("ttm_quality", {})["fcf_yield"] = "exact"
    parsed = parse_selection(legacy)
    raw_hash = selection_sha256(parsed)
    cf = project_research_inputs(parsed)["securities"]["1234"]["cash_flow"]
    assert cf["edinet"]["fcf_ttm"] == 0
    assert cf["edinet"]["reported_cash_flow_basis"]["reported_cfo_less_capex_to_market_cap"] == 0
    assert cf["jquants"]["reported_cash_flow_basis"]["reported_cfo_to_market_cap"] is None
    assert selection_sha256(parsed) == raw_hash
    assert parsed.model_dump(mode="json") == legacy
    c["market_context"]["ticker"] = "5678"
    context = project_research_inputs(parse_selection(legacy))["securities"]["1234"]
    assert context["cash_flow"]["edinet"]["reported_cash_flow_basis"]["reason"] == (
        "business_model_unverified"
    )
