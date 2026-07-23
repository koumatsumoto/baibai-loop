from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from baibai_engine.position.cli import main
from baibai_engine.position.holding_review import (
    HoldingReviewDocument,
    evaluate_holding_review,
)
from baibai_engine.position.ledger import load_portfolio_ledger
from baibai_engine.research.store import ResearchStoreService
from baibai_engine.research.thesis import (
    IndependentReview,
    ThesisDocument,
    independent_review_hash,
    thesis_core_hash,
)
from tests.helpers.db_seed import seed_ledger

FIXTURES = Path(__file__).parent / "fixtures" / "holding-review"


def _load(name: str) -> HoldingReviewDocument:
    raw = yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))
    return HoldingReviewDocument.model_validate(raw)


def _raw(name: str) -> dict[str, Any]:
    return yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))


def test_file_source_binding_is_rejected() -> None:
    raw = _raw("underwater-hold.yaml")
    raw["sources"]["ledger"] = {"ref": "records/ledger.yaml", "sha256": "0" * 64}
    with pytest.raises(ValueError, match="entity_id"):
        HoldingReviewDocument.model_validate(raw)


def test_broken_thesis_is_priority_exit() -> None:
    result = evaluate_holding_review(_load("thesis-break.yaml"))
    assert result.computed_action == "exit"
    assert result.permanent_loss_conclusion == "elevated"
    assert result.errors == ()


def test_fair_value_with_weaker_candidate_holds() -> None:
    document = _load("fair-value-hold.yaml")
    result = evaluate_holding_review(document)
    assert document.valuation_review.review_trigger is True
    assert result.computed_action == "hold"
    # A candidate exists but loses after exit tax, so holding wins.
    assert result.replacement_edge_yen is not None
    assert result.replacement_edge_yen < 0
    assert result.errors == ()


def test_underwater_intact_thesis_holds() -> None:
    result = evaluate_holding_review(_load("underwater-hold.yaml"))
    assert result.computed_action == "hold"
    assert result.permanent_loss_conclusion == "acceptable"
    assert result.errors == ()


def test_after_tax_superior_candidate_reduces() -> None:
    result = evaluate_holding_review(_load("replacement-superior.yaml"))
    assert result.computed_action == "reduce"
    assert result.replacement_edge_yen is not None
    assert result.replacement_edge_yen > 0
    assert result.tax_basis == "estimated"
    assert result.errors == ()


def test_confirmed_exit_tax_is_used_as_a_cash_flow() -> None:
    raw = _raw("replacement-superior.yaml")
    raw["replacement_comparison"]["exit_tax"] = {
        "tax_basis": "confirmed",
        "tax_yen": 20_000,
    }
    result = evaluate_holding_review(HoldingReviewDocument.model_validate(raw))
    assert result.tax_basis == "confirmed"
    assert result.replacement_edge_yen is not None
    assert result.replacement_edge_yen > 0
    assert result.computed_action == "reduce"


def test_unknown_tax_holds_and_shows_sensitivity() -> None:
    result = evaluate_holding_review(_load("tax-unknown-hold.yaml"))
    assert result.computed_action == "hold"
    # Unknown tax must not assert a switch: edge is None, sensitivity is shown.
    assert result.replacement_edge_yen is None
    assert result.tax_basis == "unknown"
    assert result.breakeven_exit_tax_rate_bps is not None
    assert result.replacement_edge_at_zero_tax_yen is not None
    assert result.replacement_edge_at_zero_tax_yen > 0
    assert any("sensitivity" in warning for warning in result.warnings)
    assert result.errors == ()


def test_missing_permanent_loss_axis_is_incomplete() -> None:
    raw = _raw("underwater-hold.yaml")
    # Drop one axis then pad to keep the 7-item shape valid at schema level, so
    # the *coverage* (not the item count) is what fails.
    axes = raw["thesis_health"]["permanent_loss_axes"]
    axes[6] = copy.deepcopy(axes[0])  # duplicate funding_liquidity, drop governance
    document = HoldingReviewDocument.model_validate(raw)
    result = evaluate_holding_review(document)
    assert result.review_status == "incomplete"
    assert any("canonical axes" in message for message in result.errors)


def test_thesis_unavailable_cannot_produce_a_replacement_verdict() -> None:
    raw = _raw("replacement-superior.yaml")
    raw["thesis_health"]["current_5y_estimate"] = {"status": "unresolved"}
    with pytest.raises(ValueError, match="requires a resolved current_5y_estimate"):
        HoldingReviewDocument.model_validate(raw)


def test_thesis_unavailable_cannot_produce_an_add() -> None:
    raw = _raw("underwater-hold.yaml")
    raw["thesis_health"]["current_5y_estimate"] = {"status": "unresolved"}
    raw["add_context"] = {
        "current_price_yen": 800,
        "max_acceptable_price_yen": 1000,
        "available_cash_yen": 100_000,
        "concentration_ok": True,
    }
    raw["action"] = "add"
    with pytest.raises(ValueError, match="add_context requires a resolved"):
        HoldingReviewDocument.model_validate(raw)


def test_unknown_permanent_loss_conclusion_cannot_add() -> None:
    raw = _raw("underwater-hold.yaml")
    raw["thesis_health"]["permanent_loss_axes"][0].update(
        assessment="unknown", evidence_status="unverified"
    )
    raw["add_context"] = {
        "current_price_yen": 800,
        "max_acceptable_price_yen": 1000,
        "available_cash_yen": 100_000,
        "concentration_ok": True,
    }
    result = evaluate_holding_review(HoldingReviewDocument.model_validate(raw))
    assert result.permanent_loss_conclusion == "unknown"
    assert result.computed_action == "hold"


def test_add_context_uses_the_valuation_review_price() -> None:
    raw = _raw("underwater-hold.yaml")
    raw["add_context"] = {
        "current_price_yen": 799,
        "max_acceptable_price_yen": 1000,
        "available_cash_yen": 100_000,
        "concentration_ok": True,
    }
    raw["action"] = "add"
    with pytest.raises(ValueError, match="must match valuation_review"):
        HoldingReviewDocument.model_validate(raw)


def test_fair_value_trigger_is_true_just_above_fair_value() -> None:
    raw = _raw("fair-value-hold.yaml")
    raw["valuation_review"]["current_price_yen"] = 1001
    result = evaluate_holding_review(HoldingReviewDocument.model_validate(raw))
    assert result.computed_action == "hold"
    assert result.errors == ()


def test_at_risk_thesis_reduces_without_a_replacement_candidate() -> None:
    raw = _raw("underwater-hold.yaml")
    raw["thesis_health"]["invalidation_status"] = "at_risk"
    raw["action"] = "reduce"
    result = evaluate_holding_review(HoldingReviewDocument.model_validate(raw))
    assert result.computed_action == "reduce"
    assert result.errors == ()


def test_concentration_excess_reduces_without_a_replacement_candidate() -> None:
    raw = _raw("underwater-hold.yaml")
    raw["reduce_context"] = {"concentration_exceeded": True}
    raw["action"] = "reduce"
    result = evaluate_holding_review(HoldingReviewDocument.model_validate(raw))
    assert result.computed_action == "reduce"
    assert result.errors == ()


def test_partially_verified_adverse_axis_does_not_auto_exit() -> None:
    raw = _raw("underwater-hold.yaml")
    raw["thesis_health"]["permanent_loss_axes"][0].update(
        assessment="adverse", evidence_status="partially_verified"
    )
    result = evaluate_holding_review(HoldingReviewDocument.model_validate(raw))
    assert result.permanent_loss_conclusion == "unknown"
    assert result.computed_action == "hold"


def test_evidence_age_must_match_review_dates() -> None:
    raw = _raw("underwater-hold.yaml")
    raw["thesis_health"]["evidence_freshness"]["age_days"] = 0
    result = evaluate_holding_review(HoldingReviewDocument.model_validate(raw))
    assert any("must equal the elapsed days" in error for error in result.errors)


def test_recorded_action_must_match_computed() -> None:
    raw = _raw("underwater-hold.yaml")
    raw["action"] = "exit"  # disagrees with computed hold
    document = HoldingReviewDocument.model_validate(raw)
    result = evaluate_holding_review(document)
    assert any("disagrees with computed" in message for message in result.errors)


def test_stale_evidence_warns() -> None:
    raw = _raw("underwater-hold.yaml")
    raw["as_of"] = "2027-11-17"
    raw["thesis_health"]["evidence_freshness"]["age_days"] = 500
    result = evaluate_holding_review(HoldingReviewDocument.model_validate(raw))
    assert any("days old" in warning for warning in result.warnings)


def _write_current_builder_sources(root: Path) -> None:
    thesis_dir = root / "theses"
    thesis_dir.mkdir()
    thesis = yaml.safe_load(
        Path("tests/fixtures/thesis/2331-decision.yaml").read_text(encoding="utf-8")
    )
    thesis["input_snapshot"]["facts"][0]["price_basis"] = "last_close_unadjusted"
    core_hash = thesis_core_hash(ThesisDocument.model_validate(thesis))
    review = yaml.safe_load(
        Path("tests/fixtures/thesis/2331-decision-review.yaml").read_text(encoding="utf-8")
    )
    review["reviewed_thesis_sha256"] = core_hash
    review_hash = independent_review_hash(IndependentReview.model_validate(review))
    thesis["human_evidence_override"]["proposal_sha256"] = core_hash
    thesis["human_evidence_override"]["review_sha256"] = review_hash
    thesis_dir.joinpath("2331-decision.yaml").write_text(
        yaml.safe_dump(thesis, sort_keys=False), encoding="utf-8"
    )
    thesis_dir.joinpath("2331-decision-review.yaml").write_text(
        yaml.safe_dump(review, sort_keys=False), encoding="utf-8"
    )
    ledger = yaml.safe_load(
        Path("tests/fixtures/portfolio-ledger/representative.yaml").read_text(encoding="utf-8")
    )
    ledger["as_of"] = "2026-07-03T10:00:00+09:00"
    ledger["events"] = [
        event for event in ledger["events"] if event["occurred_at"] <= ledger["as_of"]
    ]
    ledger["market_prices"] = [
        {
            "ticker": "2331",
            "price_yen": 1032,
            "observed_at": "2026-07-03T08:30:00+09:00",
            "source_kind": "test_fixture",
            "price_basis": "unadjusted_close",
            "source_ref": "offline-fixture:2331:2026-07-03",
        }
    ]
    root.joinpath("ledger.yaml").write_text(yaml.safe_dump(ledger), encoding="utf-8")


def test_holding_review_build_cli_writes_a_validated_draft(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_current_builder_sources(tmp_path)
    db_path = tmp_path / "app.sqlite"
    thesis = yaml.safe_load(tmp_path.joinpath("theses/2331-decision.yaml").read_text())
    review = yaml.safe_load(tmp_path.joinpath("theses/2331-decision-review.yaml").read_text())
    thesis_id = "thesis-20260703-2331-r1"
    ResearchStoreService(db_path).publish_thesis_with_review(thesis_id, thesis, review)
    ledger = load_portfolio_ledger(tmp_path / "ledger.yaml")
    seed_ledger(
        db_path,
        ledger.model_copy(
            update={
                "market_prices": tuple(
                    price.model_copy(update={"source_kind": "licensed_dataset"})
                    for price in ledger.market_prices
                )
            }
        ),
    )
    output = tmp_path / "review.yaml"
    exit_code = main(
        [
            "holding-review-build",
            "--root",
            str(tmp_path),
            "--db",
            str(db_path),
            "--thesis-id",
            thesis_id,
            "--position-id",
            "position-2331",
            "--out",
            "review.yaml",
        ]
    )
    assert exit_code == 0
    assert output.is_file()
    assert yaml.safe_load(capsys.readouterr().out)["ticker"] == "2331"
