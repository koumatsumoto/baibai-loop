"""Current-state trade proposals with fail-closed planning-limit validation."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing, nullcontext
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.foundation.time import JST
from baibai_engine.position.ledger import PortfolioSnapshot, reconcile_portfolio
from baibai_engine.position.store import load_ledger_in_transaction
from baibai_engine.research.close_source import resolve_previous_business_day_close
from baibai_engine.research.execution_policy import ExecutionPolicyError, max_acceptable_price
from baibai_engine.research.opportunity import BOARD_LOT, PLANNING_TICK_SIZE_YEN
from baibai_engine.research.portfolio_exposure import (
    planned_order_cash_warnings,
    portfolio_annotations,
    portfolio_exposure,
)
from baibai_engine.research.thesis import (
    IndependentReview,
    ThesisDocument,
    evaluate_thesis,
)

ProposalStatus = Literal["pending", "approved", "deferred", "rejected"]
ProposalDecision = Literal["approve", "defer", "reject"]

_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")


class PlannedExposureRow(BaseModel):
    model_config = _MODEL_CONFIG

    key: Annotated[str, Field(min_length=1)]
    current_and_reserved_yen: Annotated[int, Field(ge=0)]
    prospective_yen: Annotated[int, Field(gt=0)]
    prospective_pct: Annotated[float, Field(ge=0)]
    warning_pct: Annotated[float, Field(gt=0, le=100)]


class PlannedPortfolioExposure(BaseModel):
    model_config = _MODEL_CONFIG

    price_as_of: date
    price_basis: Literal["last_close_unadjusted"]
    total_capital_yen: Annotated[int, Field(gt=0)]
    holding_valuation_status: Literal["same_asof_raw_close", "mixed_with_ledger_fallback"]
    ledger_fallback_tickers: tuple[str, ...]
    common_factor_empty_tickers: tuple[str, ...]
    ticker: PlannedExposureRow
    sector: PlannedExposureRow
    common_factors: tuple[PlannedExposureRow, ...]

    @field_validator("price_as_of", mode="before")
    @classmethod
    def _parse_date(cls, value: object) -> date:
        return date.fromisoformat(value) if isinstance(value, str) else cast(date, value)

    @field_validator(
        "ledger_fallback_tickers", "common_factor_empty_tickers", "common_factors", mode="before"
    )
    @classmethod
    def _parse_tuple(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class CanonicalPlannedLimit(BaseModel):
    """Load-bearing planning values retained by a canonical proposal."""

    model_config = _MODEL_CONFIG

    ticker: Annotated[str, Field(pattern=r"^[0-9A-Z]{4}$")]
    price_as_of: date
    price_basis: Literal["last_close_unadjusted"]
    close_yen: Decimal
    max_acceptable_price_yen: Decimal
    board_lot: Annotated[int, Field(gt=0)]
    budget_min_yen: Annotated[int, Field(gt=0)]
    budget_max_yen: Annotated[int, Field(gt=0)]
    portfolio_annotations: tuple[str, ...]
    source_ledger_entity: Literal["portfolio-ledger"]
    source_ledger_append_head: Annotated[int, Field(ge=0)]
    expires_at: datetime
    limit_price_yen: Decimal
    quantity: Annotated[int, Field(gt=0)]
    notional_yen: Annotated[int, Field(gt=0)]
    portfolio_exposure: PlannedPortfolioExposure
    warnings: tuple[str, ...]
    defer_reasons: tuple[()]

    @field_validator("price_as_of", mode="before")
    @classmethod
    def _parse_date(cls, value: object) -> date:
        return date.fromisoformat(value) if isinstance(value, str) else cast(date, value)

    @field_validator("expires_at", mode="before")
    @classmethod
    def _parse_datetime(cls, value: object) -> datetime:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else cast(datetime, value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return parsed

    @field_validator("portfolio_annotations", "warnings", "defer_reasons", mode="before")
    @classmethod
    def _parse_tuple(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _coherent_order(self) -> CanonicalPlannedLimit:
        if self.budget_min_yen > self.budget_max_yen:
            raise ValueError("budget_min_yen must not exceed budget_max_yen")
        if self.board_lot != BOARD_LOT or self.quantity % self.board_lot:
            raise ValueError("quantity must use the canonical board lot")
        if self.close_yen != self.limit_price_yen:
            raise ValueError("limit_price_yen must equal the raw close")
        if self.limit_price_yen > self.max_acceptable_price_yen:
            raise ValueError("limit price exceeds max acceptable price")
        if self.limit_price_yen * self.quantity != self.notional_yen:
            raise ValueError("notional_yen does not match price and quantity")
        if self.expires_at.astimezone(JST).timetz().replace(tzinfo=None) != time(15, 30):
            raise ValueError("expires_at must be the target session close")
        return self


class PlannedLimitInput(CanonicalPlannedLimit):
    """Strict ephemeral ``research plan-limit`` output accepted at the boundary."""

    status: Literal["planned_limit"]
    thesis_ref: Annotated[str, Field(min_length=1)]
    thesis_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    thesis_core_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    independent_review_ref: Annotated[str, Field(min_length=1)]
    independent_review_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    source_ref: Annotated[str, Field(min_length=1)]


class ProposalValidationError(ValueError):
    """A proposal input does not satisfy the current domain contract."""


class ProposalConflictError(ValueError):
    """A proposal decision conflicts with current canonical state."""


class ProposalNotFoundError(ValueError):
    """A requested proposal does not exist."""


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    proposal_id: str
    ticker: str
    thesis_id: str
    review_id: str
    created_at: datetime
    status: ProposalStatus
    decided_at: datetime | None
    payload: Mapping[str, object]


class ProposalStoreService:
    """Create deterministic proposals and record only human-reported current decisions."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        market_db_path: Path = Path("data/screening/market.sqlite"),
    ) -> None:
        self._db_path = db_path
        self._market_db_path = market_db_path

    def create(
        self,
        thesis_id: str,
        planned_limit: PlannedLimitInput,
        snapshot: PortfolioSnapshot,
        *,
        snapshot_append_head: int,
        created_at: datetime,
    ) -> ProposalRecord:
        """Create one pending proposal from an immutable ready thesis and current ledger."""
        _require_aware(created_at, "created_at")
        initialize_database(self._db_path)
        with (
            closing(_open_market_snapshot(self._market_db_path)) as market_connection,
            closing(connect_rw(self._db_path)) as connection,
        ):
            connection.execute("BEGIN IMMEDIATE")
            try:
                thesis, review = _ready_thesis_and_review(connection, thesis_id)
                _validate_planned_limit(
                    connection,
                    planned_limit,
                    thesis=thesis,
                    review=review,
                    snapshot=snapshot,
                    snapshot_append_head=snapshot_append_head,
                    now=created_at,
                    market_db_path=self._market_db_path,
                    market_connection=market_connection,
                    claimed_source_ref=planned_limit.source_ref,
                    claimed_thesis_core_sha256=planned_limit.thesis_core_sha256,
                )
                proposal_id = _allocate_proposal_id(
                    connection,
                    ticker=planned_limit.ticker,
                    created_at=created_at,
                )
                payload: dict[str, object] = {
                    "thesis_id": thesis_id,
                    "review_id": review.review_id,
                    "planned_limit": _canonical_plan(planned_limit).model_dump(mode="python"),
                    "execution_proposal": _execution_proposal(planned_limit),
                }
                connection.execute(
                    """
                    INSERT INTO proposal (
                        proposal_id, ticker, thesis_id, review_id, created_at,
                        status, decided_at, payload
                    ) VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?)
                    """,
                    (
                        proposal_id,
                        planned_limit.ticker,
                        thesis_id,
                        review.review_id,
                        created_at.isoformat(),
                        canonical_json(payload),
                    ),
                )
                row = _proposal_row(connection, proposal_id)
                connection.commit()
            except (ExecutionPolicyError, ValidationError) as error:
                connection.rollback()
                raise ProposalValidationError(str(error)) from error
            except BaseException:
                connection.rollback()
                raise
        return _record(row)

    def decide(
        self,
        proposal_id: str,
        decision: ProposalDecision,
        *,
        decided_at: datetime,
        snapshot: PortfolioSnapshot | None = None,
        snapshot_append_head: int | None = None,
    ) -> ProposalRecord:
        """Record a human report, revalidating an approval against current DB state."""
        _require_aware(decided_at, "decided_at")
        target = _decision_status(decision)
        initialize_database(self._db_path)
        market_context = (
            closing(_open_market_snapshot(self._market_db_path))
            if target == "approved"
            else nullcontext(None)
        )
        with (
            market_context as market_connection,
            closing(connect_rw(self._db_path)) as connection,
        ):
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = _proposal_row(connection, proposal_id)
                current = cast(ProposalStatus, str(row["status"]))
                created_at = datetime.fromisoformat(str(row["created_at"]))
                previous_decided_at = (
                    datetime.fromisoformat(str(row["decided_at"]))
                    if row["decided_at"] is not None
                    else None
                )
                if decided_at < created_at or (
                    previous_decided_at is not None and decided_at < previous_decided_at
                ):
                    raise ProposalValidationError(
                        "decided_at cannot predate proposal creation or its current decision"
                    )
                if _ledger_references_proposal(connection, proposal_id):
                    raise ProposalConflictError(
                        "proposal is immutable after a ledger event references it"
                    )
                if current == "approved":
                    raise ProposalConflictError("approved proposal cannot be re-decided")
                if target == "approved":
                    if snapshot is None or snapshot_append_head is None:
                        raise ProposalValidationError(
                            "approval requires a current DB-derived portfolio snapshot"
                        )
                    assert market_connection is not None
                    _revalidate_approval(
                        connection,
                        row,
                        snapshot=snapshot,
                        snapshot_append_head=snapshot_append_head,
                        decided_at=decided_at,
                        market_db_path=self._market_db_path,
                        market_connection=market_connection,
                    )
                connection.execute(
                    "UPDATE proposal SET status = ?, decided_at = ? WHERE proposal_id = ?",
                    (target, decided_at.isoformat(), proposal_id),
                )
                updated = _proposal_row(connection, proposal_id)
                connection.commit()
            except (ExecutionPolicyError, ValidationError) as error:
                connection.rollback()
                raise ProposalValidationError(str(error)) from error
            except BaseException:
                connection.rollback()
                raise
        return _record(updated)

    def get(self, proposal_id: str) -> ProposalRecord:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            return _record(_proposal_row(connection, proposal_id))

    def list(self, *, status: ProposalStatus | None = None) -> tuple[ProposalRecord, ...]:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM proposal ORDER BY created_at DESC, proposal_id DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM proposal
                    WHERE status = ?
                    ORDER BY created_at DESC, proposal_id DESC
                    """,
                    (status,),
                ).fetchall()
        return tuple(_record(cast(sqlite3.Row, row)) for row in rows)


def _ready_thesis_and_review(
    connection: sqlite3.Connection,
    thesis_id: str,
    *,
    review_id: str | None = None,
) -> tuple[ThesisDocument, IndependentReview]:
    thesis_row = connection.execute(
        "SELECT payload FROM thesis WHERE thesis_id = ?",
        (thesis_id,),
    ).fetchone()
    if thesis_row is None:
        raise ProposalValidationError(f"unknown research thesis: {thesis_id}")
    thesis = ThesisDocument.model_validate_json(str(thesis_row["payload"]))
    if review_id is None:
        review_rows = connection.execute(
            """
            SELECT review_id, payload FROM thesis_review
            WHERE thesis_id = ?
            ORDER BY reviewed_at DESC, review_id DESC
            """,
            (thesis_id,),
        ).fetchall()
        if len(review_rows) != 1:
            raise ProposalValidationError(
                f"proposal requires exactly one review for thesis: {thesis_id}"
            )
        review_row = review_rows[0]
    else:
        review_row = connection.execute(
            """
            SELECT review_id, payload FROM thesis_review
            WHERE thesis_id = ? AND review_id = ?
            """,
            (thesis_id, review_id),
        ).fetchone()
        if review_row is None:
            raise ProposalConflictError("proposal review no longer matches its thesis")
    review = IndependentReview.model_validate_json(str(review_row["payload"]))
    result = evaluate_thesis(thesis, review=review)
    if result.decision_readiness != "ready" or result.errors:
        detail = "; ".join(result.errors) or result.decision_readiness
        raise ProposalValidationError(f"research thesis is not decision-ready: {detail}")
    if thesis.judgment.recommendation != "buy":
        raise ProposalValidationError("proposal requires a ready buy research thesis")
    return thesis, review


def _revalidate_approval(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    snapshot: PortfolioSnapshot,
    snapshot_append_head: int,
    decided_at: datetime,
    market_db_path: Path,
    market_connection: sqlite3.Connection,
) -> None:
    payload = _payload(row)
    raw_input = payload.get("planned_limit")
    if not isinstance(raw_input, Mapping):
        raise ProposalConflictError("stored proposal payload is incomplete")
    planned_limit = CanonicalPlannedLimit.model_validate(raw_input)
    thesis, review = _ready_thesis_and_review(
        connection,
        str(row["thesis_id"]),
        review_id=str(row["review_id"]),
    )
    _validate_planned_limit(
        connection,
        planned_limit,
        thesis=thesis,
        review=review,
        snapshot=snapshot,
        snapshot_append_head=snapshot_append_head,
        now=decided_at,
        market_db_path=market_db_path,
        market_connection=market_connection,
    )


def _validate_planned_limit(
    connection: sqlite3.Connection,
    planned: CanonicalPlannedLimit,
    *,
    thesis: ThesisDocument,
    review: IndependentReview,
    snapshot: PortfolioSnapshot,
    snapshot_append_head: int,
    now: datetime,
    market_db_path: Path,
    market_connection: sqlite3.Connection,
    claimed_source_ref: str | None = None,
    claimed_thesis_core_sha256: str | None = None,
) -> None:
    """Rebuild the load-bearing planning result from canonical current sources."""
    result = evaluate_thesis(thesis, review=review, now=now)
    if (
        claimed_thesis_core_sha256 is not None
        and result.thesis_sha256 != claimed_thesis_core_sha256
    ):
        raise ProposalConflictError("current thesis differs; create a new proposal")
    if review.reviewed_thesis_sha256 != result.thesis_sha256:
        raise ProposalConflictError("current review differs; create a new proposal")
    if thesis.input_snapshot.ticker != planned.ticker:
        raise ProposalConflictError("current thesis ticker differs; create a new proposal")

    current_document, current_head = load_ledger_in_transaction(connection)
    if (
        snapshot_append_head != current_head
        or planned.source_ledger_append_head != current_head
        or reconcile_portfolio(current_document) != snapshot
    ):
        raise ProposalConflictError("current ledger differs; create a new proposal")
    if planned.expires_at <= now:
        raise ProposalConflictError("planning limit has expired; create a new proposal")
    if any(item.ticker == planned.ticker for item in snapshot.active_reservations):
        raise ProposalConflictError(
            "current ledger has an active reservation; create a new proposal"
        )

    market_path = market_db_path.expanduser().resolve()
    if claimed_source_ref is not None:
        suffix = ":jquants_daily_bars"
        if not claimed_source_ref.endswith(suffix):
            raise ProposalValidationError("source_ref must identify jquants_daily_bars")
        claimed_market_path = Path(claimed_source_ref.removesuffix(suffix))
        if claimed_market_path.expanduser().resolve() != market_path:
            raise ProposalConflictError("planning source differs from the configured market DB")
    target_session = planned.expires_at.astimezone(JST).date()
    price = resolve_previous_business_day_close(
        sqlite_path=market_path,
        ticker=planned.ticker,
        target_session=target_session,
        connection=market_connection,
    )
    if price is None or price.corporate_action_unresolved:
        raise ProposalConflictError("current planning price is unavailable; create a new proposal")
    close = Decimal(str(price.close_yen))
    expected_max = max_acceptable_price(thesis, tick_size_yen=PLANNING_TICK_SIZE_YEN)
    if (
        price.price_as_of != planned.price_as_of
        or close != planned.close_yen
        or expected_max != planned.max_acceptable_price_yen
        or close > expected_max
    ):
        raise ProposalConflictError("current planning price differs; create a new proposal")

    lot_notional = close * BOARD_LOT
    expected_warnings: list[str] = []
    if lot_notional <= planned.budget_max_yen:
        expected_quantity = max(int(planned.budget_max_yen // lot_notional), 1) * BOARD_LOT
    else:
        expected_quantity = BOARD_LOT
        expected_warnings.append("budget_guide_exceeded")
    expected_notional = close * expected_quantity
    if expected_notional < planned.budget_min_yen:
        expected_warnings.append("budget_guide_under")
    if expected_quantity != planned.quantity or int(expected_notional) != planned.notional_yen:
        raise ProposalConflictError("current planning quantity differs; create a new proposal")

    expected_exposure, exposure_warnings, total_capital = portfolio_exposure(
        snapshot,
        sqlite_path=market_path,
        price_as_of=price.price_as_of,
        ticker=planned.ticker,
        sector=thesis.input_snapshot.sector,
        common_factors=thesis.input_snapshot.common_factors,
        order_notional_yen=planned.notional_yen,
        market_connection=market_connection,
    )
    expected_warnings.extend(
        planned_order_cash_warnings(
            snapshot,
            notional_yen=expected_notional,
            total_capital_yen=total_capital,
        )
    )
    expected_warnings.extend(exposure_warnings)
    if (
        PlannedPortfolioExposure.model_validate(expected_exposure) != planned.portfolio_exposure
        or tuple(expected_warnings) != planned.warnings
        or tuple(portfolio_annotations(snapshot, ticker=planned.ticker))
        != planned.portfolio_annotations
    ):
        raise ProposalConflictError("current portfolio constraints differ; create a new proposal")


def _open_market_snapshot(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
        connection.execute("BEGIN")
        return connection
    except sqlite3.Error as error:
        if connection is not None:
            connection.close()
        raise ProposalValidationError(f"cannot open configured market DB: {resolved}") from error


def _canonical_plan(planned: PlannedLimitInput) -> CanonicalPlannedLimit:
    return CanonicalPlannedLimit.model_validate(
        planned.model_dump(
            exclude={
                "status",
                "thesis_ref",
                "thesis_sha256",
                "thesis_core_sha256",
                "independent_review_ref",
                "independent_review_sha256",
                "source_ref",
            }
        )
    )


def _execution_proposal(planned: CanonicalPlannedLimit) -> dict[str, object]:
    """Expose the approved order shape consumed by the human-result boundary."""
    return {
        "ticker": planned.ticker,
        "orders": [
            {
                "tactic": "limit",
                "quantity": planned.quantity,
                "limit_price_yen": planned.limit_price_yen,
                "expires_at": planned.expires_at,
            }
        ],
    }


def _allocate_proposal_id(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    created_at: datetime,
) -> str:
    prefix = f"prop-{created_at.date().strftime('%Y%m%d')}-{ticker}-"
    rows = connection.execute(
        "SELECT proposal_id FROM proposal WHERE proposal_id LIKE ?",
        (f"{prefix}%",),
    ).fetchall()
    suffixes = [
        int(suffix) for row in rows if (suffix := str(row["proposal_id"])[len(prefix) :]).isdigit()
    ]
    return f"{prefix}{max(suffixes, default=0) + 1}"


def _ledger_references_proposal(connection: sqlite3.Connection, proposal_id: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM ledger_event WHERE proposal_id = ? LIMIT 1",
            (proposal_id,),
        ).fetchone()
        is not None
    )


def _proposal_row(connection: sqlite3.Connection, proposal_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM proposal WHERE proposal_id = ?",
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise ProposalNotFoundError(f"unknown proposal: {proposal_id}")
    return cast(sqlite3.Row, row)


def _record(row: sqlite3.Row) -> ProposalRecord:
    decided_at = str(row["decided_at"]) if row["decided_at"] is not None else None
    return ProposalRecord(
        proposal_id=str(row["proposal_id"]),
        ticker=str(row["ticker"]),
        thesis_id=str(row["thesis_id"]),
        review_id=str(row["review_id"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        status=cast(ProposalStatus, str(row["status"])),
        decided_at=datetime.fromisoformat(decided_at) if decided_at is not None else None,
        payload=_payload(row),
    )


def _payload(row: sqlite3.Row) -> Mapping[str, object]:
    parsed = json.loads(str(row["payload"]))
    if not isinstance(parsed, dict):
        raise ProposalConflictError("stored proposal payload must be an object")
    return cast(Mapping[str, object], parsed)


def _decision_status(decision: ProposalDecision) -> ProposalStatus:
    return cast(
        ProposalStatus,
        {
            "approve": "approved",
            "defer": "deferred",
            "reject": "rejected",
        }[decision],
    )


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProposalValidationError(f"{field} must include a timezone")
