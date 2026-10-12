"""Web/外部consumer向けのResearch Triage読取facade。実装はResearchが所有する。"""

from __future__ import annotations

from pathlib import Path

from baibai_engine.research.triage import query
from baibai_engine.research.triage.contracts import ResearchTriage


def list_research_triage_payloads(path: Path) -> list[dict[str, object]]:
    return query.list_research_triage_payloads(path)


def research_triage_payloads_for_review_set(
    path: Path, review_set_id: str
) -> list[dict[str, object]]:
    return query.research_triage_payloads_for_review_set(path, review_set_id)


def latest_research_triage_payload(path: Path) -> dict[str, object] | None:
    return query.latest_research_triage_payload(path)


def research_triage_payload(path: Path, research_triage_id: str) -> dict[str, object] | None:
    return query.research_triage_payload(path, research_triage_id)


def current_research_triage(path: Path, research_triage_id: str) -> ResearchTriage | None:
    return query.current_research_triage(path, research_triage_id)


def research_triage_payload_hash(triage: ResearchTriage) -> str:
    return query.research_triage_payload_hash(triage)


__all__ = [
    "current_research_triage",
    "latest_research_triage_payload",
    "list_research_triage_payloads",
    "research_triage_payload",
    "research_triage_payload_hash",
    "research_triage_payloads_for_review_set",
]
