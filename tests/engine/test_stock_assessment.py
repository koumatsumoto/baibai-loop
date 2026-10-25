"""Stock Assessment owns evidence, valuation and Review, independently of action inputs."""

import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from tests.helpers.assessment_v1 import (
    APPENDIX_ASSESSMENT_PUBLICATION,
    NOW,
    assessment_id,
    pair_payload,
    review_id,
    update_review_hash,
)

from baibai_engine.foundation.json import canonical_json
from baibai_engine.research.assessment import (
    AssessmentRef,
    AssessmentReview,
    StockAssessmentDocument,
    UnpublishedAssessment,
    assessment_core_hash,
    assessment_valuation_context,
    current_price_projection,
    evaluate_assessment,
)
from baibai_engine.research.assessment_store import (
    ResearchConflictError,
    ResearchValidationError,
    latest_assessment_id,
    load_latest_reviewed_assessment,
    load_reviewed_assessment,
    publish_reviewed_assessment,
    require_assessment_ref,
)
from baibai_engine.research.entry_policy import evaluate_buy_terms
from baibai_engine.research.proposal import QuoteInput
from baibai_engine.storage.application.schema import SCHEMA_SQL


@pytest.fixture
def connection():
    with closing(sqlite3.connect(":memory:")) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA_SQL)
        yield connection


def evaluation(document, review=None, *, now=NOW, refresh_hash=True):
    if review is not None and refresh_hash:
        update_review_hash(document, review)
    return evaluate_assessment(
        StockAssessmentDocument.model_validate(document),
        review=AssessmentReview.model_validate(review) if review is not None else None,
        identity=UnpublishedAssessment.DRAFT,
        now=now,
    )


def publish(connection, document=None, review=None, *, number=1, now=NOW, supersedes=None):
    if document is None:
        document, review = pair_payload(review_number=number + 1)
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    return publish_reviewed_assessment(
        connection,
        assessment_id(number),
        document,
        review,
        published_at=now,
        supersedes_id=supersedes,
    )


def test_full_appendix_parses_and_has_exact_typed_canonical_identity():
    publication = APPENDIX_ASSESSMENT_PUBLICATION
    document = StockAssessmentDocument.model_validate(publication["document"])
    review = AssessmentReview.model_validate(publication["review"])
    assert assessment_core_hash(document) == (
        "ce2ec16dda47852c005a079d970cc8e6bcf8c55d05057e1cdd1453b17825502b"
    )
    assert review.reviewed_assessment_sha256 == assessment_core_hash(document)
    assert document.valuation.base.terminal_value_per_share_yen == Decimal(1100)
    assert document.findings.hypotheses[0].origin == "research"
    assert evaluation(publication["document"], publication["review"]).errors == ()
    assert StockAssessmentDocument.model_validate_json(document.model_dump_json()) == document


def test_supported_assessment_has_no_cash_or_current_buy_input():
    document, review = pair_payload()
    result = evaluation(document, review)
    assert result.assessment_status == "ready"
    assert result.assessment_readiness == "ready"
    assert not hasattr(StockAssessmentDocument.model_validate(document), "available_cash_yen")
    document["available_cash_yen"] = 0
    with pytest.raises(ValueError, match="Extra inputs"):
        StockAssessmentDocument.model_validate(document)


@pytest.mark.parametrize("stance", ["uncertain", "unsupported"])
def test_unresolved_value_is_a_reviewed_completed_assessment(stance):
    document, review = pair_payload()
    document["conclusion"]["stance"] = stance
    document["valuation"] = dict(
        status="unresolved",
        market_price_fact_id=None,
        horizon_months=None,
        required_annual_return_pct=None,
        base=None,
        downside=None,
        unresolved_reason="重要な借換条件を確認できない",
    )
    document["input_snapshot"]["facts"] = []
    review["recalculated_projections"] = []
    result = evaluation(document, review)
    assert result.assessment_readiness == "ready"
    assert (
        assessment_valuation_context(StockAssessmentDocument.model_validate(document), result)
        is None
    )


@pytest.mark.parametrize(
    ("status", "stance", "error"),
    [
        ("broken", "supported", "requires unsupported"),
        ("broken", "uncertain", "requires unsupported"),
        ("uncertain", "supported", "cannot be supported"),
    ],
)
def test_stance_does_not_turn_invalidation_into_supported(status, stance, error):
    document, review = pair_payload()
    document["investment_case"]["status"] = status
    document["conclusion"]["stance"] = stance
    assert any(error in message for message in evaluation(document, review).errors)


@pytest.mark.parametrize("fault", ["unknown", "partial"])
def test_material_risk_unknown_remains_unknown_and_requires_review_explanation(fault):
    document, review = pair_payload()
    risk = document["permanent_loss_risks"][0]
    risk["assessment" if fault == "unknown" else "evidence_status"] = (
        "unknown" if fault == "unknown" else "partially_verified"
    )
    result = evaluation(document, review)
    assert any("uncertainty acceptance reason" in error for error in result.errors)
    review["uncertainty_acceptance_reason"] = (
        "更新後の顧客別採算は重要だが未開示。確認した契約総額と費用を起点に、"
        "継続と縮小の経路を比較した。縮小時の損失は残るが、価格に対する"
        "継続収益の見返りと既存顧客の更新実績を理由に引き受ける。"
    )
    result = evaluation(document, review)
    assert result.assessment_status == "ready_with_warnings"
    assert result.warnings
    assert risk["assessment" if fault == "unknown" else "evidence_status"] != "verified"


@pytest.mark.parametrize(
    "fault",
    [
        "source_ticker",
        "source_future",
        "source_retrieved_future",
        "fact_future",
        "fact_source_date",
        "source_duplicate",
        "risk_missing",
        "risk_duplicate",
        "price_unit",
        "price_adjusted",
        "price_zero",
        "price_date",
        "price_reference",
        "projection_source",
        "findings_source",
        "hypothesis_source",
        "primary",
        "review_source",
        "review_counterdate",
        "review_projection_missing",
        "review_projection_duplicate",
        "review_terminal",
        "review_cash",
    ],
)
def test_evidence_and_review_semantics_are_preserved(fault):
    document, review = pair_payload()
    snapshot = document["input_snapshot"]
    fact = snapshot["facts"][0]
    if fault == "source_ticker":
        snapshot["sources"][0]["ticker"] = "5678"
    elif fault == "source_future":
        snapshot["sources"][0]["as_of"] = "2026-10-15"
    elif fault == "source_retrieved_future":
        snapshot["sources"][0]["retrieved_at"] = "2026-10-14T16:01:00+09:00"
    elif fault == "fact_future":
        fact["as_of"] = "2026-10-15"
    elif fault == "fact_source_date":
        snapshot["sources"][1]["as_of"] = "2026-10-13"
    elif fault == "source_duplicate":
        snapshot["sources"].append(deepcopy(snapshot["sources"][0]))
    elif fault == "risk_missing":
        document["permanent_loss_risks"].pop()
    elif fault == "risk_duplicate":
        document["permanent_loss_risks"][0] = deepcopy(document["permanent_loss_risks"][1])
    elif fault == "price_unit":
        fact["unit"] = "JPY"
    elif fault == "price_adjusted":
        fact["price_basis"] = "last_close_adjusted"
    elif fault == "price_zero":
        fact["value"] = 0
    elif fault == "price_date":
        fact["observed_at"] = "2026-10-13T15:30:00+09:00"
    elif fault == "price_reference":
        document["valuation"]["market_price_fact_id"] = "missing"
    elif fault == "projection_source":
        document["valuation"]["base"]["source_ids"] = ["missing"]
    elif fault == "findings_source":
        document["findings"]["source_ids"] = ["missing"]
    elif fault == "hypothesis_source":
        document["findings"]["hypotheses"][0]["source_ids"] = ["missing"]
    elif fault == "primary":
        review["primary_source_check"] = "partially_verified"
    elif fault == "review_source":
        review["checked_source_ids"] = ["price"]
    elif fault == "review_counterdate":
        review["reviewed_at"] = "2026-10-14T15:59:00+09:00"
    elif fault == "review_projection_missing":
        review["recalculated_projections"].pop()
    elif fault == "review_projection_duplicate":
        review["recalculated_projections"].append(deepcopy(review["recalculated_projections"][0]))
    elif fault == "review_terminal":
        review["recalculated_projections"][0]["terminal_value_per_share_yen"] = "1101"
    else:
        review["recalculated_projections"][0]["cash_distribution_per_share_yen"] = "1"
    assert evaluation(document, review).assessment_status == "incomplete"


def test_hypothesis_sources_are_in_review_coverage():
    document, review = pair_payload()
    source = deepcopy(document["input_snapshot"]["sources"][0])
    source.update(source_id="customer", source_tier="secondary")
    document["input_snapshot"]["sources"].append(source)
    hypothesis = document["findings"]["hypotheses"][0]
    hypothesis.update(origin="user", source_ids=["customer"])
    assert "Assessment Review source coverage mismatch" in evaluation(document, review).errors
    review["checked_source_ids"].append("customer")
    assert evaluation(document, review).errors == ()
    assert document["input_snapshot"]["facts"][0]["fact_kind"] == "market_price"


def test_unresolved_hypothesis_is_not_invented_as_a_source_fact():
    document, review = pair_payload()
    document["findings"]["hypotheses"][0].update(result="unresolved", source_ids=[])
    assert evaluation(document, review).errors == ()
    document["findings"]["hypotheses"][0]["result"] = "supported"
    with pytest.raises(ValueError, match="resolved hypothesis"):
        StockAssessmentDocument.model_validate(document)


@pytest.mark.parametrize("field", ["business_understanding", "value_gap_explanation"])
def test_findings_text_is_stripped_and_nonempty(field):
    document, _ = pair_payload()
    document["findings"][field] = "  内容  "
    assert getattr(StockAssessmentDocument.model_validate(document).findings, field) == "内容"
    document["findings"][field] = " \t "
    with pytest.raises(ValueError, match="at least 1"):
        StockAssessmentDocument.model_validate(document)


def ratio_pair():
    document, review = pair_payload()
    snapshot = document["input_snapshot"]
    snapshot["facts"].extend(
        [
            dict(
                fact_id="income",
                fact_kind="other",
                value=120000000,
                unit="JPY",
                as_of="2026-10-14",
                source_ids=["annual"],
            ),
            dict(
                fact_id="shares",
                fact_kind="shares_outstanding",
                value=1000000,
                unit="shares",
                as_of="2026-10-14",
                source_ids=["annual"],
            ),
        ]
    )
    document["derived"]["metrics"] = [
        dict(
            metric_id="eps",
            value=120.0,
            unit="JPY_per_share",
            as_of="2026-10-14",
            formula="ratio",
            formula_version="ratio-v1",
            input_fact_ids=["income", "shares"],
            assumption="自己株を除いた株数",
            source_ids=["annual"],
        )
    ]
    return document, review


@pytest.mark.parametrize("fault", [None, "zero", "unit", "value", "reference", "bool", "duplicate"])
def test_ratio_calculation_and_units_are_checked(fault):
    document, review = ratio_pair()
    metric = document["derived"]["metrics"][0]
    if fault == "zero":
        document["input_snapshot"]["facts"][-1]["value"] = 0
    elif fault == "unit":
        metric["unit"] = "ratio"
    elif fault == "value":
        metric["value"] = 121.0
    elif fault == "reference":
        metric["input_fact_ids"][1] = "missing"
    elif fault == "bool":
        document["input_snapshot"]["facts"][-1]["value"] = True
    elif fault == "duplicate":
        document["derived"]["metrics"].append(deepcopy(metric))
    result = evaluation(document, review)
    assert bool(result.errors) == (fault is not None)


def test_exact_hash_covers_findings_and_conclusion_and_rejects_old_review():
    document, review = pair_payload()
    original = review["reviewed_assessment_sha256"]
    document["findings"]["realization"]["delay_risk"] = "新しい遅延リスク"
    assert assessment_core_hash(StockAssessmentDocument.model_validate(document)) != original
    assert (
        "Assessment Review hash does not match assessment"
        in evaluation(document, review, refresh_hash=False).errors
    )


def test_jst_date_uses_same_instant_for_utc_and_jst_clocks():
    document, review = pair_payload()
    jst = datetime.fromisoformat("2026-10-14T16:06:00+09:00")
    assert evaluation(document, review, now=jst) == evaluation(
        document, review, now=jst.astimezone(UTC)
    )
    # This is 00:30 JST on Oct 14, before that day's close is available.
    # Use the prior eligible close without changing the assessment's JST date.
    document, review = pair_payload(quote_as_of="2026-10-13")
    document["conclusion"]["assessed_at"] = "2026-10-13T15:30:00Z"
    document["input_snapshot"]["sources"][0]["retrieved_at"] = "2026-10-13T15:20:00Z"
    document["input_snapshot"]["sources"][1]["retrieved_at"] = "2026-10-13T15:20:00Z"
    document["input_snapshot"]["facts"][0]["observed_at"] = "2026-10-13T06:30:00Z"
    review["reviewed_at"] = "2026-10-13T15:40:00Z"
    now = datetime.fromisoformat("2026-10-13T15:45:00Z")
    result = evaluation(document, review, now=now)
    assert result.errors == ()
    assert result == evaluation(document, review, now=now.astimezone(jst.tzinfo))
    # The original same-day close claim really was unavailable at this instant.
    document["input_snapshot"]["sources"][1]["as_of"] = "2026-10-14"
    document["input_snapshot"]["facts"][0]["as_of"] = "2026-10-14"
    document["input_snapshot"]["facts"][0]["observed_at"] = "2026-10-13T15:10:00Z"
    assert (
        "market_price close date follows the available session"
        in evaluation(document, review, now=now).errors
    )


@pytest.mark.parametrize("bad", [True, "NaN", "Infinity", -1])
def test_decimal_valuation_rejects_nonfinite_bool_and_negative(bad):
    document, _ = pair_payload()
    document["valuation"]["base"]["terminal_value_per_share_yen"] = bad
    with pytest.raises(ValueError, match=r"finite|greater"):
        StockAssessmentDocument.model_validate(document)


def test_required_rate_change_requires_a_new_core_review():
    document, review = pair_payload()
    original_hash = review["reviewed_assessment_sha256"]
    document["valuation"]["required_annual_return_pct"] = "3"
    assert assessment_core_hash(StockAssessmentDocument.model_validate(document)) != original_hash
    assert (
        "Assessment Review hash does not match assessment"
        in evaluation(document, review, refresh_hash=False).errors
    )


def test_current_projection_same_day_guards_and_origin_context_are_separate():
    document, review = pair_payload()
    typed = StockAssessmentDocument.model_validate(document)
    original = assessment_core_hash(typed)
    args = dict(
        price_yen=Decimal(1000),
        price_as_of=date(2026, 10, 14),
        as_of=date(2026, 10, 14),
        basis_confirmed=True,
        max_quote_age_days=7,
    )
    projected = current_price_projection(typed, **args)
    assert projected["base"]["annualized_return_pct"] == 10
    assert projected["downside"]["annualized_return_pct"] == -30
    for changes in (
        dict(as_of=date(2026, 10, 15)),
        dict(basis_confirmed=False),
        dict(price_as_of=date(2026, 10, 6)),
        dict(price_basis="last_close_adjusted"),
        dict(price_yen=None),
    ):
        assert current_price_projection(typed, **(args | changes)) is None
    context = assessment_valuation_context(typed, evaluation(document, review))
    assert context["price_context"] == "assessment_snapshot"
    assert context["price_yen"] == 1100
    assert assessment_core_hash(typed) == original


def test_atomic_pair_retry_and_latest_unresolved_has_no_supported_fallback(connection):
    first = publish(connection)
    document, review = pair_payload(review_number=4)
    document["input_snapshot"]["facts"] = []
    document["conclusion"]["stance"] = "uncertain"
    document["valuation"] = dict(
        status="unresolved",
        market_price_fact_id=None,
        horizon_months=None,
        required_annual_return_pct=None,
        base=None,
        downside=None,
        unresolved_reason="未確認",
    )
    review["recalculated_projections"] = []
    update_review_hash(document, review)
    second = publish(
        connection, document, review, number=3, now=NOW, supersedes=first.assessment_id
    )
    assert publish(connection) == first
    assert load_latest_reviewed_assessment(connection, "1234") == second
    assert second.document.input_snapshot.facts == ()
    stored = connection.execute(
        "SELECT payload FROM stock_assessment WHERE assessment_id = ?", (first.assessment_id,)
    ).fetchone()[0]
    assert stored == canonical_json(first.document.model_dump(mode="json"))
    assert '"terminal_value_per_share_yen":"1100"' in stored
    changed, changed_review = pair_payload()
    changed_review["review_scope"] = "異なる確認範囲"
    with pytest.raises(ResearchConflictError):
        publish(connection, changed, changed_review)


def test_exact_reference_checks_all_three_values(connection):
    first = publish(connection)
    reference = AssessmentRef(
        assessment_id=first.assessment_id,
        core_sha256=first.core_sha256,
        review_id=first.review.review_id,
    )
    assert require_assessment_ref(connection, reference) == first
    for changed in (
        dict(core_sha256="0" * 64),
        dict(review_id=review_id(99)),
        dict(assessment_id=assessment_id(99)),
    ):
        with pytest.raises(ResearchValidationError):
            require_assessment_ref(connection, reference.model_copy(update=changed))


def test_caller_transaction_and_insert_failure_leave_no_partial_pair(connection):
    document, review = pair_payload()
    with pytest.raises(ResearchValidationError, match="existing transaction"):
        publish_reviewed_assessment(connection, assessment_id(), document, review, published_at=NOW)
    connection.execute(
        "CREATE TRIGGER fail_review BEFORE INSERT ON stock_assessment_review BEGIN SELECT RAISE(ABORT,'review insert failed'); END"
    )
    connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError, match="review insert failed"):
        publish(connection, document, review)
    assert connection.in_transaction
    assert connection.execute("SELECT count(*) FROM stock_assessment").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM stock_assessment_review").fetchone()[0] == 0
    connection.commit()
    assert not connection.in_transaction


def test_historical_read_uses_publication_time_and_preserves_immutable_payload(connection):
    pair = publish(connection)
    assert load_reviewed_assessment(connection, pair.assessment_id) == pair
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE stock_assessment SET payload = '{}' WHERE assessment_id = ?",
            (pair.assessment_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE stock_assessment_review SET payload = '{}' WHERE review_id = ?",
            (pair.review.review_id,),
        )


def test_latest_is_selected_before_corrupt_content_is_checked(connection):
    first = publish(connection)
    connection.execute(
        "INSERT INTO stock_assessment VALUES(?,?,?,?,?,?,?,?)",
        (
            assessment_id(10),
            "1234",
            "2026-10-15",
            "uncertain",
            "2026-10-15T16:06:00+09:00",
            first.assessment_id,
            "{}",
            "0" * 64,
        ),
    )
    assert latest_assessment_id(connection, "1234") == assessment_id(10)
    with pytest.raises(ResearchValidationError):
        load_latest_reviewed_assessment(connection, "1234")


def test_revision_order_uses_absolute_publication_instant(connection):
    first = publish(connection)
    document, review = pair_payload(review_number=4)
    later_utc = datetime.fromisoformat("2026-10-14T07:07:00Z")
    second = publish(
        connection, document, review, number=3, now=later_utc, supersedes=first.assessment_id
    )
    assert load_latest_reviewed_assessment(connection, "1234") == second
    document, review = pair_payload(review_number=6)
    with pytest.raises(ResearchConflictError, match="backwards"):
        publish(connection, document, review, number=5, now=NOW, supersedes=second.assessment_id)


@pytest.mark.parametrize("column", ["ticker", "as_of", "stance", "core_sha256", "reviewed_at"])
def test_reader_rejects_denormalized_identity_column_mismatch(connection, column):
    pair = publish(connection)
    if column == "reviewed_at":
        connection.execute("DROP TRIGGER stock_assessment_review_immutable")
        connection.execute(
            "UPDATE stock_assessment_review SET reviewed_at = '2026-10-14T16:04:00+09:00'"
        )
    else:
        connection.execute("DROP TRIGGER stock_assessment_immutable")
        changed = {
            "ticker": "5678",
            "as_of": "2026-10-13",
            "stance": "uncertain",
            "core_sha256": "0" * 64,
        }[column]
        connection.execute(f"UPDATE stock_assessment SET {column} = ?", (changed,))
    with pytest.raises(ResearchValidationError, match="identity differs"):
        load_reviewed_assessment(connection, pair.assessment_id)


def test_publication_retry_canonicalizes_decimal_input_through_typed_model(connection):
    document, review = pair_payload()
    pair = publish(connection, document, review)
    for name in ("base", "downside"):
        document["valuation"][name]["terminal_value_per_share_yen"] = int(
            document["valuation"][name]["terminal_value_per_share_yen"]
        )
        document["valuation"][name]["cash_distribution_per_share_yen"] = 0
    document["valuation"]["required_annual_return_pct"] = 10
    for recalculation in review["recalculated_projections"]:
        recalculation["terminal_value_per_share_yen"] = int(
            recalculation["terminal_value_per_share_yen"]
        )
        recalculation["cash_distribution_per_share_yen"] = 0
    assert publish(connection, document, review) == pair


def test_revision_requires_exact_latest_ticker_reference(connection):
    first = publish(connection)
    document, review = pair_payload(review_number=4)
    with pytest.raises(ResearchConflictError, match="supersede latest"):
        publish(connection, document, review, number=3)
    other, other_review = pair_payload(ticker="5678", review_number=6)
    with pytest.raises(ResearchConflictError, match="supersede latest"):
        publish(connection, other, other_review, number=5, supersedes=first.assessment_id)
    assert connection.execute("SELECT count(*) FROM stock_assessment").fetchone()[0] == 1


def test_recorded_identity_does_not_replace_document_hash():
    document, review = pair_payload()
    result = evaluate_assessment(
        StockAssessmentDocument.model_validate(document),
        review=AssessmentReview.model_validate(review),
        identity="0" * 64,
        now=NOW,
    )
    assert "recorded Assessment hash does not match assessment" in result.errors


def test_publication_requires_formal_assessment_and_review_namespaces(connection):
    document, review = pair_payload()
    connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(ValueError, match="pattern"):
        publish_reviewed_assessment(connection, "thesis-1", document, review, published_at=NOW)
    review["review_id"] = "review-1"
    with pytest.raises(ValueError, match="pattern"):
        publish(connection, document, review)
    assert connection.execute("SELECT count(*) FROM stock_assessment").fetchone()[0] == 0


def test_service_owns_and_commits_short_transaction_with_current_schema(tmp_path):
    from baibai_engine.research.assessment_store import AssessmentStoreService
    from baibai_engine.storage.application.read import connect_read_only
    from baibai_engine.storage.application.write import initialize_database

    db = tmp_path / "app.sqlite"
    initialize_database(db)
    document, review = pair_payload()
    service = AssessmentStoreService(db, clock=lambda: NOW)
    pair = service.publish_reviewed_assessment(assessment_id(), document, review)
    with closing(connect_read_only(db)) as db_connection, db_connection:
        assert load_reviewed_assessment(db_connection, pair.assessment_id) == pair
    assert service.publish_reviewed_assessment(assessment_id(), document, review) == pair


@pytest.mark.parametrize("field", ["schema_version", "snapshot_version"])
@pytest.mark.parametrize("bad", [True, 1.0, "1"])
def test_version_headers_require_integers_even_when_equal_to_literal_one(field, bad):
    document, _ = pair_payload()
    target = document if field == "schema_version" else document["input_snapshot"]
    target[field] = bad
    with pytest.raises(ValueError, match=f"{field} must be an integer"):
        StockAssessmentDocument.model_validate(document)


def test_microsecond_publication_order_precedes_assessment_id_order(connection):
    first_time = datetime.fromisoformat("2026-10-14T16:06:00.000100+09:00")
    second_time = datetime.fromisoformat("2026-10-14T16:06:00.000400+09:00")
    first = publish(connection, number=8, now=first_time)
    second = publish(connection, number=1, now=second_time, supersedes=first.assessment_id)
    assert first.assessment_id > second.assessment_id
    assert latest_assessment_id(connection, "1234") == second.assessment_id
    assert load_latest_reviewed_assessment(connection, "1234") == second
    stored_times = dict(
        connection.execute("SELECT assessment_id,published_at FROM stock_assessment")
    )
    assert stored_times == {
        first.assessment_id: first_time.isoformat(),
        second.assessment_id: second_time.isoformat(),
    }
    third = publish(
        connection,
        number=3,
        now=datetime.fromisoformat("2026-10-14T07:06:00.000500Z"),
        supersedes=second.assessment_id,
    )
    assert latest_assessment_id(connection, "1234") == third.assessment_id


def test_equal_microsecond_instant_with_different_offsets_uses_id_tiebreak(connection):
    jst = datetime.fromisoformat("2026-10-14T16:06:00.123456+09:00")
    utc = datetime.fromisoformat("2026-10-14T07:06:00.123456Z")
    assert jst == utc
    first = publish(connection, number=1, now=jst)
    second = publish(connection, number=3, now=utc, supersedes=first.assessment_id)
    assert latest_assessment_id(connection, "1234") == second.assessment_id
    assert load_latest_reviewed_assessment(connection, "1234") == second
    document, review = pair_payload(review_number=5)
    with pytest.raises(ResearchConflictError, match="advance the latest revision order"):
        publish(connection, document, review, number=2, now=jst, supersedes=second.assessment_id)
    stored_times = dict(
        connection.execute("SELECT assessment_id,published_at FROM stock_assessment")
    )
    assert stored_times == {
        first.assessment_id: jst.isoformat(),
        second.assessment_id: utc.isoformat(),
    }


def test_invalid_same_day_publication_header_does_not_hide_behind_valid_revision(connection):
    publish(connection)
    connection.execute(
        "INSERT INTO stock_assessment VALUES(?,?,?,?,?,?,?,?)",
        (
            assessment_id(99),
            "1234",
            "2026-10-14",
            "uncertain",
            "2026-10-14T16:07:00",
            None,
            "{}",
            "0" * 64,
        ),
    )
    with pytest.raises(ResearchValidationError, match="invalid Assessment revision header"):
        latest_assessment_id(connection, "1234")


def test_material_unknown_and_downside_loss_can_reach_buy_terms(connection):
    document, review = pair_payload()
    risk = document["permanent_loss_risks"][4]
    risk.update(
        assessment="unknown",
        evidence_status="partially_verified",
        summary="合成例。顧客別採算は非開示で、更新後の減益幅は重要な未知。",
    )
    document["findings"]["unresolved_questions"] = ["更新後の顧客別採算"]
    review["uncertainty_acceptance_reason"] = (
        "合成例。確認した契約総額と更新実績を中心に継続を評価する。"
        "1000円でBase年率10%、顧客縮小時は30%損失の経路。"
        "採算の未知は重要なままだが、継続実績と価格差を理由に引き受ける。"
    )
    update_review_hash(document, review)
    pair = publish(connection, document, review)
    result = evaluate_buy_terms(
        pair.document,
        reviewed=True,
        latest=True,
        as_of=date(2026, 10, 14),
        quote=QuoteInput(
            ticker="1234",
            price_yen=Decimal(1100),
            price_as_of=date(2026, 10, 14),
            price_basis="last_close_unadjusted",
            source_ref="fixture:daily_bars",
        ),
        proposed_limit_yen=Decimal(1000),
        basis_confirmed=True,
        market_price_max_age_days=7,
        held_quantity=0,
        active_reservation=False,
        proposal_executed=False,
        available_cash_yen=Decimal(100000),
        budget_max_yen=Decimal(100000),
        board_lot=100,
    )
    assert pair.evaluation.assessment_status == "ready_with_warnings"
    assert pair.document.permanent_loss_risks[4].assessment == "unknown"
    assert pair.document.permanent_loss_risks[4].evidence_status == "partially_verified"
    assert result.hard_blockers == ()
    assert result.quantity == 100
    assert result.maximum_price_yen == Decimal(1000)
    assert result.current_price_above_maximum is True
    assert pair.document.valuation.downside.terminal_value_per_share_yen == Decimal(700)
    # Acceptance prose cannot repair an unevaluated case or unverified primary observations.
    document["investment_case"]["status"] = "uncertain"
    assert "uncertain investment case cannot be supported" in evaluation(document, review).errors
    document["investment_case"]["status"] = "intact"
    review["primary_source_check"] = "partially_verified"
    assert (
        "supported requires verified material primary evidence"
        in evaluation(document, review).errors
    )


def test_legacy_review_read_and_retry_preserve_stored_payload(connection):
    document, review = pair_payload()
    review["uncertainty_acceptance_reason"] = "細目は非重要とした当時の説明"
    update_review_hash(document, review)
    typed = StockAssessmentDocument.model_validate(document)
    typed_review = AssessmentReview.model_validate(review)
    legacy = typed_review.model_dump(mode="json")
    legacy["nonmaterial_unknown_reason"] = legacy.pop("uncertainty_acceptance_reason")
    legacy_json = canonical_json(legacy)
    # Seed the historical representation directly; published rows remain immutable.
    connection.execute(
        "INSERT INTO stock_assessment VALUES(?,?,?,?,?,?,?,?)",
        (
            assessment_id(),
            "1234",
            "2026-10-14",
            "supported",
            NOW.isoformat(),
            None,
            canonical_json(typed.model_dump(mode="json")),
            assessment_core_hash(typed),
        ),
    )
    connection.execute(
        "INSERT INTO stock_assessment_review VALUES(?,?,?,?)",
        (
            typed_review.review_id,
            assessment_id(),
            typed_review.reviewed_at.isoformat(),
            legacy_json,
        ),
    )
    pair = load_reviewed_assessment(connection, assessment_id())
    assert pair.document == typed
    assert pair.review == typed_review
    assert publish(connection, document, legacy) == pair
    assert publish(connection, document, review) == pair
    assert (
        connection.execute(
            "SELECT payload FROM stock_assessment_review WHERE review_id = ?",
            (pair.review.review_id,),
        ).fetchone()[0]
        == legacy_json
    )
    assert load_reviewed_assessment(connection, pair.assessment_id).core_sha256 == pair.core_sha256
    changed = dict(review, uncertainty_acceptance_reason="後から異なるリスクを受容")
    with pytest.raises(ResearchConflictError):
        publish(connection, document, changed)
