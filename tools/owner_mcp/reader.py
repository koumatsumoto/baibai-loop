"""候補比較へ凍結Review Set・exact Triage入力・判断とledger時点除外をread-onlyで見せる。"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator

from baibai_batch.analysis.io import canonical_json
from baibai_batch.analysis.model_input import build_model_input, review_set_candidates
from baibai_engine.appdb.read import connect_read_only
from baibai_engine.batch_api import validated_review_set
from baibai_engine.foundation.repository_layout import APPLICATION_DB_PATH, RUNS_DB_PATH
from baibai_engine.foundation.research_triage import ResearchTriage
from baibai_engine.macro.context.models import MacroContextDocument
from baibai_engine.position.ledger import replay_events_through, require_resolved_expiries
from baibai_engine.position.ledger_read import load_ledger_document
from baibai_engine.read_api.macro import macro_context_payload
from baibai_engine.read_api.research_triage import (
    current_research_triage,
    list_research_triage_payloads,
    research_triage_payload_hash,
)
from baibai_engine.screening.run_store import RunStoreAmbiguousError, ScreeningRunReader
from tools.l1_mcp.contract import InputModel


class OwnerError(Exception):
    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


class TriageRef(InputModel):
    research_triage_id: str = Field(min_length=1)
    research_triage_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Selector(InputModel):
    research_triage_id: str | None = Field(default=None, min_length=1)
    as_of: str | None = None
    not_before: str | None = None

    @field_validator("as_of", "not_before")
    @classmethod
    def iso_date(cls, value: str | None) -> str | None:
        if value is not None and date.fromisoformat(value).isoformat() != value:
            raise ValueError("ISO date required")
        return value


class ReviewSetSelector(InputModel):
    review_set_id: str | None = Field(default=None, min_length=1)
    public_run_id: str | None = Field(default=None, min_length=1)
    as_of: str | None = None
    not_before: str | None = None

    @field_validator("as_of", "not_before")
    @classmethod
    def iso_date(cls, value: str | None) -> str | None:
        return Selector.iso_date(value)


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
        candidates = [item.model_dump(mode="json") for item in review_set_candidates(review_set)]
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

    def judgment(self, identifier: str) -> ResearchTriage:
        triage = current_research_triage(self.app_path, identifier)
        if triage is None:
            raise OwnerError("SOURCE_UNAVAILABLE")
        if triage.research_triage_id != identifier:
            raise OwnerError("CONTRACT_MISMATCH")
        return triage

    def reconstruct(self, triage: ResearchTriage) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            return self._reconstruct(triage)
        except ValueError as exc:
            raise OwnerError("CONTRACT_MISMATCH") from exc

    def _reconstruct(self, triage: ResearchTriage) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.runs_path.is_file():
            raise OwnerError("SOURCE_UNAVAILABLE")
        reader = ScreeningRunReader(self.runs_path)
        publication = reader.get_review_set(triage.review_set_id)
        run = reader.get_run(triage.run_revision_id)
        if publication is None or run is None:
            raise OwnerError("SOURCE_UNAVAILABLE")
        review_set = validated_review_set(publication)
        if (
            triage.review_set_id != review_set.review_set_id
            or triage.run_revision_id != review_set.run_revision_id
            or triage.as_of != review_set.as_of
            or triage.screening_rules_hash != review_set.screening_rules_hash
            or triage.candidate_discovery_method != review_set.method
            or run.run_revision_id != triage.run_revision_id
            or run.as_of_date != triage.as_of.isoformat()
            or run.payload.get("screening_rules_hash") != triage.screening_rules_hash
        ):
            raise OwnerError("CONTRACT_MISMATCH")
        macro = None
        if triage.macro_context_id is not None:
            try:
                payload = macro_context_payload(
                    self.app_path, context_id=triage.macro_context_id, as_of=triage.as_of
                )
            except ValueError as exc:
                raise OwnerError("SOURCE_UNAVAILABLE") from exc
            macro = MacroContextDocument.model_validate(payload)
            if macro.context_id != triage.macro_context_id or macro.as_of > triage.as_of:
                raise OwnerError("CONTRACT_MISMATCH")
        model_input = build_model_input(review_set, macro)
        snapshots = {candidate.ticker: candidate.snapshot for candidate in model_input.candidates}
        if {entry.ticker: entry.candidate_snapshot for entry in triage.entries} != snapshots:
            raise OwnerError("CONTRACT_MISMATCH")
        run_at = datetime.fromisoformat(run.run_at)
        if run_at.tzinfo is None or run_at.utcoffset() is None:
            raise OwnerError("CONTRACT_MISMATCH")
        return model_input.model_dump(mode="json"), {
            "as_of": triage.as_of.isoformat(),
            "published_at": triage.published_at.isoformat(),
            "review_set_id": triage.review_set_id,
            "run_revision_id": triage.run_revision_id,
            "public_run_id": run.public_run_id,
            "run_at": run.run_at,
            "macro_context_id": triage.macro_context_id,
            "screening_rules_hash": triage.screening_rules_hash,
            "candidate_discovery_method": triage.candidate_discovery_method.model_dump(mode="json"),
        }

    def resolved(self, triage: ResearchTriage) -> dict[str, Any]:
        payload, metadata = self.reconstruct(triage)
        return {
            "triage_ref": TriageRef(
                research_triage_id=triage.research_triage_id,
                research_triage_sha256=research_triage_payload_hash(triage),
                model_input_sha256=digest(payload),
            ).model_dump(),
            **metadata,
            "input_basis": "reconstructed_from_production_contract",
        }

    def resolve(self, selector: Selector) -> dict[str, Any]:
        if sum(value is not None for value in selector.model_dump().values()) > 1:
            raise OwnerError("INVALID_ARGUMENT")
        if selector.research_triage_id is not None:
            return self.resolved(self.judgment(selector.research_triage_id))
        triages = [
            ResearchTriage.model_validate(payload)
            for payload in list_research_triage_payloads(self.app_path)
        ]
        triages.sort(
            key=lambda item: (item.as_of, item.published_at, item.research_triage_id),
            reverse=selector.not_before is None,
        )
        matches = []
        for triage in triages:
            if selector.as_of is not None and triage.as_of.isoformat() != selector.as_of:
                continue
            if selector.not_before is not None and triage.as_of.isoformat() < selector.not_before:
                continue
            try:
                result = self.resolved(triage)
            except OwnerError as exc:
                if exc.code not in {"SOURCE_UNAVAILABLE", "CONTRACT_MISMATCH"}:
                    raise
                continue
            if selector.as_of is None:
                return result
            matches.append(result)
            if len(matches) > 1:
                raise OwnerError("AMBIGUOUS_SELECTION")
        if not matches:
            raise OwnerError("SOURCE_UNAVAILABLE")
        return matches[0]

    def verified_judgment(self, reference: TriageRef) -> ResearchTriage:
        triage = self.judgment(reference.research_triage_id)
        if research_triage_payload_hash(triage) != reference.research_triage_sha256:
            raise OwnerError("REFERENCE_MISMATCH")
        return triage

    def get_input(self, reference: TriageRef) -> dict[str, Any]:
        payload, _ = self.reconstruct(self.verified_judgment(reference))
        if digest(payload) != reference.model_input_sha256:
            raise OwnerError("REFERENCE_MISMATCH")
        return {
            "triage_ref": reference.model_dump(),
            "input_basis": "reconstructed_from_production_contract",
            "model_input": payload,
        }

    def get_judgment(self, reference: TriageRef) -> dict[str, Any]:
        return {
            "triage_ref": reference.model_dump(),
            "research_triage": self.verified_judgment(reference).model_dump(mode="json"),
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
