from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from baibai_engine.screening.shortlist import (
    ReviewedShortlist,
    ReviewedShortlistService,
    SelectionBinding,
    ShortlistConflictError,
)


def _shortlist() -> ReviewedShortlist:
    return ReviewedShortlist.model_validate(
        {
            "schema_version": 1,
            "kind": "reviewed-shortlist",
            "shortlist_id": "shortlist-20260719-base",
            "selection_id": "selection-test",
            "run_revision_id": "runrev-test",
            "as_of": "2026-07-19",
            "published_at": "2026-07-19T14:00:00+09:00",
            "profile": "default",
            "macro_context_id": "macro-context-2026-07-19-base",
            "entries": [
                {"ticker": "2331", "decision": "selected", "reason": "一次IRへ進める"},
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
    service = ReviewedShortlistService(path)
    shortlist = _shortlist()
    service.publish(shortlist, selection=_binding())
    service.publish(shortlist, selection=_binding())

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM reviewed_shortlist").fetchone()[0] == 1
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
        ReviewedShortlist.model_validate(payload)
