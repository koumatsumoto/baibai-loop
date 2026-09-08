"""原Thesisの診断が公開可否・原本・現在価格の判断を変えないことを確認する。"""

import socket
import sqlite3
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext

import pytest
import yaml
from tests.helpers.research_v4 import pair_payload

from baibai_engine.research import thesis as thesis_module
from baibai_engine.research import thesis_evaluation_cli as cli
from baibai_engine.research.thesis import (
    ThesisDocument,
    ThesisReview,
    UnpublishedThesis,
    evaluate_thesis,
    evaluation_to_payload,
    thesis_core_hash,
    thesis_valuation_context,
)

NOW = datetime.fromisoformat("2026-09-07T18:00:00+09:00")


@pytest.fixture(autouse=True)
def forbid_database_and_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("evaluate must not connect to a database or network")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)


def diagnostic_pair():
    thesis, review = pair_payload()
    thesis["valuation"]["required_annual_return_pct"] = 8.5
    for name, terminal, cash in (("base", 1270, 30), ("downside", 680, 20)):
        thesis["valuation"][name].update(
            terminal_value_per_share_yen=terminal, cash_distribution_per_share_yen=cash
        )
        next(item for item in review["recalculated_projections"] if item["name"] == name).update(
            terminal_value_per_share_yen=terminal, cash_distribution_per_share_yen=cash
        )
    review["reviewed_thesis_sha256"] = thesis_core_hash(ThesisDocument.model_validate(thesis))
    return thesis, review


def run_cli(tmp_path, capsys, thesis, review, *, now=NOW):
    draft = tmp_path / "thesis.yaml"
    review_path = tmp_path / "review.yaml"
    draft.write_text(yaml.safe_dump(thesis))
    if review is not None:
        review_path.write_text(yaml.safe_dump(review))
    paths = list(tmp_path.rglob("*"))
    before = {path: path.read_bytes() for path in paths if path.is_file()}
    exit_code = cli.main([str(draft), "--review", str(review_path)], now=now)
    output = capsys.readouterr()
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    payload = yaml.safe_load(output.out)
    document = ThesisDocument.model_validate(thesis)
    expected = evaluation_to_payload(
        evaluate_thesis(
            document,
            review=ThesisReview.model_validate(review) if review is not None else None,
            identity=UnpublishedThesis.DRAFT,
            now=now,
        )
    )
    assert {key: value for key, value in payload.items() if key != "valuation_context"} == expected
    assert exit_code == (0 if expected["decision_readiness"] == "ready" else 2)
    return payload, output.err, exit_code


def test_ready_context_matches_recorded_price_and_issue_example(tmp_path, capsys):
    payload, stderr, code = run_cli(tmp_path, capsys, *diagnostic_pair())
    assert code == 0
    assert stderr == ""
    assert payload["valuation_context"] == {
        "price_context": "thesis_snapshot",
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
    thesis, _ = diagnostic_pair()
    payload, stderr, code = run_cli(tmp_path, capsys, thesis, None)
    assert code == 2
    assert stderr == ""
    assert payload["thesis_status"] == "review_required"
    assert payload["valuation_context"] is not None
    # An omitted --review and the scaffold's not-yet-created review path are equivalent.
    assert cli.main([str(tmp_path / "thesis.yaml")], now=NOW) == 2
    assert yaml.safe_load(capsys.readouterr().out) == payload


@pytest.mark.parametrize(
    ("fault", "with_review"),
    [(fault, attached) for fault in ("source", "price", "unit") for attached in (True, False)]
    + [("hash", True)],
)
def test_invalid_content_has_no_diagnostic(tmp_path, capsys, fault, with_review):
    thesis, review = diagnostic_pair()
    if fault == "source":
        thesis["valuation"]["base"]["source_ids"] = ["missing"]
    elif fault == "price":
        thesis["valuation"]["market_price_fact_id"] = "missing"
    elif fault == "unit":
        thesis["input_snapshot"]["facts"][0]["unit"] = "JPY"
    if fault != "hash":
        review["reviewed_thesis_sha256"] = thesis_core_hash(ThesisDocument.model_validate(thesis))
    else:
        review["reviewed_thesis_sha256"] = "0" * 64
    payload, stderr, code = run_cli(tmp_path, capsys, thesis, review if with_review else None)
    assert code == 2
    assert stderr == ""
    assert payload["thesis_status"] == "incomplete"
    assert payload["valuation_context"] is None


@pytest.mark.parametrize("disposition", ["defer", "reject"])
def test_unresolved_needs_no_invented_price_or_projections(tmp_path, capsys, disposition):
    thesis, review = diagnostic_pair()
    thesis["input_snapshot"]["facts"] = []
    thesis["judgment"]["disposition"] = disposition
    thesis["valuation"] = dict(
        status="unresolved",
        market_price_fact_id=None,
        horizon_months=None,
        required_annual_return_pct=None,
        base=None,
        downside=None,
        unresolved_reason="返済条件未確認",
    )
    review["recalculated_projections"] = []
    review["reviewed_thesis_sha256"] = thesis_core_hash(ThesisDocument.model_validate(thesis))
    payload, stderr, code = run_cli(tmp_path, capsys, thesis, review)
    assert code == 0
    assert stderr == ""
    assert payload["valuation_context"] is None


def test_warnings_do_not_remove_resolved_context(tmp_path, capsys):
    thesis, review = diagnostic_pair()
    thesis["permanent_loss_risks"][0]["assessment"] = "unknown"
    review["nonmaterial_unknown_reason"] = "架空例: 開示済み資金と全返済予定から当該細目は非重要"
    review["reviewed_thesis_sha256"] = thesis_core_hash(ThesisDocument.model_validate(thesis))
    payload, _, code = run_cli(tmp_path, capsys, thesis, review)
    assert code == 0
    assert payload["thesis_status"] == "ready_with_warnings"
    assert payload["valuation_context"] is not None


def test_old_thesis_keeps_origin_period_and_referenced_fact(tmp_path, capsys):
    thesis, review = pair_payload(as_of="2025-09-07", quote_as_of="2025-09-04")
    thesis["valuation"]["horizon_months"] = 6
    thesis["valuation"]["required_annual_return_pct"] = 21
    other = deepcopy(thesis["input_snapshot"]["facts"][0])
    other.update(fact_id="other-price", value=2000)
    thesis["input_snapshot"]["facts"].insert(0, other)
    thesis["input_snapshot"]["facts"][1]["price_basis"] = "realtime"
    review["reviewed_thesis_sha256"] = thesis_core_hash(ThesisDocument.model_validate(thesis))
    payload, _, _ = run_cli(tmp_path, capsys, thesis, review)
    context = payload["valuation_context"]
    assert context["valuation_as_of"] == "2025-09-07"
    assert context["price_as_of"] == "2025-09-04"
    assert context["price_basis"] == "realtime"
    assert context["price_yen"] == 1000
    assert context["horizon_months"] == 6
    assert context["required_annual_return_pct"] == 21
    assert context["fixed_value_delay"]["horizon_months"] == 18


def test_context_does_not_mutate_document_hash_or_review_binding():
    thesis, review = diagnostic_pair()
    document, review_document = (
        ThesisDocument.model_validate(thesis),
        ThesisReview.model_validate(review),
    )
    before = document.model_dump(mode="json"), review_document.model_dump(mode="json")
    evaluation = evaluate_thesis(
        document, review=review_document, identity=UnpublishedThesis.DRAFT, now=NOW
    )
    assert thesis_valuation_context(document, evaluation) is not None
    assert (document.model_dump(mode="json"), review_document.model_dump(mode="json")) == before
    assert thesis_core_hash(document) == review_document.reviewed_thesis_sha256
    assert (
        evaluate_thesis(document, review=review_document, identity=UnpublishedThesis.DRAFT, now=NOW)
        == evaluation
    )


@pytest.mark.parametrize("with_review", [True, False])
@pytest.mark.parametrize("failure", ["decimal", "float_conversion", "nonfinite"])
def test_diagnostic_numeric_failure_preserves_evaluation(
    tmp_path, capsys, monkeypatch, failure, with_review
):
    thesis, review = diagnostic_pair()
    if failure == "decimal":

        def fail(*args, **kwargs):
            raise InvalidOperation("diagnostic arithmetic")

        monkeypatch.setattr(thesis_module, "valuation_conditions", fail)
    elif failure == "float_conversion":
        # The existing converter can overflow to inf for a finite fractional Decimal.
        with localcontext() as context:
            context.prec = 400
            huge = Decimal("1e310") + Decimal("0.5")
        monkeypatch.setattr(thesis_module, "decimal_to_number", lambda value: float(huge))
    else:
        monkeypatch.setattr(thesis_module, "decimal_to_number", lambda value: float("nan"))
    payload, stderr, code = run_cli(tmp_path, capsys, thesis, review if with_review else None)
    assert payload["valuation_context"] is None
    assert "valuation_context unavailable:" in stderr
    assert code == (0 if with_review else 2)


@pytest.mark.parametrize("months", [1_000_000_000, 2_000_000])
def test_real_diagnostic_overflow_does_not_make_new_gate(tmp_path, capsys, months):
    thesis, review = diagnostic_pair()
    thesis["valuation"]["horizon_months"] = months
    review["reviewed_thesis_sha256"] = thesis_core_hash(ThesisDocument.model_validate(thesis))
    payload, stderr, code = run_cli(tmp_path, capsys, thesis, review)
    assert code == 0
    assert payload["valuation_context"] is None
    assert "valuation_context unavailable:" in stderr


def test_programming_errors_are_not_hidden(tmp_path, monkeypatch):
    thesis, _ = diagnostic_pair()
    draft = tmp_path / "thesis.yaml"
    draft.write_text(yaml.safe_dump(thesis))

    def bug(*args):
        raise TypeError("programming error")

    monkeypatch.setattr(cli, "thesis_valuation_context", bug)
    with pytest.raises(TypeError, match="programming error"):
        cli.main([str(draft)], now=NOW)


@pytest.mark.parametrize("content", ["[", "[]", "schema_version: 99"])
def test_parse_failure_keeps_existing_error(tmp_path, capsys, content):
    draft = tmp_path / "thesis.yaml"
    draft.write_text(content)
    assert cli.main([str(draft)], now=NOW) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("error:")
