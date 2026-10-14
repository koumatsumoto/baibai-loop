"""A refresh reuses exact evidence and requires newly authored period/distribution checks."""

import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest
from tests.helpers.assessment_v1 import (
    NOW,
    assessment_id,
    pair_payload,
    review_id,
    update_review_hash,
)

from baibai_engine.research.assessment import (
    AssessmentReview,
    StockAssessmentDocument,
    UnpublishedAssessment,
    current_price_projection,
    evaluate_assessment,
)
from baibai_engine.research.assessment_refresh import scaffold_assessment_refresh
from baibai_engine.research.assessment_store import (
    ResearchValidationError,
    load_reviewed_assessment,
    publish_reviewed_assessment,
)
from baibai_engine.storage.application.schema import SCHEMA_SQL
from baibai_engine.valuation.scenario import maximum_entry_price, project_return


@pytest.fixture
def prior(connection):
    document, review = pair_payload(as_of="2026-07-14")
    document["valuation"]["base"]["terminal_value_per_share_yen"] = "1200"
    document["valuation"]["base"]["cash_distribution_per_share_yen"] = "60"
    review["recalculated_projections"][0]["terminal_value_per_share_yen"] = "1200"
    review["recalculated_projections"][0]["cash_distribution_per_share_yen"] = "60"
    update_review_hash(document, review)
    connection.execute("BEGIN IMMEDIATE")
    return publish_reviewed_assessment(
        connection,
        assessment_id(),
        document,
        review,
        published_at=datetime.fromisoformat("2026-07-14T16:06:00+09:00"),
    )


@pytest.fixture
def connection():
    with sqlite3.connect(":memory:") as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA_SQL)
        yield connection


def authored_refresh(prior):
    draft = scaffold_assessment_refresh(
        prior,
        as_of=date(2026, 10, 14),
        assessed_at=datetime.fromisoformat("2026-10-14T16:00:00+09:00"),
    )
    draft["valuation"]["horizon_months"] = 9
    draft["valuation"]["base"]["cash_distribution_per_share_yen"] = "40"
    draft["valuation"]["downside"]["cash_distribution_per_share_yen"] = "0"
    draft["refresh"].update(
        checked_at="2026-10-14T15:55:00+09:00",
        material_information_check="今回重要開示と仮説を確認、materialな反証なし",
        changed_sections=["valuation"],
        unchanged_assumptions_reason="旧保存pairの一次根拠を読み直し終価1200を維持",
        horizon_reason="起点から3ヶ月経過、今回から残る約9ヶ月を確認",
        distribution_reason="資料から20を受取済みと確認、今後40のみ計上",
        corporate_action_reason="同じ起点一株の権利単位を確認",
    )
    _, review = pair_payload(review_number=4)
    review["review_scope"] = (
        "旧pairの保存根拠を再読、今回material開示を確認、Base/Downsideを独立再計算。原典の全文再取得はしていない"
    )
    review["recalculated_projections"][0].update(
        terminal_value_per_share_yen="1200",
        cash_distribution_per_share_yen="40",
    )
    update_review_hash(draft, review)
    return draft, review


def test_refresh_scaffold_preserves_old_sources_facts_and_cannot_publish(prior, connection):
    before = prior.document.model_dump(mode="json")
    draft = scaffold_assessment_refresh(
        prior,
        as_of=date(2026, 10, 14),
        assessed_at=datetime.fromisoformat("2026-10-14T16:00:00+09:00"),
    )
    assert draft["input_snapshot"]["sources"] == before["input_snapshot"]["sources"]
    assert draft["input_snapshot"]["facts"] == before["input_snapshot"]["facts"]
    assert draft["valuation"]["horizon_months"] is None
    assert draft["valuation"]["base"]["cash_distribution_per_share_yen"] is None
    assert draft["valuation"]["downside"]["cash_distribution_per_share_yen"] is None
    assert draft["valuation"]["base"]["terminal_value_per_share_yen"] == "1200"
    assert all(
        draft["refresh"][field] is None
        for field in (
            "checked_at",
            "horizon_reason",
            "distribution_reason",
            "material_information_check",
        )
    )
    assert "review" not in draft
    with pytest.raises(ValueError, match="validation errors"):
        publish_reviewed_assessment(
            connection,
            assessment_id(3),
            draft,
            prior.review.model_dump(mode="json"),
            published_at=NOW,
            supersedes_id=prior.assessment_id,
        )
    assert connection.execute("SELECT count(*) FROM stock_assessment").fetchone()[0] == 1
    assert prior.document.model_dump(mode="json") == before


def test_same_day_reuses_original_immutable_pair_without_new_publication(connection):
    document, review = pair_payload()
    connection.execute("BEGIN IMMEDIATE")
    pair = publish_reviewed_assessment(
        connection, assessment_id(), document, review, published_at=NOW
    )
    first_hash = pair.core_sha256
    args = dict(
        price_as_of=date(2026, 10, 14),
        as_of=date(2026, 10, 14),
        basis_confirmed=True,
        max_quote_age_days=7,
    )
    assert (
        current_price_projection(pair.document, price_yen=Decimal(1100), **args)["base"][
            "annualized_return_pct"
        ]
        == 0
    )
    assert (
        current_price_projection(pair.document, price_yen=Decimal(1000), **args)["base"][
            "annualized_return_pct"
        ]
        == 10
    )
    assert load_reviewed_assessment(connection, pair.assessment_id).core_sha256 == first_hash
    assert connection.execute("SELECT count(*) FROM stock_assessment").fetchone()[0] == 1


def test_independent_refresh_rebases_12_to_9_months_and_60_to_40_distribution(prior, connection):
    draft, review = authored_refresh(prior)
    refreshed = publish_reviewed_assessment(
        connection,
        assessment_id(3),
        draft,
        review,
        published_at=NOW,
        supersedes_id=prior.assessment_id,
    )
    assert refreshed.document.valuation.horizon_months == 9
    assert refreshed.document.valuation.base.cash_distribution_per_share_yen == 40
    assert refreshed.document.refresh.prior_assessment_ref.core_sha256 == prior.core_sha256
    assert "原典の全文再取得はしていない" in refreshed.review.review_scope
    result = project_return(
        refreshed.document.valuation.base, price_yen=Decimal(1000), horizon_months=9
    )
    assert result.total_value_yen == 1240
    assert result.total_return_pct == 24
    assert abs(result.annualized_return_pct - Decimal("33.21779680263184")) < Decimal("1e-12")
    assert maximum_entry_price(
        refreshed.document.valuation.base, horizon_months=9, required_annual_return_pct=Decimal(10)
    ) > Decimal(1100)
    assert prior.document.valuation.horizon_months == 12
    assert prior.document.valuation.base.cash_distribution_per_share_yen == 60
    assert load_reviewed_assessment(connection, refreshed.assessment_id) == refreshed


def test_old_review_hash_cannot_be_relabelled_as_new_review(prior):
    draft, review = authored_refresh(prior)
    review["reviewed_assessment_sha256"] = prior.core_sha256
    result = evaluate_assessment(
        StockAssessmentDocument.model_validate(draft),
        review=AssessmentReview.model_validate(review),
        identity=UnpublishedAssessment.DRAFT,
        now=NOW,
    )
    assert "Assessment Review hash does not match assessment" in result.errors


def test_new_period_and_distribution_require_independent_recalculation(prior):
    draft, review = authored_refresh(prior)
    review["recalculated_projections"][0]["cash_distribution_per_share_yen"] = "60"
    result = evaluate_assessment(
        StockAssessmentDocument.model_validate(draft),
        review=AssessmentReview.model_validate(review),
        identity=UnpublishedAssessment.DRAFT,
        now=NOW,
    )
    assert "Review base cash_distribution_per_share_yen mismatch" in result.errors


def test_unknown_future_distribution_stays_unresolved_without_zero_fill(prior, connection):
    draft, review = authored_refresh(prior)
    draft["conclusion"]["stance"] = "uncertain"
    draft["valuation"] = dict(
        status="unresolved",
        market_price_fact_id=None,
        horizon_months=None,
        required_annual_return_pct=None,
        base=None,
        downside=None,
        unresolved_reason="権利済み・今後分配の資料が不足",
    )
    review["recalculated_projections"] = []
    update_review_hash(draft, review)
    pair = publish_reviewed_assessment(
        connection,
        assessment_id(3),
        draft,
        review,
        published_at=NOW,
        supersedes_id=prior.assessment_id,
    )
    assert pair.document.valuation.base is None
    assert pair.document.valuation.horizon_months is None
    assert pair.evaluation.assessment_readiness == "ready"
    assert (
        current_price_projection(
            pair.document,
            price_yen=Decimal(1000),
            price_as_of=date(2026, 10, 14),
            as_of=date(2026, 10, 14),
            basis_confirmed=True,
            max_quote_age_days=7,
        )
        is None
    )


@pytest.mark.parametrize("field", ["core_sha256", "review_id", "assessment_id"])
def test_refresh_exact_prior_reference_is_checked_before_any_insert(prior, connection, field):
    draft, review = authored_refresh(prior)
    ref = draft["refresh"]["prior_assessment_ref"]
    ref[field] = {
        "core_sha256": "0" * 64,
        "review_id": review_id(99),
        "assessment_id": assessment_id(99),
    }[field]
    update_review_hash(draft, review)
    with pytest.raises(ResearchValidationError, match=r"reference|unknown"):
        publish_reviewed_assessment(
            connection,
            assessment_id(3),
            draft,
            review,
            published_at=NOW,
            supersedes_id=prior.assessment_id,
        )
    assert connection.execute("SELECT count(*) FROM stock_assessment").fetchone()[0] == 1


@pytest.mark.parametrize("checked_at", ["2026-10-13T15:55:00+09:00", "2026-10-14T16:01:00+09:00"])
def test_refresh_confirmation_time_cannot_be_old_or_follow_assessment(prior, checked_at):
    draft, review = authored_refresh(prior)
    draft["refresh"]["checked_at"] = checked_at
    update_review_hash(draft, review)
    result = evaluate_assessment(
        StockAssessmentDocument.model_validate(draft),
        review=AssessmentReview.model_validate(review),
        identity=UnpublishedAssessment.DRAFT,
        now=NOW,
    )
    assert any("refresh check" in error for error in result.errors)


def test_refresh_scaffold_rejects_backward_and_future_dates(prior):
    for as_of in (date(2026, 7, 13), date(2026, 10, 15)):
        with pytest.raises(ValueError, match=r"backwards|follow"):
            scaffold_assessment_refresh(prior, as_of=as_of, assessed_at=NOW)


def test_refresh_review_scope_is_required_and_nonempty(prior):
    _, review = authored_refresh(prior)
    review["review_scope"] = "   "
    with pytest.raises(ValueError, match="review_scope"):
        AssessmentReview.model_validate(review)


def test_refresh_can_use_exact_older_material_and_supersede_current_latest(prior, connection):
    document, review = pair_payload(as_of="2026-08-14", review_number=6)
    document["conclusion"]["stance"] = "uncertain"
    document["valuation"] = dict(
        status="unresolved",
        market_price_fact_id=None,
        horizon_months=None,
        required_annual_return_pct=None,
        base=None,
        downside=None,
        unresolved_reason="追加資料を確認中",
    )
    review["recalculated_projections"] = []
    update_review_hash(document, review)
    intervening = publish_reviewed_assessment(
        connection,
        assessment_id(5),
        document,
        review,
        published_at=datetime.fromisoformat("2026-08-14T16:06:00+09:00"),
        supersedes_id=prior.assessment_id,
    )
    draft, review = authored_refresh(prior)
    refreshed = publish_reviewed_assessment(
        connection,
        assessment_id(3),
        draft,
        review,
        published_at=NOW,
        supersedes_id=intervening.assessment_id,
    )
    assert refreshed.document.refresh.prior_assessment_ref.assessment_id == prior.assessment_id
    assert (
        connection.execute(
            "SELECT supersedes_id FROM stock_assessment WHERE assessment_id = ?",
            (refreshed.assessment_id,),
        ).fetchone()[0]
        == intervening.assessment_id
    )
    assert load_reviewed_assessment(connection, refreshed.assessment_id) == refreshed


def test_refresh_reader_rechecks_prior_review_arithmetic_without_inherited_review_chain(
    prior, connection
):
    from baibai_engine.storage.application.json import canonical_json

    draft, review = authored_refresh(prior)
    refreshed = publish_reviewed_assessment(
        connection,
        assessment_id(3),
        draft,
        review,
        published_at=NOW,
        supersedes_id=prior.assessment_id,
    )
    tampered = prior.review.model_dump(mode="json")
    tampered["recalculated_projections"][0]["cash_distribution_per_share_yen"] = "61"
    connection.execute("DROP TRIGGER stock_assessment_review_immutable")
    connection.execute(
        "UPDATE stock_assessment_review SET payload = ? WHERE review_id = ?",
        (canonical_json(tampered), prior.review.review_id),
    )
    with pytest.raises(ResearchValidationError, match="cash_distribution_per_share_yen mismatch"):
        load_reviewed_assessment(connection, refreshed.assessment_id)


def same_day_prior(connection):
    document, review = pair_payload()
    document["valuation"]["base"]["terminal_value_per_share_yen"] = "1200"
    document["valuation"]["base"]["cash_distribution_per_share_yen"] = "60"
    review["recalculated_projections"][0]["terminal_value_per_share_yen"] = "1200"
    review["recalculated_projections"][0]["cash_distribution_per_share_yen"] = "60"
    for source in document["input_snapshot"]["sources"]:
        source["retrieved_at"] = "2026-10-14T15:40:00+09:00"
    update_review_hash(document, review)
    connection.execute("BEGIN IMMEDIATE")
    return publish_reviewed_assessment(
        connection, assessment_id(), document, review, published_at=NOW
    )


def same_day_refresh(prior, *, assessed_at, checked_at, reviewed_at):
    draft, review = authored_refresh(prior)
    draft["conclusion"]["assessed_at"] = assessed_at
    draft["refresh"]["checked_at"] = checked_at
    review["reviewed_at"] = reviewed_at
    update_review_hash(draft, review)
    return draft, review


@pytest.mark.parametrize(
    ("assessed_at", "checked_at", "reviewed_at", "message"),
    [
        (
            "2026-10-14T15:50:00+09:00",
            "2026-10-14T15:45:00+09:00",
            "2026-10-14T15:55:00+09:00",
            "prior assessment",
        ),
        (
            "2026-10-14T16:06:30+09:00",
            "2026-10-14T16:05:59+09:00",
            "2026-10-14T16:06:40+09:00",
            "prior publication",
        ),
    ],
)
def test_same_day_refresh_publish_rejects_checks_before_the_prior_pair(
    connection, assessed_at, checked_at, reviewed_at, message
):
    prior = same_day_prior(connection)
    draft, review = same_day_refresh(
        prior, assessed_at=assessed_at, checked_at=checked_at, reviewed_at=reviewed_at
    )
    assert (
        evaluate_assessment(
            StockAssessmentDocument.model_validate(draft),
            review=AssessmentReview.model_validate(review),
            identity=UnpublishedAssessment.DRAFT,
            now=datetime.fromisoformat("2026-10-14T16:07:00+09:00"),
        ).errors
        == ()
    )
    with pytest.raises(ResearchValidationError, match=message):
        publish_reviewed_assessment(
            connection,
            assessment_id(3),
            draft,
            review,
            published_at=datetime.fromisoformat("2026-10-14T16:07:00+09:00"),
            supersedes_id=prior.assessment_id,
        )
    assert connection.execute("SELECT count(*) FROM stock_assessment").fetchone()[0] == 1


@pytest.mark.parametrize("fault", ["assessed_at", "checked_at"])
def test_same_day_refresh_read_rechecks_authoritative_prior_time(connection, fault):
    from baibai_engine.research.assessment import assessment_core_hash
    from baibai_engine.storage.application.json import canonical_json

    prior = same_day_prior(connection)
    draft, review = same_day_refresh(
        prior,
        assessed_at="2026-10-14T16:06:30+09:00",
        checked_at="2026-10-14T16:06:00+09:00",
        reviewed_at="2026-10-14T16:06:40+09:00",
    )
    pair = publish_reviewed_assessment(
        connection,
        assessment_id(3),
        draft,
        review,
        published_at=datetime.fromisoformat("2026-10-14T16:07:00+09:00"),
        supersedes_id=prior.assessment_id,
    )
    if fault == "assessed_at":
        draft["conclusion"]["assessed_at"] = "2026-10-14T15:50:00+09:00"
        draft["refresh"]["checked_at"] = "2026-10-14T15:45:00+09:00"
        review["reviewed_at"] = "2026-10-14T15:55:00+09:00"
    else:
        draft["refresh"]["checked_at"] = "2026-10-14T16:05:59+09:00"
    update_review_hash(draft, review)
    document = StockAssessmentDocument.model_validate(draft)
    assert (
        evaluate_assessment(
            document,
            review=AssessmentReview.model_validate(review),
            identity=UnpublishedAssessment.DRAFT,
            now=datetime.fromisoformat("2026-10-14T16:07:00+09:00"),
        ).errors
        == ()
    )
    connection.execute("DROP TRIGGER stock_assessment_immutable")
    connection.execute("DROP TRIGGER stock_assessment_review_immutable")
    connection.execute(
        "UPDATE stock_assessment SET payload = ?, core_sha256 = ? WHERE assessment_id = ?",
        (
            canonical_json(document.model_dump(mode="json")),
            assessment_core_hash(document),
            pair.assessment_id,
        ),
    )
    connection.execute(
        "UPDATE stock_assessment_review SET payload = ?, reviewed_at = ? WHERE review_id = ?",
        (
            canonical_json(AssessmentReview.model_validate(review).model_dump(mode="json")),
            review["reviewed_at"],
            pair.review.review_id,
        ),
    )
    with pytest.raises(ResearchValidationError, match=r"prior assessment|prior publication"):
        load_reviewed_assessment(connection, pair.assessment_id)


def test_same_day_refresh_accepts_exact_publication_instant_with_utc_offset(connection):
    prior = same_day_prior(connection)
    draft, review = same_day_refresh(
        prior,
        assessed_at="2026-10-14T07:06:00Z",
        checked_at="2026-10-14T07:06:00Z",
        reviewed_at="2026-10-14T07:06:00Z",
    )
    pair = publish_reviewed_assessment(
        connection,
        assessment_id(3),
        draft,
        review,
        published_at=datetime.fromisoformat("2026-10-14T16:07:00+09:00"),
        supersedes_id=prior.assessment_id,
    )
    assert pair.document.refresh.checked_at == NOW
    assert pair.document.refresh.checked_at.isoformat() == "2026-10-14T07:06:00+00:00"
    assert load_reviewed_assessment(connection, pair.assessment_id) == pair


def test_same_day_scaffold_keeps_assessment_time_guard_and_check_unfilled(connection):
    prior = same_day_prior(connection)
    with pytest.raises(ValueError, match="prior assessment"):
        scaffold_assessment_refresh(
            prior,
            as_of=date(2026, 10, 14),
            assessed_at=datetime.fromisoformat("2026-10-14T15:50:00+09:00"),
        )
    draft = scaffold_assessment_refresh(
        prior, as_of=date(2026, 10, 14), assessed_at=datetime.fromisoformat("2026-10-14T07:00:00Z")
    )
    assert draft["conclusion"]["assessed_at"] == "2026-10-14T07:00:00+00:00"
    assert draft["refresh"]["checked_at"] is None
