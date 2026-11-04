from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import UTC, datetime

from baibai_engine.data_collection.providers.jquants.contract import JQUANTS_SOURCE_PAGES_DDL
from baibai_jobs.acquisition.jquants_pending import iter_pending_requests


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
        iter_pending_requests(
            path, source="financial_summaries", known_at=datetime(2026, 11, 4, tzinfo=UTC)
        )
    )
    assert [entry.request_id for entry in entries] == ["complete", "partial"]
    assert entries[0].request is not None
    assert entries[0].request.records() == []
    assert entries[1].request is None
    assert entries[1].first_query == {"date": "20261102"}
    assert entries[1].last_received_at == datetime(2026, 11, 3, tzinfo=UTC)


def test_only_exact_release_marker_proves_inclusion() -> None:
    from baibai_jobs.acquisition.jquants_pending import select_unproven_handoffs

    def entry(stage, status, generation):
        return {
            "stage_id": stage,
            "source": "jquants-financial",
            "manifest_sha256": "c" * 64,
            "request_ids_complete": True,
            "integration": {
                "source": "jquants-financial",
                "manifest_sha256": "c" * 64,
                "status": status,
                "canonical_generations": generation,
                "integrated_at": "2020-01-01T00:00:00+00:00",
            },
        }

    exact = {"lake_release_id": "fixed", "lake_manifest_sha256": "a" * 64}
    older = {"lake_release_id": "older", "lake_manifest_sha256": "b" * 64}
    entries = [
        entry("same", "integrated", exact),
        entry("older", "integrated", older),
        entry("recover", "needs_explicit_recovery", exact),
        {"stage_id": "pending", "source": "jquants-financial", "integration": None},
        {"stage_id": "bad", "source": "jquants-financial", "invalid": "ValueError"},
    ]
    selected, unavailable = select_unproven_handoffs(
        entries, release_id="fixed", manifest_sha256="a" * 64, source="jquants-financial"
    )
    assert [entry["stage_id"] for entry in selected] == ["older", "recover", "pending"]
    assert unavailable == ("bad",)
    entries[1]["request_ids"] = ["already-in-l1"]
    selected, _ = select_unproven_handoffs(
        entries,
        release_id="fixed",
        manifest_sha256="a" * 64,
        source="jquants-financial",
        l1_request_ids=frozenset({"already-in-l1"}),
    )
    assert [entry["stage_id"] for entry in selected] == ["recover", "pending"]


def test_unreadable_handoff_is_explicit_unknown(tmp_path) -> None:
    path = tmp_path / "receipts.sqlite"
    path.write_bytes(b"not a sqlite database")
    entries = list(
        iter_pending_requests(path, source="daily_bars", known_at=datetime(2026, 11, 4, tzinfo=UTC))
    )
    assert len(entries) == 1
    assert entries[0].request is None
    assert entries[0].error == "invalid handoff database"


def test_stale_integration_marker_does_not_prove_inclusion() -> None:
    from baibai_jobs.acquisition.jquants_pending import select_unproven_handoffs

    entry = {
        "stage_id": "stale",
        "source": "jquants-financial",
        "manifest_sha256": "a" * 64,
        "request_ids": ["id"],
        "integration": {
            "status": "integrated",
            "source": "jquants-financial",
            "manifest_sha256": "b" * 64,
            "canonical_generations": {"lake_release_id": "r", "lake_manifest_sha256": "c" * 64},
        },
    }
    selected, _ = select_unproven_handoffs(
        [entry],
        release_id="r",
        manifest_sha256="c" * 64,
        source="jquants-financial",
        l1_request_ids=frozenset({"id"}),
    )
    assert selected == [entry]


def test_legacy_handoff_without_complete_id_contract_is_retained() -> None:
    from baibai_jobs.acquisition.jquants_pending import select_unproven_handoffs

    entry = {
        "stage_id": "legacy",
        "source": "jquants-financial",
        "manifest_sha256": "a" * 64,
        "request_ids": ["id"],
        "integration": {
            "status": "integrated",
            "source": "jquants-financial",
            "manifest_sha256": "a" * 64,
            "canonical_generations": {"lake_release_id": "r", "lake_manifest_sha256": "b" * 64},
        },
    }
    selected, _ = select_unproven_handoffs(
        [entry],
        release_id="r",
        manifest_sha256="b" * 64,
        source="jquants-financial",
        l1_request_ids=frozenset({"id"}),
    )
    assert selected == [entry]
