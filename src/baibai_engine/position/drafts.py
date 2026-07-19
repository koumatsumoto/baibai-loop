"""Ephemeral, source-bound drafts for explicit human-confirmed ledger writes."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from baibai_engine.position.ledger import (
    ConfirmedTaxEvent,
    ContributionEvent,
    CostEvent,
    HumanOverride,
    IncomeEvent,
    PortfolioLedgerDocument,
    WithdrawalEvent,
    reconcile_portfolio,
)
from baibai_engine.position.store import LedgerApplyResult, LedgerStoreService

DraftKind = Literal["event", "record-result", "market-price", "override", "meta"]
HumanEvent = ContributionEvent | WithdrawalEvent | IncomeEvent | CostEvent | ConfirmedTaxEvent


class LedgerDraft(BaseModel):
    """A reviewable replacement bound to the exact DB state it was built from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: DraftKind
    expected_head: int = Field(ge=0)
    source: PortfolioLedgerDocument
    replacement: PortfolioLedgerDocument
    human_reported: bool


def build_event_draft(
    service: LedgerStoreService,
    event: HumanEvent,
) -> LedgerDraft:
    source = service.load()
    raw = source.model_dump(mode="json")
    raw["events"] = sorted(
        [*raw["events"], event.model_dump(mode="json")],
        key=lambda item: datetime.fromisoformat(str(item["occurred_at"])),
    )
    raw["as_of"] = max(source.as_of, event.occurred_at).isoformat()
    replacement = PortfolioLedgerDocument.model_validate(raw)
    reconcile_portfolio(replacement)
    return LedgerDraft(
        kind="event",
        expected_head=service.append_head(),
        source=source,
        replacement=replacement,
        human_reported=True,
    )


def build_override_draft(
    service: LedgerStoreService,
    override: HumanOverride,
) -> LedgerDraft:
    source = service.load()
    if any(item.override_id == override.override_id for item in source.overrides):
        raise ValueError(f"duplicate override_id: {override.override_id}")
    replacement = source.model_copy(
        update={
            "overrides": tuple(
                sorted((*source.overrides, override), key=lambda item: item.override_id)
            )
        }
    )
    reconcile_portfolio(replacement)
    return LedgerDraft(
        kind="override",
        expected_head=service.append_head(),
        source=source,
        replacement=replacement,
        human_reported=True,
    )


def build_meta_draft(
    service: LedgerStoreService,
    *,
    as_of: datetime,
    estimated_exit_tax_rate_bps: int | None,
    estimated_exit_tax_basis: str | None,
) -> LedgerDraft:
    source = service.load()
    replacement = PortfolioLedgerDocument.model_validate(
        {
            **source.model_dump(mode="json"),
            "as_of": max(source.as_of, as_of).isoformat(),
            "estimated_exit_tax_rate_bps": estimated_exit_tax_rate_bps,
            "estimated_exit_tax_basis": estimated_exit_tax_basis,
        }
    )
    reconcile_portfolio(replacement)
    return LedgerDraft(
        kind="meta",
        expected_head=service.append_head(),
        source=source,
        replacement=replacement,
        human_reported=True,
    )


def write_draft(path: Path, draft: LedgerDraft) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        yaml.safe_dump(draft.model_dump(mode="json"), stream, sort_keys=False, allow_unicode=True)


def load_draft(path: Path) -> LedgerDraft:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return LedgerDraft.model_validate(raw)


def apply_draft(
    service: LedgerStoreService,
    draft: LedgerDraft,
    *,
    human_confirmed: bool,
) -> LedgerApplyResult:
    if not human_confirmed:
        raise ValueError("ledger draft apply requires explicit human confirmation")
    if not draft.human_reported:
        raise ValueError("ledger draft does not contain a human-reported action")
    reconcile_portfolio(draft.replacement)
    return service.apply_document(
        expected_head=draft.expected_head,
        expected_document=draft.source,
        replacement=draft.replacement,
    )


__all__ = [
    "DraftKind",
    "LedgerDraft",
    "apply_draft",
    "build_event_draft",
    "build_meta_draft",
    "build_override_draft",
    "load_draft",
    "write_draft",
]
