from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from baibai_loop.foundation.yaml_io import safe_load
from baibai_loop.thesis.decision_cli import main as decision_main
from baibai_loop.thesis.decision_packet import (
    DecisionPacketDocument,
    DecisionPacketError,
    DecisionPacketResult,
    IndependentReview,
    decision_packet_core_hash,
    decision_packet_json_schema,
    evaluate_decision_packet,
    independent_review_json_schema,
    load_decision_packet,
    load_independent_review,
)
from baibai_loop.validation.cli import main as validation_main
from baibai_loop.validation.decision_packet import validate_decision_packet_file

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/decision-packet/2331-decision.yaml"
REVIEW_FIXTURE = ROOT / "tests/fixtures/decision-packet/2331-decision-review.yaml"
SCHEMA = ROOT / "records/_schemas/decision-packet.json"
REVIEW_SCHEMA = ROOT / "records/_schemas/decision-review.json"


def _raw() -> dict[str, object]:
    raw = safe_load(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _document(raw: dict[str, object] | None = None) -> DecisionPacketDocument:
    return DecisionPacketDocument.model_validate(raw or _raw())


def _review_raw() -> dict[str, object]:
    raw = safe_load(REVIEW_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _bind_review(
    raw: dict[str, object], review_raw: dict[str, object] | None = None
) -> tuple[DecisionPacketDocument, IndependentReview]:
    document = _document(raw)
    review = review_raw or _review_raw()
    review["reviewed_packet_sha256"] = decision_packet_core_hash(document)
    return document, IndependentReview.model_validate(review)


def _evaluate(
    raw: dict[str, object],
    review_raw: dict[str, object] | None = None,
    *,
    now: datetime | None = None,
) -> DecisionPacketResult:
    document, review = _bind_review(raw, review_raw)
    return evaluate_decision_packet(document, review=review, now=now)


def test_golden_packet_is_ready_with_explicit_evidence_warning() -> None:
    result = evaluate_decision_packet(
        load_decision_packet(FIXTURE), review=load_independent_review(REVIEW_FIXTURE)
    )

    assert result.packet_status == "ready_with_warnings"
    assert result.decision_readiness == "ready"
    assert result.errors == ()
    assert result.warnings == ("permanent-loss evidence incomplete: ['customer_concentration']",)
    assert [(item.horizon_years, item.name) for item in result.scenarios] == [
        (3, "bear"),
        (3, "base"),
        (3, "bull"),
        (5, "bear"),
        (5, "base"),
        (5, "bull"),
    ]
    base_five = next(
        item for item in result.scenarios if item.horizon_years == 5 and item.name == "base"
    )
    assert base_five.terminal_share_count == 97_524_875.3122
    assert base_five.terminal_price_yen == 1439.5401
    assert base_five.total_return_cagr_pct == 9.57


def test_generated_schema_matches_tracked_contract() -> None:
    assert json.loads(SCHEMA.read_text(encoding="utf-8")) == decision_packet_json_schema()
    assert json.loads(REVIEW_SCHEMA.read_text(encoding="utf-8")) == independent_review_json_schema()


def test_packet_requires_input_snapshot() -> None:
    raw = _raw()
    del raw["input_snapshot"]

    with pytest.raises(ValueError, match="input_snapshot"):
        _document(raw)


@pytest.mark.parametrize("value", [None, 0, -1])
def test_packet_requires_an_explicit_positive_5y_base_return(value: object) -> None:
    raw = _raw()
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    if value is None:
        del estimates["required_5y_base_cagr_pct"]
    else:
        estimates["required_5y_base_cagr_pct"] = value

    with pytest.raises(ValueError, match="required_5y_base_cagr_pct"):
        _document(raw)


def test_source_ticker_must_match_snapshot_ticker() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    sources = snapshot["sources"]
    assert isinstance(sources, list)
    sources[0]["ticker"] = "2332"

    result = _evaluate(raw)

    assert any("ticker does not match input_snapshot" in error for error in result.errors)


def test_snapshot_future_as_of_is_rejected() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    snapshot["as_of"] = "2099-01-01"

    result = _evaluate(raw)

    assert "packet as_of cannot be in the future" in result.errors


def test_future_source_retrieval_is_rejected() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    sources = snapshot["sources"]
    assert isinstance(sources, list)
    sources[0]["retrieved_at"] = "2099-01-01T00:00:00+09:00"

    result = _evaluate(raw)

    assert any("retrieval is future-dated" in error for error in result.errors)


def test_source_retrieved_after_proposal_is_rejected() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    sources = snapshot["sources"]
    assert isinstance(sources, list)
    sources[0]["retrieved_at"] = "2026-07-03T09:00:01+09:00"

    result = _evaluate(raw)

    assert any("retrieved after the AI proposal" in error for error in result.errors)


def test_market_price_unit_is_checked() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    facts = snapshot["facts"]
    assert isinstance(facts, list)
    facts[0]["unit"] = "JPY"

    result = _evaluate(raw)

    assert "market_price fact unit must be JPY_per_share" in result.errors


def test_observed_entry_price_must_equal_snapshot_market_price() -> None:
    raw = _raw()
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    estimates["entry_price_basis_yen"] = 1031

    result = _evaluate(raw)

    assert "observed_market_price entry basis must equal snapshot market price" in result.errors


def test_valuation_fact_must_be_numeric() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    facts = snapshot["facts"]
    assert isinstance(facts, list)
    facts[1]["value"] = "cheap"

    result = _evaluate(raw)

    assert "valuation fact trailing-per must be numeric" in result.errors


def test_snapshot_fact_requires_known_source() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    facts = snapshot["facts"]
    assert isinstance(facts, list)
    facts[0]["source_ids"] = ["missing-source"]

    result = _evaluate(raw)

    assert any("references unknown sources" in error for error in result.errors)


def test_snapshot_requires_exactly_one_market_price() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    facts = snapshot["facts"]
    assert isinstance(facts, list)
    facts[0]["fact_kind"] = "other"
    facts[0].pop("observed_at")
    facts[0].pop("price_basis")

    result = _evaluate(raw)

    assert "input_snapshot requires exactly one market_price fact" in result.errors


def test_snapshot_rejects_multiple_market_prices() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    facts = snapshot["facts"]
    assert isinstance(facts, list)
    duplicate = copy.deepcopy(facts[0])
    duplicate["fact_id"] = "second-market-price"
    facts.append(duplicate)

    result = _evaluate(raw)

    assert "input_snapshot requires exactly one market_price fact" in result.errors


def test_snapshot_requires_valuation_fact() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    facts = snapshot["facts"]
    assert isinstance(facts, list)
    facts[1]["fact_kind"] = "other"

    result = _evaluate(raw)

    assert "input_snapshot requires at least one valuation_metric fact" in result.errors


def test_fixture_lineage_is_self_contained_for_clean_checkout() -> None:
    document = load_decision_packet(FIXTURE)

    assert all(
        source.ref is None
        for source in document.input_snapshot.sources
        if source.source_tier == "local_data"
    )
    assert all(
        source.provider and source.dataset
        for source in document.input_snapshot.sources
        if source.source_tier == "local_data"
    )
    assert "candidate_ref" not in FIXTURE.read_text(encoding="utf-8")


def test_missing_risk_axis_makes_packet_incomplete() -> None:
    raw = _raw()
    risks = raw["permanent_loss_risks"]
    assert isinstance(risks, list)
    risks.pop()

    result = _evaluate(raw)

    assert result.packet_status == "incomplete"
    assert any("missing permanent-loss risk axis" in error for error in result.errors)


def test_missing_source_reference_makes_packet_incomplete() -> None:
    raw = _raw()
    risks = raw["permanent_loss_risks"]
    assert isinstance(risks, list)
    risks[0]["source_ids"] = []

    result = _evaluate(raw)

    assert any("risk funding_liquidity requires source_ids" in error for error in result.errors)


def test_future_as_of_makes_packet_incomplete() -> None:
    raw = _raw()
    risks = raw["permanent_loss_risks"]
    assert isinstance(risks, list)
    risks[0]["as_of"] = "2026-07-04"

    result = _evaluate(raw)

    assert any(
        "risk funding_liquidity as_of is after packet as_of" in error for error in result.errors
    )


def test_adverse_risk_cannot_be_called_acceptable() -> None:
    raw = _raw()
    risks = raw["permanent_loss_risks"]
    judgment = raw["judgment"]
    assert isinstance(risks, list)
    assert isinstance(judgment, dict)
    risks[0]["assessment"] = "adverse"
    judgment["permanent_loss_conclusion"] = "acceptable"

    result = _evaluate(raw)

    assert any("contradicts permanent-loss risk axes" in error for error in result.errors)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("claimed_terminal_share_count", "terminal share count mismatch"),
        ("claimed_terminal_price_yen", "terminal price mismatch"),
        ("claimed_total_return_cagr_pct", "total-return CAGR mismatch"),
    ],
)
def test_scenario_arithmetic_is_recalculated(field: str, message: str) -> None:
    raw = _raw()
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    scenarios = estimates["scenarios"]
    assert isinstance(scenarios, list)
    scenarios[0][field] = 999

    result = _evaluate(raw)

    assert any(message in error for error in result.errors)


def test_scenario_starting_values_must_match_observed_facts() -> None:
    raw = _raw()
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    scenarios = estimates["scenarios"]
    assert isinstance(scenarios, list)
    scenarios[0]["starting_earnings_yen"] = 99_000_000_000

    result = _evaluate(raw)

    assert any("does not match observed fact normalized-profit" in error for error in result.errors)


def test_scenario_fact_kind_must_match_earnings_basis() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    facts = snapshot["facts"]
    assert isinstance(facts, list)
    facts[2]["fact_kind"] = "other"

    result = _evaluate(raw)

    assert any("fact kind must be net_income" in error for error in result.errors)


def test_derived_metric_is_recalculated_from_fact_ids() -> None:
    raw = _raw()
    derived = raw["derived"]
    assert isinstance(derived, dict)
    metrics = derived["metrics"]
    assert isinstance(metrics, list)
    metrics[0]["value"] = 999

    result = _evaluate(raw)

    assert any("metric normalized-eps value mismatch" in error for error in result.errors)


def test_derived_ratio_unit_is_checked() -> None:
    raw = _raw()
    derived = raw["derived"]
    assert isinstance(derived, dict)
    metrics = derived["metrics"]
    assert isinstance(metrics, list)
    metrics[0]["unit"] = "percent"

    result = _evaluate(raw)

    assert any("unit mismatch for ratio inputs" in error for error in result.errors)


def test_dividends_cannot_be_embedded_in_terminal_price() -> None:
    raw = _raw()
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    scenarios = estimates["scenarios"]
    assert isinstance(scenarios, list)
    scenarios[0]["terminal_price_includes_dividends"] = True

    with pytest.raises(ValueError, match="Input should be False"):
        _document(raw)


def test_incomplete_evidence_cannot_claim_high_confidence() -> None:
    raw = _raw()
    judgment = raw["judgment"]
    assert isinstance(judgment, dict)
    judgment["confidence"] = "high"

    result = _evaluate(raw)

    assert any("high confidence" in error for error in result.errors)


def test_verified_review_must_check_a_primary_source() -> None:
    raw = _raw()
    review = _review_raw()
    review["checked_source_ids"] = ["internal-screen"]

    result = _evaluate(raw, review)

    assert any("must check a primary source" in error for error in result.errors)


def test_partially_verified_primary_review_requires_override_for_buy() -> None:
    raw = _raw()
    risks = raw["permanent_loss_risks"]
    judgment = raw["judgment"]
    review = _review_raw()
    assert isinstance(risks, list)
    assert isinstance(judgment, dict)
    for risk in risks:
        risk["assessment"] = "acceptable"
        risk["evidence_status"] = "verified"
    judgment["permanent_loss_conclusion"] = "acceptable"
    judgment["sizing_action"] = "normal"
    raw["human_evidence_override"] = None
    review["primary_source_check"] = "partially_verified"

    result = _evaluate(raw, review)

    assert any("without fully verified primary review" in error for error in result.errors)


def test_nonfinite_metric_is_rejected() -> None:
    raw = _raw()
    derived = raw["derived"]
    assert isinstance(derived, dict)
    metrics = derived["metrics"]
    assert isinstance(metrics, list)
    metrics[0]["value"] = float("nan")

    with pytest.raises(ValueError, match="finite number"):
        _document(raw)


def test_buy_with_evidence_gap_requires_reduced_sizing_and_override() -> None:
    raw = _raw()
    judgment = raw["judgment"]
    assert isinstance(judgment, dict)
    judgment["sizing_action"] = "normal"
    raw["human_evidence_override"] = None

    result = _evaluate(raw)

    assert any("requires a human override and reduced sizing" in error for error in result.errors)


def test_future_human_override_cannot_authorize_earlier_judgment() -> None:
    raw = _raw()
    override = raw["human_evidence_override"]
    assert isinstance(override, dict)
    override["approved_at"] = "2026-07-03T12:00:00+09:00"

    result = _evaluate(raw, now=datetime.fromisoformat("2026-07-03T10:30:00+09:00"))

    assert any("requires a human override" in error for error in result.errors)


def test_expired_human_override_cannot_keep_buy_ready() -> None:
    result = _evaluate(_raw(), now=datetime.fromisoformat("2026-08-01T10:00:00+09:00"))

    assert any("requires a human override" in error for error in result.errors)


def test_override_is_invalid_at_exact_expiry() -> None:
    result = _evaluate(_raw(), now=datetime.fromisoformat("2026-07-31T15:30:00+09:00"))

    assert any("requires a human override" in error for error in result.errors)


def test_human_override_is_bound_to_exact_review_artifact() -> None:
    raw = _raw()
    review = _review_raw()
    review["strongest_countercase"] = "A different countercase not accepted by the human."

    result = _evaluate(raw, review)

    assert any("requires a human override" in error for error in result.errors)


@pytest.mark.parametrize("field", ["proposal_sha256", "review_id", "review_sha256"])
def test_human_override_binding_fields_cannot_be_reused(field: str) -> None:
    raw = _raw()
    override = raw["human_evidence_override"]
    assert isinstance(override, dict)
    override[field] = "0" * 64 if field.endswith("sha256") else "different-review"

    result = _evaluate(raw)

    assert any("requires a human override" in error for error in result.errors)


def test_review_must_follow_initial_proposal() -> None:
    raw = _raw()
    review = _review_raw()
    review["reviewed_at"] = "2026-07-03T08:00:00+09:00"

    result = _evaluate(raw, review)

    assert any("cannot predate the AI proposal" in error for error in result.errors)


def test_review_must_cover_every_load_bearing_source() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    sources = snapshot["sources"]
    facts = snapshot["facts"]
    assert isinstance(sources, list)
    assert isinstance(facts, list)
    sources.append(
        {
            "source_id": "fact-only-source",
            "ticker": "2331",
            "source_tier": "secondary",
            "ref": "https://example.com/fact",
            "retrieved_at": "2026-07-03T08:45:00+09:00",
            "as_of": "2026-06-30",
            "used_for": "scenario starting fact",
        }
    )
    facts[2]["source_ids"] = ["fact-only-source"]
    review = _review_raw()

    result = _evaluate(raw, review)

    assert any("did not check load-bearing sources" in error for error in result.errors)


def test_extreme_growth_is_rejected_before_arithmetic() -> None:
    raw = _raw()
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    scenarios = estimates["scenarios"]
    assert isinstance(scenarios, list)
    scenarios[0]["annual_earnings_growth_pct"] = 1e308

    with pytest.raises(ValueError, match="less than or equal to 50"):
        _document(raw)


def test_huge_observed_integer_is_rejected_before_hashing() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    facts = snapshot["facts"]
    assert isinstance(facts, list)
    facts[0]["value"] = 10**10_000

    with pytest.raises(ValueError, match="less than or equal"):
        _document(raw)


def test_stale_source_cannot_support_current_fact() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    sources = snapshot["sources"]
    assert isinstance(sources, list)
    sources[0]["as_of"] = "2020-01-01"

    result = _evaluate(raw)

    assert any(
        "source primary-results is more than 400 days old" in error for error in result.errors
    )


def test_old_fact_date_cannot_be_hidden_by_matching_old_source_date() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    sources = snapshot["sources"]
    facts = snapshot["facts"]
    assert isinstance(sources, list)
    assert isinstance(facts, list)
    sources.append(
        {
            "source_id": "old-fact-source",
            "ticker": "2331",
            "source_tier": "secondary",
            "ref": "https://example.com/old",
            "retrieved_at": "2026-07-03T08:45:00+09:00",
            "as_of": "2020-01-01",
            "used_for": "stale starting fact",
        }
    )
    facts[2]["as_of"] = "2020-01-01"
    facts[2]["source_ids"] = ["old-fact-source"]
    review = _review_raw()
    checked = review["checked_source_ids"]
    assert isinstance(checked, list)
    checked.append("old-fact-source")

    result = _evaluate(raw, review)

    assert any(
        "fact normalized-profit is more than 400 days older" in error for error in result.errors
    )


def test_verified_adverse_risk_requires_human_override_and_reduced_sizing() -> None:
    raw = _raw()
    risks = raw["permanent_loss_risks"]
    judgment = raw["judgment"]
    assert isinstance(risks, list)
    assert isinstance(judgment, dict)
    risks[0]["assessment"] = "adverse"
    judgment["permanent_loss_conclusion"] = "elevated"
    judgment["sizing_action"] = "normal"
    raw["human_evidence_override"] = None

    result = _evaluate(raw)

    assert any("incomplete or adverse evidence" in error for error in result.errors)


def test_malformed_decimal_is_a_structured_load_error(tmp_path: Path) -> None:
    raw = _raw()
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    estimates["entry_price_basis_yen"] = "bad"
    path = tmp_path / "bad-decision.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(DecisionPacketError, match="fixed-point notation"):
        load_decision_packet(path)


def test_scientific_decimal_string_is_rejected_like_public_schema() -> None:
    raw = _raw()
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    estimates["entry_price_basis_yen"] = "1e3"

    with pytest.raises(ValueError, match="fixed-point notation"):
        _document(raw)


def test_future_proposal_is_not_decision_ready() -> None:
    raw = _raw()
    judgment = raw["judgment"]
    assert isinstance(judgment, dict)
    judgment["proposed_at"] = "2026-07-12T11:00:00+09:00"

    result = _evaluate(raw)

    assert "proposal cannot be future-dated" in result.errors


def test_buy_candidate_requires_independent_second_pass() -> None:
    raw = _raw()
    raw["independent_review_ref"] = None

    result = evaluate_decision_packet(_document(raw))

    assert result.packet_status == "review_required"
    assert result.errors == ("buy recommendation requires an independent second-pass review",)


def test_changed_second_pass_requires_packet_regeneration() -> None:
    raw = _raw()
    review = _review_raw()
    review["proposal_changed"] = True
    review["change_rationale"] = "Use the lower price ceiling after recalculation."

    result = _evaluate(raw, review)

    assert any("changed the proposal" in error for error in result.errors)


def test_validator_and_read_only_cli_use_same_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "records/03-thesis/2026/07/2026-07-03-2331-decision.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    (path.parent / "2331-decision-review.yaml").write_text(
        REVIEW_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    findings = validate_decision_packet_file(path)
    assert [finding.severity for finding in findings] == ["warning"]
    assert validation_main(["--root", str(tmp_path), "--target", "decision-packet"]) == 0
    capsys.readouterr()

    assert decision_main([str(path)]) == 0
    output = yaml.safe_load(capsys.readouterr().out)
    assert output["packet_status"] == "ready_with_warnings"
    assert output["decision_readiness"] == "ready"


@pytest.mark.parametrize(
    ("filename", "message"),
    [
        ("2026-07-03-9999-decision.yaml", "filename ticker"),
        ("2026-07-04-2331-decision.yaml", "filename date"),
        ("noncanonical-2331-decision.yaml", "must use YYYY-MM-DD"),
    ],
)
def test_validator_rejects_canonical_filename_identity_mismatch(
    tmp_path: Path, filename: str, message: str
) -> None:
    path = tmp_path / "records/03-thesis/2026/07" / filename
    path.parent.mkdir(parents=True)
    path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    (path.parent / "2331-decision-review.yaml").write_text(
        REVIEW_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    findings = validate_decision_packet_file(path)

    assert any(message in finding.message for finding in findings)


def test_validator_rejects_canonical_path_date_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "records/03-thesis/2025/12/2026-07-03-2331-decision.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    findings = validate_decision_packet_file(path)

    assert any("path year/month" in finding.message for finding in findings)


def test_independent_review_hash_changes_with_initial_proposal() -> None:
    raw = copy.deepcopy(_raw())
    original = decision_packet_core_hash(_document(raw))
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    estimates["entry_price_basis_yen"] = 1040

    assert decision_packet_core_hash(_document(raw)) != original
