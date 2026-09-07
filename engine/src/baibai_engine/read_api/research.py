"""Query-only research revision views."""

from __future__ import annotations

import json
from pathlib import Path

from .sqlite import read_application_rows as read_rows


def list_thesis_payloads(
    path: Path,
    *,
    ticker: str | None = None,
) -> list[dict[str, object]]:
    return _many(
        path,
        "thesis",
        where=None if ticker is None else ("ticker = ?", (ticker,)),
        order="as_of DESC, published_at DESC, thesis_id DESC",
    )


def list_position_review_payloads(
    path: Path,
    *,
    ticker: str | None = None,
) -> list[dict[str, object]]:
    return _many(
        path,
        "position_review",
        where=None if ticker is None else ("ticker = ?", (ticker,)),
        order="as_of DESC, position_review_id DESC",
    )


def thesis_publication(
    path: Path,
    *,
    thesis_id: str,
) -> dict[str, object] | None:
    rows = _publications(
        path,
        "thesis",
        (
            "thesis_id",
            "ticker",
            "as_of",
            "recommendation",
            "published_at",
            "supersedes_id",
            "core_sha256",
        ),
        where=("thesis_id = ?", (thesis_id,)),
        order="thesis_id",
    )
    return None if not rows else rows[0]


def list_thesis_publications(
    path: Path,
    *,
    ticker: str | None = None,
) -> list[dict[str, object]]:
    return _publications(
        path,
        "thesis",
        (
            "thesis_id",
            "ticker",
            "as_of",
            "recommendation",
            "published_at",
            "supersedes_id",
            "core_sha256",
        ),
        where=None if ticker is None else ("ticker = ?", (ticker,)),
        order="as_of DESC, published_at DESC, thesis_id DESC",
    )


def list_thesis_review_publications(
    path: Path,
    *,
    thesis_id: str | None = None,
) -> list[dict[str, object]]:
    return _publications(
        path,
        "thesis_review",
        ("review_id", "thesis_id", "reviewed_at"),
        where=None if thesis_id is None else ("thesis_id = ?", (thesis_id,)),
        order="reviewed_at DESC, review_id DESC",
    )


def list_position_review_publications(
    path: Path,
    *,
    ticker: str | None = None,
) -> list[dict[str, object]]:
    return _publications(
        path,
        "position_review",
        ("position_review_id", "ticker", "as_of", "thesis_id", "candidate_thesis_id"),
        where=None if ticker is None else ("ticker = ?", (ticker,)),
        order="as_of DESC, position_review_id DESC",
    )


def _many(
    path: Path,
    table: str,
    *,
    where: tuple[str, tuple[object, ...]] | None,
    order: str,
) -> list[dict[str, object]]:
    clause, parameters = ("", ()) if where is None else (f" WHERE {where[0]}", where[1])
    rows = read_rows(
        path,
        # Private callers provide fixed schema fragments; all values stay bound.
        f"SELECT payload FROM {table}{clause} ORDER BY {order}",  # nosec B608
        parameters,
    )
    return [_object(str(row[0])) for row in rows]


def _object(payload: str) -> dict[str, object]:
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("research payload must be an object")
    return parsed


def _publications(
    path: Path,
    table: str,
    columns: tuple[str, ...],
    *,
    where: tuple[str, tuple[object, ...]] | None,
    order: str,
) -> list[dict[str, object]]:
    clause, parameters = ("", ()) if where is None else (f" WHERE {where[0]}", where[1])
    selected = ", ".join((*columns, "payload"))
    rows = read_rows(
        path,
        # Private callers provide fixed schema fragments; all values stay bound.
        f"SELECT {selected} FROM {table}{clause} ORDER BY {order}",  # nosec B608
        parameters,
    )
    return [
        {**{column: row[column] for column in columns}, "payload": _object(str(row["payload"]))}
        for row in rows
    ]


__all__ = [
    "list_position_review_payloads",
    "list_position_review_publications",
    "list_thesis_payloads",
    "list_thesis_publications",
    "list_thesis_review_publications",
    "thesis_publication",
]


def reviewed_thesis_projection(path: Path, *, thesis_id: str) -> dict[str, object]:
    """Expose the exact pair and conditional values, or a historical-only raw record."""
    from contextlib import closing
    from decimal import Decimal

    from baibai_engine.appdb.read import connect_read_only
    from baibai_engine.research.thesis_store import ResearchValidationError, load_reviewed_thesis
    from baibai_engine.research.valuation import maximum_entry_price, project_return

    publication = thesis_publication(path, thesis_id=thesis_id)
    if publication is None:
        return {}
    try:
        with closing(connect_read_only(path)) as connection:
            pair = load_reviewed_thesis(connection, thesis_id)
    except ResearchValidationError:
        return {
            "status": "requires_reassessment",
            "thesis_id": thesis_id,
            "as_of": publication["as_of"],
            "raw": publication["payload"],
        }
    thesis = pair.document
    valuation = thesis.valuation
    price_fact = next(
        (
            fact
            for fact in thesis.input_snapshot.facts
            if fact.fact_id == valuation.market_price_fact_id
        ),
        None,
    )
    projected: dict[str, object] = {}
    maximum: float | None = None
    if valuation.horizon_months is not None and price_fact is not None:
        price = Decimal(str(price_fact.value))
        for name, scenario in (("base", valuation.base), ("downside", valuation.downside)):
            if scenario is not None:
                result = project_return(
                    scenario, price_yen=price, horizon_months=valuation.horizon_months
                )
                projected[name] = {
                    **scenario.model_dump(mode="json"),
                    "total_return_pct": round(float(result.total_return_pct), 4),
                    "annualized_return_pct": round(float(result.annualized_return_pct), 4),
                }
        if valuation.base is not None and valuation.required_annual_return_pct is not None:
            maximum = float(
                maximum_entry_price(
                    valuation.base,
                    horizon_months=valuation.horizon_months,
                    required_annual_return_pct=valuation.required_annual_return_pct,
                )
            )
    return {
        "status": valuation.status,
        "thesis_id": thesis_id,
        "review_id": pair.review.review_id,
        "as_of": thesis.input_snapshot.as_of.isoformat(),
        "disposition": thesis.judgment.disposition,
        "investment_case": thesis.investment_case.model_dump(mode="json"),
        "horizon_months": valuation.horizon_months,
        "unresolved_reason": valuation.unresolved_reason,
        "strongest_countercase": thesis.judgment.strongest_countercase,
        "original_price_basis": None if price_fact is None else price_fact.price_basis,
        "required_annual_return_pct": None
        if valuation.required_annual_return_pct is None
        else float(valuation.required_annual_return_pct),
        "pmax_raw_yen": maximum,
        "projections": projected,
        "original_price_yen": None if price_fact is None else price_fact.value,
        "original_quote_at": None
        if price_fact is None or price_fact.observed_at is None
        else price_fact.observed_at.isoformat(),
        "return_basis": "条件付き・原評価起点・税費用控除前・分配再投資なし",
        "raw": thesis.model_dump(mode="json"),
    }
