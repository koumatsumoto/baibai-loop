from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.helpers.screening_selection import value_carry_selection_payload

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
    return {
        "run_id": f"screening-{as_of.replace('-', '')}",
        "run_date": as_of,
        "asof_date": as_of,
        "run_at": run_at,
        "universe_size": 1,
        "screening_rules_hash": "rules-preflight-fixture",
        "er_model_version": "expected-return-v1",
        "candidates": [
            {
                "ticker": "2331",
                "name": "ALSOK",
                "market_cap_oku": 1000.0,
                "avg_turnover_oku": 10.0,
                "listing_span_days": 1000,
                "jpx_flags": [],
                "evidence_hits": [],
                "metrics": {"er_annual": 0.1},
            }
        ],
    }


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


def _summary(path: Path, *, run_id: str, selection_id: str, as_of: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workflow": "cloud-daily-batch",
                "repository": "example/baibai-loop",
                "trigger": "schedule",
                "run_attempt": "1",
                "run_url": "https://github.com/example/actions/runs/1",
                "finished_at": f"{as_of}T10:00:00+00:00",
                "duration_seconds": 10.0,
                "overall_outcome": "succeeded",
                "publish_state": "published",
                "asof": as_of,
                "delivery": {"status": "delivered", "detail": ""},
                "workflow_errors": [],
                "execution": {
                    "kind": "available",
                    "summary": {
                        "schema_version": 1,
                        "asof": as_of,
                        "outcome": "succeeded",
                        "started_at": f"{as_of}T09:59:00+00:00",
                        "finished_at": f"{as_of}T10:00:00+00:00",
                        "duration_seconds": 60.0,
                        "local_export": True,
                        "batches": [
                            {
                                "batch_name": "screening",
                                "datasets": ["screening-run", "screening-selection"],
                                "status": "ok",
                                "duration_seconds": 30.0,
                                "errors": [],
                                "metrics": {
                                    "asof": as_of,
                                    "run_revision_id": run_id,
                                    "selection_id": selection_id,
                                    "universe": 1,
                                    "candidates": 1,
                                    "selected": 1,
                                    "edinet_quarantined_events": 0,
                                    "edinet_quarantined_tickers": 0,
                                    "edinet_quarantine_sample": "",
                                },
                            }
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def _canonical_shortlist(
    path: Path,
    *,
    as_of: str,
    run_id: str,
    selection_id: str,
) -> None:
    initialize_database(path)
    payload = {
        "schema_version": 4,
        "shortlist_id": "shortlist-20260806-canonical",
        "as_of": as_of,
        "run_revision_id": run_id,
        "selection_id": selection_id,
        "entries": [{"ticker": "2331", "decision": "rejected"}],
    }
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


def test_preflight_reuses_exact_cloud_publication_and_resolves_canonical_previous(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    summary = tmp_path / "latest-run.json"
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
    _summary(summary, run_id=current, selection_id="selection-cloud", as_of="2026-08-07")
    with sqlite3.connect(runs) as connection:
        before = connection.execute("SELECT count(*) FROM screening_run").fetchone()[0]

    reports = [
        shortlist_preflight(
            as_of=date(2026, 8, 7),
            cloud_summary_path=summary,
            runs_db_path=runs,
            app_db_path=app,
            repo_root=tmp_path,
            git_state=GitState(commit=COMMIT, clean=True),
        )
        for _ in range(2)
    ]

    assert all(report["decision"] == "reuse" for report in reports)
    assert reports[0]["reusable"] == {
        "source": "cloud-batch",
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
    summary = tmp_path / "latest-run.json"
    initialize_database(app)
    store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    current = store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    _selection(store, current, "selection-cloud", datetime.now(UTC).isoformat())
    _summary(summary, run_id=current, selection_id="selection-cloud", as_of="2026-08-07")

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        cloud_summary_path=summary,
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit="b" * 40, clean=True),
    )

    assert report["decision"] == "rerun-current-code"
    assert report["reasons"] == []


def test_preflight_reuses_interrupted_current_code_publication_on_resume(tmp_path: Path) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    summary = tmp_path / "latest-run.json"
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
    _summary(summary, run_id=cloud_run, selection_id="selection-cloud", as_of="2026-08-07")

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        cloud_summary_path=summary,
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit="b" * 40, clean=True),
    )

    assert report["decision"] == "reuse"
    assert report["reusable"] == {
        "source": "local-current-code",
        "run_revision_id": "run-local-current",
        "selection_id": "selection-local-current",
        "application_git_commit": "b" * 40,
    }


def test_preflight_ignores_non_default_profile_when_resuming_current_code(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    summary = tmp_path / "latest-run.json"
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
    _summary(summary, run_id=cloud_run, selection_id="selection-cloud", as_of="2026-08-07")

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        cloud_summary_path=summary,
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
    summary = tmp_path / "latest-run.json"
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
    _summary(summary, run_id=cloud_run, selection_id="selection-cloud", as_of="2026-08-07")

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        cloud_summary_path=summary,
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit="b" * 40, clean=True),
    )

    assert report["decision"] == "resume-current-code"
    assert report["reusable"]["run_revision_id"] == "run-local-current"
    assert report["reusable"]["selection_id"] is None


def test_preflight_blocks_multiple_current_code_selections_on_the_same_run(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    summary = tmp_path / "latest-run.json"
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
    _summary(summary, run_id=cloud, selection_id="selection-cloud", as_of="2026-08-07")

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        cloud_summary_path=summary,
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit="b" * 40, clean=True),
    )

    assert report["decision"] == "blocked"
    assert report["current_code"]["status"] == "ambiguous"
    assert {item["selection_id"] for item in report["current_code"]["candidates"]} == {
        "selection-current-a",
        "selection-current-b",
    }


def test_preflight_prefers_exact_cloud_publication_when_head_matches(tmp_path: Path) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    summary = tmp_path / "latest-run.json"
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
    _summary(summary, run_id=cloud, selection_id="selection-cloud", as_of="2026-08-07")

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        cloud_summary_path=summary,
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit=COMMIT, clean=True),
    )

    assert report["decision"] == "reuse"
    assert report["reusable"]["source"] == "cloud-batch"
    assert report["reusable"]["selection_id"] == "selection-cloud"


def test_preflight_blocks_dirty_or_missing_local_cloud_publication(tmp_path: Path) -> None:
    app = tmp_path / "app.sqlite"
    runs = tmp_path / "runs.sqlite"
    summary = tmp_path / "latest-run.json"
    initialize_database(app)
    ScreeningRunStore(runs).publish_run(
        _run("2026-08-06", "2026-08-06T12:00:00+09:00"),
        run_revision_id="unrelated",
    )
    _summary(summary, run_id="run-cloud", selection_id="selection-cloud", as_of="2026-08-07")

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        cloud_summary_path=summary,
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit=COMMIT, clean=False),
    )

    assert report["decision"] == "blocked"
    assert "checked-out worktree is dirty" in report["reasons"]
    assert any("pull-runs" in reason for reason in report["reasons"])


def test_preflight_lists_ambiguous_previous_publications_instead_of_guessing(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    summary = tmp_path / "latest-run.json"
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
    _summary(summary, run_id=current, selection_id="selection-cloud", as_of="2026-08-07")

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        cloud_summary_path=summary,
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
        cloud_summary_path=summary,
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


def test_preflight_rejects_contradictory_cloud_asof_values(tmp_path: Path) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    summary = tmp_path / "latest-run.json"
    initialize_database(app)
    store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    current = store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    _selection(store, current, "selection-cloud", "2026-08-07T12:10:00+00:00")
    _summary(summary, run_id=current, selection_id="selection-cloud", as_of="2026-08-07")
    payload = json.loads(summary.read_text(encoding="utf-8"))
    payload["execution"]["summary"]["asof"] = "2026-08-06"
    summary.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="contradictory as-of"):
        shortlist_preflight(
            as_of=date(2026, 8, 7),
            cloud_summary_path=summary,
            runs_db_path=runs,
            app_db_path=app,
            repo_root=tmp_path,
            git_state=GitState(commit=COMMIT, clean=True),
        )


def test_preflight_rejects_malformed_or_contradictory_cloud_summary(tmp_path: Path) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    summary = tmp_path / "latest-run.json"
    initialize_database(app)
    store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    current = store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    _selection(store, current, "selection-cloud", "2026-08-07T12:10:00+00:00")
    _summary(summary, run_id=current, selection_id="selection-cloud", as_of="2026-08-07")
    payload = json.loads(summary.read_text(encoding="utf-8"))
    payload["execution"]["summary"]["outcome"] = "failed"
    payload["execution"]["summary"]["batches"].append(
        {**payload["execution"]["summary"]["batches"][0]}
    )
    summary.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate batch names"):
        shortlist_preflight(
            as_of=date(2026, 8, 7),
            cloud_summary_path=summary,
            runs_db_path=runs,
            app_db_path=app,
            repo_root=tmp_path,
            git_state=GitState(commit=COMMIT, clean=True),
        )


def test_preflight_rejects_unknown_or_non_scalar_screening_metrics(tmp_path: Path) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    summary = tmp_path / "latest-run.json"
    initialize_database(app)
    store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    current = store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    _selection(store, current, "selection-cloud", "2026-08-07T12:10:00+00:00")
    _summary(summary, run_id=current, selection_id="selection-cloud", as_of="2026-08-07")
    payload = json.loads(summary.read_text(encoding="utf-8"))
    payload["execution"]["summary"]["batches"][0]["metrics"]["unexpected_nested"] = {
        "bad": float("nan")
    }
    summary.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="metrics mismatch"):
        shortlist_preflight(
            as_of=date(2026, 8, 7),
            cloud_summary_path=summary,
            runs_db_path=runs,
            app_db_path=app,
            repo_root=tmp_path,
            git_state=GitState(commit=COMMIT, clean=True),
        )


def test_preflight_rejects_malformed_typed_errors(tmp_path: Path) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    summary = tmp_path / "latest-run.json"
    initialize_database(app)
    store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    current = store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    _selection(store, current, "selection-cloud", "2026-08-07T12:10:00+00:00")
    _summary(summary, run_id=current, selection_id="selection-cloud", as_of="2026-08-07")
    payload = json.loads(summary.read_text(encoding="utf-8"))
    payload["workflow_errors"] = [{"bogus": 1}]
    summary.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="error has an invalid contract"):
        shortlist_preflight(
            as_of=date(2026, 8, 7),
            cloud_summary_path=summary,
            runs_db_path=runs,
            app_db_path=app,
            repo_root=tmp_path,
            git_state=GitState(commit=COMMIT, clean=True),
        )


def test_preflight_does_not_substitute_when_canonical_previous_was_pruned(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    summary = tmp_path / "latest-run.json"
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
    _summary(summary, run_id=current, selection_id="selection-cloud", as_of="2026-08-07")

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        cloud_summary_path=summary,
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
    summary = tmp_path / "latest-run.json"
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
    _summary(summary, run_id=current, selection_id="selection-cloud", as_of="2026-08-07")

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        cloud_summary_path=summary,
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
    summary = tmp_path / "latest-run.json"
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
    _summary(summary, run_id=cloud, selection_id="selection-cloud", as_of="2026-08-07")

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        cloud_summary_path=summary,
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
    summary = tmp_path / "latest-run.json"
    initialize_database(app)
    run_store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    current = run_store.publish_run(
        _run("2026-08-07", "2026-08-07T12:00:00+09:00"),
        run_revision_id="run-cloud",
    ).publication_id
    foreign_selector = ScreeningRunStore(runs, git_commit_factory=lambda: "b" * 40)
    _selection(foreign_selector, current, "selection-cloud", "2026-08-07T03:10:00+00:00")
    _summary(summary, run_id=current, selection_id="selection-cloud", as_of="2026-08-07")

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        cloud_summary_path=summary,
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
    summary = tmp_path / "latest-run.json"
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
    _summary(summary, run_id=current, selection_id="selection-cloud", as_of="2026-08-07")

    report = shortlist_preflight(
        as_of=date(2026, 8, 7),
        cloud_summary_path=summary,
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit=COMMIT, clean=True),
    )

    assert report["previous"]["status"] == "resolved-shortlist"
    assert report["previous"]["as_of"] == "2026-08-06"
    assert report["previous"]["run_revision_id"] == "run-old-pruned"


def test_error_vocabulary_matches_the_workflow_summary_producer() -> None:
    """The preflight re-validates the producer contract without importing it at runtime,
    so the vocabularies live in two files. This bridge forces a producer addition (a new
    stage or code) to land in the preflight allowlist in the same change — the drift
    surfaced as a preflight crash on the first failed run after the lake cutover added
    the hydrate / publish-lake stages.
    """

    from baibai_batch.observability import summary as workflow_summary

    assert set(workflow_summary.ERROR_STAGES) == shortlist_preflight_module._ERROR_STAGES
    assert set(workflow_summary.ERROR_CODES) == shortlist_preflight_module._ERROR_CODES
    assert set(workflow_summary.ERROR_IMPACTS) == {"failed", "degraded"}


def test_preflight_reads_a_failed_lake_publication_run_and_blocks_with_reasons(
    tmp_path: Path,
) -> None:
    """A cloud run that failed at publish-lake is a normal operational state: the
    summary must parse, and the decision must say what stands in the way rather
    than refusing the summary itself.
    """

    runs = tmp_path / "runs.sqlite"
    app = tmp_path / "app.sqlite"
    summary = tmp_path / "latest-run.json"
    initialize_database(app)
    store = ScreeningRunStore(runs, git_commit_factory=lambda: COMMIT)
    store.publish_run(
        _run("2026-08-18", "2026-08-18T12:00:00+09:00"),
        run_revision_id="run-previous",
    )
    _summary(summary, run_id="run-cloud", selection_id="selection-cloud", as_of="2026-08-19")
    payload = json.loads(summary.read_text(encoding="utf-8"))
    payload["overall_outcome"] = "failed"
    payload["publish_state"] = "generated"
    payload["workflow_errors"] = [
        {
            "code": "step_failed",
            "stage": "publish-lake",
            "impact": "failed",
            "message": "GitHub Actions step failed; open the run log",
        }
    ]
    summary.write_text(json.dumps(payload), encoding="utf-8")

    report = shortlist_preflight(
        as_of=date(2026, 8, 19),
        cloud_summary_path=summary,
        runs_db_path=runs,
        app_db_path=app,
        repo_root=tmp_path,
        git_state=GitState(commit=COMMIT, clean=True),
    )

    assert report["decision"] == "blocked"
    assert "cloud batch did not finish with a reusable outcome" in report["reasons"]
