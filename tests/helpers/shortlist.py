"""The v5 shortlist payload and the narrative every selected entry carries.

Five files were each writing the fifteen-key narrative and the shortlist envelope,
so a schema move had to be applied five times — and two of them were still writing
v4 while production had moved on. The builder emits what `publish_shortlist`
accepts; a test that is about an older revision asks for it by name.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

RESEARCH_GATE_CONTRACT_ID = "research-gate-v1"


def narrative(**overrides: Any) -> dict[str, Any]:
    """The fifteen fields a `selected` entry must state, plus optional dated ones."""

    payload: dict[str, Any] = {
        "ploss": "中低",
        "why": "受注端境による一時的な減益を市場が恒常化と見ている可能性がある",
        "temporary": "翌期受注残の積み上がりは端境解消の仮説を支持する",
        "structural": "顧客集中が続く範囲では需要鈍化が構造要因である可能性も残る",
        "survive": "営業黒字と現預金余力は5年の調整期間を支えるが、受注半減時の資金流出は未確認",
        "unlock": "受注回復が粗利改善とcash flowへ波及すれば価値実現へつながる",
        "upside": "受注が平年並みに戻る場合の正常利益にPER12倍を置いた水準を暫定上値とする",
        "downside": "受注半減時の営業利益とcash flowから暫定下値を検証し、簿価を株価の床とはしない",
        "rr": "暫定上値と受注半減時の下値の比較では正の非対称があるが、下値のcash flow根拠は一次研究で確認する",
        "catalyst": "2Q決算の受注残と粗利率で判断を更新する",
        "macro": "金利上昇は借入負担を増やす方向に効くため、下値の資金流出検証へ反映する",
        "counter": "受注が構造的に鈍化している可能性",
        "research": "決算短信と説明資料で受注残・粗利率を確認し、回復が無ければ一時要因仮説を棄却する",
        "value": "現金保有より優れるかを一時要因と構造要因の識別で判定でき、他候補より一次リサーチ枠の追加価値がある",
        "prov": "一次リサーチへ進めるが、受注回復が粗利へ波及するかは未解決",
    }
    payload.update(overrides)
    return payload


def selected_entry(ticker: str = "2331", *, rank: int = 1, **overrides: Any) -> dict[str, Any]:
    """A `selected` entry, which is the only decision that carries a narrative."""

    entry: dict[str, Any] = {
        "ticker": ticker,
        "decision": "selected",
        "rank": rank,
        "reason": "一次リサーチへ進める：受注端境と構造鈍化を一次開示で識別できる",
        "narrative": narrative(),
    }
    entry.update(overrides)
    return entry


def rejected_entry(
    ticker: str,
    *,
    reason: str = "見送る：暫定上値が現値を上回らず、一次リサーチで識別する仮説がない",
    reject_class: str = "other",
    **overrides: Any,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "ticker": ticker,
        "decision": "rejected",
        "reason": reason,
        "reject_class": reject_class,
    }
    entry.update(overrides)
    return entry


def shortlist_payload(
    *,
    shortlist_id: str = "shortlist-20260719-base",
    selection_id: str = "selection-test",
    run_revision_id: str = "runrev-test",
    as_of: str = "2026-07-19",
    published_at: str | None = None,
    profile: str = "default",
    macro_context_id: str | None = None,
    review_basis_shortlist_id: str | None = None,
    research_gate_contract_id: str = RESEARCH_GATE_CONTRACT_ID,
    entries: Sequence[Mapping[str, Any]] | None = None,
    schema_version: int = 6,
    **extra: Any,
) -> dict[str, Any]:
    """A publishable shortlist document at the current schema version."""

    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "kind": "shortlist",
        "shortlist_id": shortlist_id,
        "selection_id": selection_id,
        "run_revision_id": run_revision_id,
        "as_of": as_of,
        "published_at": published_at or f"{as_of}T14:00:00+09:00",
        "profile": profile,
        "macro_context_id": macro_context_id,
        "review_basis_shortlist_id": review_basis_shortlist_id,
        "research_gate_contract_id": research_gate_contract_id,
        "entries": [dict(entry) for entry in (entries or [selected_entry()])],
    }
    payload.update(extra)
    return payload


def shortlist_from_selection(
    selection: Mapping[str, Any],
    *,
    shortlist_id: str,
    run_revision_id: str,
    as_of: str,
    entries: Sequence[Mapping[str, Any]],
    published_at: str | None = None,
    macro_context_id: str | None = None,
) -> dict[str, Any]:
    """The draft an operator writes against one published selection.

    Source identity and review basis are copied from the selection.
    """

    block = selection["selection"]
    assert isinstance(block, Mapping)
    return shortlist_payload(
        shortlist_id=shortlist_id,
        selection_id=str(selection["selection_id"]),
        run_revision_id=run_revision_id,
        as_of=as_of,
        published_at=published_at or f"{as_of}T15:00:00+09:00",
        profile=str(block["profile"]),
        macro_context_id=macro_context_id,
        review_basis_shortlist_id=selection["review_basis"]["judged_through_shortlist_id"],
        entries=entries,
    )
