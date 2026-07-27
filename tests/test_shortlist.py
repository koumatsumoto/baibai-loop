from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from baibai_engine.screening.shortlist import (
    SelectionBinding,
    Shortlist,
    ShortlistConflictError,
    ShortlistService,
)
from baibai_engine.screening.shortlist_cli import reevaluation_task_suggestions


def _narrative() -> dict[str, str]:
    return {
        "ploss": "中低",
        "why": "一時的な受注端境で売られている",
        "temporary": "翌期の受注残は積み上がっている",
        "structural": "構造的な需要毀損の証拠はない",
        "survive": "net cashで5年の下振れに耐えられる",
        "unlock": "自己株買いと増配で還元余地がある",
        "counter": "受注が構造的に鈍化している可能性",
        "research": "受注残と粗利率の推移を一次IRで確認",
        "value": "FV乖離が大きく深掘り価値が高い",
        "prov": "深掘り最優先",
    }


def _shortlist() -> Shortlist:
    return Shortlist.model_validate(
        {
            "schema_version": 2,
            "kind": "shortlist",
            "shortlist_id": "shortlist-20260719-base",
            "selection_id": "selection-test",
            "run_revision_id": "runrev-test",
            "as_of": "2026-07-19",
            "published_at": "2026-07-19T14:00:00+09:00",
            "profile": "default",
            "macro_context_id": "macro-context-2026-07-19-base",
            "entries": [
                {
                    "ticker": "2331",
                    "decision": "selected",
                    "reason": "一次IRへ進める",
                    "narrative": _narrative(),
                },
                {"ticker": "0001", "decision": "rejected", "reason": "根拠が弱い"},
            ],
        }
    )


def _binding() -> SelectionBinding:
    shortlist = _shortlist()
    return SelectionBinding(
        selection_id=shortlist.selection_id,
        run_revision_id=shortlist.run_revision_id,
        as_of=shortlist.as_of,
        profile=shortlist.profile,
        macro_context_id=shortlist.macro_context_id,
        candidate_tickers=frozenset({"2331", "0001"}),
    )


def test_shortlist_publish_is_immutable_and_identical_retry_is_no_change(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    service = ShortlistService(path)
    shortlist = _shortlist()
    service.publish(shortlist, selection=_binding())
    service.publish(shortlist, selection=_binding())

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM shortlist").fetchone()[0] == 1
    changed = shortlist.model_copy(
        update={"entries": shortlist.entries[:1]},
    )
    with pytest.raises(ShortlistConflictError):
        service.publish(changed, selection=_binding())


def test_shortlist_rejects_duplicate_ticker_and_missing_selected() -> None:
    payload = _shortlist().payload()
    payload["entries"] = [
        {"ticker": "2331", "decision": "rejected", "reason": "a"},
        {"ticker": "2331", "decision": "rejected", "reason": "b"},
    ]
    with pytest.raises(ValidationError):
        Shortlist.model_validate(payload)


def test_shortlist_selected_entry_requires_narrative() -> None:
    payload = _shortlist().payload()
    payload["entries"] = [{"ticker": "2331", "decision": "selected", "reason": "深掘りへ"}]
    with pytest.raises(ValidationError):
        Shortlist.model_validate(payload)


def test_shortlist_rejected_entry_forbids_narrative() -> None:
    payload = _shortlist().payload()
    payload["entries"] = [
        {"ticker": "2331", "decision": "selected", "reason": "深掘りへ", "narrative": _narrative()},
        {"ticker": "0001", "decision": "rejected", "reason": "弱い", "narrative": _narrative()},
    ]
    with pytest.raises(ValidationError):
        Shortlist.model_validate(payload)


def test_reevaluation_suggestion_emits_runnable_task_add_for_rejected_with_earnings_date() -> None:
    suggestions = reevaluation_task_suggestions(
        _shortlist(), {"2331": "2026-07-30", "0001": "2026-08-06"}
    )

    assert suggestions == [
        (
            "baibai-engine task add --kind follow-up --ticker 0001 "
            '--title "0001 決算で見送り判断を再評価" '
            "--due 2026-08-06 --event-date 2026-08-06 "
            '--event-label "0001 決算"'
        )
    ]


def test_reevaluation_suggestion_notes_missing_earnings_date() -> None:
    suggestions = reevaluation_task_suggestions(_shortlist(), {"2331": "2026-07-30", "0001": None})

    assert len(suggestions) == 1
    note = suggestions[0]
    assert note.startswith("# 0001")
    assert "決算日未公表" in note
    assert "--ticker 0001" in note
