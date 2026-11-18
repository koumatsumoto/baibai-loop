"""同じ事業姿勢・算術状態でも条件価格の有無は元の査読判断に従う。"""

from datetime import timedelta

import pytest
from tests.engine.investment_judgment.stock_assessment.test_price_value import (
    _claim,
    _deadline,
    _save,
)
from tests.helpers.assessment_v1 import APPENDIX_ASSESSMENT_PUBLICATION, NOW
from tests.helpers.db_seed import seed_historical_assessment

from baibai_engine.investment_judgment.company_research.query import research_catalog


def _at(path, instant=NOW):
    return research_catalog(path, as_of=instant.date(), known_at=instant)["items"]


@pytest.mark.parametrize(
    ("status", "has_condition", "ceiling"),
    [
        ("undetermined", False, None),
        ("supported_at_quote", False, None),
        ("not_supported_at_quote", False, None),
        ("not_supported_at_quote", True, None),
        ("not_supported_at_quote", True, "75.00001"),
    ],
)
def test_catalog_distinguishes_authored_cases_at_the_same_old_quote(
    tmp_path, status, has_condition, ceiling
):
    path = tmp_path / "app"
    doc, review = _claim(status=status)
    facts = doc["input_snapshot"]["facts"]
    facts.insert(0, {**facts[0], "fact_id": "unused_quote", "value": 10000})
    case = doc["price_value_case"]
    if has_condition:
        case["price_only_condition"] = {
            "explanation": "権利と費用が同じ時だけ。",
            "ceiling_yen": ceiling,
        }
        _deadline(
            doc,
            "2026-10-14" if ceiling else None,
            fact_kind="last_eligible_trade_date" if ceiling else "other",
        )
    pair = _save(path, doc, review)
    before = path.read_bytes()
    assert _at(path, NOW - timedelta(microseconds=1)) == []
    shown = _at(path)[0]["assessment"]
    assert shown["reference"]["core_sha256"] == pair.core_sha256
    assert shown["stance"] == "supported"
    assert shown["valuation_context"]["status"] == "resolved"
    result = shown["price_value_case"]
    assert result["status"] == status
    assert result["observed_quote"] == {
        "fact_id": "close",
        "price_yen": 100,
        "as_of": "2026-10-14",
        "observed_at": None,
        "price_basis": "last_close_unadjusted",
        "source_ids": ["price"],
    }
    assert result["price_only_condition"] == case.get("price_only_condition")
    assert result["acquisition_limit"] == case.get("acquisition_limit")
    assert not {"action", "quantity", "current_price", "price_reached"} & result.keys()
    assert path.read_bytes() == before


def test_old_or_explicitly_undetermined_without_a_selected_price(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    seed_historical_assessment(old, APPENDIX_ASSESSMENT_PUBLICATION, NOW)
    assert "price_value_case" not in _at(old)[0]["assessment"]
    doc, review = _claim(status="undetermined")
    doc["valuation"].update(
        status="unresolved",
        market_price_fact_id=None,
        horizon_months=None,
        required_annual_return_pct=None,
        base=None,
        downside=None,
        unresolved_reason="普通株の権利未照合。",
    )
    doc["conclusion"]["stance"] = "uncertain"
    review["recalculated_projections"] = []
    _save(new, doc, review)
    assert any(f["fact_kind"] == "market_price" for f in doc["input_snapshot"]["facts"])
    result = _at(new)[0]["assessment"]["price_value_case"]
    assert result["status"] == "undetermined"
    assert result["observed_quote"] is None


def test_revisit_summarizes_general_case_but_keeps_price_reversal_qualifications(tmp_path):
    path = tmp_path / "app"
    doc, review = _claim(status="not_supported_at_quote", ceiling="75.0123")
    facts, case = doc["input_snapshot"]["facts"], doc["price_value_case"]
    for i in range(3):
        facts.append({**facts[-1], "fact_id": f"economic{i}"})
        case["value_fact_ids"].append(f"economic{i}")
    general = "原典の経営条件と必要投資。" * 50
    case["price_compatible_conditions"] = general
    case["price_only_condition"]["explanation"] = (
        "新規取得者に権利が残るときだけ。" * 35 + "最終取得日は未確認。"
    )
    _deadline(doc, None, fact_kind="other")
    _save(path, doc, review)
    shown = _at(path)[0]["assessment"]["price_value_case"]
    assert shown["price_compatible_conditions"] == general[:399] + "…"
    assert shown["price_compatible_conditions_truncated"] is True
    assert shown["value_fact_ids"] == case["value_fact_ids"][:3]
    assert shown["value_fact_ids_omitted"] == 2
    assert shown["price_only_condition"] == case["price_only_condition"]
    assert shown["acquisition_limit"] == case["acquisition_limit"]
