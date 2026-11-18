"""明示した資産仮説を、同世代で実際に観測された他社のPBRと照合する。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from math import isclose, isfinite
from pathlib import Path
from statistics import median
from typing import Any

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.query import stream_generation_subjects


def _object(value: JsonValue | None) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _positive(value: JsonValue | None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if isfinite(value) and value > 0 else None


def _day(value: JsonValue | None) -> date | None:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return None


@dataclass(frozen=True, slots=True)
class _BookPrice:
    value: float
    price_date: str
    measurement_date: date
    disclosed_at: date
    method: str
    statement_basis: str
    standard: str | None

    @property
    def basis(self) -> tuple[object, ...] | None:
        return (
            (self.measurement_date, self.method, self.statement_basis, self.standard)
            if self.standard is not None
            else None
        )


def _observed(payload: dict[str, JsonValue], closing_date: str, as_of: date) -> _BookPrice | None:
    financial, quote = _object(payload.get("financial")), _object(payload.get("earnings_price"))
    book = _object(financial.get("book_equity"))
    at, measured, disclosed = (
        _day(closing_date),
        _day(book.get("measurement_date")),
        _day(book.get("disclosed_at")),
    )
    pbr, equity, capital, quoted_cap, shares, price = (
        _positive(financial.get("pbr")),
        _positive(book.get("amount_yen")),
        _positive(financial.get("market_cap")),
        _positive(quote.get("market_cap_yen")),
        _positive(quote.get("shares_ex_treasury")),
        _positive(quote.get("price_yen")),
    )
    method, basis, standard = (
        book.get("method"),
        book.get("statement_basis"),
        book.get("accounting_standard"),
    )
    # 赤字はPBRの欠測ではない。一方、異なる株価日や株数をつないで安さを補作しない。
    if (
        quote.get("price_date") != closing_date
        or payload.get("expected_price_date") != closing_date
        or at is None
        or at > as_of
        or measured is None
        or disclosed is None
        or not measured <= disclosed <= as_of
        or not isinstance(method, str)
        or not method
        or not isinstance(basis, str)
        or basis not in {"consolidated", "nonconsolidated"}
        or pbr is None
        or equity is None
        or capital is None
        or quoted_cap is None
        or price is None
        or shares is None
        or not isclose(capital, quoted_cap, rel_tol=1e-12)
        or not isclose(quoted_cap, price * shares, rel_tol=1e-12)
        or not isclose(pbr, capital / equity, rel_tol=1e-12)
    ):
        return None
    return _BookPrice(
        pbr,
        closing_date,
        measured,
        disclosed,
        method,
        basis,
        standard if isinstance(standard, str) and standard else None,
    )


@dataclass(frozen=True, slots=True)
class BookPriceComparison:
    maximum: float
    sectors: Mapping[str, str | None]
    prices: Mapping[str, _BookPrice]

    def detail(self, ticker: str) -> dict[str, Any]:
        own = self.prices[ticker]
        sector = self.sectors[ticker]

        def comparison(sector_only: bool) -> dict[str, Any]:
            others = [
                t
                for t, s in self.sectors.items()
                if t != ticker and (not sector_only or s == sector)
            ]
            dated = [
                p for t in others if (p := self.prices.get(t)) and p.price_date == own.price_date
            ]
            exact = [p.value for p in dated if own.basis is not None and p.basis == own.basis]
            return {
                "other_companies": len(others),
                "same_price_date": {
                    "count": len(dated),
                    "median_pbr": median(p.value for p in dated) if dated else None,
                },
                "same_book_basis": (
                    {"count": len(exact), "median_pbr": median(exact) if exact else None}
                    if own.basis is not None
                    else None
                ),
            }

        return {
            "reported_pbr": own.value,
            "price_date": own.price_date,
            "book_measurement_date": own.measurement_date.isoformat(),
            "book_disclosed_at": own.disclosed_at.isoformat(),
            "book_method": own.method,
            "statement_basis": own.statement_basis,
            "accounting_standard": own.standard,
            "market": comparison(False),
            "sector": comparison(True) if sector else None,
        }


def read_book_prices(
    *,
    maximum: float,
    generation_id: str,
    as_of: date,
    listings: dict[str, tuple[str | None, str | None]],
    path: Path,
) -> BookPriceComparison:
    sectors = {t: s for t, (s, _) in listings.items()}
    stocks = tuple(t for t, (_, day) in listings.items() if day is not None)
    prices = {}
    with stream_generation_subjects(
        generation_id, subjects=stocks, model_ids=("company_financials",), path=path
    ) as saved:
        if saved is None or saved[0].as_of != as_of:
            raise ValueError("the fixed generation for the reported book comparison is unavailable")
        for ticker, (model,) in saved[1]:
            price_date = listings[ticker][1]
            assert price_date is not None
            observed = _observed(model.payload, price_date, as_of)
            if observed is not None:
                prices[ticker] = observed
    return BookPriceComparison(maximum, sectors, prices)
