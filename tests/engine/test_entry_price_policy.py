from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from tests.helpers.fixed_now import FIXED_NOW

from baibai_engine.research.entry_price_policy import (
    EntryPricePolicyError,
    maximum_acceptable_entry_price,
)
from baibai_engine.research.thesis import (
    ThesisDocument,
    ThesisEvaluation,
    UnpublishedThesis,
    evaluate_thesis,
    load_thesis,
    load_thesis_review,
)

ROOT = Path(__file__).parents[2]
THESIS = ROOT / "tests/fixtures/thesis/2331-decision.yaml"
REVIEW = ROOT / "tests/fixtures/thesis/2331-decision-review.yaml"


def _thesis() -> tuple[ThesisDocument, ThesisEvaluation]:
    document = load_thesis(THESIS)
    result = evaluate_thesis(
        document,
        review=load_thesis_review(REVIEW),
        now=FIXED_NOW,
        identity=UnpublishedThesis.DRAFT,
    )
    assert result.decision_readiness == "ready"
    return document, result


def test_max_price_is_recalculated_from_5y_base_and_required_return() -> None:
    document, _ = _thesis()

    max_price = maximum_acceptable_entry_price(document, tick_size_yen=Decimal("1"))

    assert max_price == 1083
    assert max_price != document.estimates.entry_price_basis_yen


def test_max_price_floors_to_a_legal_tick_rather_than_rounding_up() -> None:
    """Rounding up would authorise a price the thesis does not support."""

    document, _ = _thesis()

    fine = maximum_acceptable_entry_price(document, tick_size_yen=Decimal("1"))
    coarse = maximum_acceptable_entry_price(document, tick_size_yen=Decimal("100"))

    assert coarse == 1000
    assert coarse <= fine


def test_max_price_requires_a_5y_base_scenario() -> None:
    document, _ = _thesis()
    without_base = document.model_copy(
        update={
            "estimates": document.estimates.model_copy(
                update={
                    "scenarios": tuple(
                        item
                        for item in document.estimates.scenarios
                        if not (item.horizon_years == 5 and item.name == "base")
                    )
                }
            )
        }
    )

    with pytest.raises(EntryPricePolicyError, match="no 5y/base scenario"):
        maximum_acceptable_entry_price(without_base, tick_size_yen=Decimal("1"))


def test_max_price_rejects_a_tick_size_that_rounds_the_ceiling_away() -> None:
    document, _ = _thesis()

    with pytest.raises(EntryPricePolicyError, match="must remain positive"):
        maximum_acceptable_entry_price(document, tick_size_yen=Decimal("100000"))
