from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from baibai_engine.macro.models import MacroContextDocument
from baibai_engine.macro.service import MacroContextConflictError, MacroContextService


def _document(
    *,
    context_id: str = "macro-context-2026-07-19-base",
    as_of: str = "2026-07-19",
    valid_until: str = "2026-08-19",
    published_at: str = "2026-07-19T12:00:00+09:00",
) -> MacroContextDocument:
    return MacroContextDocument.model_validate(
        {
            "schema_version": 2,
            "kind": "macro-context",
            "context_id": context_id,
            "as_of": as_of,
            "valid_until": valid_until,
            "published_at": published_at,
            "summary": "金利上昇を注視する。",
            "inputs": {
                "articles": [
                    {
                        "input_id": "boj",
                        "source": "BOJ",
                        "title": "Statement",
                        "url": "https://www.boj.or.jp/example",
                        "published_at": published_at,
                        "accessed_at": published_at,
                        "status": "ok",
                        "used_for": "政策金利の確認",
                    }
                ],
                "indicator_series": [],
            },
            "material_deltas": [
                {
                    "channel": "discount_rate",
                    "direction": "adverse",
                    "materiality": "medium",
                    "summary": "割引率が上昇した。",
                    "used_for": "必要利回り",
                    "source_ids": ["boj"],
                }
            ],
            "sizing_cautions": [],
            "research_questions": [],
            "refresh_triggers": ["次回会合"],
            "changes_since_previous": [],
        }
    )


def test_publish_is_immutable_and_requires_compare_and_swap_head(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    service = MacroContextService(path)
    first = _document()
    service.publish(first, expected_head=None)

    with pytest.raises(MacroContextConflictError):
        service.publish(
            _document(
                context_id="macro-context-2026-07-20-stale",
                as_of="2026-07-20",
                published_at="2026-07-20T12:00:00+09:00",
            ),
            expected_head=None,
        )
    with pytest.raises(MacroContextConflictError):
        service.publish(first, expected_head=first.context_id)

    second = _document(
        context_id="macro-context-2026-07-19-revision",
        published_at="2026-07-19T13:00:00+09:00",
    )
    service.publish(second, expected_head=first.context_id)

    assert service.head_id() == second.context_id
    assert service.latest_for(date(2026, 7, 18)) is None
    assert service.latest_for(date(2026, 7, 19)) == second
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT context_id, supersedes_id FROM macro_context ORDER BY published_at"
        ).fetchall()
    assert rows == [(first.context_id, None), (second.context_id, first.context_id)]


def test_explicit_future_context_is_rejected(tmp_path: Path) -> None:
    service = MacroContextService(tmp_path / "app.sqlite")
    document = _document()
    service.publish(document, expected_head=None)

    with pytest.raises(MacroContextConflictError, match="future"):
        service.get_for(document.context_id, as_of=date(2026, 7, 18))


@pytest.mark.parametrize(
    "change",
    [
        {"valid_until": "2026-07-18"},
        {"published_at": "2026-07-18T12:00:00+09:00"},
        {"context_id": "invalid"},
    ],
)
def test_document_rejects_invalid_canonical_fields(change: dict[str, str]) -> None:
    payload = _document().payload()
    payload.update(change)
    with pytest.raises(ValidationError):
        MacroContextDocument.model_validate(payload)
