"""時点付きの普通株の経済判定。口座の制約や達成hからは推定しない。"""

from contextlib import closing
from copy import deepcopy
from decimal import Decimal

import pytest
from tests.helpers.assessment_v1 import (
    APPENDIX_ASSESSMENT_PUBLICATION,
    NOW,
    assessment_id,
    pair_payload,
    update_review_hash,
)
from tests.helpers.db_seed import seed_historical_assessment

from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    assessment_core_hash,
)
from baibai_engine.investment_judgment.stock_assessment.price_value import (
    reviewed_price_value_case,
)
from baibai_engine.investment_judgment.stock_assessment.refresh import scaffold_assessment_refresh
from baibai_engine.investment_judgment.stock_assessment.store import (
    AssessmentStoreService,
    load_reviewed_assessment,
)
from baibai_engine.storage.sqlite.application.read import connect_read_only


def _claim(*, status="supported_at_quote", ceiling=None):
    raw, review = pair_payload(price_case=status)
    raw["input_snapshot"]["facts"][0].update(value=100, observed_at=None)
    if status != "undetermined":
        raw["price_value_case"]["price_compatible_conditions"] = (
            "新規取得者への清算分配が遅延・減額すれば現値100円と両立する。"
        )
        raw["investment_case"]["explanation"] = (
            "新規に取得する普通株一株への分配。決議と源泉費用を原典で照合した。"
        )
        raw["investment_case"]["invalidation_conditions"] = [
            "権利確定を過ぎている、又は優先債務が増えたなら失効。"
        ]
    if ceiling is not None:
        raw["price_value_case"]["price_only_condition"] = {
            "explanation": "同じ受取権・費用・期間なら75円以下で初めて余裕が生じる。",
            "ceiling_yen": ceiling,
        }
    return raw, review


def _save(path, raw, review):
    update_review_hash(raw, review)
    return AssessmentStoreService(path, clock=lambda: NOW).publish_reviewed_assessment(
        assessment_id(),
        raw,
        review,
    )


@pytest.mark.parametrize("status", ["supported_at_quote", "not_supported_at_quote", "undetermined"])
def test_exact_ordinary_claim_is_independent_of_account_and_need_not_have_earnings_dcf(
    tmp_path, status
):
    raw, review = _claim(status=status)
    raw["valuation"].update(
        status="unresolved",
        horizon_months=None,
        required_annual_return_pct=None,
        base=None,
        downside=None,
        unresolved_reason="継続事業の利益・成長DCFは採用せず。",
    )
    raw["conclusion"]["stance"] = "uncertain"
    review["recalculated_projections"] = []
    path = tmp_path / "app"
    pair = _save(path, raw, review)
    with closing(connect_read_only(path)) as conn:
        reread = load_reviewed_assessment(conn, pair.assessment_id)
    output = reviewed_price_value_case(reread)
    assert output == reviewed_price_value_case(pair)
    assert output["status"] == status
    assert output["observed_quote"] == {
        "fact_id": "close",
        "price_yen": 100,
        "as_of": "2026-10-14",
        "observed_at": None,
        "price_basis": "last_close_unadjusted",
        "source_ids": ["price"],
    }
    assert output["invalidation_conditions"] == raw["investment_case"]["invalidation_conditions"]
    assert output["price_only_condition"] is None
    assert not {"quantity", "action", "maximum_entry_price_yen"} & output.keys()
    fresh = scaffold_assessment_refresh(
        pair, as_of=pair.document.input_snapshot.as_of, assessed_at=NOW
    )
    assert fresh["price_value_case"]["status"] is None


@pytest.mark.parametrize("ceiling", [None, "75.000001"])
def test_stated_reversal_retains_exact_ceiling_or_just_prose(tmp_path, ceiling):
    raw, review = _claim(status="not_supported_at_quote", ceiling="75")
    raw["price_value_case"]["price_only_condition"]["ceiling_yen"] = ceiling
    # One official document can report the price and a separate ordinary-share right.
    raw["input_snapshot"]["facts"][0]["source_ids"] = ["annual"]
    pair = _save(tmp_path / "app", raw, review)
    condition = pair.document.price_value_case.price_only_condition
    assert condition.ceiling_yen == (None if ceiling is None else Decimal(ceiling))
    assert reviewed_price_value_case(pair)["price_only_condition"] == condition.model_dump(
        mode="json"
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("price_only", "independent economic fact"),
        ("no_source", "primary common-share"),
        ("unknown", "independent economic fact"),
        ("multiple", "unique"),
        ("no_quote", "observed"),
        ("adjusted", "unadjusted"),
        ("wrong_unit", "unadjusted"),
        ("equal_ceiling", "below"),
        ("higher_ceiling", "below"),
        ("negative", "greater than"),
        ("bool", "finite decimal"),
        ("nan", "finite decimal"),
        ("supported_condition", "requires not_supported"),
        ("condition_on_unknown", "requires not_supported"),
        ("review_unclear", "primary-source check"),
    ],
)
def test_new_case_refuses_false_evidence_or_unfounded_condition(tmp_path, change, message):
    raw, review = _claim(status="not_supported_at_quote", ceiling=75)
    case = raw["price_value_case"]
    if change in {"price_only", "unknown", "multiple"}:
        case["value_fact_ids"] = {
            "price_only": ["close"],
            "unknown": ["missing"],
            "multiple": ["owner_receipt"] * 2,
        }[change]
    elif change == "no_source":
        raw["input_snapshot"]["facts"][1]["source_ids"] = ["price"]
    elif change == "no_quote":
        raw["valuation"].update(
            market_price_fact_id=None,
            status="unresolved",
            horizon_months=None,
            required_annual_return_pct=None,
            base=None,
            downside=None,
            unresolved_reason="未取得。",
        )
        raw["conclusion"]["stance"] = "uncertain"
        review["recalculated_projections"] = []
    elif change in {"equal_ceiling", "higher_ceiling", "negative", "bool", "nan"}:
        case["price_only_condition"]["ceiling_yen"] = {
            "equal_ceiling": 100,
            "higher_ceiling": 101,
            "negative": -1,
            "bool": True,
            "nan": "NaN",
        }[change]
    elif change in {"supported_condition", "condition_on_unknown"}:
        case["status"] = "supported_at_quote" if change == "supported_condition" else "undetermined"
    elif change == "review_unclear":
        raw["conclusion"]["stance"] = "uncertain"
        review["primary_source_check"] = "unverified"
    else:
        field, value = (
            ("price_basis", "last_close_adjusted") if change == "adjusted" else ("unit", "JPY")
        )
        raw["input_snapshot"]["facts"][0][field] = value
    with pytest.raises(ValueError, match=message):
        _save(tmp_path / "app", raw, review)


def test_absence_is_an_archived_fact_not_undetermined_or_a_new_publication(tmp_path):
    saved = deepcopy(APPENDIX_ASSESSMENT_PUBLICATION)
    doc = saved["document"]
    assert (
        assessment_core_hash(StockAssessmentDocument.model_validate(doc))
        == saved["review"]["reviewed_assessment_sha256"]
    )
    writer = AssessmentStoreService(tmp_path / "app", clock=lambda: NOW)
    with pytest.raises(ValueError, match="explicit price_value_case"):
        writer.publish_reviewed_assessment(saved["assessment_id"], doc, saved["review"])
    seed_historical_assessment(tmp_path / "app", saved, NOW)
    retried = writer.publish_reviewed_assessment(saved["assessment_id"], doc, saved["review"])
    assert reviewed_price_value_case(retried) is None
    assert "price_value_case" not in retried.document.model_dump(mode="json")
    raw, review = _claim(status="undetermined")
    raw["valuation"].update(
        status="unresolved",
        market_price_fact_id=None,
        horizon_months=None,
        required_annual_return_pct=None,
        base=None,
        downside=None,
        unresolved_reason="現物価格も入金権もまだ未確認。",
    )
    raw["conclusion"]["stance"] = "uncertain"
    review["recalculated_projections"] = []
    output = reviewed_price_value_case(_save(tmp_path / "new", raw, review))
    assert output["status"] == "undetermined"
    assert output["observed_quote"] is None
    assert output["price_compatible_conditions"] is None


def _deadline(raw, day, *, reported=None, fact_kind="last_eligible_trade_date"):
    fact = {
        "fact_id": "right_cutoff",
        "fact_kind": fact_kind,
        "unit": "date" if day else "text",
        "value": reported or day or "取得期限はあるが取引所未発表。",
        "as_of": raw["input_snapshot"]["as_of"],
        "source_ids": ["annual"],
    }
    raw["input_snapshot"]["facts"].append(fact)
    case = raw["price_value_case"]
    case["value_fact_ids"].append(fact["fact_id"])
    case["acquisition_limit"] = {"value_fact_id": fact["fact_id"], "last_eligible_trade_date": day}


@pytest.mark.parametrize("day", ["2026-10-14", None])
def test_one_off_entitlement_retains_last_trade_or_explicit_unknown(tmp_path, day):
    raw, review = _claim()
    _deadline(raw, day, fact_kind="last_eligible_trade_date" if day else "other")
    pair = _save(tmp_path / "app", raw, review)
    assert reviewed_price_value_case(pair)["acquisition_limit"] == {
        "value_fact_id": "right_cutoff",
        "last_eligible_trade_date": day,
    }


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("expired", "expired acquisition"),
        ("expired_condition", "expired acquisition"),
        ("cash_payment_day", "typed source fact"),
        ("fact_differs", "typed source fact"),
        ("unsupported_date", "primary source"),
        ("date_only", "primary common-share"),
    ],
)
def test_right_deadline_cannot_be_invented_or_extended(tmp_path, fault, message):
    raw, review = _claim(
        status="not_supported_at_quote" if fault == "expired_condition" else "supported_at_quote",
        ceiling=75 if fault == "expired_condition" else None,
    )
    _deadline(
        raw,
        "2026-10-13" if fault.startswith("expired") else "2026-10-14",
        reported="2026-11-10" if fault == "fact_differs" else None,
        fact_kind="other" if fault == "cash_payment_day" else "last_eligible_trade_date",
    )
    if fault == "unsupported_date":
        raw["input_snapshot"]["facts"][-1]["source_ids"] = ["price"]
    if fault == "date_only":
        raw["price_value_case"]["value_fact_ids"] = ["right_cutoff"]
    with pytest.raises(ValueError, match=message):
        _save(tmp_path / "app", raw, review)


def test_a_known_time_limit_in_the_cited_thesis_cannot_be_silently_dropped(tmp_path):
    raw, review = _claim()
    _deadline(raw, "2026-10-14")
    raw["price_value_case"].pop("acquisition_limit")
    with pytest.raises(ValueError, match="must bind"):
        _save(tmp_path / "app", raw, review)
