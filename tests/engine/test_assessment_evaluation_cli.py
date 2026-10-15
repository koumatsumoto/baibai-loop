"""原Assessmentの診断が公開可否・原本・現在価格の判断を変えないことを確認する。"""

import socket
import sqlite3
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext

import pytest
import yaml
from tests.helpers.assessment_v1 import pair_payload

from baibai_engine.research import assessment as assessment_module
from baibai_engine.research import assessment_evaluation_cli as cli
from baibai_engine.research.assessment import (
    AssessmentReview,
    StockAssessmentDocument,
    UnpublishedAssessment,
    assessment_core_hash,
    assessment_valuation_context,
    evaluate_assessment,
    evaluation_to_payload,
)

NOW = datetime.fromisoformat("2026-09-07T18:00:00+09:00")


@pytest.fixture(autouse=True)
def forbid_database_and_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("evaluate must not connect to a database or network")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)


def diagnostic_pair():
    assessment, review = pair_payload(as_of="2026-09-07", quote_as_of="2026-09-04")
    assessment["input_snapshot"]["facts"][0]["value"] = 1000
    assessment["valuation"]["required_annual_return_pct"] = 8.5
    for name, terminal, cash in (("base", 1270, 30), ("downside", 680, 20)):
        assessment["valuation"][name].update(
            terminal_value_per_share_yen=terminal, cash_distribution_per_share_yen=cash
        )
        next(item for item in review["recalculated_projections"] if item["name"] == name).update(
            terminal_value_per_share_yen=terminal, cash_distribution_per_share_yen=cash
        )
    review["reviewed_assessment_sha256"] = assessment_core_hash(
        StockAssessmentDocument.model_validate(assessment)
    )
    return assessment, review


def run_cli(tmp_path, capsys, assessment, review, *, now=NOW):
    draft = tmp_path / "assessment.yaml"
    review_path = tmp_path / "review.yaml"
    draft.write_text(yaml.safe_dump(assessment))
    if review is not None:
        review_path.write_text(yaml.safe_dump(review))
    paths = list(tmp_path.rglob("*"))
    before = {path: path.read_bytes() for path in paths if path.is_file()}
    exit_code = cli.main([str(draft), "--review", str(review_path)], now=now)
    output = capsys.readouterr()
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    payload = yaml.safe_load(output.out)
    document = StockAssessmentDocument.model_validate(assessment)
    expected = evaluation_to_payload(
        evaluate_assessment(
            document,
            review=AssessmentReview.model_validate(review) if review is not None else None,
            identity=UnpublishedAssessment.DRAFT,
            now=now,
        )
    )
    assert {key: value for key, value in payload.items() if key != "valuation_context"} == expected
    assert exit_code == (0 if expected["assessment_readiness"] == "ready" else 2)
    return payload, output.err, exit_code


def test_ready_context_matches_recorded_price_and_issue_example(tmp_path, capsys):
    payload, stderr, code = run_cli(tmp_path, capsys, *diagnostic_pair())
    assert code == 0
    assert stderr == ""
    assert payload["valuation_context"] == {
        "price_context": "assessment_snapshot",
        "valuation_as_of": "2026-09-07",
        "price_as_of": "2026-09-04",
        "price_basis": "last_close_unadjusted",
        "price_yen": 1000,
        "horizon_months": 12,
        "required_annual_return_pct": 8.5,
        "return_basis": "conditional_pretax_without_reinvestment",
        "required_total_value_yen": 1085,
        "base": {
            "terminal_value_per_share_yen": 1270,
            "cash_distribution_per_share_yen": 30,
            "total_value_yen": 1300,
            "total_return_pct": 30,
            "annualized_return_pct": 30,
            "required_terminal_value_per_share_yen": 1055,
            "total_value_surplus_yen": 215,
        },
        "downside": {
            "terminal_value_per_share_yen": 680,
            "cash_distribution_per_share_yen": 20,
            "total_value_yen": 700,
            "total_return_pct": -30,
            "annualized_return_pct": -30,
            "required_terminal_value_per_share_yen": 1065,
            "total_value_surplus_yen": -385,
        },
        "fixed_value_delay": {
            "additional_months": 12,
            "horizon_months": 24,
            "assumption": "terminal_and_cumulative_cash_unchanged",
            "base_annualized_return_pct": 14.0175,
            "downside_annualized_return_pct": -16.334,
        },
    }


def test_review_missing_still_not_ready_even_with_context(tmp_path, capsys):
    assessment, _ = diagnostic_pair()
    payload, stderr, code = run_cli(tmp_path, capsys, assessment, None)
    assert code == 2
    assert stderr == ""
    assert payload["assessment_status"] == "review_required"
    assert payload["valuation_context"] is not None
    # An omitted --review and the scaffold's not-yet-created review path are equivalent.
    assert cli.main([str(tmp_path / "assessment.yaml")], now=NOW) == 2
    assert yaml.safe_load(capsys.readouterr().out) == payload


@pytest.mark.parametrize(
    ("fault", "with_review"),
    [(fault, attached) for fault in ("source", "price", "unit") for attached in (True, False)]
    + [("hash", True)],
)
def test_invalid_content_has_no_diagnostic(tmp_path, capsys, fault, with_review):
    assessment, review = diagnostic_pair()
    if fault == "source":
        assessment["valuation"]["base"]["source_ids"] = ["missing"]
    elif fault == "price":
        assessment["valuation"]["market_price_fact_id"] = "missing"
    elif fault == "unit":
        assessment["input_snapshot"]["facts"][0]["unit"] = "JPY"
    if fault != "hash":
        review["reviewed_assessment_sha256"] = assessment_core_hash(
            StockAssessmentDocument.model_validate(assessment)
        )
    else:
        review["reviewed_assessment_sha256"] = "0" * 64
    payload, stderr, code = run_cli(tmp_path, capsys, assessment, review if with_review else None)
    assert code == 2
    assert stderr == ""
    assert payload["assessment_status"] == "incomplete"
    assert payload["valuation_context"] is None


@pytest.mark.parametrize("stance", ["uncertain", "unsupported"])
def test_unresolved_needs_no_invented_price_or_projections(tmp_path, capsys, stance):
    assessment, review = diagnostic_pair()
    assessment["input_snapshot"]["facts"] = []
    assessment["conclusion"]["stance"] = stance
    assessment["valuation"] = dict(
        status="unresolved",
        market_price_fact_id=None,
        horizon_months=None,
        required_annual_return_pct=None,
        base=None,
        downside=None,
        unresolved_reason="返済条件未確認",
    )
    review["recalculated_projections"] = []
    review["reviewed_assessment_sha256"] = assessment_core_hash(
        StockAssessmentDocument.model_validate(assessment)
    )
    payload, stderr, code = run_cli(tmp_path, capsys, assessment, review)
    assert code == 0
    assert stderr == ""
    assert payload["valuation_context"] is None


def test_warnings_do_not_remove_resolved_context(tmp_path, capsys):
    assessment, review = diagnostic_pair()
    assessment["permanent_loss_risks"][0]["assessment"] = "unknown"
    review["nonmaterial_unknown_reason"] = "架空例: 開示済み資金と全返済予定から当該細目は非重要"
    review["reviewed_assessment_sha256"] = assessment_core_hash(
        StockAssessmentDocument.model_validate(assessment)
    )
    payload, _, code = run_cli(tmp_path, capsys, assessment, review)
    assert code == 0
    assert payload["assessment_status"] == "ready_with_warnings"
    assert payload["valuation_context"] is not None


def test_old_assessment_keeps_origin_period_and_referenced_fact(tmp_path, capsys):
    assessment, review = pair_payload(as_of="2025-09-07", quote_as_of="2025-09-04")
    assessment["input_snapshot"]["facts"][0]["value"] = 1000
    assessment["valuation"]["horizon_months"] = 6
    assessment["valuation"]["required_annual_return_pct"] = 21
    other = deepcopy(assessment["input_snapshot"]["facts"][0])
    other.update(fact_id="other-price", value=2000)
    assessment["input_snapshot"]["facts"].insert(0, other)
    assessment["input_snapshot"]["facts"][1]["price_basis"] = "realtime"
    review["reviewed_assessment_sha256"] = assessment_core_hash(
        StockAssessmentDocument.model_validate(assessment)
    )
    payload, _, _ = run_cli(tmp_path, capsys, assessment, review)
    context = payload["valuation_context"]
    assert context["valuation_as_of"] == "2025-09-07"
    assert context["price_as_of"] == "2025-09-04"
    assert context["price_basis"] == "realtime"
    assert context["price_yen"] == 1000
    assert context["horizon_months"] == 6
    assert context["required_annual_return_pct"] == 21
    assert context["fixed_value_delay"]["horizon_months"] == 18


def test_context_does_not_mutate_document_hash_or_review_binding():
    assessment, review = diagnostic_pair()
    document, review_document = (
        StockAssessmentDocument.model_validate(assessment),
        AssessmentReview.model_validate(review),
    )
    before = document.model_dump(mode="json"), review_document.model_dump(mode="json")
    evaluation = evaluate_assessment(
        document, review=review_document, identity=UnpublishedAssessment.DRAFT, now=NOW
    )
    assert assessment_valuation_context(document, evaluation) is not None
    assert (document.model_dump(mode="json"), review_document.model_dump(mode="json")) == before
    assert assessment_core_hash(document) == review_document.reviewed_assessment_sha256
    assert (
        evaluate_assessment(
            document, review=review_document, identity=UnpublishedAssessment.DRAFT, now=NOW
        )
        == evaluation
    )


@pytest.mark.parametrize("with_review", [True, False])
@pytest.mark.parametrize("failure", ["decimal", "float_conversion", "nonfinite"])
def test_diagnostic_numeric_failure_preserves_evaluation(
    tmp_path, capsys, monkeypatch, failure, with_review
):
    assessment, review = diagnostic_pair()
    if failure == "decimal":

        def fail(*args, **kwargs):
            raise InvalidOperation("diagnostic arithmetic")

        monkeypatch.setattr(assessment_module, "valuation_conditions", fail)
    elif failure == "float_conversion":
        # The existing converter can overflow to inf for a finite fractional Decimal.
        with localcontext() as context:
            context.prec = 400
            huge = Decimal("1e310") + Decimal("0.5")
        monkeypatch.setattr(assessment_module, "decimal_to_number", lambda value: float(huge))
    else:
        monkeypatch.setattr(assessment_module, "decimal_to_number", lambda value: float("nan"))
    payload, stderr, code = run_cli(tmp_path, capsys, assessment, review if with_review else None)
    assert payload["valuation_context"] is None
    assert "valuation_context unavailable:" in stderr
    assert code == (0 if with_review else 2)


@pytest.mark.parametrize("months", [1_000_000_000, 2_000_000])
def test_real_diagnostic_overflow_does_not_make_new_gate(tmp_path, capsys, months):
    assessment, review = diagnostic_pair()
    assessment["valuation"]["horizon_months"] = months
    review["reviewed_assessment_sha256"] = assessment_core_hash(
        StockAssessmentDocument.model_validate(assessment)
    )
    payload, stderr, code = run_cli(tmp_path, capsys, assessment, review)
    assert code == 0
    assert payload["valuation_context"] is None
    assert "valuation_context unavailable:" in stderr


def test_programming_errors_are_not_hidden(tmp_path, monkeypatch):
    assessment, _ = diagnostic_pair()
    draft = tmp_path / "assessment.yaml"
    draft.write_text(yaml.safe_dump(assessment))

    def bug(*args):
        raise TypeError("programming error")

    monkeypatch.setattr(cli, "assessment_valuation_context", bug)
    with pytest.raises(TypeError, match="programming error"):
        cli.main([str(draft)], now=NOW)


@pytest.mark.parametrize("content", ["[", "[]", "schema_version: 99"])
def test_parse_failure_keeps_existing_error(tmp_path, capsys, content):
    draft = tmp_path / "assessment.yaml"
    draft.write_text(content)
    assert cli.main([str(draft)], now=NOW) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("error:")
