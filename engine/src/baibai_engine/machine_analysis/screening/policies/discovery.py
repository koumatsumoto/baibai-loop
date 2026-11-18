"""一社の正準モデル列から通常探索の観測だけを残す。旧カードは生成しない。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import cast

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel, GenerationMetadata
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.discovery_contract import (
    BookEquity,
    DiscoveryPolicy,
    DiscoveryRow,
    DiscoverySelection,
)
from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import (
    _project_economic_paths,
)
from baibai_engine.machine_analysis.screening.policies.economic_paths import CompanyCoordinates
from baibai_engine.machine_analysis.screening.policies.financial_growth import (
    GrowthDiscovery,
    read_growth_discovery,
)
from baibai_engine.machine_analysis.screening.policies.technical_discovery import (
    TechnicalDiscovery,
    evaluate_technical_strategy,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    financial_screening_lane,
)
from baibai_engine.primitives.stocks import normalize_ticker


def _object(value: JsonValue | None) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _text(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) else None


def read_discovery_observations(
    ticker: str, *, models: tuple[GeneratedModel, ...], metadata: GenerationMetadata
) -> tuple[
    TechnicalDiscovery, GrowthDiscovery, CompanyCoordinates, tuple[dict[str, JsonValue], ...]
]:
    technical = evaluate_technical_strategy(ticker, models=models, metadata=metadata)
    # 財務による独立発見をTAの合否・欠測で止めない。
    gate, path_growth = read_growth_discovery(
        ticker,
        models=tuple(
            model for model in models if model.model_id in {"company_financials", "company_growth"}
        ),
        metadata=metadata,
    )
    coordinates, input_reviews = _project_economic_paths(
        ticker,
        index={model.model_id: model for model in models},
        metadata=metadata,
        growth_read=path_growth,
    )
    return technical, gate, coordinates, input_reviews


def project_discovery_row(
    ticker: str, *, models: tuple[GeneratedModel, ...], metadata: GenerationMetadata
) -> DiscoveryRow:
    technical, gate, coordinates, reviews = read_discovery_observations(
        ticker, models=models, metadata=metadata
    )
    source = {model.model_id: model.payload for model in models}
    price = _object(source.get("market_context", {}).get("price"))
    close = price.get("close")
    amount = close if isinstance(close, (int, float)) and not isinstance(close, bool) else None
    earnings = _object(source.get("company_financials", {}).get("earnings_price"))
    financial = source.get("company_financials", {})
    book = _object(financial.get("financial")).get("book_equity")
    actions = financial.get("capital_action_evidence")
    adjustments = earnings.get("share_basis_adjustments")
    f_paths = [path for path in coordinates.paths if path.eligible]
    f_routes: list[str] = sorted({path.path_id for path in f_paths})
    g_state, t_state = gate.state, technical.state
    needs_review = (
        g_state in {"input_unresolved", "not_selected"} and not f_routes and bool(reviews)
    )
    missing = {
        "financial:" + reason
        for item in reviews
        if needs_review and isinstance(reason := item.get("reason"), str)
    }
    missing.update("growth:" + item for item in gate.missing)
    expected = technical.expected_price_date
    day = _text(price.get("resolved_date"))
    return DiscoveryRow(
        ticker=ticker,
        models_recorded=bool(models),
        name=coordinates.name,
        sector=coordinates.sector,
        screening_lane=financial_screening_lane(
            growth_state=g_state,
            technical_match=t_state == "matched",
            financial_path_match=bool(f_routes),
            financial_input_review=needs_review,
        ),
        financial_state=g_state,
        technical_state=t_state,
        matched_paths={"financial": f_routes, "growth": list(gate.matched_paths)},
        missing=sorted(missing),
        coordinates=[
            {
                "path_id": path.path_id,
                "variant": path.variant,
                "price_group": path.price_group,
                "business_group": path.business_group,
                "price_value": path.price_value,
                "business_value": path.business_value,
            }
            for path in f_paths
        ],
        price=amount,
        price_date=day,
        expected_price_date=expected,
        basis=_text(price.get("price_basis")),
        price_ready=(
            amount is not None
            and amount > 0
            and day is not None
            and day == expected
            and technical.price_ready
        ),
        book_equity=(
            cast(
                BookEquity,
                {
                    k: book[k]
                    for k in (
                        "amount_yen",
                        "method",
                        "measurement_date",
                        "statement_basis",
                        "disclosed_at",
                    )
                    if k in book
                },
            )
            if isinstance(book, dict)
            else None
        ),
        capital_action=(
            {
                "observations": len(actions) if isinstance(actions, list) else None,
                "share_basis_adjustments": len(adjustments)
                if isinstance(adjustments, list)
                else None,
            }
            if "capital_action_evidence" in financial or "share_basis_adjustments" in earnings
            else None
        ),
    )


def evaluate_discovery_rows(
    metadata: GenerationMetadata, *, tickers: Sequence[str], rows: Mapping[str, DiscoveryRow]
) -> ScreeningSelection:
    subjects = tuple(sorted(normalize_ticker(ticker) for ticker in tickers))
    if len(set(subjects)) != len(subjects) or not rows.keys() <= set(subjects):
        raise ValueError("discovery requires unique planned companies")
    if metadata.subject_plan is not None and set(subjects) != {
        ticker
        for ticker, definition in metadata.subject_plan.items()
        if definition.scope == "stock"
    }:
        raise ValueError("discovery universe differs from the saved stock plan")
    complete = tuple(
        rows[t] if t in rows else project_discovery_row(t, models=(), metadata=metadata)
        for t in subjects
    )
    if any(row["ticker"] != ticker for row, ticker in zip(complete, subjects, strict=True)):
        raise ValueError("discovery observation differs from its subject")
    days = Counter(row["price_date"] or "missing" for row in complete)
    expected = Counter(row["expected_price_date"] or "missing" for row in complete)
    valid = sum(row["price_ready"] for row in complete)
    prices: dict[str, JsonValue] = {
        "basis": "saved_market_adjusted_close_not_an_order_quote",
        "universe_count": len(complete),
        "valid_count": valid,
        "invalid_count": len(complete) - valid,
        "date_mismatch_count": sum(
            row["price_date"] is not None
            and row["expected_price_date"] is not None
            and row["price_date"] != row["expected_price_date"]
            for row in complete
        ),
        "price_dates": dict(sorted(days.items())),
        "expected_price_dates": dict(sorted(expected.items())),
    }
    return ScreeningSelection(
        metadata.generation_id,
        subjects,
        DiscoverySelection(
            DiscoveryPolicy(),
            complete,
            {
                "planned_stocks": len(complete),
                "financial_gate": dict(Counter(row["financial_state"] for row in complete)),
                "technical": dict(Counter(row["technical_state"] for row in complete)),
                "screening_lanes": dict(Counter(row["screening_lane"] for row in complete)),
            },
        ),
        prices,
    )
