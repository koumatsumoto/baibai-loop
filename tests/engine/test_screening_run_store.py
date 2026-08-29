from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tests.helpers.screening_run import screening_candidate, screening_run_payload
from tests.helpers.screening_selection import ranked_selection_payload

from baibai_engine.screening.run_store import (
    RunStoreAmbiguousError,
    RunStoreNotFoundError,
    ScreeningRunReader,
    ScreeningRunStore,
    initialize_run_store,
)
from baibai_engine.screening.run_store.schema import RUN_STORE_SCHEMA_VERSION


def _run(
    *,
    as_of: str = "2026-07-08",
    run_at: str = "2026-07-09T01:59:42+09:00",
    ticker: str = "1301",
) -> dict[str, object]:
    return screening_run_payload(
        as_of=as_of,
        run_at=run_at,
        rules_hash="rules-fixture",
        model_id="expected-return-v1",
        candidates=[
            screening_candidate(
                ticker,
                metrics={"dividend_yield": 0.021, "er_annual": 0.13},
            )
        ],
    )


def _selection(
    *,
    ticker: str = "1301",
    asof: str = "2026-07-08",
    candidates_ref: str = "run-a",
    source_candidates: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    candidates = source_candidates or _run(as_of=asof, ticker=ticker)["candidates"]
    assert isinstance(candidates, list)
    return ranked_selection_payload(
        ticker=ticker,
        er_annual=0.13,
        rules_hash="rules-fixture",
        asof=asof,
        candidates_ref=candidates_ref,
        source_candidates=candidates,
    )


def test_current_schema_is_created_once(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"

    assert initialize_run_store(database) == RUN_STORE_SCHEMA_VERSION
    assert initialize_run_store(database) == RUN_STORE_SCHEMA_VERSION

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == RUN_STORE_SCHEMA_VERSION
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(screening_selection)").fetchall()
        }
    assert "publication_kind" not in columns
    assert "source_selection_id" not in columns


def test_obsolete_cache_is_rejected_instead_of_migrated(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE old_cache (value TEXT)")
        connection.execute("PRAGMA user_version = 3")

    with pytest.raises(RuntimeError, match="rebuild it"):
        initialize_run_store(database)


def test_run_and_ranked_set_publish_and_read_atomically(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database, git_commit_factory=lambda: "a" * 40)
    run = _run()
    store.publish_run(run, run_revision_id="run-a")
    selection = _selection(source_candidates=run["candidates"])  # type: ignore[arg-type]

    inserted = store.publish_selection(
        run_revision_id="run-a",
        profile="default",
        macro_context_id=None,
        payload=selection,
        selection_id="selection-a",
    )
    retry = store.publish_selection(
        run_revision_id="run-a",
        profile="default",
        macro_context_id=None,
        payload=selection,
        selection_id="selection-a",
    )

    assert inserted.inserted is True
    assert retry.inserted is False
    publication = ScreeningRunReader(database).get_selection("selection-a")
    assert publication is not None
    assert publication.entries == tuple(selection["ranked_set"])  # type: ignore[arg-type]
    assert publication.application_git_commit == "a" * 40


def test_ranked_set_must_match_source_candidate_value(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    run = _run()
    store.publish_run(run, run_revision_id="run-a")
    selection = _selection(source_candidates=run["candidates"])  # type: ignore[arg-type]
    selection["ranked_set"][0]["er_annual"] = 0.99  # type: ignore[index]
    selection["ranked_set"][0]["expected_return_pct"] = 99.0  # type: ignore[index]

    with pytest.raises(ValueError, match=r"ranked-set E\[r\]"):
        store.publish_selection(
            run_revision_id="run-a",
            profile="default",
            macro_context_id=None,
            payload=selection,
        )


def test_selection_requires_an_existing_run(tmp_path: Path) -> None:
    with pytest.raises(RunStoreNotFoundError):
        ScreeningRunStore(tmp_path / "runs.sqlite").publish_selection(
            run_revision_id="missing",
            profile="default",
            macro_context_id=None,
            payload=_selection(),
        )


def test_asof_lookup_rejects_ambiguous_revisions(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    store.publish_run(_run(run_at="2026-07-09T01:00:00+09:00"), run_revision_id="run-a")
    store.publish_run(_run(run_at="2026-07-09T02:00:00+09:00"), run_revision_id="run-b")

    with pytest.raises(RunStoreAmbiguousError):
        ScreeningRunReader(database).resolve_run(as_of_date="2026-07-08")


def test_prune_keeps_only_the_newest_run_and_its_selection(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    old = _run(as_of="2026-07-07", run_at="2026-07-08T01:00:00+09:00")
    new = _run()
    store.publish_run(old, run_revision_id="old")
    store.publish_run(new, run_revision_id="new")
    store.publish_selection(
        run_revision_id="old",
        profile="default",
        macro_context_id=None,
        payload=_selection(
            asof="2026-07-07", candidates_ref="old", source_candidates=old["candidates"]
        ),  # type: ignore[arg-type]
        selection_id="old-selection",
    )

    result = store.prune(keep=1)

    assert result.deleted_runs == 1
    assert result.deleted_selections == 1
    assert [item.run_revision_id for item in ScreeningRunReader(database).list_runs()] == ["new"]
