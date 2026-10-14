"""同じrunの保存候補とledger時点除外をread-onlyで見せる。"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator

from baibai_batch.storage.canonical_json import canonical_json
from baibai_engine.foundation.repository_layout import APPLICATION_DB_PATH, RUNS_DB_PATH
from baibai_engine.position.ledger import replay_events_through, require_resolved_expiries
from baibai_engine.position.ledger_read import load_ledger_document
from baibai_engine.read_api.screening import screening_candidates_payload
from baibai_engine.storage.application.read import connect_read_only
from tools.l1_mcp.contract import InputModel


class OwnerError(Exception):
    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


class CandidateSelector(InputModel):
    run_revision_id: str | None = Field(default=None, min_length=1)
    as_of: str | None = None

    @field_validator("as_of")
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

    def get_candidates(self, selector: CandidateSelector) -> dict[str, Any]:
        if selector.run_revision_id is not None and selector.as_of is not None:
            raise OwnerError("INVALID_ARGUMENT")
        payload = screening_candidates_payload(
            self.runs_path,
            run_revision_id=selector.run_revision_id,
            as_of_date=None if selector.as_of is None else date.fromisoformat(selector.as_of),
        )
        if payload is None:
            raise OwnerError("not_found")
        return payload

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
