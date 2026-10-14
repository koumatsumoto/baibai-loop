from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from tests.helpers.assessment_history import historical_context_payload, seed_history
from tests.helpers.screening_sqlite import market_store_with_fetch_claim

from baibai_engine.market_context.assessment.models import StoredAssessment
from baibai_engine.market_context.indicators.db import insert_observations, open_connection
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.indicators.observations import ObservationRecord
from baibai_engine.market_context.reading.rules import (
    DEFAULT_RULES_PATH as READING_RULES_PATH,
)
from baibai_engine.position.drafts import apply_draft, build_sell_execution_draft
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.screening.run_store import ScreeningRunReader
from baibai_engine.storage.application import LATEST_VERSION
from baibai_engine.storage.application.json import canonical_json
from baibai_web import materialize as export_module
from baibai_web.api.server import create_app
from baibai_web.materialize import main
from baibai_web.readmodel.builders import build_meta
from baibai_web.readmodel.models import (
    AssessmentPublicationView,
    CapitalAllocationAssessmentsView,
    CapitalAllocationAssessmentView,
    DashboardView,
    MarketContextView,
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
        root / "stores/market_context/indicators.sqlite",
    )


def _publish_assessment(root: Path, *, assessment_id: str, research_triage_id: str) -> None:
    """Seed historical CAA bytes; current services do not execute old Thesis models."""
    with sqlite3.connect(root / "stores/application/baibai.sqlite") as connection:
        thesis_id, core = connection.execute(
            "SELECT thesis_id, core_sha256 FROM thesis LIMIT 1"
        ).fetchone()
        review_id = connection.execute(
            "SELECT review_id FROM thesis_review WHERE thesis_id=?", (thesis_id,)
        ).fetchone()[0]
        payload = {
            "schema_version": 2,
            "kind": "capital_allocation_assessment",
            "capital_allocation_assessment_id": assessment_id,
            "as_of": "2026-07-09",
            "published_at": "2026-07-09T12:00:00+09:00",
            "result": "no_allocation",
            "headline": "現金維持",
            "research_triage_id": research_triage_id,
            "market_context_assessment_id": None,
            "comparison": "当時の比較",
            "forgone": "見送り",
            "alternatives": [
                {
                    "ticker": "2331",
                    "thesis_id": thesis_id,
                    "thesis_core_sha256": core,
                    "thesis_review_id": review_id,
                    "disposition": "decline",
                    "rationale": "当時の判断",
                }
            ],
            "review": {
                "attempt": 1,
                "reviewer_identity": "historical",
                "reviewed_at": "2026-07-09T12:00:00+09:00",
                "conclusion": "pass",
                "draft_sha256": "1" * 64,
                "open_findings": [],
            },
        }
        connection.execute(
            "INSERT INTO capital_allocation_assessment VALUES (?,?,?,?,?,?)",
            (
                assessment_id,
                payload["as_of"],
                payload["published_at"],
                payload["result"],
                research_triage_id,
                canonical_json(payload),
            ),
        )


def _seed_indicator_observations(root: Path) -> None:
    conn = open_connection(root / "stores/market_context/indicators.sqlite")
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
    _seed_indicator_observations(app_method_root)
    seed_history(
        app_method_root / "stores/application/baibai.sqlite",
        (StoredAssessment.from_payload(historical_context_payload())).document,
        supersedes=None,
    )

    view = build_meta(_meta_source(app_method_root), batch="daily")

    assert view.screening_as_of == date(2026, 7, 8)
    assert view.indicators_as_of == date(2026, 7, 17)
    assert view.app_db_updated_at == datetime(2026, 7, 19, 12, 0, tzinfo=JST)
    assert view.data_updated_at == datetime(2026, 7, 19, 12, 0, tzinfo=JST)
    assert view.batch == "daily"
    assert view.generated_at.tzinfo is not None


def test_build_meta_freshness_ignores_retained_unregistered_series(
    app_method_root: Path,
) -> None:
    _seed_indicator_observations(app_method_root)
    database = app_method_root / "stores/market_context/indicators.sqlite"
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

    assert view.indicators_as_of == date(2026, 7, 17)
    assert view.data_updated_at == datetime(2026, 7, 18, 0, 0, tzinfo=JST)


def test_build_meta_takes_the_latest_judgment_write_across_stores(
    app_method_root: Path,
) -> None:
    # The fixture's newest judgment write without a macro context is a task created
    # on 2026-07-18; its date-only column is read at JST midnight.
    view = build_meta(_meta_source(app_method_root))

    assert view.indicators_as_of is None
    assert view.app_db_updated_at == datetime(2026, 7, 18, 0, 0, tzinfo=JST)
    assert view.batch is None


def test_build_meta_reflects_a_newly_written_operation_session(
    app_method_root: Path,
) -> None:
    # A judgment write in a table beyond ledger/research/market-context must move freshness.
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
    assert view.indicators_as_of is None
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
        "market-context.json",
        "screening_latest.json",
        "operations.json",
        "capital-allocation-assessments.json",
        "tasks.json",
        "meta.json",
        "security--0001.json",
        "security--0002.json",
        "security--2331.json",
    } | {f"indicator-series--{item.series_id}.json" for item in load_definitions().series}
    assert {item.name for item in views.iterdir()} == expected

    DashboardView.model_validate_json((views / "dashboard.json").read_text(encoding="utf-8"))
    screening = ScreeningView.model_validate_json(
        (views / "screening_latest.json").read_text(encoding="utf-8")
    )
    assert screening.run is not None
    assert screening.run.analyzed_security_count == 3
    assert screening.candidate_discovery.candidate_count == 0
    OperationsView.model_validate_json((views / "operations.json").read_text(encoding="utf-8"))
    TasksView.model_validate_json((views / "tasks.json").read_text(encoding="utf-8"))
    meta = MetaView.model_validate_json((views / "meta.json").read_text(encoding="utf-8"))
    assert meta.batch == "daily"
    assert meta.screening_as_of == date(2026, 7, 8)
    market_context = MarketContextView.model_validate_json(
        (views / "market-context.json").read_text(encoding="utf-8")
    )
    assert market_context.reading.as_of == datetime.now(JST).date()
    assert (
        sum(len(series.points) for group in market_context.groups for series in group.series) == 0
    )
    for name in ("security--0001.json", "security--0002.json", "security--2331.json"):
        detail = SecurityDetailView.model_validate_json((views / name).read_text(encoding="utf-8"))
        assert detail.stock_analysis is not None

    assert not (output_dir / "history/select").exists()

    pool_files = sorted((output_dir / "history/candidate-views").iterdir())
    assert [item.name for item in pool_files] == ["2026-07-01.json", "2026-07-08.json"]
    pool = json.loads(pool_files[0].read_text(encoding="utf-8"))
    assert [candidate["ticker"] for candidate in pool["rows"]] == ["2331", "0001", "0002"]
    assert pool["run"]["as_of"] == "2026-07-01"
    latest_pool = json.loads(pool_files[1].read_text(encoding="utf-8"))
    assert latest_pool["run"]["run_revision_id"] == run.run_revision_id


def test_export_writes_market_context_assessment_detail_views(
    app_method_root: Path, tmp_path: Path
) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    db_path = app_method_root / "stores/application/baibai.sqlite"
    document = StoredAssessment.from_payload(historical_context_payload())
    seed_history(db_path, (document).document, supersedes=None)
    output_dir = tmp_path / "export"

    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 0

    detail_path = output_dir / "views" / f"market-context-assessment--{document.context_id}.json"
    assert detail_path.is_file()
    detail = AssessmentPublicationView.model_validate_json(detail_path.read_text(encoding="utf-8"))
    assert detail.context_id == document.context_id
    assert len(detail.core) == 10
    assert detail.connection.section_id == "japan_equity_loop"
    # The current report is carried by the structured excerpt, not duplicated in
    # the collapsed past-revision index.
    overview = MarketContextView.model_validate_json(
        (output_dir / "views/market-context.json").read_text(encoding="utf-8")
    )
    assert overview.latest_context is not None
    assert overview.latest_context.context_id == document.context_id
    assert overview.reports == []


def test_export_keeps_historical_assessment_detail_outside_current_screening_index(
    app_method_root: Path, tmp_path: Path
) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
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
    assert (
        screening.capital_allocation_assessments[0].capital_allocation_assessment_id
        == assessment_id
    )
    detail_path = output_dir / "views" / f"capital-allocation-assessment--{assessment_id}.json"
    detail = CapitalAllocationAssessmentView.model_validate_json(
        detail_path.read_text(encoding="utf-8")
    )
    assert detail.capital_allocation_assessment_id == assessment_id
    index_path = output_dir / "views/capital-allocation-assessments.json"
    index = CapitalAllocationAssessmentsView.model_validate_json(index_path.read_text())
    assert [item.capital_allocation_assessment_id for item in index.assessments] == [assessment_id]
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        api_detail = client.get(f"/api/capital-allocation-assessments/{assessment_id}")
        api_index = client.get("/api/capital-allocation-assessments")
        assert client.get("/api/research-triages/missing").status_code == 404
    assert api_detail.status_code == 200
    assert json.loads(detail_path.read_text(encoding="utf-8")) == api_detail.json()
    assert json.loads(index_path.read_text()) == api_index.json()


def test_export_fails_before_writing_when_reading_rules_are_absent(
    app_method_root: Path, tmp_path: Path, capsys
) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (app_method_root / READING_RULES_PATH).unlink()
    output_dir = tmp_path / "export"

    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 1
    assert "failed to read Reading rules" in capsys.readouterr().err
    assert not output_dir.exists()


def test_export_fails_before_writing_when_rules_do_not_cover_the_registry(
    app_method_root: Path, tmp_path: Path, capsys
) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (app_method_root / READING_RULES_PATH).write_text(
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
    assert "Reading rules have no defaults for frequency" in capsys.readouterr().err
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
    service = LedgerStoreService(app_method_root / "stores/application/baibai.sqlite")
    draft = build_sell_execution_draft(
        service,
        occurred_at=datetime(2026, 7, 12, 10, tzinfo=JST),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1100"),
        decision_reference="position-review-20260711-2331-position-2331",
    )
    apply_draft(service, draft, human_confirmed=True)
    output_dir = tmp_path / "export"

    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 0

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        api_screening = client.get("/api/screening/latest").json()
        api_market_context = client.get("/api/market-context").json()
        api_dashboard = client.get("/api/dashboard").json()
        api_tasks = client.get("/api/tasks").json()
    exported_screening = json.loads(
        (output_dir / "views/screening_latest.json").read_text(encoding="utf-8")
    )
    assert exported_screening == api_screening
    exported_market_context = json.loads(
        (output_dir / "views/market-context.json").read_text(encoding="utf-8")
    )
    assert exported_market_context == api_market_context
    exported_dashboard = json.loads(
        (output_dir / "views/dashboard.json").read_text(encoding="utf-8")
    )
    assert (
        exported_dashboard["realized_gross_pnl_yen"]
        == api_dashboard["realized_gross_pnl_yen"]
        == 6_000
    )
    del exported_dashboard["generated_at"], api_dashboard["generated_at"]
    assert exported_dashboard == api_dashboard
    exported_tasks = json.loads((output_dir / "views/tasks.json").read_text(encoding="utf-8"))
    del exported_tasks["generated_at"], api_tasks["generated_at"]
    assert exported_tasks == api_tasks


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
    # Dates outside the retained run store are left untouched.
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


@pytest.mark.parametrize("count", [1, 10, 100])
def test_export_reuses_research_history_and_ledger_per_export(
    app_method_root: Path, tmp_path: Path, count: int, mocker
) -> None:
    from baibai_web.readmodel.stocks import screening as stocks
    from baibai_web.sources import db_sources

    # Both exports compare the same read cutoff, including unobserved series.
    mocker.patch("baibai_web.readmodel.stocks.detail.datetime").now.return_value = datetime(
        2026, 10, 12, 12, tzinfo=JST
    )
    prepare = mocker.spy(export_module, "prepare_security_inputs")
    convert = mocker.spy(stocks, "_candidate_summary")
    read = mocker.spy(db_sources, "list_thesis_publications")
    replay = mocker.spy(db_sources, "current_portfolio")
    mocker.patch.object(
        export_module,
        "_security_tickers",
        return_value=["2331"] + [f"{4000 + index}" for index in range(count - 1)],
    )
    export_module.export_read_models(app_method_root, tmp_path / "first")
    assert read.call_count == 1
    assert replay.call_count == 1
    assert prepare.call_count == 1
    conversions = convert.call_count
    assert conversions < 10
    # A second export must read its own inputs; no result escapes the first export.
    export_module.export_read_models(app_method_root, tmp_path / "second")
    assert read.call_count == 2
    assert replay.call_count == 2
    assert prepare.call_count == 2
    assert convert.call_count == 2 * conversions
    first = tmp_path / "first/views/security--2331.json"
    second = tmp_path / "second/views/security--2331.json"
    assert json.loads(first.read_text()) == json.loads(second.read_text())


def test_indicator_only_export_keeps_retained_history_bytes_and_screening_clock(
    app_method_root: Path, tmp_path: Path
) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    output = tmp_path / "serving"
    assert main(["--output-dir", str(output), "--repo-root", str(app_method_root)]) == 0
    history_before = {
        p.relative_to(output): p.read_bytes() for p in (output / "history").rglob("*.json")
    }
    assert history_before
    screening_before = json.loads((output / "views/screening_latest.json").read_text())
    _seed_indicator_observations(app_method_root)
    assert (
        main(["--output-dir", str(output), "--repo-root", str(app_method_root), "--skip-history"])
        == 0
    )
    assert {
        p.relative_to(output): p.read_bytes() for p in (output / "history").rglob("*.json")
    } == history_before
    screening_after = json.loads((output / "views/screening_latest.json").read_text())
    assert screening_after["run"] == screening_before["run"]
    fresh_output = tmp_path / "indicator-only"
    assert (
        main(
            [
                "--output-dir",
                str(fresh_output),
                "--repo-root",
                str(app_method_root),
                "--skip-history",
            ]
        )
        == 0
    )
    assert not (fresh_output / "history").exists()
    assert (fresh_output / "views/meta.json").is_file()
