"""候補比較へ凍結Review Set・exact Triage入力・判断とledger時点除外をread-onlyで見せる。"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator

from baibai_batch.storage.canonical_json import canonical_json
from baibai_engine.batch_api import validated_review_set
from baibai_engine.foundation.repository_layout import APPLICATION_DB_PATH, RUNS_DB_PATH
from baibai_engine.position.ledger import replay_events_through, require_resolved_expiries
from baibai_engine.position.ledger_read import load_ledger_document
from baibai_engine.screening.discovery.projection import review_set_candidates
from baibai_engine.screening.run_store import RunStoreAmbiguousError, ScreeningRunReader
from baibai_engine.storage.application.read import connect_read_only
from tools.l1_mcp.contract import InputModel


class OwnerError(Exception):
    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


class ReviewSetSelector(InputModel):
    review_set_id: str | None = Field(default=None, min_length=1)
    public_run_id: str | None = Field(default=None, min_length=1)
    as_of: str | None = None
    not_before: str | None = None

    @field_validator("as_of", "not_before")
    @classmethod
    def iso_date(cls, value: str | None) -> str | None:
        if value is not None and date.fromisoformat(value).isoformat() != value:
            raise ValueError("ISO date required")
        return value


def digest(payload: object) -> str:
    return sha256(canonical_json(payload)).hexdigest()


class Reader:
    def __init__(
        self, app_path: Path = APPLICATION_DB_PATH, runs_path: Path = RUNS_DB_PATH
    ) -> None:
        self.app_path = app_path
        self.runs_path = runs_path

    def get_review_set(self, selector: ReviewSetSelector) -> dict[str, Any]:
        if sum(value is not None for value in selector.model_dump().values()) > 1:
            raise OwnerError("INVALID_ARGUMENT")
        if not self.runs_path.is_file():
            raise OwnerError("SOURCE_UNAVAILABLE")
        reader = ScreeningRunReader(self.runs_path)
        try:
            publication = reader.resolve_review_set(
                review_set_id=selector.review_set_id,
                public_run_id=selector.public_run_id,
                as_of_date=selector.as_of,
                not_before=selector.not_before,
            )
        except RunStoreAmbiguousError as exc:
            raise OwnerError("AMBIGUOUS_SELECTION") from exc
        if publication is None:
            raise OwnerError("SOURCE_UNAVAILABLE")
        review_set = validated_review_set(publication)
        run = reader.get_run(review_set.run_revision_id)
        if run is None:
            raise OwnerError("SOURCE_UNAVAILABLE")
        if (
            run.as_of_date != review_set.as_of.isoformat()
            or run.payload.get("screening_rules_hash") != review_set.screening_rules_hash
        ):
            raise OwnerError("CONTRACT_MISMATCH")
        candidates = list(review_set_candidates(review_set))
        return {
            "review_set_ref": {
                "review_set_id": review_set.review_set_id,
                "run_revision_id": review_set.run_revision_id,
                "public_run_id": run.public_run_id,
            },
            "as_of": review_set.as_of.isoformat(),
            "run_at": run.run_at,
            "created_at": publication.created_at,
            "screening_rules_hash": review_set.screening_rules_hash,
            "candidate_discovery_method": review_set.method.model_dump(mode="json"),
            "input_basis": "frozen_review_set",
            "candidate_count": len(candidates),
            "candidates": candidates,
        }

    def exclusions(self, at: str) -> dict[str, Any]:
        try:
            instant = datetime.fromisoformat(at)
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise ValueError("timezone required")
        except ValueError as exc:
            raise OwnerError("INVALID_ARGUMENT") from exc
        if not self.app_path.is_file():
            raise OwnerError("PORTFOLIO_UNAVAILABLE")
        with closing(connect_read_only(self.app_path)) as connection:
            connection.execute("BEGIN")
            ledger = load_ledger_document(connection)
        if ledger is None:
            raise OwnerError("PORTFOLIO_UNAVAILABLE")
        if instant > ledger.as_of:
            raise OwnerError("PORTFOLIO_COVERAGE_INSUFFICIENT")
        state = replay_events_through(ledger.events, instant)
        try:
            require_resolved_expiries(state)
        except ValueError as exc:
            raise OwnerError("PORTFOLIO_UNRESOLVED") from exc
        held = sorted(
            ticker for ticker, lots in state.lots.items() if any(lot.quantity > 0 for lot in lots)
        )
        reserved = sorted({item.ticker for item in state.active_reservations.values()})
        basis = {"at": instant.isoformat(), "held_tickers": held, "reserved_tickers": reserved}
        return {
            **basis,
            "ledger_as_of": ledger.as_of.isoformat(),
            "exclude_tickers": sorted(set(held) | set(reserved)),
            "exclusion_sha256": digest(basis),
        }
