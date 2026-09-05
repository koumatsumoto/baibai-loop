from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from tests.helpers.macro_context import macro_context_payload
from tests.helpers.research_triage import research_entry, research_triage_payload, skip_entry
from tests.helpers.screening_sqlite import market_store_with_fetch_claim

from baibai_engine.appdb import LATEST_VERSION
from baibai_engine.appdb.json import canonical_json
from baibai_engine.macro.context.models import MacroContextDocument
from baibai_engine.macro.context.service import MacroContextService
from baibai_engine.macro.indicators.db import (
    ObservationRecord,
    insert_observations,
    open_connection,
)
from baibai_engine.macro.indicators.definitions import load_definitions
from baibai_engine.macro.reading.rules import DEFAULT_RULES_PATH as MACRO_READING_RULES_PATH
from baibai_engine.research.capital_allocation import (
    CapitalAllocationAssessment,
    capital_allocation_draft_sha256,
)
from baibai_engine.research.capital_allocation_scaffold import scaffold_capital_allocation
from baibai_engine.research.capital_allocation_service import (
    CapitalAllocationAssessmentService,
)
from baibai_engine.screening.run_store import ScreeningRunReader
from baibai_web import materialize as export_module
from baibai_web.api.server import create_app
from baibai_web.materialize import main
from baibai_web.readmodel.builders import build_meta
from baibai_web.readmodel.models import (
    CapitalAllocationAssessmentView,
    DashboardView,
    MacroContextView,
    MacroView,
    MetaView,
    OperationsView,
    ScreeningView,
    SecurityDetailView,
    TasksView,
)
from baibai_web.sources.db_sources import DbMetaSource

JST = ZoneInfo("Asia/Tokyo")


def _meta_source(root: Path) -> DbMetaSource:
    return DbMetaSource(
        root / "stores/application/baibai.sqlite",
        root / "stores/screening/runs.sqlite",
        root / "stores/macro/macro.sqlite",
    )


def _insert_research_triage(root: Path, payload: Mapping[str, object]) -> None:
    with sqlite3.connect(root / "stores/application/baibai.sqlite") as connection:
        connection.execute(
            """
            INSERT INTO research_triage(
                research_triage_id, review_set_id, run_revision_id, as_of, published_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload["research_triage_id"],
                payload["review_set_id"],
                payload["run_revision_id"],
                payload["as_of"],
                payload["published_at"],
                canonical_json(dict(payload)),
            ),
        )


def _publish_assessment(root: Path, *, assessment_id: str, research_triage_id: str) -> None:
    db_path = root / "stores/application/baibai.sqlite"
    draft = scaffold_capital_allocation(
        db_path=db_path,
        capital_allocation_assessment_id=assessment_id,
        as_of=date(2026, 7, 9),
        research_triage_id=research_triage_id,
        thesis_ids=["thesis-20260714-2331-r1"],
        published_at=datetime(2026, 7, 9, 12, tzinfo=JST),
    )
    draft["headline"] = "要求利回り未達のため配分しない"
    draft["comparison"] = "調査済み候補は要求利回りを満たさない"
    draft["forgone"] = "現金を維持する"
    alternatives = draft["alternatives"]
    assert isinstance(alternatives, list)
    alternative = alternatives[0]
    assert isinstance(alternative, dict)
    alternative["rationale"] = "Thesis Review済みthesisの期待値が不足する"
    review = draft["review"]
    assert isinstance(review, dict)
    review["reviewer_identity"] = "independent-reviewer"
    review["reviewed_at"] = "2026-07-09T11:00:00+09:00"
    review["draft_sha256"] = capital_allocation_draft_sha256(
        CapitalAllocationAssessment.model_validate(draft)
    )
    CapitalAllocationAssessmentService(db_path).publish(
        CapitalAllocationAssessment.model_validate(draft)
    )


def _insert_review_set(root: Path, *, review_set_id: str) -> str:
    runs_db = root / "stores/screening/runs.sqlite"
    run = ScreeningRunReader(runs_db).latest_run()
    assert run is not None
    payload = {
        "schema_version": 2,
        "kind": "review_set",
        "review_set_id": review_set_id,
        "run_revision_id": run.run_revision_id,
        "as_of": run.as_of_date,
        "created_at": "2026-07-08T12:01:00+09:00",
        "screening_rules_hash": "1" * 64,
        "method": {
            "method_id": "multi-valuation-v3",
            "method_hash": "0" * 64,
            "nomination_depth": 20,
        },
        "entries": [],
        "diagnostics": {},
    }
    with sqlite3.connect(runs_db) as connection:
        connection.execute(
            "INSERT INTO review_set(review_set_id, run_revision_id, created_at, payload) "
            "VALUES (?, ?, ?, ?)",
            (
                review_set_id,
                run.run_revision_id,
                "2026-07-08T12:01:00+09:00",
                canonical_json(payload),
            ),
        )
    return run.run_revision_id


def _seed_macro_observations(root: Path) -> None:
    conn = open_connection(root / "stores/macro/macro.sqlite")
    try:
        insert_observations(
            conn,
            [
                ObservationRecord(
                    series_id="us.10y",
                    observed_at=date(2026, 7, 15),
                    value=4.3,
                    unit="percent",
                    source_url="https://example.com/us10y",
                ),
                ObservationRecord(
                    series_id="jp.10y",
                    observed_at=date(2026, 7, 17),
                    value=1.1,
                    unit="percent",
                    source_url="https://example.com/jp10y",
                ),
                ObservationRecord(
                    series_id="jp.10y",
                    observed_at=date(2026, 7, 20),
                    value=1.2,
                    unit="percent",
                    source_url="https://example.com/jp10y",
                    fetch_status="failed",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_build_meta_derives_store_asof_from_fixture_stores(app_method_root: Path) -> None:
    _seed_macro_observations(app_method_root)
    MacroContextService(app_method_root / "stores/application/baibai.sqlite").publish(
        MacroContextDocument.model_validate(macro_context_payload()), expected_head=None
    )

    view = build_meta(_meta_source(app_method_root), batch="daily")

    assert view.screening_as_of == date(2026, 7, 8)
    assert view.macro_as_of == date(2026, 7, 17)
    assert view.app_db_updated_at == datetime(2026, 7, 19, 12, 0, tzinfo=JST)
    assert view.data_updated_at == datetime(2026, 7, 19, 12, 0, tzinfo=JST)
    assert view.batch == "daily"
    assert view.generated_at.tzinfo is not None


def test_build_meta_freshness_ignores_retained_unregistered_series(
    app_method_root: Path,
) -> None:
    _seed_macro_observations(app_method_root)
    database = app_method_root / "stores/macro/macro.sqlite"
    connection = open_connection(database)
    try:
        connection.execute(
            "INSERT INTO series("
            "series_id, name, category, geography, frequency, unit, provider, "
            "provider_series_id, source_id, source_url"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "retired.series",
                "Retired",
                "test",
                "world",
                "daily",
                "index",
                "fred_csv",
                "RETIRED",
                "retired",
                "https://example.com/retired",
            ),
        )
        insert_observations(
            connection,
            [
                ObservationRecord(
                    series_id="retired.series",
                    observed_at=date(2026, 8, 1),
                    value=100.0,
                    unit="index",
                    source_url="https://example.com/retired",
                )
            ],
        )
        connection.commit()
    finally:
        connection.close()

    view = build_meta(_meta_source(app_method_root))

    assert view.macro_as_of == date(2026, 7, 17)
    assert view.data_updated_at == datetime(2026, 7, 18, 0, 0, tzinfo=JST)


def test_build_meta_takes_the_latest_judgment_write_across_stores(
    app_method_root: Path,
) -> None:
    # The fixture's newest judgment write without a macro context is a task created
    # on 2026-07-18; its date-only column is read at JST midnight.
    view = build_meta(_meta_source(app_method_root))

    assert view.macro_as_of is None
    assert view.app_db_updated_at == datetime(2026, 7, 18, 0, 0, tzinfo=JST)
    assert view.batch is None


def test_build_meta_reflects_a_newly_written_operation_session(
    app_method_root: Path,
) -> None:
    # A judgment write in a table beyond ledger/research/macro must move freshness.
    started_at = datetime(2026, 8, 1, 9, 30, tzinfo=JST)
    with sqlite3.connect(app_method_root / "stores/application/baibai.sqlite") as connection:
        connection.execute(
            """
            INSERT INTO operation_session(
                operation_id, session_kind, status, as_of, ticker,
                started_at, completed_at, payload
            ) VALUES (?, 'capital-allocation', 'active', ?, NULL, ?, NULL, ?)
            """,
            (
                "operation-20260801-capital-allocation",
                "2026-08-01",
                started_at.isoformat(),
                canonical_json({"kind": "capital-allocation"}),
            ),
        )

    view = build_meta(_meta_source(app_method_root))

    assert view.app_db_updated_at == started_at
    assert view.data_updated_at == started_at


def test_build_meta_returns_none_for_missing_stores(tmp_path: Path) -> None:
    view = build_meta(_meta_source(tmp_path))

    assert view.screening_as_of is None
    assert view.macro_as_of is None
    assert view.app_db_updated_at is None
    assert view.data_updated_at is None


def test_export_writes_expected_view_tree(app_method_root: Path, tmp_path: Path) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    runs_db = app_method_root / "stores/screening/runs.sqlite"
    run = ScreeningRunReader(runs_db).latest_run()
    assert run is not None
    output_dir = tmp_path / "export"

    exit_code = main(
        [
            "--output-dir",
            str(output_dir),
            "--repo-root",
            str(app_method_root),
            "--batch",
            "daily",
        ]
    )

    assert exit_code == 0
    views = output_dir / "views"
    expected = {
        "daily-delta.json",
        "dashboard.json",
        "macro.json",
        "screening_latest.json",
        "operations.json",
        "tasks.json",
        "meta.json",
        "security--0001.json",
        "security--0002.json",
        "security--2331.json",
    } | {f"macro-series--{item.series_id}.json" for item in load_definitions().series}
    assert {item.name for item in views.iterdir()} == expected

    DashboardView.model_validate_json((views / "dashboard.json").read_text(encoding="utf-8"))
    screening = ScreeningView.model_validate_json(
        (views / "screening_latest.json").read_text(encoding="utf-8")
    )
    assert screening.run is not None
    assert screening.run.analyzed_security_count == 3
    assert screening.review_sets == []
    OperationsView.model_validate_json((views / "operations.json").read_text(encoding="utf-8"))
    TasksView.model_validate_json((views / "tasks.json").read_text(encoding="utf-8"))
    meta = MetaView.model_validate_json((views / "meta.json").read_text(encoding="utf-8"))
    assert meta.batch == "daily"
    assert meta.screening_as_of == date(2026, 7, 8)
    macro = MacroView.model_validate_json((views / "macro.json").read_text(encoding="utf-8"))
    assert macro.reading.as_of == datetime.now(JST).date()
    assert sum(len(series.points) for group in macro.groups for series in group.series) == 0
    for name in ("security--0001.json", "security--0002.json", "security--2331.json"):
        detail = SecurityDetailView.model_validate_json((views / name).read_text(encoding="utf-8"))
        assert detail.security_analysis is not None

    assert not (output_dir / "history/select").exists()

    pool_files = sorted((output_dir / "history/candidate-views").iterdir())
    assert [item.name for item in pool_files] == ["2026-07-01.json", "2026-07-08.json"]
    pool = json.loads(pool_files[0].read_text(encoding="utf-8"))
    assert [candidate["ticker"] for candidate in pool["rows"]] == ["2331", "0001", "0002"]
    assert pool["run"]["as_of"] == "2026-07-01"
    latest_pool = json.loads(pool_files[1].read_text(encoding="utf-8"))
    assert latest_pool["run"]["run_revision_id"] == run.run_revision_id


def test_export_writes_macro_context_detail_views(app_method_root: Path, tmp_path: Path) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    db_path = app_method_root / "stores/application/baibai.sqlite"
    document = MacroContextDocument.model_validate(macro_context_payload())
    MacroContextService(db_path).publish(document, expected_head=None)
    output_dir = tmp_path / "export"

    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 0

    detail_path = output_dir / "views" / f"macro-context--{document.context_id}.json"
    assert detail_path.is_file()
    detail = MacroContextView.model_validate_json(detail_path.read_text(encoding="utf-8"))
    assert detail.context_id == document.context_id
    assert len(detail.core) == 10
    assert detail.connection.section_id == "japan_equity_loop"
    # The current report is carried by the structured excerpt, not duplicated in
    # the collapsed past-revision index.
    overview = MacroView.model_validate_json(
        (output_dir / "views/macro.json").read_text(encoding="utf-8")
    )
    assert overview.latest_context is not None
    assert overview.latest_context.context_id == document.context_id
    assert overview.reports == []


def test_export_keeps_historical_assessment_detail_outside_current_screening_index(
    app_method_root: Path, tmp_path: Path
) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    current_revision_id = _insert_review_set(app_method_root, review_set_id="review-set-current")
    current_triage = research_triage_payload(
        research_triage_id="research-triage-current",
        review_set_id="review-set-current",
        run_revision_id=current_revision_id,
        as_of="2026-07-08",
        entries=[skip_entry("2331")],
    )
    historical_triage = research_triage_payload(
        research_triage_id="research-triage-historical",
        review_set_id="review-set-historical",
        run_revision_id="run-revision-historical",
        as_of="2026-07-01",
        entries=[research_entry("2331")],
    )
    _insert_research_triage(app_method_root, historical_triage)
    _insert_research_triage(app_method_root, current_triage)
    assessment_id = "capital-allocation-assessment-20260709-historical"
    _publish_assessment(
        app_method_root,
        assessment_id=assessment_id,
        research_triage_id="research-triage-historical",
    )
    output_dir = tmp_path / "export"

    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 0

    screening = ScreeningView.model_validate_json(
        (output_dir / "views/screening_latest.json").read_text(encoding="utf-8")
    )
    assert screening.capital_allocation_assessments == []
    detail_path = output_dir / "views" / f"capital-allocation-assessment--{assessment_id}.json"
    detail = CapitalAllocationAssessmentView.model_validate_json(
        detail_path.read_text(encoding="utf-8")
    )
    assert detail.capital_allocation_assessment_id == assessment_id
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        api_detail = client.get(f"/api/capital-allocation-assessments/{assessment_id}")
    assert api_detail.status_code == 200
    assert json.loads(detail_path.read_text(encoding="utf-8")) == api_detail.json()


def test_export_fails_before_writing_when_reading_rules_are_absent(
    app_method_root: Path, tmp_path: Path, capsys
) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (app_method_root / MACRO_READING_RULES_PATH).unlink()
    output_dir = tmp_path / "export"

    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 1
    assert "failed to read macro reading rules" in capsys.readouterr().err
    assert not output_dir.exists()


def test_export_fails_before_writing_when_rules_do_not_cover_the_registry(
    app_method_root: Path, tmp_path: Path, capsys
) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (app_method_root / MACRO_READING_RULES_PATH).write_text(
        "schema_version: 2\n"
        "defaults:\n"
        "  monthly:\n"
        "    percentile_window_years: 10\n"
        "    short_trend_months: 3\n"
        "    long_trend_months: 12\n"
        "    publication_lag_days: 45\n"
        "    staleness_margin_days: 7\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "export"

    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 1
    assert "macro reading rules have no defaults for frequency" in capsys.readouterr().err
    assert not output_dir.exists()


def test_export_fails_before_writing_when_the_market_store_was_never_hydrated(
    app_method_root: Path, tmp_path: Path, capsys
) -> None:
    """The published market store carries its fetch ledger and none of the rows.

    Every lake-owned table still answers, so the export writes valuations and security
    views with no price behind them and reports success. The 2026-08-17 manual publish
    did exactly that.
    """

    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    market_store_with_fetch_claim(app_method_root, claimed_rows=15_052_807, held_rows=0)
    output_dir = tmp_path / "export"

    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 1
    message = capsys.readouterr().err
    assert "market store is not hydrated" in message
    assert "jquants.daily_bars claims 15052807 row(s) and holds none" in message
    assert not output_dir.exists()


def test_exported_views_match_api_responses(app_method_root: Path, tmp_path: Path) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    output_dir = tmp_path / "export"

    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 0

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        api_screening = client.get("/api/screening/latest").json()
        api_macro = client.get("/api/macro").json()
        api_dashboard = client.get("/api/dashboard").json()
        api_tasks = client.get("/api/tasks").json()
    exported_screening = json.loads(
        (output_dir / "views/screening_latest.json").read_text(encoding="utf-8")
    )
    assert exported_screening == api_screening
    exported_macro = json.loads((output_dir / "views/macro.json").read_text(encoding="utf-8"))
    assert exported_macro == api_macro
    exported_dashboard = json.loads(
        (output_dir / "views/dashboard.json").read_text(encoding="utf-8")
    )
    del exported_dashboard["generated_at"], api_dashboard["generated_at"]
    assert exported_dashboard == api_dashboard
    exported_tasks = json.loads((output_dir / "views/tasks.json").read_text(encoding="utf-8"))
    del exported_tasks["generated_at"], api_tasks["generated_at"]
    assert exported_tasks == api_tasks


def test_export_skips_security_view_for_ticker_no_source_knows(
    app_method_root: Path, tmp_path: Path, capsys
) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    run_revision_id = _insert_review_set(
        app_method_root,
        review_set_id="review-set-current",
    )
    payload = research_triage_payload(
        research_triage_id="research_triage-20260708-value",
        review_set_id="review-set-current",
        run_revision_id=run_revision_id,
        as_of="2026-07-08",
        published_at="2026-07-08T13:00:00+09:00",
        entries=[skip_entry("9999", reason="決算後に再評価")],
    )
    _insert_research_triage(app_method_root, payload)
    output_dir = tmp_path / "export"

    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 0

    assert not (output_dir / "views/security--9999.json").exists()
    assert (output_dir / "views/security--2331.json").exists()
    assert "9999" in capsys.readouterr().err


def test_export_excludes_retired_research_triage_shape(
    app_method_root: Path, tmp_path: Path
) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    run_revision_id = _insert_review_set(app_method_root, review_set_id="review_set-test")
    entry = skip_entry("2331", machine_snapshot={"review_position": 4})
    entry.pop("candidate_snapshot")
    payload = research_triage_payload(
        run_revision_id=run_revision_id,
        schema_version=1,
        triage_contract_id="research-triage-v1",
        entries=[entry],
    )
    _insert_research_triage(app_method_root, payload)
    output_dir = tmp_path / "export"

    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 0

    screening = ScreeningView.model_validate_json(
        (output_dir / "views/screening_latest.json").read_text(encoding="utf-8")
    )
    assert screening.research_triages == []


def test_export_replaces_views_but_keeps_history(app_method_root: Path, tmp_path: Path) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    output_dir = tmp_path / "export"
    stale_view = output_dir / "views/security--0009.json"
    stale_view.parent.mkdir(parents=True)
    stale_view.write_text("{}", encoding="utf-8")
    kept_history = output_dir / "history/candidate-views/2026-01-01.json"
    kept_history.parent.mkdir(parents=True)
    kept_history.write_text("{}", encoding="utf-8")

    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 0

    # views/ is the complete image of one export: a view outside the current target
    # set does not survive.
    assert not stale_view.exists()
    assert (output_dir / "views/security--2331.json").exists()
    # history/ only appends, so a prior day's entry is retained.
    assert kept_history.exists()


def test_export_writes_meta_after_every_other_file(
    app_method_root: Path, tmp_path: Path, mocker
) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    output_dir = tmp_path / "export"
    real_build_meta = export_module.build_meta
    seen_before_meta: dict[str, list[str]] = {}

    def _capture(*args: object, **kwargs: object) -> object:
        views = output_dir / "views"
        seen_before_meta["views"] = sorted(item.name for item in views.iterdir())
        seen_before_meta["history"] = sorted(
            str(item.relative_to(output_dir)) for item in (output_dir / "history").rglob("*.json")
        )
        return real_build_meta(*args, **kwargs)

    mocker.patch.object(export_module, "build_meta", side_effect=_capture)

    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 0

    # meta.json is written only after every view and history file exists, so a run that
    # fails mid-export never leaves a fresh-claiming meta over stale content.
    assert "meta.json" not in seen_before_meta["views"]
    assert "dashboard.json" in seen_before_meta["views"]
    assert "security--2331.json" in seen_before_meta["views"]
    assert seen_before_meta["history"]
    assert (output_dir / "views/meta.json").exists()


def test_main_rejects_a_root_without_project_markers(tmp_path: Path) -> None:
    assert main(["--output-dir", str(tmp_path / "out"), "--repo-root", str(tmp_path)]) == 1
    assert not (tmp_path / "out").exists()


def test_export_refuses_an_app_store_on_a_different_schema(
    app_method_root: Path, tmp_path: Path, capsys
) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    with sqlite3.connect(app_method_root / "stores/application/baibai.sqlite") as connection:
        connection.execute(f"PRAGMA user_version = {LATEST_VERSION - 1}")
    output_dir = tmp_path / "export"

    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 1

    # The mismatch is named before any view exists, so a partially written export never
    # reaches the serving upload.
    assert not output_dir.exists()
    message = capsys.readouterr().err
    assert "obsolete application database schema" in message
    assert f"found user_version={LATEST_VERSION - 1}" in message
    assert f"expected={LATEST_VERSION}" in message
    assert "matching application release" in message


def test_export_treats_an_absent_app_store_as_empty_judgment(
    app_method_root: Path, tmp_path: Path
) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (app_method_root / "stores/application/baibai.sqlite").unlink()
    output_dir = tmp_path / "export"

    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 0

    assert (output_dir / "views/meta.json").exists()
    assert json.loads((output_dir / "views/dashboard.json").read_text(encoding="utf-8"))


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_SOURCE = REPO_ROOT / "web/edge/src/index.ts"
EXPORTER_SOURCE = REPO_ROOT / "web/backend/src/baibai_web/materialize.py"
# `view('x.json')` / `view(`x--${id}.json`)` — the Worker's whole `views/` surface.
_WORKER_VIEW = re.compile(r"view\([`']([^`']+)[`']\)")
# `views_dir / "x.json"` and `views_dir / f"x--{...}.json"` in the exporter.
_EXPORTED_VIEW = re.compile(r'views_dir / f?"([^"]+)"')


def _view_shape(filename: str) -> str:
    """Collapse an interpolated segment so both sides compare as the same view kind."""
    return re.sub(r"\$?\{[^}]+\}", "*", filename)


def _exported_views() -> set[str]:
    source = EXPORTER_SOURCE.read_text(encoding="utf-8")
    return {_view_shape(match) for match in _EXPORTED_VIEW.findall(source)}


def test_every_worker_view_route_is_produced_by_the_exporter() -> None:
    """A route whose view the export never writes is a permanent 404 in production.

    The two files are the only places the `views/` key space is written down, and
    neither imports the other, so nothing else notices when one of them moves.
    """

    source = WORKER_SOURCE.read_text(encoding="utf-8")
    worker_views = {_view_shape(match) for match in _WORKER_VIEW.findall(source)}

    assert worker_views, "the Worker route table must map at least one view"
    assert worker_views <= _exported_views(), sorted(worker_views - _exported_views())


def test_the_batch_operations_doc_lists_every_exported_view() -> None:
    readme = (REPO_ROOT / "batch/OPERATIONS.md").read_text(encoding="utf-8")
    # 一覧は placeholder を `<name>` で書くので、比較の前に同じ形へ寄せる。
    documented = {
        re.sub(r"<[^>]+>", "*", _view_shape(name))
        for name in re.findall(r"`views/([^`]+)`", readme)
    }

    assert _exported_views() <= documented, sorted(_exported_views() - documented)
