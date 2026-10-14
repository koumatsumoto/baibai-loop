"""保存された銘柄評価と独立 Review を原評価の条件付き算術と共に読む。"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
from pathlib import Path

from baibai_engine.research.assessment import AssessmentRef
from baibai_engine.research.assessment_store import load_reviewed_assessment, require_assessment_ref
from baibai_engine.research.decimal_number import decimal_to_number
from baibai_engine.storage.application.read import connect_read_only
from baibai_engine.valuation.scenario import finite_decimal, maximum_entry_price, project_return

from .sqlite import read_application_rows as read_rows


def list_stock_assessment_publications(
    path: Path, *, ticker: str | None = None
) -> list[dict[str, object]]:
    rows = read_rows(
        path,
        "SELECT assessment_id,ticker,as_of,stance,published_at,supersedes_id,payload,core_sha256 "
        "FROM stock_assessment WHERE (? IS NULL OR ticker=?)",
        (ticker, ticker),
    )
    if not rows:
        return []
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        publications = []
        for row in rows:
            pair = load_reviewed_assessment(connection, str(row["assessment_id"]))
            publications.append({**dict(row), "payload": pair.document.model_dump(mode="json")})
    return sorted(
        publications,
        key=lambda row: (
            str(row["as_of"]),
            datetime.fromisoformat(str(row["published_at"])),
            str(row["assessment_id"]),
        ),
        reverse=True,
    )


def stock_assessment_publication(path: Path, *, assessment_id: str) -> dict[str, object] | None:
    rows = read_rows(
        path,
        "SELECT assessment_id,ticker,as_of,stance,published_at,supersedes_id,payload,core_sha256 "
        "FROM stock_assessment WHERE assessment_id=?",
        (assessment_id,),
    )
    if not rows:
        return None
    with closing(connect_read_only(path)) as connection:
        pair = load_reviewed_assessment(connection, assessment_id)
    return {**dict(rows[0]), "payload": pair.document.model_dump(mode="json")}


def list_assessment_review_publications(
    path: Path, *, assessment_id: str | None = None
) -> list[dict[str, object]]:
    rows = read_rows(
        path,
        "SELECT review_id,assessment_id,reviewed_at,payload FROM stock_assessment_review "
        "WHERE (? IS NULL OR assessment_id=?)",
        (assessment_id, assessment_id),
    )
    if not rows:
        return []
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        publications = []
        for row in rows:
            pair = load_reviewed_assessment(connection, str(row["assessment_id"]))
            publications.append({**dict(row), "payload": pair.review.model_dump(mode="json")})
    return sorted(
        publications,
        key=lambda row: (datetime.fromisoformat(str(row["reviewed_at"])), str(row["review_id"])),
        reverse=True,
    )


def reviewed_assessment_projection(
    path: Path, *, assessment_id: str, assessment_ref: AssessmentRef | None = None
) -> dict[str, object]:
    """正確な保存pairの原価格算術だけを表示し、現在の資格・価格を混ぜない。"""
    publication = stock_assessment_publication(path, assessment_id=assessment_id)
    if publication is None and assessment_ref is None:
        return {}
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        pair = (
            load_reviewed_assessment(connection, assessment_id)
            if assessment_ref is None
            else require_assessment_ref(connection, assessment_ref)
        )
        if pair.assessment_id != assessment_id:
            raise ValueError("assessment reference identity differs")
    document = pair.document
    valuation = document.valuation
    price_fact = next(
        (
            fact
            for fact in document.input_snapshot.facts
            if fact.fact_id == valuation.market_price_fact_id
        ),
        None,
    )
    projections: dict[str, object] = {}
    maximum: float | int | None = None
    if valuation.horizon_months is not None and price_fact is not None:
        price = finite_decimal(price_fact.value)
        for name, scenario in (("base", valuation.base), ("downside", valuation.downside)):
            if scenario is not None:
                result = project_return(
                    scenario, price_yen=price, horizon_months=valuation.horizon_months
                )
                projections[name] = {
                    **scenario.model_dump(mode="json"),
                    "total_return_pct": decimal_to_number(result.total_return_pct),
                    "annualized_return_pct": decimal_to_number(result.annualized_return_pct),
                }
        if valuation.base is not None and valuation.required_annual_return_pct is not None:
            maximum = decimal_to_number(
                maximum_entry_price(
                    valuation.base,
                    horizon_months=valuation.horizon_months,
                    required_annual_return_pct=valuation.required_annual_return_pct,
                )
            )
    return {
        "status": valuation.status,
        "assessment_id": assessment_id,
        "review_id": pair.review.review_id,
        "core_sha256": pair.core_sha256,
        "as_of": document.input_snapshot.as_of.isoformat(),
        "stance": document.conclusion.stance,
        "conclusion": document.conclusion.model_dump(mode="json"),
        "investment_case": document.investment_case.model_dump(mode="json"),
        "horizon_months": valuation.horizon_months,
        "unresolved_reason": valuation.unresolved_reason,
        "strongest_countercase": document.conclusion.strongest_countercase,
        "original_price_basis": None if price_fact is None else price_fact.price_basis,
        "required_annual_return_pct": None
        if valuation.required_annual_return_pct is None
        else decimal_to_number(valuation.required_annual_return_pct),
        "pmax_original_yen": maximum,
        "projections": projections,
        "original_price_yen": None
        if price_fact is None
        else decimal_to_number(finite_decimal(price_fact.value)),
        "original_quote_at": None
        if price_fact is None or price_fact.observed_at is None
        else price_fact.observed_at.isoformat(),
        "return_basis": "条件付き・原評価起点・税費用控除前・分配再投資なし",
        "raw": document.model_dump(mode="json"),
    }
