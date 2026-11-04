from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import UTC, datetime

from baibai_engine.data_collection.providers.jquants.contract import JQUANTS_SOURCE_PAGES_DDL
from baibai_jobs.acquisition.jquants_pending import iter_pending_financial_requests


def test_pending_reads_only_original_pages_and_retains_partial(tmp_path) -> None:
    path = tmp_path / "originals.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(JQUANTS_SOURCE_PAGES_DDL)
        for identity, source, body in [
            ("complete", "financial_summaries", {"data": []}),
            ("partial", "financial_summaries", {"data": [], "pagination_key": "next"}),
            ("other", "daily_bars", {"data": []}),
        ]:
            conn.execute(
                "INSERT INTO jquants_source_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    identity,
                    0,
                    source,
                    json.dumps({"date": "20261102"}),
                    "2026-11-03T00:00:00.000000+00:00",
                    "2026-11-03",
                    gzip.compress(json.dumps(body).encode(), mtime=0),
                ),
            )
    entries = list(
        iter_pending_financial_requests(path, known_at=datetime(2026, 11, 4, tzinfo=UTC))
    )
    assert [entry.request_id for entry in entries] == ["complete", "partial"]
    assert entries[0].request is not None
    assert entries[0].request.records() == []
    assert entries[1].request is None
    assert entries[1].first_query == {"date": "20261102"}
    assert entries[1].last_received_at == datetime(2026, 11, 3, tzinfo=UTC)


def test_select_pending_uses_snapshot_capture_not_release_or_l2() -> None:
    from baibai_jobs.acquisition.jquants_pending import select_pending_financial_handoffs

    capture = datetime(2026, 11, 3, 20, 52, tzinfo=UTC)
    entries = [
        {
            "stage_id": "before",
            "source": "jquants-financial",
            "integration": {"status": "integrated", "integrated_at": "2026-11-03T20:51:00+00:00"},
        },
        {
            "stage_id": "after",
            "source": "jquants-financial",
            "integration": {"status": "integrated", "integrated_at": "2026-11-03T20:53:00+00:00"},
        },
        {
            "stage_id": "same",
            "source": "jquants-financial",
            "integration": {"status": "integrated", "integrated_at": "2026-11-03T20:52:00+00:00"},
        },
        {"stage_id": "pending", "source": "jquants-financial", "integration": None},
        {
            "stage_id": "late",
            "source": "jquants-financial",
            "integration": {"status": "needs_explicit_recovery"},
            "late_before_integrated": True,
        },
        {"stage_id": "bad", "source": "jquants-financial", "invalid": "ValueError"},
    ]
    selected, unavailable = select_pending_financial_handoffs(entries, captured_at=capture)
    assert [entry["stage_id"] for entry in selected] == ["after", "same", "pending", "late"]
    assert unavailable == ("bad",)
    all_selected, _ = select_pending_financial_handoffs(entries, captured_at=None)
    assert len(all_selected) == 5
