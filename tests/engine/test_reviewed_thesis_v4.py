"""F01/F02/F03/F06/F08: valuation and atomic Reviewed Thesis publication owners."""

from copy import deepcopy
from datetime import datetime
from decimal import Decimal

import pytest
from tests.helpers.research_v4 import pair_payload

from baibai_engine.appdb.read import connect_read_only
from baibai_engine.research.thesis import ThesisDocument, thesis_core_hash
from baibai_engine.research.thesis_store import (
    ResearchConflictError,
    ResearchValidationError,
    ThesisStoreService,
    load_latest_reviewed_thesis,
)
from baibai_engine.research.valuation import (
    Projection,
    maximum_entry_price,
    project_return,
    required_total_value,
    valuation_conditions,
)

NOW = datetime.fromisoformat("2026-09-07T18:00:00+09:00")


def projection(terminal=1204, cash=30):
    return Projection.model_validate(
        {
            "terminal_value_per_share_yen": terminal,
            "cash_distribution_per_share_yen": cash,
            "calculation": "独立した財務検算",
            "source_ids": ["ir"],
        }
    )


@pytest.mark.parametrize(
    ("terminal", "cash", "price", "months", "total", "annual"),
    [
        (1204, 30, 1000, 12, "23.4", "23.4"),
        (1210, 0, 1000, 24, "21", "10"),
        (0, 0, 1000, 12, "-100", "-100"),
        (800, 200, 1000, 12, "0", "0"),
        (1000, 0, 1000, 12, "0", "0"),
        (1204, 30, 1250, 12, "-1.28", "-1.28"),
        (1450, 30, 1250, 12, "18.4", "18.4"),
    ],
)
def test_conditional_financial_examples(terminal, cash, price, months, total, annual):
    result = project_return(
        projection(terminal, cash), price_yen=Decimal(price), horizon_months=months
    )
    assert result.total_return_pct == Decimal(total)
    assert abs(result.annualized_return_pct - Decimal(annual)) < Decimal("1e-40")


def test_raw_price_boundary_and_inverse():
    ceiling = maximum_entry_price(
        projection(), horizon_months=12, required_annual_return_pct=Decimal(12)
    )
    assert Decimal(1101) < ceiling < Decimal(1102)
    assert abs(
        required_total_value(
            price_yen=ceiling, horizon_months=12, required_annual_return_pct=Decimal(12)
        )
        - 1234
    ) < Decimal("1e-40")


@pytest.mark.parametrize("bad", [True, False, float("nan"), float("inf"), "NaN", "Infinity", -1])
def test_projection_rejects_invalid_numbers(bad):
    with pytest.raises(ValueError, match=r"finite|positive|integer|greater"):
        projection(bad)


@pytest.mark.parametrize("bad", [True, 0, -1, float("inf"), Decimal("NaN")])
def test_price_and_rate_reject_invalid_numbers(bad):
    with pytest.raises(ValueError, match=r"finite|positive|integer|greater"):
        project_return(projection(), price_yen=bad, horizon_months=12)
    with pytest.raises(ValueError, match=r"finite|positive|integer|greater"):
        maximum_entry_price(projection(), horizon_months=12, required_annual_return_pct=bad)


@pytest.mark.parametrize("bad", [True, 0, -1, 1.5, float("nan")])
def test_horizon_rejects_invalid_numbers(bad):
    with pytest.raises(ValueError, match=r"finite|positive|integer|greater"):
        project_return(projection(), price_yen=Decimal(1000), horizon_months=bad)


def test_atomic_pair_retry_and_latest_unresolved(tmp_path):
    db = tmp_path / "app.sqlite"
    service = ThesisStoreService(db, clock=lambda: NOW)
    thesis, review = pair_payload()
    service.publish_reviewed_thesis("thesis-1", thesis, review)
    unresolved, second_review = deepcopy(thesis), deepcopy(review)
    unresolved["input_snapshot"]["facts"] = []
    unresolved["judgment"]["disposition"] = "defer"
    unresolved["valuation"] = dict(
        status="unresolved",
        market_price_fact_id=None,
        horizon_months=None,
        required_annual_return_pct=None,
        base=None,
        downside=None,
        unresolved_reason="価格不明",
    )
    second_review["review_id"] = "review-2"
    second_review["recalculated_projections"] = []
    second_review["reviewed_thesis_sha256"] = thesis_core_hash(
        ThesisDocument.model_validate(unresolved)
    )
    service.publish_reviewed_thesis("thesis-2", unresolved, second_review, supersedes_id="thesis-1")
    service.publish_reviewed_thesis("thesis-1", thesis, review)
    with connect_read_only(db) as connection:
        latest = load_latest_reviewed_thesis(connection, "1234")
        assert latest.thesis_id == "thesis-2"
        assert latest.document.valuation.status == "unresolved"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 20
    changed = deepcopy(review)
    changed["strongest_countercase"] = "異なる内容"
    with pytest.raises(ResearchConflictError):
        service.publish_reviewed_thesis("thesis-1", thesis, changed)


@pytest.mark.parametrize("fault", ["hash", "terminal", "cash", "source", "unit"])
def test_invalid_pair_never_publishes(tmp_path, fault):
    thesis, review = pair_payload()
    if fault == "hash":
        review["reviewed_thesis_sha256"] = "0" * 64
    elif fault in {"terminal", "cash"}:
        key = (
            "terminal_value_per_share_yen"
            if fault == "terminal"
            else "cash_distribution_per_share_yen"
        )
        review["recalculated_projections"][0][key] += 1
    elif fault == "source":
        thesis["valuation"]["base"]["source_ids"] = ["missing"]
    else:
        thesis["input_snapshot"]["facts"][0]["unit"] = "JPY"
    db = tmp_path / "app.sqlite"
    with pytest.raises(ResearchValidationError):
        ThesisStoreService(db, clock=lambda: NOW).publish_reviewed_thesis(
            "thesis-1", thesis, review
        )
    with connect_read_only(db) as connection:
        assert connection.execute("SELECT count(*) FROM thesis").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM thesis_review").fetchone()[0] == 0


def test_review_insert_failure_rolls_back_thesis_in_caller_transaction(tmp_path):
    import sqlite3

    from baibai_engine.appdb.write import connect_rw, initialize_database
    from baibai_engine.research.thesis_store import publish_reviewed_thesis

    db = tmp_path / "app.sqlite"
    initialize_database(db)
    thesis, review = pair_payload()
    with connect_rw(db) as connection:
        connection.execute(
            "CREATE TRIGGER fail_review BEFORE INSERT ON thesis_review BEGIN SELECT RAISE(ABORT, 'review insert failed'); END"
        )
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError, match="review insert failed"):
            publish_reviewed_thesis(connection, "new", thesis, review, published_at=NOW)
        assert connection.in_transaction
        assert connection.execute("SELECT count(*) FROM thesis").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM thesis_review").fetchone()[0] == 0
        connection.commit()


@pytest.mark.parametrize(
    ("terminal", "cash", "calculation", "wrong"),
    [
        (
            1204,
            30,
            "赤字回復: 売上30億×営業利益率6%、金利800万、税30%、100万株、PER10 = 1204円",
            1260,
        ),
        (1200, 0, "株主利益1.2億円×PER10÷100万株 = 1200円。負債は再控除しない", 900),
        (800, 200, "資産価値1000円のうち200円分配、分配後terminal800円", 1000),
        (1000, 0, "2分割後500円×起点1株の権利2株 = 1000円", 500),
        (1000, 0, "利益9000万円×PER10÷自己株10万を除く90万株 = 1000円", 900),
        (750, 0, "株主価値9億円÷希薄化後120万株 = 750円", 900),
    ],
)
def test_independently_checked_financial_golden_cases(tmp_path, terminal, cash, calculation, wrong):
    thesis, review = pair_payload()
    thesis["valuation"]["base"].update(
        terminal_value_per_share_yen=terminal,
        cash_distribution_per_share_yen=cash,
        calculation=calculation,
    )
    review["recalculated_projections"][0].update(
        terminal_value_per_share_yen=terminal, cash_distribution_per_share_yen=cash
    )
    review["reviewed_thesis_sha256"] = thesis_core_hash(ThesisDocument.model_validate(thesis))
    service = ThesisStoreService(tmp_path / "app.sqlite", clock=lambda: NOW)
    service.publish_reviewed_thesis("correct", thesis, review)
    thesis["valuation"]["base"]["terminal_value_per_share_yen"] = wrong
    review["review_id"] = "review-wrong"
    review["reviewed_thesis_sha256"] = thesis_core_hash(ThesisDocument.model_validate(thesis))
    # A hash match cannot replace independently checked financial arithmetic.
    with pytest.raises(ResearchValidationError, match="terminal"):
        service.publish_reviewed_thesis("wrong", thesis, review, supersedes_id="correct")


@pytest.mark.parametrize(
    ("rate", "months", "terminal", "cash", "required", "minimum", "surplus", "annual", "delayed"),
    [
        ("8.5", 12, 1270, 30, 1085, 1055, 215, "30", "14.0175"),
        ("8.5", 12, 680, 20, 1085, 1065, -385, "-30", "-16.3340"),
        ("8.5", 12, 0, 0, 1085, 1085, -1085, "-100", "-100"),
        ("8.5", 12, 0, 1100, 1085, 0, 15, "10", "4.8809"),
        ("21", 6, 1200, 10, 1100, 1090, 110, "46.41", "13.5508"),
        ("10", 24, 1170, 40, 1210, 1170, 0, "10", "6.5602"),
    ],
)
def test_price_conditions_and_fixed_value_delay(
    rate, months, terminal, cash, required, minimum, surplus, annual, delayed
):
    scenario = projection(terminal, cash)
    before = scenario.model_dump()
    result = valuation_conditions(
        scenario,
        price_yen=Decimal(1000),
        horizon_months=months,
        required_annual_return_pct=Decimal(rate),
    )
    assert abs(result.required_total_value_yen - required) < Decimal("1e-40")
    assert abs(result.required_terminal_value_per_share_yen - minimum) < Decimal("1e-40")
    assert abs(result.total_value_surplus_yen - surplus) < Decimal("1e-40")
    assert abs(result.returns.annualized_return_pct - Decimal(annual)) < Decimal("1e-40")
    assert round(result.delayed_returns.annualized_return_pct, 4) == Decimal(delayed)
    assert (
        result.returns.total_value_yen == result.delayed_returns.total_value_yen == terminal + cash
    )
    assert result.returns.total_return_pct == result.delayed_returns.total_return_pct
    assert scenario.model_dump() == before


@pytest.mark.parametrize("field", ["price_yen", "required_annual_return_pct", "horizon_months"])
@pytest.mark.parametrize("bad", [True, False, 0, -1, float("nan"), float("inf"), Decimal("NaN")])
def test_conditions_keep_existing_input_rejection(field, bad):
    inputs = dict(
        price_yen=Decimal(1000), required_annual_return_pct=Decimal(12), horizon_months=12
    )
    inputs[field] = bad
    with pytest.raises(ValueError, match=r"finite|positive|integer"):
        valuation_conditions(projection(), **inputs)


def test_conditions_keep_valuation_precision_for_small_surplus():
    result = valuation_conditions(
        projection("1085.00000000000000000000000000000000000001", 0),
        price_yen=Decimal(1000),
        horizon_months=12,
        required_annual_return_pct=Decimal("8.5"),
    )
    assert result.total_value_surplus_yen == Decimal("1e-38")
