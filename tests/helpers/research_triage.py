"""Current-schema Research Triage payload helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

TRIAGE_CONTRACT_ID = "research-triage-v1"


def research_entry(ticker: str = "2331", *, rank: int = 1, **overrides: Any) -> dict[str, Any]:
    """A research entry in judgment priority order."""

    entry: dict[str, Any] = {
        "ticker": ticker,
        "decision": "research",
        "priority": rank,
        "rationale": "受注端境と構造鈍化を一次開示で識別できる",
        "research_question": "受注回復は粗利とcash flowへ波及するか",
        "key_risk": "需要鈍化が構造的である可能性",
    }
    entry.update(overrides)
    return entry


def skip_entry(
    ticker: str,
    *,
    reason: str = "見送る：暫定上値が現値を上回らず、一次リサーチで識別する仮説がない",
    **overrides: Any,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "ticker": ticker,
        "decision": "skip",
        "priority": None,
        "rationale": reason,
        "research_question": None,
        "key_risk": None,
    }
    entry.update(overrides)
    return entry


def research_triage_payload(
    *,
    research_triage_id: str = "research_triage-20260719-base",
    review_set_id: str = "review_set-test",
    run_revision_id: str = "runrev-test",
    as_of: str = "2026-07-19",
    published_at: str | None = None,
    macro_context_id: str | None = None,
    review_basis_research_triage_id: str | None = None,
    triage_contract_id: str = TRIAGE_CONTRACT_ID,
    entries: Sequence[Mapping[str, Any]] | None = None,
    schema_version: int = 1,
    **extra: Any,
) -> dict[str, Any]:
    """A publishable research_triage document at the current schema version."""

    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "kind": "research_triage",
        "research_triage_id": research_triage_id,
        "review_set_id": review_set_id,
        "run_revision_id": run_revision_id,
        "as_of": as_of,
        "published_at": published_at or f"{as_of}T14:00:00+09:00",
        "macro_context_id": macro_context_id,
        "review_basis_research_triage_id": review_basis_research_triage_id,
        "triage_contract_id": triage_contract_id,
        "entries": [dict(entry) for entry in (entries or [research_entry()])],
    }
    payload.update(extra)
    return payload


def research_triage_from_review_set(
    review_set: Mapping[str, Any],
    *,
    research_triage_id: str,
    run_revision_id: str,
    as_of: str,
    entries: Sequence[Mapping[str, Any]],
    published_at: str | None = None,
    macro_context_id: str | None = None,
) -> dict[str, Any]:
    """The draft an operator writes against one published review_set.

    Source identity and review basis are copied from the review_set.
    """

    return research_triage_payload(
        research_triage_id=research_triage_id,
        review_set_id=str(review_set["review_set_id"]),
        run_revision_id=run_revision_id,
        as_of=as_of,
        published_at=published_at or f"{as_of}T15:00:00+09:00",
        macro_context_id=macro_context_id,
        review_basis_research_triage_id=review_set["review_basis"][
            "judged_through_research_triage_id"
        ],
        entries=entries,
    )
