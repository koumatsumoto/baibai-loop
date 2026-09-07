"""対象保有の入力組立・検証・人間確認後の公開を産む保有判断 use case。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.paths import database_path
from baibai_engine.appdb.read import connect_read_only
from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.foundation.repository_layout import MARKET_DB_PATH
from baibai_engine.foundation.time import JST
from baibai_engine.position.ledger import ExecutionEvent, replay_events_through
from baibai_engine.position.store import load_ledger_in_transaction
from baibai_engine.research.market_close_source import (
    read_holding_unadjusted_close_on_basis,
    read_unadjusted_close,
)
from baibai_engine.research.position_review import (
    HoldingInput,
    PositionReviewDocument,
    PositionReviewEvaluation,
    QuoteInput,
    evaluate_position_review,
)
from baibai_engine.research.thesis_store import (
    ResearchConflictError,
    ReviewedThesis,
    latest_thesis_id,
    load_reviewed_thesis,
)


class PositionReviewService:
    def __init__(
        self,
        db_path: Path | None = None,
        *,
        sqlite_path: Path = MARKET_DB_PATH,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db_path = db_path
        self._sqlite_path = sqlite_path
        self._clock = clock or (lambda: datetime.now(JST))

    def _inputs(
        self, connection: sqlite3.Connection, thesis_id: str, now: datetime
    ) -> tuple[ReviewedThesis, HoldingInput, QuoteInput | None]:
        pair = load_reviewed_thesis(connection, thesis_id)
        ticker = pair.document.input_snapshot.ticker
        if latest_thesis_id(connection, ticker) != thesis_id:
            raise ResearchConflictError("refresh draft with latest Reviewed Thesis")
        ledger, _ = load_ledger_in_transaction(connection)
        if any(event.occurred_at > now for event in ledger.events):
            raise ResearchConflictError("judgment instant omits reported ledger facts")
        state = replay_events_through(ledger.events, now)
        lots = state.lots.get(ticker, [])
        quantity = sum(lot.quantity for lot in lots)
        if quantity <= 0:
            raise ResearchConflictError("Position Review requires a current holding")
        quote = read_unadjusted_close(sqlite_path=self._sqlite_path, ticker=ticker, at=now)
        # Start with the current holding episode, not a previous closed position.
        balance = 0
        acquired_on = now.date()
        for event in ledger.events:
            if (
                isinstance(event, ExecutionEvent)
                and event.ticker == ticker
                and event.occurred_at <= now
            ):
                if event.side == "buy":
                    if balance == 0:
                        acquired_on = event.occurred_at.date()
                    balance += event.quantity
                else:
                    balance -= event.quantity
        holding_basis_confirmed = (
            quote is not None
            and read_holding_unadjusted_close_on_basis(
                sqlite_path=self._sqlite_path,
                ticker=ticker,
                ledger_price_observed_on=min(acquired_on, quote.price_as_of),
                basis_as_of=quote.price_as_of,
            )
            is not None
        )
        original_price = next(
            (
                fact
                for fact in pair.document.input_snapshot.facts
                if fact.fact_id == pair.document.valuation.market_price_fact_id
            ),
            None,
        )
        valuation_basis_confirmed = (
            holding_basis_confirmed
            and original_price is not None
            and quote is not None
            and read_holding_unadjusted_close_on_basis(
                sqlite_path=self._sqlite_path,
                ticker=ticker,
                ledger_price_observed_on=original_price.as_of,
                basis_as_of=quote.price_as_of,
            )
            is not None
        )
        holding = HoldingInput(
            quantity=quantity,
            cost_yen=sum((lot.price_yen * lot.quantity for lot in lots), Decimal(0)),
            quantity_basis_confirmed=holding_basis_confirmed,
        )
        quoted = (
            None
            if quote is None
            else QuoteInput(
                price_yen=Decimal(str(quote.close_yen)),
                observed_at=datetime.combine(quote.price_as_of, time(15, 30), tzinfo=JST),
                price_basis="last_close_unadjusted",
                source_ref="jquants_daily_bars",
                basis_confirmed=valuation_basis_confirmed,
            )
        )
        return pair, holding, quoted

    def build(self, *, thesis_id: str, position_id: str) -> PositionReviewDocument:
        now = self._clock()
        with closing(connect_read_only(database_path(self._db_path))) as connection:
            connection.execute("BEGIN")
            pair, holding, quote = self._inputs(connection, thesis_id, now)
        document = PositionReviewDocument(
            schema_version=3,
            position_review_id=f"position-review-{now:%Y%m%d}-{pair.document.input_snapshot.ticker}-{position_id}",
            thesis_id=thesis_id,
            position_id=position_id,
            ticker=pair.document.input_snapshot.ticker,
            as_of=now.date(),
            holding=holding,
            quote=quote,
            remaining_reward=None,
            action=None,
            unresolved_reason="残存見返りの経済的な評価を記入してください",
        )
        evaluation = evaluate_position_review(
            document, pair.document, primary_verified=pair.review.primary_source_check == "verified"
        )
        return document.model_copy(update={"action": evaluation.action})

    def _check(
        self, connection: sqlite3.Connection, document: PositionReviewDocument, now: datetime
    ) -> PositionReviewEvaluation:
        if document.as_of != now.date():
            raise ResearchConflictError("refresh formal judgment basis")
        pair, holding, quote = self._inputs(connection, document.thesis_id, now)
        if document.holding != holding:
            raise ResearchConflictError(
                "holding quantity/cost/basis changed; refresh and reconfirm"
            )
        broken = (
            pair.document.investment_case.status == "broken"
            and pair.review.primary_source_check == "verified"
        )
        if not broken and document.quote != quote:
            raise ResearchConflictError("quote changed; refresh and reconfirm")
        result = evaluate_position_review(
            document, pair.document, primary_verified=pair.review.primary_source_check == "verified"
        )
        if result.action != document.action:
            raise ValueError("submitted action differs from economic judgment")
        if (
            result.action is None
            and not (
                document.unresolved_reason
                or (document.remaining_reward.reason if document.remaining_reward else "")
            ).strip()
        ):
            raise ValueError("null judgment requires an economic reason")
        return result

    def check(self, document: PositionReviewDocument) -> PositionReviewEvaluation:
        with closing(connect_read_only(database_path(self._db_path))) as connection:
            connection.execute("BEGIN")
            return self._check(connection, document, self._clock())

    def publish(
        self, document: PositionReviewDocument, *, confirmed: bool
    ) -> PositionReviewDocument:
        if not confirmed:
            raise ValueError("Position Review publication requires human confirmation")
        initialize_database(self._db_path)
        payload = canonical_json(document.model_dump(mode="json"))
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT payload FROM position_review WHERE position_review_id = ?",
                    (document.position_review_id,),
                ).fetchone()
                if row is not None:
                    if str(row[0]) != payload:
                        raise ResearchConflictError("immutable Position Review differs")
                    connection.rollback()
                    return document
                self._check(connection, document, self._clock())
                connection.execute(
                    "INSERT INTO position_review(position_review_id,ticker,as_of,thesis_id,"
                    "candidate_thesis_id,payload) VALUES (?,?,?,?,NULL,?)",
                    (
                        document.position_review_id,
                        document.ticker,
                        document.as_of.isoformat(),
                        document.thesis_id,
                        payload,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return document
