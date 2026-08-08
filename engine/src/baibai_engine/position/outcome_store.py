"""Immutable application-DB publications for portfolio outcomes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, model_validator

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.market.jpx_total_return import BenchmarkObservation


class PortfolioOutcomeConflictError(ValueError):
    """An outcome ID already names different immutable content."""


@dataclass(frozen=True, slots=True)
class PortfolioOutcomePublication:
    outcome_id: str
    horizon: str
    period_start_date: str
    period_end_date: str
    status: str
    payload: Mapping[str, object]


type OutcomeNumber = StrictInt | StrictFloat


class PortfolioOutcomePayload(BaseModel):
    """Strict canonical payload validated again at the DB publication boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    kind: Literal["portfolio_outcome"]
    status: Literal["resolved", "unresolved"]
    reason: (
        Literal[
            "activation_pending",
            "insufficient_portfolio_history",
            "benchmark_unavailable",
            "basis_mismatch",
            "missing_market_price",
            "corporate_action_unresolved",
            "zero_or_negative_nav",
        ]
        | None
    )
    horizon: Literal["1y", "3y", "5y"]
    period_start_date: date
    period_end_date: date
    benchmark_id: Literal["jpx-topix-gross-total-return"]
    market_data_ref: Annotated[str, Field(min_length=1)]
    market_data_sha256: str | None
    market_data_coverage_start_date: date
    market_data_coverage_end_date: date
    market_data_fingerprint: str | None
    portfolio_twr_pct: OutcomeNumber | None = None
    benchmark_cumulative_return_pct: OutcomeNumber | None = None
    excess_percentage_points: OutcomeNumber | None = None
    portfolio_annualized_return_pct: OutcomeNumber | None = None
    benchmark_annualized_return_pct: OutcomeNumber | None = None
    ending_cash_yen: StrictInt | None = None
    ending_reserved_cash_yen: StrictInt | None = None
    ending_holdings_market_value_yen: StrictInt | None = None
    confirmed_income_yen: StrictInt | None = None
    confirmed_cost_yen: StrictInt | None = None
    confirmed_tax_yen: StrictInt | None = None
    estimated_exit_tax_yen: StrictInt | None = None
    estimated_exit_tax_status: Literal["estimated", "unknown"] | None = None
    open_tickers: tuple[Annotated[str, Field(pattern=r"^[0-9A-Z]{4}$")], ...] | None = None
    closed_tickers: tuple[Annotated[str, Field(pattern=r"^[0-9A-Z]{4}$")], ...] | None = None
    benchmark_observation: BenchmarkObservation

    @model_validator(mode="after")
    def validate_publication_contract(self) -> Self:
        if self.period_end_date < self.period_start_date:
            raise ValueError("outcome period_end_date must not predate period_start_date")
        benchmark = self.benchmark_observation
        for field, actual, expected in (
            ("horizon", self.horizon, benchmark.horizon),
            ("period_start_date", self.period_start_date, benchmark.period_start_date),
            ("period_end_date", self.period_end_date, benchmark.period_end_date),
            ("benchmark_id", self.benchmark_id, benchmark.benchmark_id),
            (
                "market_data_coverage_start_date",
                self.market_data_coverage_start_date,
                self.period_start_date,
            ),
            (
                "market_data_coverage_end_date",
                self.market_data_coverage_end_date,
                self.period_end_date,
            ),
        ):
            if actual != expected:
                raise ValueError(f"outcome {field} does not match its canonical source")
        if self.status == "unresolved":
            if self.reason is None:
                raise ValueError("unresolved outcome requires a reason")
            return self
        if self.reason is not None:
            raise ValueError("resolved outcome reason must be null")
        required = {
            "portfolio_twr_pct": self.portfolio_twr_pct,
            "benchmark_cumulative_return_pct": self.benchmark_cumulative_return_pct,
            "excess_percentage_points": self.excess_percentage_points,
            "portfolio_annualized_return_pct": self.portfolio_annualized_return_pct,
            "ending_cash_yen": self.ending_cash_yen,
            "ending_reserved_cash_yen": self.ending_reserved_cash_yen,
            "ending_holdings_market_value_yen": self.ending_holdings_market_value_yen,
            "confirmed_income_yen": self.confirmed_income_yen,
            "confirmed_cost_yen": self.confirmed_cost_yen,
            "confirmed_tax_yen": self.confirmed_tax_yen,
            "estimated_exit_tax_yen": self.estimated_exit_tax_yen,
            "estimated_exit_tax_status": self.estimated_exit_tax_status,
            "open_tickers": self.open_tickers,
            "closed_tickers": self.closed_tickers,
            "market_data_sha256": self.market_data_sha256,
            "market_data_fingerprint": self.market_data_fingerprint,
        }
        missing = sorted(field for field, value in required.items() if value is None)
        if missing:
            raise ValueError(f"resolved outcome fields are required: {', '.join(missing)}")
        if self.benchmark_cumulative_return_pct != benchmark.cumulative_return_pct:
            raise ValueError("outcome benchmark return does not match benchmark observation")
        assert self.portfolio_twr_pct is not None
        assert self.benchmark_cumulative_return_pct is not None
        assert self.excess_percentage_points is not None
        if (
            abs(
                (float(self.portfolio_twr_pct) - float(self.benchmark_cumulative_return_pct))
                - float(self.excess_percentage_points)
            )
            > 1e-9
        ):
            raise ValueError("excess_percentage_points does not equal portfolio minus benchmark")
        return self


class PortfolioOutcomeStore:
    """Publish and query immutable outcome records."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def publish(self, publication: PortfolioOutcomePublication) -> bool:
        _validate(publication)
        encoded = canonical_json(publication.payload)
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT horizon, period_start_date, period_end_date, status, payload "
                    "FROM portfolio_outcome WHERE outcome_id = ?",
                    (publication.outcome_id,),
                ).fetchone()
                expected = (
                    publication.horizon,
                    publication.period_start_date,
                    publication.period_end_date,
                    publication.status,
                    encoded,
                )
                if existing is not None:
                    if tuple(existing) != expected:
                        raise PortfolioOutcomeConflictError(
                            f"outcome_id has different content: {publication.outcome_id}"
                        )
                    connection.commit()
                    return False
                connection.execute(
                    "INSERT INTO portfolio_outcome "
                    "(outcome_id, horizon, period_start_date, period_end_date, status, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (publication.outcome_id, *expected),
                )
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise

    def list(self) -> tuple[Mapping[str, object], ...]:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            rows = connection.execute(
                "SELECT outcome_id, payload FROM portfolio_outcome "
                "ORDER BY period_end_date DESC, outcome_id DESC"
            ).fetchall()
        return tuple(
            {"outcome_id": str(row["outcome_id"]), **json.loads(str(row["payload"]))}
            for row in rows
        )


def _validate(publication: PortfolioOutcomePublication) -> None:
    payload = PortfolioOutcomePayload.model_validate(publication.payload)
    required = {
        "kind": "portfolio_outcome",
        "horizon": publication.horizon,
        "period_start_date": publication.period_start_date,
        "period_end_date": publication.period_end_date,
        "status": publication.status,
    }
    for field, expected in required.items():
        actual = getattr(payload, field)
        if isinstance(actual, date):
            actual = actual.isoformat()
        if actual != expected:
            raise ValueError(f"outcome payload {field} does not match publication")
