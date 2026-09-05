"""Position Review application-store integration tests."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from tests.helpers.db_seed import seed_ledger
from tests.helpers.fixed_now import FIXED_NOW
from tests.helpers.ledger import load_portfolio_ledger

import baibai_engine.research.position_review_builder as position_review_builder_module
import baibai_engine.research.store as research_store_module
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.cli import main as position_main
from baibai_engine.position.drafts import apply_draft, build_event_draft
from baibai_engine.position.ledger import ContributionEvent
from baibai_engine.position.position_review import PositionReviewDocument
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.research.position_review_builder import (
    PositionReviewError,
    build_position_review_from_db,
)
from baibai_engine.research.store import ResearchStoreService
from baibai_engine.research.thesis import (
    ThesisDocument,
    ThesisIdentity,
    ThesisReview,
    thesis_core_hash,
    thesis_review_hash,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"
THESIS_ID = "thesis-20260714-2331-r1"


def _replace_ticker(value: object, ticker: str) -> object:
    if isinstance(value, dict):
        return {key: _replace_ticker(item, ticker) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_ticker(item, ticker) for item in value]
    if isinstance(value, str):
        return value.replace("2331", ticker)
    return value


def _research_payloads(
    *,
    ticker: str = "2331",
    post_close_decision: bool = False,
    expires_at: str = "2026-07-31T15:30:00+09:00",
    review_id: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    raw_thesis = safe_load((FIXTURES / "thesis/2331-decision.yaml").read_text(encoding="utf-8"))
    raw_review = safe_load(
        (FIXTURES / "thesis/2331-decision-review.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(raw_thesis, dict)
    assert isinstance(raw_review, dict)
    thesis = _replace_ticker(raw_thesis, ticker)
    review = _replace_ticker(raw_review, ticker)
    assert isinstance(thesis, dict)
    assert isinstance(review, dict)
    if review_id is not None:
        review["review_id"] = review_id
    input_snapshot = thesis["input_snapshot"]
    assert isinstance(input_snapshot, dict)
    facts = input_snapshot["facts"]
    assert isinstance(facts, list)
    assert isinstance(facts[0], dict)
    facts[0]["price_basis"] = "last_close_unadjusted"
    judgment = thesis["judgment"]
    assert isinstance(judgment, dict)
    evidence_override = thesis["human_evidence_override"]
    assert isinstance(evidence_override, dict)
    if post_close_decision:
        judgment["proposed_at"] = "2026-07-03T16:00:00+09:00"
        review["reviewed_at"] = "2026-07-03T16:30:00+09:00"
        evidence_override["approved_at"] = "2026-07-03T17:00:00+09:00"
    evidence_override["expires_at"] = expires_at
    core_hash = thesis_core_hash(ThesisDocument.model_validate(thesis))
    review["reviewed_thesis_sha256"] = core_hash
    parsed_review = ThesisReview.model_validate(review)
    evidence_override["thesis_sha256"] = core_hash
    evidence_override["review_id"] = parsed_review.review_id
    evidence_override["review_sha256"] = thesis_review_hash(parsed_review)
    return thesis, review


def _database(
    tmp_path: Path,
    *,
    post_close_decision: bool = False,
    expires_at: str = "2026-07-31T15:30:00+09:00",
) -> Path:
    db = tmp_path / "app.sqlite"
    ledger = load_portfolio_ledger(FIXTURES / "portfolio-ledger/representative.yaml")
    seed_ledger(
        db,
        ledger.model_copy(
            update={
                "as_of": datetime.fromisoformat("2026-07-03T10:00:00+09:00"),
                "market_prices": tuple(
                    price.model_copy(
                        update={
                            "price_yen": Decimal("1032"),
                            "observed_at": datetime.fromisoformat("2026-07-03T08:30:00+09:00"),
                            "source_kind": "licensed_dataset",
                            "price_basis": "unadjusted_close",
                        }
                    )
                    for price in ledger.market_prices
                ),
            }
        ),
    )
    thesis, review = _research_payloads(
        post_close_decision=post_close_decision,
        expires_at=expires_at,
    )
    ResearchStoreService(db, clock=lambda: FIXED_NOW).publish_thesis_with_review(
        THESIS_ID, thesis, review
    )
    return db


def _publish_candidate(
    db: Path,
    *,
    candidate_id: str,
    expires_at: str,
) -> str:
    thesis, review = _research_payloads(
        ticker="9999",
        expires_at=expires_at,
        review_id=f"review-{candidate_id}",
    )
    ResearchStoreService(db, clock=lambda: FIXED_NOW).publish_thesis_with_review(
        candidate_id,
        thesis,
        review,
    )
    return candidate_id


def _reprice_holding(
    db: Path, *, ticker: str, observed_at: str, price_yen: Decimal | None = None
) -> None:
    """Re-apply one holding's ledger market price, the way a new price draft does."""

    service = LedgerStoreService(db)
    document, head = service.load_with_head()
    observed = datetime.fromisoformat(observed_at)
    service.apply_document(
        expected_head=head,
        expected_document=document,
        replacement=document.model_copy(
            update={
                # The ledger refuses a price observed after as_of, and events after it;
                # a real price draft only ever moves as_of forward.
                "as_of": max(document.as_of, observed),
                "market_prices": tuple(
                    price.model_copy(
                        update={
                            "observed_at": observed,
                            **({} if price_yen is None else {"price_yen": price_yen}),
                        }
                    )
                    if price.ticker == ticker
                    else price
                    for price in document.market_prices
                ),
            }
        ),
    )


@pytest.mark.parametrize(
    ("observed_at", "price_yen", "expected"),
    [
        pytest.param(
            "2026-07-06T08:30:00+09:00",
            None,
            "thesis as_of must equal the holding market-price observation date",
            id="observation_date_moved",
        ),
        pytest.param(
            "2026-07-03T08:30:00+09:00",
            Decimal("1040"),
            "holding thesis market price does not match ledger unadjusted close",
            id="observed_price_corrected",
        ),
    ],
)
def test_position_review_build_refuses_a_thesis_the_ledger_price_no_longer_supports(
    tmp_path: Path, observed_at: str, price_yen: Decimal | None, expected: str
) -> None:
    """The canonical review is built against the ledger's own price observation.

    This is what makes a workspace whose ledger price moved already unusable, and
    therefore what the research boundary is entitled to refuse early: the thesis it
    would let the operator finish could never become a Position Review.
    """
    db = _database(tmp_path)
    head_before = LedgerStoreService(db).append_head()
    _reprice_holding(db, ticker="2331", observed_at=observed_at, price_yen=price_yen)
    # A price-only write is invisible to the append head every workspace pins.
    assert LedgerStoreService(db).append_head() == head_before

    with pytest.raises(PositionReviewError, match=expected):
        build_position_review_from_db(
            db_path=db,
            holding_thesis_id=THESIS_ID,
            position_id="2331",
            now=FIXED_NOW,
        )


def test_db_position_review_build_and_publish_recheck_canonical_revisions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = _database(tmp_path)
    document = build_position_review_from_db(
        db_path=db,
        holding_thesis_id=THESIS_ID,
        position_id="2331",
        now=FIXED_NOW,
    )

    published = ResearchStoreService(db, clock=lambda: FIXED_NOW).publish_position_review(
        "position-review-20260714-2331-db-r2",
        THESIS_ID,
        document.model_dump(mode="json"),
    )
    assert published.sources.ledger.append_head == LedgerStoreService(db).append_head()

    stale = build_position_review_from_db(
        db_path=db,
        holding_thesis_id=THESIS_ID,
        position_id="2331",
        now=FIXED_NOW,
    )
    ledger = LedgerStoreService(db)
    mutation = build_event_draft(
        ledger,
        ContributionEvent.model_validate(
            {
                "event_id": "human-contribution-after-review-draft",
                "type": "contribution",
                "occurred_at": "2026-07-04T12:00:00+09:00",
                "amount_yen": 1_000,
            }
        ),
    )
    apply_draft(ledger, mutation, human_confirmed=True)

    with pytest.raises(ValueError, match="source changed"):
        ResearchStoreService(db, clock=lambda: FIXED_NOW).publish_position_review(
            "position-review-20260714-2331-stale",
            THESIS_ID,
            stale.model_dump(mode="json"),
        )

    draft = tmp_path / "stale-position-review.yaml"
    draft.write_text(
        yaml.safe_dump(stale.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    assert (
        position_main(
            ["position-review", "--db", str(db), "--input", str(draft)],
            now=FIXED_NOW,
        )
        == 2
    )
    assert "source changed" in capsys.readouterr().err


def test_holding_publish_revalidates_inside_write_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _database(tmp_path)
    document = build_position_review_from_db(
        db_path=db,
        holding_thesis_id=THESIS_ID,
        position_id="2331",
        now=FIXED_NOW,
    )
    transaction_states: list[bool] = []
    real_validate = research_store_module._validate_position_review_scalars_in_transaction

    def recording_validate(
        review_document: PositionReviewDocument,
        *,
        connection: sqlite3.Connection,
        now: datetime,
    ) -> None:
        transaction_states.append(connection.in_transaction)
        real_validate(review_document, connection=connection, now=now)

    monkeypatch.setattr(
        research_store_module,
        "_validate_position_review_scalars_in_transaction",
        recording_validate,
    )
    ResearchStoreService(db, clock=lambda: FIXED_NOW).publish_position_review(
        "position-review-20260714-2331-transaction",
        THESIS_ID,
        document.model_dump(mode="json"),
    )

    assert transaction_states == [True]


@pytest.mark.parametrize(
    "operation_now",
    [
        datetime.fromisoformat("2026-07-03T17:01:00+09:00"),
        datetime.fromisoformat("2026-07-04T09:00:00+09:00"),
    ],
)
def test_position_review_accepts_post_close_decision_at_current_operation_time(
    tmp_path: Path,
    operation_now: datetime,
) -> None:
    db = _database(tmp_path, post_close_decision=True)

    document = build_position_review_from_db(
        db_path=db,
        holding_thesis_id=THESIS_ID,
        position_id="2331",
        now=operation_now,
    )

    assert document.thesis_health.current_5y_estimate.status == "resolved"
    assert document.valuation_review.status == "resolved"


def test_expired_holding_degrades_without_changing_invalidation_or_action(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path)
    expires_at = datetime.fromisoformat("2026-07-31T15:30:00+09:00")

    before = build_position_review_from_db(
        db_path=db,
        holding_thesis_id=THESIS_ID,
        position_id="2331",
        now=expires_at - timedelta(microseconds=1),
    )
    expired = build_position_review_from_db(
        db_path=db,
        holding_thesis_id=THESIS_ID,
        position_id="2331",
        now=expires_at,
    )
    after_expiry = build_position_review_from_db(
        db_path=db,
        holding_thesis_id=THESIS_ID,
        position_id="2331",
        now=expires_at + timedelta(seconds=1),
    )

    assert before.thesis_health.current_5y_estimate.status == "resolved"
    assert expired.thesis_health.current_5y_estimate.status == "unresolved"
    assert expired.valuation_review.status == "unresolved"
    assert expired.replacement_comparison.status == "no_candidate"
    assert expired.thesis_health.invalidation_status == before.thesis_health.invalidation_status
    assert expired.action == "hold"
    assert after_expiry.thesis_health.current_5y_estimate.status == "unresolved"
    assert after_expiry.valuation_review.status == "unresolved"
    assert after_expiry.action == "hold"
    ResearchStoreService(db, clock=lambda: expires_at).publish_position_review(
        "position-review-20260731-2331-expired",
        THESIS_ID,
        expired.model_dump(mode="json"),
    )


@pytest.mark.parametrize(
    "publish_now",
    [
        datetime.fromisoformat("2026-07-31T15:30:00+09:00"),
        datetime.fromisoformat("2026-07-31T15:30:01+09:00"),
    ],
)
def test_holding_publish_rejects_draft_that_expired_after_build(
    tmp_path: Path,
    publish_now: datetime,
) -> None:
    db = _database(tmp_path)
    expires_at = datetime.fromisoformat("2026-07-31T15:30:00+09:00")
    document = build_position_review_from_db(
        db_path=db,
        holding_thesis_id=THESIS_ID,
        position_id="2331",
        now=expires_at - timedelta(microseconds=1),
    )
    review_id = f"position-review-expired-{publish_now.timestamp()}"

    with pytest.raises(ValueError, match="load-bearing values"):
        ResearchStoreService(db, clock=lambda: publish_now).publish_position_review(
            review_id,
            THESIS_ID,
            document.model_dump(mode="json"),
        )

    with sqlite3.connect(db) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM position_review WHERE position_review_id = ?",
                (review_id,),
            ).fetchone()[0]
            == 0
        )


def test_expired_holding_cannot_enter_replacement_comparison(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path)
    holding_expiry = datetime.fromisoformat("2026-07-31T15:30:00+09:00")
    active_candidate = _publish_candidate(
        db,
        candidate_id="thesis-20260714-9999-active",
        expires_at="2026-08-02T15:30:00+09:00",
    )

    with pytest.raises(ValueError, match="expired holding thesis cannot be compared"):
        build_position_review_from_db(
            db_path=db,
            holding_thesis_id=THESIS_ID,
            replacement_thesis_id=active_candidate,
            position_id="2331",
            now=holding_expiry,
        )


@pytest.mark.parametrize(
    "holding_expires_at",
    ["2026-08-02T15:30:00+09:00", "2026-07-31T15:30:00+09:00"],
)
def test_expired_candidate_cannot_enter_replacement_comparison(
    tmp_path: Path,
    holding_expires_at: str,
) -> None:
    db = _database(tmp_path, expires_at=holding_expires_at)
    candidate_expiry = datetime.fromisoformat("2026-07-31T15:30:00+09:00")
    expired_candidate = _publish_candidate(
        db,
        candidate_id="thesis-20260714-9999-expired",
        expires_at="2026-07-31T15:30:00+09:00",
    )
    with pytest.raises(ValueError, match=r"candidate|not ready|human override"):
        build_position_review_from_db(
            db_path=db,
            holding_thesis_id=THESIS_ID,
            replacement_thesis_id=expired_candidate,
            position_id="2331",
            now=candidate_expiry,
        )


def test_position_review_rejects_naive_operation_clock_before_write(tmp_path: Path) -> None:
    db = _database(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        build_position_review_from_db(
            db_path=db,
            holding_thesis_id=THESIS_ID,
            position_id="2331",
            now=FIXED_NOW.replace(tzinfo=None),
        )


def test_holding_build_and_publish_forward_one_operation_instant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _database(tmp_path)
    operation_now = datetime.fromisoformat("2026-07-31T15:30:00+09:00")
    validation_instants: list[datetime | None] = []
    real_classify = position_review_builder_module._classify_current_thesis_eligibility

    def recording_classify(
        document: ThesisDocument,
        *,
        review: ThesisReview,
        now: datetime,
        identity: ThesisIdentity,
    ) -> object:
        validation_instants.append(now)
        return real_classify(document, review=review, now=now, identity=identity)

    monkeypatch.setattr(
        position_review_builder_module,
        "_classify_current_thesis_eligibility",
        recording_classify,
    )
    document = build_position_review_from_db(
        db_path=db,
        holding_thesis_id=THESIS_ID,
        position_id="2331",
        now=operation_now,
    )
    ResearchStoreService(db, clock=lambda: operation_now).publish_position_review(
        "position-review-20260714-2331-clock",
        THESIS_ID,
        document.model_dump(mode="json"),
    )

    assert validation_instants
    assert set(validation_instants) == {operation_now}


def test_a_fractional_fair_value_is_floored_onto_the_whole_yen_grid() -> None:
    """研究 FV は小数で出る。拒否すると documented な research 経路が保有に使えない。

    `scenario_arithmetic --required-cagr-pct` は 4 桁の小数を出し、それが
    `estimates.current_fair_value_yen` の既定の置き方である。購入側は同じ問いを
    ceiling の floor で解いている — `execution_policy.maximum_acceptable_entry_price` — ので、
    保有側もそれに揃える。切り上げは thesis が主張していない upside を作る。
    """

    assert position_review_builder_module._whole_yen(Decimal("963.3259")) == 963
    assert position_review_builder_module._whole_yen(Decimal("963.9999")) == 963
    assert position_review_builder_module._whole_yen(Decimal("1044")) == 1044


def test_a_price_that_floors_to_zero_is_refused() -> None:
    """1 円未満は whole-yen の格子に乗らない。0 を返すと下流の除算が壊れる。"""

    with pytest.raises(position_review_builder_module.PositionReviewError):
        position_review_builder_module._whole_yen(Decimal("0.4"))
