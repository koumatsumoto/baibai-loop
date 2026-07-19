from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from baibai_engine.screening.run_store import (
    RunStoreAmbiguousError,
    RunStoreConflictError,
    RunStoreNotFoundError,
    ScreeningRunReader,
    ScreeningRunStore,
    initialize_run_store,
)


def _run(
    *,
    as_of: str = "2026-07-08",
    run_at: str = "2026-07-09T01:59:42+09:00",
    ticker: str = "1301",
) -> dict[str, object]:
    compact = as_of.replace("-", "")
    return {
        "run_date": as_of,
        "asof_date": as_of,
        "universe_size": 3744,
        "filters": {"scope": "all-common-stocks"},
        "generated_by": "screening-cli-v1",
        "data_sources": ["j-quants-light"],
        "run_at": run_at,
        "run_id": f"screening-{compact}",
        "candidates": [
            {
                "ticker": ticker,
                "name": "極洋",
                "sector_33": "水産・農林業",
                "per_forward": 7.43,
                "per_trailing": 7.82,
                "pbr": 0.69,
                "metrics": {"dividend_yield": 0.021, "er_annual": 0.13},
                "evidence_hits": [],
            }
        ],
        "provider_status_lines": [],
        "fallback_lines": [],
    }


def _selection(ticker: str = "1301") -> dict[str, object]:
    return {
        "recommendations": [{"ticker": ticker, "rank": 1, "reason_tags": ["cheap"]}],
        "selection": {
            "asof": "2026-07-08",
            "profile": "default",
            "input_refs": {"candidates_ref": "run-revision-fixture"},
        },
    }


def test_run_store_has_independent_forward_schema(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"

    assert initialize_run_store(database) == 1
    assert initialize_run_store(database) == 1

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "screening_run",
            "screening_candidate",
            "screening_selection",
            "selection_entry",
        } <= tables


def test_run_publication_is_immutable_and_same_asof_keeps_revisions(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    first_payload = _run()

    first = store.publish_run(first_payload, run_revision_id="run-revision-a")
    retry = store.publish_run(first_payload)
    second = store.publish_run(
        _run(run_at="2026-07-09T02:30:00+09:00"),
        run_revision_id="run-revision-b",
    )

    assert first.inserted is True
    assert retry == type(retry)("run-revision-a", inserted=False)
    assert second.inserted is True
    changed = dict(first_payload)
    changed["universe_size"] = 999
    with pytest.raises(RunStoreConflictError):
        store.publish_run(changed)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM screening_run").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM screening_candidate").fetchone()[0] == 2


def test_run_parent_and_candidates_are_one_transaction(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    initialize_run_store(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER inject_candidate_failure
            BEFORE INSERT ON screening_candidate
            WHEN NEW.ticker = '1332'
            BEGIN
                SELECT RAISE(ABORT, 'injected candidate failure');
            END
            """
        )
    payload = _run()
    second_candidate = dict(payload["candidates"][0])  # type: ignore[index]
    second_candidate["ticker"] = "1332"
    payload["candidates"] = [
        payload["candidates"][0],  # type: ignore[index]
        second_candidate,
    ]

    with pytest.raises(sqlite3.IntegrityError, match="injected candidate failure"):
        ScreeningRunStore(database).publish_run(payload)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM screening_run").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM screening_candidate").fetchone()[0] == 0


def test_selection_binds_explicit_run_and_read_facade_exposes_metadata(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    store.publish_run(_run(), run_revision_id="run-revision-fixture")

    result = store.publish_selection(
        run_revision_id="run-revision-fixture",
        profile="default",
        macro_context_id="macro-context-2026-07-08-base",
        payload=_selection(),
        selection_id="selection-fixture",
    )
    retry = store.publish_selection(
        run_revision_id="run-revision-fixture",
        profile="default",
        macro_context_id="macro-context-2026-07-08-base",
        payload=_selection(),
        selection_id="selection-fixture",
    )

    assert result.inserted is True
    assert retry.inserted is False
    publication = ScreeningRunReader(database).get_selection("selection-fixture")
    assert publication is not None
    assert publication.selection_id == "selection-fixture"
    assert publication.run_revision_id == "run-revision-fixture"
    assert publication.as_of_date == "2026-07-08"
    assert publication.as_of == "2026-07-08"
    assert publication.profile == "default"
    assert publication.macro_context_id == "macro-context-2026-07-08-base"
    assert publication.payload == _selection()
    assert publication.entries == tuple(_selection()["recommendations"])  # type: ignore[arg-type]
    with sqlite3.connect(database) as connection:
        stored = json.loads(
            connection.execute(
                "SELECT payload FROM screening_selection WHERE selection_id = 'selection-fixture'"
            ).fetchone()[0]
        )
    assert stored == _selection()


def test_selection_rejects_unknown_or_cross_run_source(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    with pytest.raises(RunStoreNotFoundError):
        store.publish_selection(
            run_revision_id="missing",
            profile="default",
            macro_context_id=None,
            payload=_selection(),
        )
    store.publish_run(_run(), run_revision_id="run-a")
    store.publish_run(
        _run(as_of="2026-07-09", run_at="2026-07-10T01:00:00+09:00"),
        run_revision_id="run-b",
    )
    store.publish_selection(
        run_revision_id="run-a",
        profile="default",
        macro_context_id=None,
        payload=_selection(),
        selection_id="selection-a",
    )
    with pytest.raises(RunStoreConflictError, match="same run revision"):
        store.publish_selection(
            run_revision_id="run-b",
            profile="default",
            macro_context_id=None,
            payload=_selection(),
            source_selection_id="selection-a",
        )


def test_convenience_queries_fail_on_ambiguous_revision(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    store.publish_run(
        _run(as_of="2026-07-07", run_at="2026-07-08T01:00:00+09:00"),
        run_revision_id="previous-a",
    )
    store.publish_run(
        _run(as_of="2026-07-07", run_at="2026-07-08T02:00:00+09:00"),
        run_revision_id="previous-b",
    )
    store.publish_run(_run(), run_revision_id="current")
    reader = ScreeningRunReader(database)

    with pytest.raises(RunStoreAmbiguousError):
        reader.resolve_run(as_of_date="2026-07-07")
    with pytest.raises(RunStoreAmbiguousError):
        reader.previous_run(before_as_of_date="2026-07-08")
    assert reader.resolve_run(run_revision_id="previous-a") is not None
    latest = reader.latest_run()
    assert latest is not None
    assert latest.run_revision_id == "current"
    assert [run.run_revision_id for run in reader.list_runs()] == [
        "current",
        "previous-b",
        "previous-a",
    ]

    store.publish_run(
        _run(run_at="2026-07-09T03:00:00+09:00"),
        run_revision_id="current-b",
    )
    with pytest.raises(RunStoreAmbiguousError):
        reader.latest_run()


def test_reader_connection_is_query_only(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    ScreeningRunStore(database).publish_run(_run())
    reader = ScreeningRunReader(database)

    connection = reader._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM screening_run")
    finally:
        connection.close()


def test_reader_returns_none_for_missing_publications(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    initialize_run_store(database)
    reader = ScreeningRunReader(database)

    assert reader.latest_run() is None
    assert reader.get_run("missing") is None
    assert reader.get_selection("missing") is None
    assert reader.resolve_run(as_of_date="2026-07-08") is None


@pytest.mark.parametrize(
    ("evidence_hit", "message"),
    [
        (
            {"name": "x", "playbook_id": "p", "source_status": "invalid", "sizing_eligible": False},
            "source_status",
        ),
        (
            {"name": "x", "playbook_id": "p", "source_status": "warning", "sizing_eligible": True},
            "non-ok evidence",
        ),
        (
            {"name": "", "playbook_id": "p", "source_status": "ok", "sizing_eligible": True},
            "name must",
        ),
    ],
)
def test_run_rejects_invalid_evidence_contract(
    tmp_path: Path,
    evidence_hit: dict[str, object],
    message: str,
) -> None:
    payload = _run()
    candidate = dict(payload["candidates"][0])  # type: ignore[index]
    candidate["evidence_hits"] = [evidence_hit]
    payload["candidates"] = [candidate]

    with pytest.raises(ValueError, match=message):
        ScreeningRunStore(tmp_path / "runs.sqlite").publish_run(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("ticker", "123", "invalid format"),
        ("name", "", "name must"),
        ("evidence_hits", None, "evidence_hits must"),
    ],
)
def test_run_rejects_invalid_candidate_shape(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    payload = _run()
    candidate = dict(payload["candidates"][0])  # type: ignore[index]
    candidate[field] = value
    payload["candidates"] = [candidate]
    with pytest.raises(ValueError, match=message):
        ScreeningRunStore(tmp_path / "runs.sqlite").publish_run(payload)


def test_run_rejects_invalid_evidence_summary(tmp_path: Path) -> None:
    payload = _run()
    payload["evidence_hits_summary"] = {"cheap": -1}
    with pytest.raises(ValueError, match="non-negative integers"):
        ScreeningRunStore(tmp_path / "runs.sqlite").publish_run(payload)


@pytest.mark.parametrize("field", ["run_date", "asof_date", "run_at", "run_id", "candidates"])
def test_run_rejects_missing_retired_schema_root_fields(tmp_path: Path, field: str) -> None:
    payload = _run()
    del payload[field]

    with pytest.raises(ValueError, match=field):
        ScreeningRunStore(tmp_path / "runs.sqlite").publish_run(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("playbook_id", None, "playbook_id"),
        ("sizing_eligible", 1, "sizing_eligible must be boolean"),
    ],
)
def test_run_rejects_missing_or_mistyped_evidence_fields(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    payload = _run()
    candidate = dict(payload["candidates"][0])  # type: ignore[index]
    evidence_hit: dict[str, object] = {
        "name": "evidence",
        "playbook_id": "playbook",
        "source_status": "ok",
        "sizing_eligible": True,
    }
    if value is None:
        del evidence_hit[field]
    else:
        evidence_hit[field] = value
    candidate["evidence_hits"] = [evidence_hit]
    payload["candidates"] = [candidate]

    with pytest.raises(ValueError, match=message):
        ScreeningRunStore(tmp_path / "runs.sqlite").publish_run(payload)
