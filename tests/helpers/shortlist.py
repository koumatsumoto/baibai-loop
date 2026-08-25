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
        "why": "一時的な受注端境で売られている",
        "temporary": "翌期の受注残は積み上がっている",
        "structural": "構造的な需要毀損の証拠はない",
        "survive": "net cashで5年の下振れに耐えられる",
        "unlock": "自己株買いと増配で還元余地がある",
        "upside": "受注が平年並みに戻れば正常利益ベースでPER12倍相当まで",
        "downside": "受注が半減しても営業黒字を保ち、簿価純資産が下値を支える",
        "rr": "下値が資産で支えられる一方、正常化の上値が倍近い",
        "catalyst": "2Q決算で受注残の回復が確認できるか",
        "macro": "connectionのsizing cautionに該当なし",
        "counter": "受注が構造的に鈍化している可能性",
        "research": "受注残と粗利率の推移を一次IRで確認",
        "value": "FV乖離が大きく深掘り価値が高い",
        "prov": "深掘り最優先",
    }
    payload.update(overrides)
    return payload


def selected_entry(ticker: str = "2331", *, rank: int = 1, **overrides: Any) -> dict[str, Any]:
    """A `selected` entry, which is the only decision that carries a narrative."""

    entry: dict[str, Any] = {
        "ticker": ticker,
        "decision": "selected",
        "rank": rank,
        "reason": "一次IRへ進める",
        "narrative": narrative(),
    }
    entry.update(overrides)
    return entry


def rejected_entry(
    ticker: str,
    *,
    reason: str = "根拠が弱い",
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
    attention_policy_id: str = "value-carry-only-v1",
    attention_policy_hash: str = "a" * 64,
    attention_policy_parameters: Mapping[str, Any] | None = None,
    review_basis_shortlist_id: str | None = None,
    research_gate_contract_id: str = RESEARCH_GATE_CONTRACT_ID,
    entries: Sequence[Mapping[str, Any]] | None = None,
    schema_version: int = 5,
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
        "attention_policy_id": attention_policy_id,
        "attention_policy_hash": attention_policy_hash,
        "attention_policy_parameters": dict(
            attention_policy_parameters or {"value_carry_limit": 2}
        ),
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

    Six fields are copied from the selection rather than chosen: the publisher
    refuses a draft whose attention policy or review basis is not the one the
    selection it names was produced under.
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
        attention_policy_id=str(selection["attention_policy_id"]),
        attention_policy_hash=str(selection["attention_policy_hash"]),
        attention_policy_parameters=selection["attention_policy_parameters"],
        review_basis_shortlist_id=selection["review_basis"]["judged_through_shortlist_id"],
        entries=entries,
    )
