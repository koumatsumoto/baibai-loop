"""Ephemeral, source-bound drafts for explicit human-confirmed ledger writes."""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.ledger import (
    ConfirmedTaxEvent,
    ContributionEvent,
    CostEvent,
    HumanOverride,
    IncomeEvent,
    PortfolioLedgerDocument,
    WithdrawalEvent,
    reconcile_portfolio,
    replay_events_through,
    require_resolved_expiries,
)
from baibai_engine.position.store import LedgerApplyResult, LedgerStoreService

DraftKind = Literal["event", "record-result", "sell-result", "market-price", "override", "meta"]
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
        confirmation_required=True,
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
        confirmation_required=True,
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
        confirmation_required=True,
    )


def build_sell_execution_draft(
    service: LedgerStoreService,
    *,
    occurred_at: datetime,
    ticker: str,
    quantity: int,
    price_yen: Decimal,
    fees_yen: int | None = None,
    tax_yen: int | None = None,
    decision_reference: str | None = None,
) -> LedgerDraft:
    """Record a human-reported sell fill as a source-bound execution draft.

    The sell consumes FIFO cost from current holdings; ``reconcile_portfolio``
    rejects a quantity above the reconciled holding, a non board-lot quantity,
    and any confirmed fee or tax that overdraws available cash. Broker fees and
    the confirmed capital-gain tax are recorded as their own cost / tax events so
    realized proceeds and cost stay separable in replay. ``decision_reference``
    binds the sell to the Position Review that judged the reduce / exit.
    """

    source = service.load()
    suffix = _sell_event_suffix(
        occurred_at=occurred_at,
        ticker=ticker,
        quantity=quantity,
        price_yen=price_yen,
        decision_reference=decision_reference,
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
    reconcile_portfolio(replacement)
    return LedgerDraft(
        kind="sell-result",
        expected_head=service.append_head(),
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
) -> str:
    raw = "|".join(
        (
            decision_reference or "",
            ticker,
            str(quantity),
            str(price_yen),
            occurred_at.isoformat(),
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
    if draft.kind == "record-result":
        # A broker result changes cash / reservations / lots, but does not assert a
        # fresh portfolio valuation. Recheck the complete event state and expiry
        # invariants without letting an unrelated stale holding quote block the
        # human-reported result.
        state = replay_events_through(draft.replacement.events, draft.replacement.as_of)
        require_resolved_expiries(state)
    else:
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
    "build_sell_execution_draft",
    "load_draft",
    "write_draft",
]
