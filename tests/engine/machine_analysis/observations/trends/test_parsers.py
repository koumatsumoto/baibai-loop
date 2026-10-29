"""実記事をコピーせず、異なる文型・紛らわしい非採用値を固定する。"""

from decimal import Decimal

import pytest
from tests.trend_fixtures import BOJ, INFORMATION, NOW, PHARMA, document

from baibai_engine.machine_analysis.observations.trends.contracts import (
    DocumentText,
    ExtractionArtifact,
    Span,
)
from baibai_engine.machine_analysis.observations.trends.parsers import (
    extract_boj_decision,
    extract_supported_family,
    extract_tsr_performance,
)


def test_tsr_survey_population_not_calendar_bankruptcies_or_profit_margin():
    projected = document(PHARMA)
    result = extract_tsr_performance(projected, created_at=NOW)
    assert result.status == "extracted"
    claims = {c.claim_id: c for c in result.claims}
    assert claims["revenue"].quantity.value == Decimal("2340000000000")
    assert claims["revenue"].quantity.resolution == Decimal("100000000")
    assert {c.scope.population_size for c in result.claims} == {512}
    assert {c.scope.period_basis for c in result.claims} == {"fiscal_year_end_months"}
    current = claims["profitable_share_2025"]
    previous = claims["profitable_share_2024"]
    assert current.quantity.value == Decimal("78.1")
    assert previous.quantity.value == Decimal("80.1")
    assert current.quantity.basis == "companies_with_positive_reported_profit"
    assert all(
        c.predicate not in ("profit_margin", "bankruptcies", "net_income") for c in result.claims
    )
    concentration = claims["revenue_concentration_revenue"]
    assert concentration.quantity.value == Decimal("42.7")
    assert "超" in concentration.scope.target
    result.verify_text(projected)
    assert ExtractionArtifact.model_validate_json(result.model_dump_json()).ref == result.ref


def test_tsr_second_sentence_family_and_precision_are_not_article_id_specific():
    result = extract_tsr_performance(document(INFORMATION, article_id="another-id"), created_at=NOW)
    claims = {c.claim_id: c for c in result.claims}
    assert result.status == "extracted"
    assert claims["revenue"].quantity.value == Decimal("10234500000000")
    assert claims["net_income"].quantity.value == Decimal("1234500000000")
    assert claims["net_income"].quantity.basis == "net_income"
    assert all(c.scope.population_size == 2222 for c in result.claims)
    assert claims["revenue_concentration_employees"].quantity.value == Decimal("82.3")


def test_tsr_missing_method_or_unrecognised_scope_does_not_guess():
    for text in ("ある産業の売上は12兆円。", INFORMATION.replace("情報サービス業", "未知産業")):
        result = extract_tsr_performance(document(text), created_at=NOW)
        assert result.status == "unsupported"
        assert result.claims == ()


def test_boj_main_decision_and_linked_note_exclude_dissent_and_other_facilities():
    result = extract_boj_decision(document(BOJ, source="boj"), created_at=NOW)
    assert result.status == "extracted"
    decision, schedule = result.claims
    assert decision.quantity.value == Decimal("1.75")
    assert decision.quantity.approximate is True
    assert decision.quantity.resolution == Decimal("0.01")
    assert decision.status == "decided"
    assert schedule.status == "scheduled"
    assert schedule.kind == "announced_plan"
    assert schedule.scheduled_for.isoformat() == "2026-09-24"
    assert {c.attribution for c in result.claims} == {"日本銀行"}
    assert {c.quantity.value for c in result.claims} == {Decimal("1.75")}


def test_boj_no_decision_note_cannot_borrow_a_facility_date():
    without_note = BOJ.replace("促す1。", "促す。")
    result = extract_boj_decision(document(without_note, source="boj"), created_at=NOW)
    assert len(result.claims) == 1
    assert result.claims[0].scheduled_for is None
    unresolved = BOJ.replace("1 新たな金融市場調節方針", "3 新たな金融市場調節方針")
    result = extract_boj_decision(document(unresolved, source="boj"), created_at=NOW)
    assert len(result.claims) == 1
    assert "decision_footnote_not_resolved" in result.limitations


def test_headline_receipt_and_dissent_only_are_not_extracted_decisions():
    headline = extract_supported_family(
        document(BOJ, source="boj", content_scope="headline"), created_at=NOW
    )
    assert headline.status == "unsupported"
    dissent = BOJ[BOJ.index("ある委員") :]
    assert extract_boj_decision(document(dissent, source="boj"), created_at=NOW).claims == ()
    assert (
        extract_supported_family(document(BOJ, source="other"), created_at=NOW).status
        == "unsupported"
    )


def test_artifact_identity_and_span_validation_fail_closed():
    projected = document(PHARMA)
    raw = projected.model_dump()
    raw["text"] += "変更"
    with pytest.raises(ValueError, match="projection digest"):
        DocumentText.model_validate(raw)
    artifact = extract_tsr_performance(projected, created_at=NOW)
    changed = artifact.model_copy(update={"method_revision": "next"})
    assert changed.ref != artifact.ref
    bad = artifact.model_copy(
        update={
            "claims": (
                artifact.claims[0].model_copy(
                    update={
                        "spans": (Span(start=0, end=len(projected.text) + 1),),
                    }
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="outside"):
        bad.verify_text(projected)
