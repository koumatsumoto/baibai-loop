from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.helpers.screening_run import screening_candidate, screening_run_payload
from tests.helpers.screening_selection import value_carry_selection_payload
from tests.helpers.shortlist import rejected_entry, shortlist_payload

from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.screening import shortlist_preflight as shortlist_preflight_module
from baibai_engine.screening.run_store import ScreeningRunStore
from baibai_engine.screening.shortlist_preflight import GitState, shortlist_preflight

COMMIT = "a" * 40


def test_git_state_rejects_head_change_during_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter((COMMIT, "", "b" * 40))
    monkeypatch.setattr(shortlist_preflight_module, "_git", lambda *_args: next(responses))

    state = shortlist_preflight_module._git_state(tmp_path)

    assert state == GitState(commit="b" * 40, clean=False)


def _run(as_of: str, run_at: str) -> dict[str, object]:
    return screening_run_payload(
        as_of=as_of,
        run_at=run_at,
        universe_size=1,
        rules_hash="rules-preflight-fixture",
        candidates=[
            screening_candidate(
                "2331", name="ALSOK", sector_33="サービス業", metrics={"er_annual": 0.1}
            )
        ],
    )


def _selection(
    store: ScreeningRunStore,
    run_id: str,
    selection_id: str,
    created: str,
    *,
    profile: str = "balanced",
) -> None:
    assert store._path is not None
    with sqlite3.connect(store._path) as connection:
        row = connection.execute(
            "SELECT asof_date FROM screening_run WHERE run_revision_id = ?",
            (run_id,),
        ).fetchone()
        candidate_rows = connection.execute(
            "SELECT payload FROM screening_candidate WHERE run_revision_id = ?",
            (run_id,),
        ).fetchall()
    assert row is not None
    asof = str(row[0])
    store.publish_selection(
        run_revision_id=run_id,
        profile=profile,
        macro_context_id=None,
        payload=value_carry_selection_payload(
            ticker="2331",
            er_annual=0.1,
            rules_hash="rules-preflight-fixture",
            asof=asof,
            profile=profile,
            candidates_ref=run_id,
            source_candidates=[json.loads(str(item[0])) for item in candidate_rows],
        ),
        selection_id=selection_id,
        created_at=datetime.fromisoformat(created),
    )


def _canonical_shortlist(
    path: Path,
    *,
    as_of: str,
    run_id: str,
    selection_id: str,
) -> None:
    initialize_database(path)
    payload = shortlist_payload(
        shortlist_id="shortlist-20260806-canonical",
        as_of=as_of,
        published_at=f"{as_of}T18:00:00+09:00",
        run_revision_id=run_id,
        selection_id=selection_id,
        entries=[rejected_entry("2331")],
    )
    connection = connect_rw(path)
    try:
        connection.execute(
            """
            INSERT INTO shortlist (
                shortlist_id, selection_id, run_revision_id, as_of, published_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload["shortlist_id"],
                selection_id,
                run_id,
                as_of,
                f"{as_of}T18:00:00+09:00",
                json.dumps(payload),
            ),
        )
    finally:
        connection.close()


def test_preflight_reuses_the_head_publication_and_resolves_canonical_previous(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    old_a = store.publish_run(
        _run("2026-08-06", "2026-08-06T12:00:00+09:00"),
        run_revision_id="run-old-a",
    ).publication_id
    old_b = store.publish_run(
        _run("2026-08-06", "2026-08-06T13:00:00+09:00"),
        run_revision_id="run-old-b",
    ).publication_id
    current = store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    _selection(store, old_a, "selection-old-a", "2026-08-06T12:10:00+00:00")
    _selection(store, old_b, "selection-old-b", "2026-08-06T13:10:00+00:00")
    _selection(store, current, "selection-cloud", "2026-08-07T12:10:00+00:00")
    _canonical_shortlist(
        app,
        as_of="2026-08-06",
        run_id=old_a,
        selection_id="selection-old-a",
    )
    with sqlite3.connect(runs) as connection:
        before = connection.execute("SELECT count(*) FROM screening_run").fetchone()[0]

    reports = [
        shortlist_preflight(
            as_of=date(2026, 8, 7),
            runs_db_path=runs,
            app_db_path=app,
            repo_root=tmp_path,
            git_state=GitState(commit=COMMIT, clean=True),
        )
        for _ in range(2)
    ]

    assert all(report["decision"] == "reuse" for report in reports)
    assert reports[0]["reusable"] == {
        "run_revision_id": "run-cloud",
        "selection_id": "selection-cloud",
        "application_git_commit": COMMIT,
    }
    assert reports[0]["previous"]["run_revision_id"] == "run-old-a"
    assert reports[0]["previous"]["source"] == "canonical-shortlist"
    with sqlite3.connect(runs) as connection:
        assert connection.execute("SELECT count(*) FROM screening_run").fetchone()[0] == before


def test_preflight_requests_one_current_code_rerun_when_commit_differs(tmp_path: Path) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    initialize_database(app)
    store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    current = store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    _selection(store, current, "selection-cloud", datetime.now(UTC).isoformat())

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit="b" * 40, clean=True),
    )

    assert report["decision"] == "rerun-current-code"
    assert report["reasons"] == []
    assert report["reusable"] is None


def test_preflight_reuses_interrupted_current_code_publication_on_resume(tmp_path: Path) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    initialize_database(app)
    cloud_store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    cloud_run = cloud_store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    _selection(cloud_store, cloud_run, "selection-cloud", "2026-08-07T03:10:00+00:00")
    current_store = ScreeningRunStore(runs, git_commit_factory=lambda: "b" * 40)
    current_run = current_store.publish_run(
        _run("2026-08-07", "2026-08-07T13:00:00+09:00"),
        run_revision_id="run-local-current",
    ).publication_id
    _selection(
        current_store,
        current_run,
        "selection-local-current",
        "2026-08-07T04:10:00+00:00",
    )

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit="b" * 40, clean=True),
    )

    assert report["decision"] == "reuse"
    assert report["reusable"] == {
        "run_revision_id": "run-local-current",
        "selection_id": "selection-local-current",
        "application_git_commit": "b" * 40,
    }


def test_preflight_ignores_non_default_profile_when_resuming_current_code(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    initialize_database(app)
    cloud_store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    cloud_run = cloud_store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    _selection(cloud_store, cloud_run, "selection-cloud", "2026-08-07T03:10:00+00:00")
    current_store = ScreeningRunStore(runs, git_commit_factory=lambda: "b" * 40)
    current_run = current_store.publish_run(
        _run("2026-08-07", "2026-08-07T13:00:00+09:00"),
        run_revision_id="run-local-current",
    ).publication_id
    _selection(
        current_store,
        current_run,
        "selection-local-current",
        "2026-08-07T04:10:00+00:00",
    )
    _selection(
        current_store,
        current_run,
        "selection-experimental",
        "2026-08-07T04:20:00+00:00",
        profile="experimental",
    )

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit="b" * 40, clean=True),
    )

    assert report["decision"] == "reuse"
    assert report["reusable"]["selection_id"] == "selection-local-current"


def test_preflight_resumes_select_only_for_current_code_run(tmp_path: Path) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    initialize_database(app)
    cloud_store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    cloud_run = cloud_store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    _selection(cloud_store, cloud_run, "selection-cloud", "2026-08-07T03:10:00+00:00")
    current_store = ScreeningRunStore(runs, git_commit_factory=lambda: "b" * 40)
    current_store.publish_run(
        _run("2026-08-07", "2026-08-07T13:00:00+09:00"),
        run_revision_id="run-local-current",
    )

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit="b" * 40, clean=True),
    )

    assert report["decision"] == "resume-current-code"
    assert report["reusable"]["run_revision_id"] == "run-local-current"
    assert report["reusable"]["selection_id"] is None


def test_preflight_reuses_the_newest_of_several_current_code_selections(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    initialize_database(app)
    cloud_store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    cloud = cloud_store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    _selection(cloud_store, cloud, "selection-cloud", "2026-08-07T03:00:00+00:00")
    current_store = ScreeningRunStore(runs, git_commit_factory=lambda: "b" * 40)
    current = current_store.publish_run(
        _run("2026-08-07", "2026-08-07T13:00:00+09:00"),
        run_revision_id="run-local-current",
    ).publication_id
    _selection(current_store, current, "selection-current-a", "2026-08-07T04:10:00+00:00")
    _selection(current_store, current, "selection-current-b", "2026-08-07T04:20:00+00:00")

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit="b" * 40, clean=True),
    )

    # Same commit, same day: the newest publication is reused and every candidate
    # is still listed, so nothing is hidden and nothing is pushed back to the reader.
    assert report["decision"] == "reuse"
    assert report["reusable"]["selection_id"] == "selection-current-b"
    assert {item["selection_id"] for item in report["current_code"]["candidates"]} == {
        "selection-current-a",
        "selection-current-b",
    }


def test_preflight_reuses_the_newest_head_publication_when_two_exist(tmp_path: Path) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    initialize_database(app)
    store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    cloud = store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    _selection(store, cloud, "selection-cloud", "2026-08-07T03:10:00+00:00")
    other = store.publish_run(
        _run("2026-08-07", "2026-08-07T13:00:00+09:00"),
        run_revision_id="run-local-other",
    ).publication_id
    _selection(store, other, "selection-local-other", "2026-08-07T04:10:00+00:00")

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit=COMMIT, clean=True),
    )

    assert report["decision"] == "reuse"
    assert report["reusable"]["selection_id"] == "selection-local-other"


def test_preflight_blocks_a_dirty_worktree(tmp_path: Path) -> None:
    app = tmp_path / "app.sqlite"
    runs = tmp_path / "runs.sqlite"
    initialize_database(app)
    ScreeningRunStore(runs).publish_run(
        _run("2026-08-06", "2026-08-06T12:00:00+09:00"),
        run_revision_id="unrelated",
    )

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit=COMMIT, clean=False),
    )

    assert report["decision"] == "blocked"
    assert report["reasons"] == ["checked-out worktree is dirty"]
    assert report["reusable"] is None


def test_preflight_lists_ambiguous_previous_publications_instead_of_guessing(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    initialize_database(app)
    store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    for suffix, hour in (("a", 12), ("b", 13)):
        run_id = store.publish_run(
            _run("2026-08-06", f"2026-08-06T{hour}:00:00+09:00"),
            run_revision_id=f"run-old-{suffix}",
        ).publication_id
        _selection(
            store,
            run_id,
            f"selection-old-{suffix}",
            f"2026-08-06T{hour}:10:00+00:00",
        )
    current = store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    _selection(store, current, "selection-cloud", "2026-08-07T12:10:00+00:00")

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit="b" * 40, clean=True),
    )

    previous = report["previous"]
    assert isinstance(previous, dict)
    assert previous["status"] == "ambiguous"
    assert report["decision"] == "blocked"
    assert {item["run_revision_id"] for item in previous["candidates"]} == {
        "run-old-a",
        "run-old-b",
    }
    assert "--previous-run-revision-id" in previous["selection_rule"]

    resolved = shortlist_preflight(
        as_of=date(2026, 8, 7),
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        previous_run_revision_id="run-old-a",
        git_state=GitState(commit="b" * 40, clean=True),
    )
    assert resolved["decision"] == "rerun-current-code"
    assert resolved["previous"]["status"] == "resolved"
    assert resolved["previous"]["source"] == "explicit-run"
    assert resolved["previous"]["selection_arguments"] == [
        "--previous-run-revision-id",
        "run-old-a",
    ]


def test_preflight_does_not_substitute_when_canonical_previous_was_pruned(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    noncanonical = store.publish_run(
        _run("2026-08-06", "2026-08-06T13:00:00+09:00"),
        run_revision_id="run-old-noncanonical",
    ).publication_id
    _selection(
        store,
        noncanonical,
        "selection-old-noncanonical",
        "2026-08-06T04:10:00+00:00",
    )
    current = store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    _selection(store, current, "selection-cloud", "2026-08-07T03:10:00+00:00")
    _canonical_shortlist(
        app,
        as_of="2026-08-06",
        run_id="run-old-pruned",
        selection_id="selection-old-pruned",
    )

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit="b" * 40, clean=True),
    )

    assert report["previous"]["status"] == "resolved-shortlist"
    assert report["previous"]["source"] == "canonical-shortlist-retained"
    assert report["previous"]["selection_arguments"] == [
        "--previous-shortlist-id",
        "shortlist-20260806-canonical",
    ]
    assert report["decision"] == "rerun-current-code"
    assert report["previous"]["run_revision_id"] == "run-old-pruned"


def test_preflight_uses_the_greatest_prior_run_even_when_it_has_no_selection(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    older = store.publish_run(
        _run("2026-08-05", "2026-08-05T12:00:00+09:00"),
        run_revision_id="run-older",
    ).publication_id
    _selection(store, older, "selection-older", "2026-08-05T03:10:00+00:00")
    store.publish_run(
        _run("2026-08-06", "2026-08-06T12:00:00+09:00"),
        run_revision_id="run-interrupted-prior",
    )
    current = store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    _selection(store, current, "selection-cloud", "2026-08-07T03:10:00+00:00")
    _canonical_shortlist(
        app,
        as_of="2026-08-05",
        run_id=older,
        selection_id="selection-older",
    )

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit=COMMIT, clean=True),
    )

    assert report["previous"]["status"] == "resolved"
    assert report["previous"]["as_of"] == "2026-08-06"
    assert report["previous"]["run_revision_id"] == "run-interrupted-prior"


def test_preflight_does_not_reuse_a_selection_from_an_unproven_commit(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    initialize_database(app)
    cloud_store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    cloud = cloud_store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    _selection(cloud_store, cloud, "selection-cloud", "2026-08-07T03:10:00+00:00")
    current_store = ScreeningRunStore(runs, git_commit_factory=lambda: "b" * 40)
    current = current_store.publish_run(
        _run("2026-08-07", "2026-08-07T13:00:00+09:00"),
        run_revision_id="run-current",
    ).publication_id
    foreign_selector = ScreeningRunStore(runs, git_commit_factory=lambda: "c" * 40)
    _selection(foreign_selector, current, "selection-foreign", "2026-08-07T04:10:00+00:00")

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit="b" * 40, clean=True),
    )

    assert report["decision"] == "resume-current-code"
    assert report["reusable"]["run_revision_id"] == "run-current"


def test_preflight_does_not_treat_foreign_selection_as_exact_cloud_output(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    initialize_database(app)
    run_store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    current = run_store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    foreign_selector = ScreeningRunStore(runs, git_commit_factory=lambda: "b" * 40)
    _selection(foreign_selector, current, "selection-cloud", "2026-08-07T03:10:00+00:00")

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit=COMMIT, clean=True),
    )

    assert report["decision"] == "resume-current-code"
    assert report["reusable"]["run_revision_id"] == "run-cloud"


def test_preflight_keeps_newer_canonical_previous_when_all_its_runs_were_pruned(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    older = store.publish_run(
        _run("2026-08-05", "2026-08-05T12:00:00+09:00"),
        run_revision_id="run-older",
    ).publication_id
    _selection(store, older, "selection-older", "2026-08-05T03:10:00+00:00")
    current = store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    _selection(store, current, "selection-cloud", "2026-08-07T03:10:00+00:00")
    _canonical_shortlist(
        app,
        as_of="2026-08-06",
        run_id="run-old-pruned",
        selection_id="selection-old-pruned",
    )

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit=COMMIT, clean=True),
    )

    assert report["previous"]["status"] == "resolved-shortlist"
    assert report["previous"]["as_of"] == "2026-08-06"
    assert report["previous"]["run_revision_id"] == "run-old-pruned"
