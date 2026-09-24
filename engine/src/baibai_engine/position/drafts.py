"""Ephemeral, source-bound drafts for explicit human-confirmed ledger writes."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.ledger import (
    ConfirmedTaxEvent,
    ContributionEvent,
    CostEvent,
    HumanOverride,
    IncomeEvent,
    PortfolioLedgerDocument,
    WithdrawalEvent,
    active_human_overrides,
    replay_events_through,
    require_resolved_expiries,
)
from baibai_engine.position.store import LedgerApplyResult, LedgerStoreService

DraftKind = Literal["event", "broker-fact", "sell-execution", "override", "meta"]
HumanEvent = ContributionEvent | WithdrawalEvent | IncomeEvent | CostEvent | ConfirmedTaxEvent


class LedgerDraft(BaseModel):
    """A reviewable replacement bound to the exact DB state it was built from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: DraftKind
    expected_head: int = Field(ge=0)
    source: PortfolioLedgerDocument
    replacement: PortfolioLedgerDocument
    confirmation_required: bool = True


def build_event_draft(
    service: LedgerStoreService,
    event: HumanEvent,
) -> LedgerDraft:
    source, expected_head = service.load_with_head()
    raw = source.model_dump(mode="json")
    raw["events"] = sorted(
        [*raw["events"], event.model_dump(mode="json")],
        key=lambda item: datetime.fromisoformat(str(item["occurred_at"])),
    )
    raw["as_of"] = max(source.as_of, event.occurred_at).isoformat()
    replacement = PortfolioLedgerDocument.model_validate(raw)
    replay_events_through(replacement.events, replacement.as_of)
    return LedgerDraft(
        kind="event",
        expected_head=expected_head,
        source=source,
        replacement=replacement,
        confirmation_required=True,
    )


def build_override_draft(
    service: LedgerStoreService,
    override: HumanOverride,
) -> LedgerDraft:
    source, expected_head = service.load_with_head()
    if any(item.override_id == override.override_id for item in source.overrides):
        raise ValueError(f"duplicate override_id: {override.override_id}")
    replacement = source.model_copy(
        update={
            "overrides": tuple(
                sorted((*source.overrides, override), key=lambda item: item.override_id)
            )
        }
    )
    active_human_overrides(replacement.overrides, as_of=replacement.as_of)
    replay_events_through(replacement.events, replacement.as_of)
    return LedgerDraft(
        kind="override",
        expected_head=expected_head,
        source=source,
        replacement=replacement,
        confirmation_required=True,
    )


def build_meta_draft(
    service: LedgerStoreService,
    *,
    as_of: datetime,
    estimated_exit_tax_rate_bps: int | None,
    estimated_exit_tax_basis: str | None,
) -> LedgerDraft:
    source, expected_head = service.load_with_head()
    replacement = PortfolioLedgerDocument.model_validate(
        {
            **source.model_dump(mode="json"),
            "as_of": max(source.as_of, as_of).isoformat(),
            "estimated_exit_tax_rate_bps": estimated_exit_tax_rate_bps,
            "estimated_exit_tax_basis": estimated_exit_tax_basis,
        }
    )
    replay_events_through(replacement.events, replacement.as_of)
    return LedgerDraft(
        kind="meta",
        expected_head=expected_head,
        source=source,
        replacement=replacement,
        confirmation_required=True,
    )


def build_sell_execution_draft(
    service: LedgerStoreService,
    *,
    occurred_at: datetime | None = None,
    occurred_on: date | None = None,
    ticker: str,
    quantity: int,
    price_yen: Decimal,
    fees_yen: int | None = None,
    tax_yen: int | None = None,
    decision_reference: str | None = None,
) -> LedgerDraft:
    """Record a human-reported sell fill as a source-bound execution draft.

    The sell consumes FIFO cost from current holdings; event replay
    rejects a quantity above the reconciled holding, a non board-lot quantity,
    and any confirmed fee or tax that overdraws available cash. Broker fees and
    the confirmed capital-gain tax are recorded as their own cost / tax events so
    realized proceeds and cost stay separable in replay. ``decision_reference``
    binds the sell to the Position Review that judged the exit.
    """

    if (occurred_at is None) == (occurred_on is None):
        raise ValueError("specify exactly one of occurred_at and occurred_on")
    if occurred_on is not None:
        # This is only a replay key. occurred_on is the reported broker fact;
        # the midnight key does not assert that a fill happened at midnight.
        occurred_at = datetime.combine(occurred_on, time.min, tzinfo=JST)
    assert occurred_at is not None
    source, expected_head = service.load_with_head()
    suffix = _sell_event_suffix(
        occurred_at=occurred_at,
        ticker=ticker,
        quantity=quantity,
        price_yen=price_yen,
        decision_reference=decision_reference,
        occurred_on=occurred_on,
    )
    occurred_at_text = occurred_at.isoformat()
    additions: list[dict[str, object]] = [
        {
            "event_id": f"human-sell-{suffix}",
            "type": "execution",
            "occurred_at": occurred_at_text,
            "execution_id": f"sell-execution-{suffix}",
            "ticker": ticker,
            "side": "sell",
            "quantity": quantity,
            "price_yen": str(price_yen),
            "decision_reference": decision_reference,
            **({"occurred_on": occurred_on.isoformat()} if occurred_on is not None else {}),
        }
    ]
    if fees_yen is not None:
        additions.append(
            {
                "event_id": f"human-sell-fee-{suffix}",
                "type": "cost",
                "occurred_at": occurred_at_text,
                "ticker": ticker,
                "cost_kind": "commission",
                "amount_yen": fees_yen,
            }
        )
    if tax_yen is not None:
        additions.append(
            {
                "event_id": f"human-sell-tax-{suffix}",
                "type": "tax_confirmed",
                "occurred_at": occurred_at_text,
                "ticker": ticker,
                "tax_kind": "capital_gain",
                "amount_yen": tax_yen,
            }
        )
    raw = source.model_dump(mode="json")
    raw["events"] = sorted(
        [*raw["events"], *additions],
        key=lambda item: datetime.fromisoformat(str(item["occurred_at"])),
    )
    raw["as_of"] = max(source.as_of, occurred_at).isoformat()
    replacement = PortfolioLedgerDocument.model_validate(raw)
    replay_events_through(replacement.events, replacement.as_of)
    return LedgerDraft(
        kind="sell-execution",
        expected_head=expected_head,
        source=source,
        replacement=replacement,
        confirmation_required=True,
    )


def _sell_event_suffix(
    *,
    occurred_at: datetime,
    ticker: str,
    quantity: int,
    price_yen: Decimal,
    decision_reference: str | None,
    occurred_on: date | None = None,
) -> str:
    raw = "|".join(
        (
            decision_reference or "",
            ticker,
            str(quantity),
            str(price_yen),
            (
                f"date:{occurred_on.isoformat()}"
                if occurred_on is not None
                else occurred_at.isoformat()
            ),
        )
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def write_draft(path: Path, draft: LedgerDraft) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        yaml.safe_dump(draft.model_dump(mode="json"), stream, sort_keys=False, allow_unicode=True)


def load_draft(path: Path) -> LedgerDraft:
    raw = safe_load(path.read_text(encoding="utf-8"))
    return LedgerDraft.model_validate(raw)


def apply_draft(
    service: LedgerStoreService,
    draft: LedgerDraft,
    *,
    human_confirmed: bool,
) -> LedgerApplyResult:
    if not human_confirmed:
        raise ValueError("ledger draft apply requires explicit human confirmation")
    if not draft.confirmation_required:
        raise ValueError("ledger draft is not eligible for explicit apply")
    active_human_overrides(draft.replacement.overrides, as_of=draft.replacement.as_of)
    state = replay_events_through(draft.replacement.events, draft.replacement.as_of)
    if draft.kind == "broker-fact":
        require_resolved_expiries(state)
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
    "build_sell_execution_draft",
    "load_draft",
    "write_draft",
]
