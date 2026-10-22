from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from tests.helpers.assessment_history import historical_context_payload, seed_history
from tests.helpers.indicator_store import store_h15_observations, store_mof_observations
from tests.helpers.screening_run import screening_run_payload
from tests.helpers.screening_sqlite import market_store_with_fetch_claim
from tests.web.test_research_assessment_views import seed_web_proposal

from baibai_engine.collection.economic.records import store_snapshot
from baibai_engine.collection.economic.store import open_connection, record_source_run
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.indicators.observations import ObservationRecord
from baibai_engine.market_context.reading.rules import (
    DEFAULT_RULES_PATH as READING_RULES_PATH,
)
from baibai_engine.position.drafts import apply_draft, build_sell_execution_draft
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.research.market_context.models import StoredAssessment
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.run_store import ScreeningRunReader, ScreeningRunStore
from baibai_engine.storage.application import LATEST_VERSION
from baibai_engine.storage.application.json import canonical_json
from baibai_web import materialize as export_module
from baibai_web.api.server import create_app
from baibai_web.materialize import main
from baibai_web.readmodel.builders import build_meta
from baibai_web.readmodel.models import (
    AssessmentPublicationView,
    DashboardView,
    InvestmentProposalsView,
    InvestmentProposalView,
    MarketContextView,
    MetaView,
    OperationsView,
    ScreeningView,
    SecurityDetailView,
    TasksView,
)
from baibai_web.sources.db_sources import DbMetaSource, DbResearchSource, DbScreeningSource

JST = ZoneInfo("Asia/Tokyo")


def _meta_source(root: Path) -> DbMetaSource:
    return DbMetaSource(
        root / "stores/application/baibai.sqlite",
        DbScreeningSource(root / "stores/screening/runs.sqlite"),
        root / "stores/market_context/indicators.sqlite",
    )


def _seed_economic_sources(root: Path) -> None:
    conn = open_connection(root / "stores/market_context/indicators.sqlite")
    try:
        store_mof_observations(
            conn,
            [
                ObservationRecord(
                    series_id="jp.10y",
                    observed_at=date(2026, 7, 17),
                    value=1.1,
                    unit="percent",
                    source_url="https://example.com/jp10y",
                )
            ],
        )
        store_h15_observations(
            conn,
            [
                ObservationRecord(
                    series_id="us.10y",
                    observed_at=date(2026, 7, 15),
                    value=4.3,
                    unit="percent",
                    source_url="https://example.com/us10y",
                )
            ],
        )
        record_source_run(
            conn,
            source_id="mof-jgbcm-all",
            start=date(2026, 7, 20),
            end=date(2026, 7, 20),
            started_at=datetime(2026, 7, 20, tzinfo=UTC),
            status="failed",
            record_count=0,
            error_message="fixture source unavailable",
        )
        conn.commit()
    finally:
        conn.close()


def test_build_meta_derives_store_asof_from_fixture_stores(app_method_root: Path) -> None:
    _seed_economic_sources(app_method_root)
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


def test_build_meta_freshness_ignores_unreferenced_source(
    app_method_root: Path,
) -> None:
    _seed_economic_sources(app_method_root)
    database = app_method_root / "stores/market_context/indicators.sqlite"
    connection = open_connection(database)
    try:
        store_snapshot(
            connection,
            source_id="unreferenced-source",
            observed_at=date(2026, 8, 1),
            fetched_at=datetime(2026, 8, 2, tzinfo=UTC),
            records=[{"raw": 100}],
        )
        connection.commit()
    finally:
        connection.close()

    view = build_meta(_meta_source(app_method_root))

    assert view.indicators_as_of == date(2026, 7, 17)
    assert view.data_updated_at == datetime(2026, 7, 18, 0, 0, tzinfo=JST)


def test_build_meta_takes_the_latest_business_record_timestamp(
    app_method_root: Path,
) -> None:
    # The fixture's latest business-record timestamp without a macro context is a task created
    # on 2026-07-18; its date-only column is read at JST midnight.
    view = build_meta(_meta_source(app_method_root))

    assert view.indicators_as_of is None
    assert view.app_db_updated_at == datetime(2026, 7, 18, 0, 0, tzinfo=JST)
    assert view.batch is None


def test_build_meta_reflects_the_operation_session_start_time(
    app_method_root: Path,
) -> None:
    # Operation start time contributes to freshness; this is not the physical database write time.
    started_at = datetime(2026, 8, 1, 9, 30, tzinfo=JST)
    database = app_method_root / "stores/application/baibai.sqlite"
    with closing(sqlite3.connect(database)) as connection, connection:
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


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        ("2026-10-14T18:00:00+09:00", "2026-10-14T10:00:00+00:00"),
        ("2026-10-14T19:00:00.123000+09:00", "2026-10-14T10:00:00.123001+00:00"),
        ("2026-10-14T10:00:00Z", "2026-10-14T10:00:00.000001+00:00"),
        ("2026-10-14", "2026-10-14T00:00:00.000001"),
    ],
)
def test_application_freshness_compares_instants_within_the_same_table_without_writes(
    app_method_root: Path, older: str, newer: str
) -> None:
    database = app_method_root / "stores/application/baibai.sqlite"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executemany(
            "INSERT INTO stock_assessment("
            "assessment_id, ticker, as_of, stance, published_at, payload, core_sha256"
            ") VALUES (?, '1234', '2026-10-14', 'uncertain', ?, '{}', 'synthetic')",
            [("older-assessment", older), ("newer-assessment", newer)],
        )
    before = database.read_bytes()

    view = build_meta(_meta_source(app_method_root))

    expected = datetime.fromisoformat(newer)
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=JST)
    assert view.app_db_updated_at == expected
    assert view.data_updated_at == expected
    assert database.read_bytes() == before


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
        "stock-assessments.json",
        "investment-proposals.json",
        f"stock-assessment--{DbResearchSource(app_method_root / 'stores/application/baibai.sqlite').revisions()[0].assessment_id}.json",
        "tasks.json",
        "meta.json",
        "security--0001.json",
        "security--0002.json",
        "security--2331.json",
        "security--8929.json",
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

    reserved = SecurityDetailView.model_validate_json(
        (views / "security--8929.json").read_text(encoding="utf-8")
    )
    assert reserved.ticker == "8929"
    assert reserved.holding is None
    assert reserved.stock_analysis is None

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


@pytest.mark.parametrize("failure", ["detail", "identity"])
def test_export_fails_when_an_indexed_market_context_history_detail_is_unavailable(
    app_method_root: Path, tmp_path: Path, mocker, failure
) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    document = StoredAssessment.from_payload(historical_context_payload())
    seed_history(app_method_root / "stores/application/baibai.sqlite", document.document)
    if failure == "detail":
        mocker.patch.object(
            export_module,
            "build_market_context_assessment_detail",
            side_effect=ValueError("synthetic indexed history is corrupt"),
        )
    else:
        mocker.patch(
            "baibai_web.sources.db_sources.DbMarketContextSource.assessment_history",
            return_value=[{**document.document, "context_id": "../invalid"}],
        )
    output = tmp_path / "export"

    with pytest.raises(ValueError, match=r"indexed.*(corrupt|unexpected format)"):
        main(["--output-dir", str(output), "--repo-root", str(app_method_root)])

    assert not (output / "views/meta.json").exists()


def test_export_keeps_saved_proposal_detail_independent_of_current_screening(
    app_method_root: Path, tmp_path: Path
) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    _, _, _, proposal = seed_web_proposal(app_method_root)
    proposal_id = proposal.proposal_id
    output_dir = tmp_path / "export"
    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 0
    screening = ScreeningView.model_validate_json(
        (output_dir / "views/screening_latest.json").read_text(encoding="utf-8")
    )
    assert screening.investment_proposals[0].proposal_id == proposal_id
    detail_path = output_dir / "views" / f"investment-proposal--{proposal_id}.json"
    detail = InvestmentProposalView.model_validate_json(detail_path.read_text(encoding="utf-8"))
    assert detail.proposal_id == proposal_id
    assert detail.research_scope == proposal.research_scope.model_dump(mode="json")
    index_path = output_dir / "views/investment-proposals.json"
    index = InvestmentProposalsView.model_validate_json(index_path.read_text(encoding="utf-8"))
    assert [item.proposal_id for item in index.proposals] == [proposal_id]
    assert (output_dir / f"views/investment-proposal-preview--{proposal_id}.json").is_file()
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        api_detail = client.get(f"/api/investment-proposals/{proposal_id}")
        api_index = client.get("/api/investment-proposals")
        assert client.get("/api/capital-allocation-assessments").status_code == 404
    assert api_detail.status_code == 200
    assert json.loads(detail_path.read_text(encoding="utf-8")) == api_detail.json()
    assert json.loads(index_path.read_text(encoding="utf-8")) == api_index.json()


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
        occurred_on=date(2026, 7, 12),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1100"),
        decision_reference="synthetic-human-sale-note",
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
    database = app_method_root / "stores/application/baibai.sqlite"
    with closing(sqlite3.connect(database)) as connection, connection:
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
_WORKER_RESEARCH_INDEX = re.compile(r"kind: 'research-index',\s*key: 'views/([^']+)'")
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

    Workerが直接参照するviewとresearch indexのkeyがexporterの生成宣言に存在することを確認する。
    静的なkeyの対応検証であり、全分岐の実行やR2への公開成功を証明するものではない。
    """

    source = WORKER_SOURCE.read_text(encoding="utf-8")
    worker_views = {
        _view_shape(match)
        for match in [*_WORKER_VIEW.findall(source), *_WORKER_RESEARCH_INDEX.findall(source)]
    }

    assert worker_views, "the Worker route table must map at least one view"
    assert worker_views <= _exported_views(), sorted(worker_views - _exported_views())


@pytest.mark.parametrize("count", [1, 10, 100])
def test_export_reuses_run_research_history_and_ledger_per_export(
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
    read = mocker.spy(db_sources, "list_stock_assessment_publications")
    replay = mocker.spy(db_sources, "current_portfolio")
    runs = mocker.spy(db_sources, "screening_run_payload")
    mocker.patch.object(
        export_module,
        "_security_tickers",
        return_value=["2331"] + [f"{4000 + index}" for index in range(count - 1)],
    )
    export_module.export_read_models(app_method_root, tmp_path / "first")
    assert read.call_count == 1
    assert replay.call_count == 1
    assert prepare.call_count == 1
    assert sum(not call.kwargs for call in runs.call_args_list) == 1
    assert all(call.kwargs.get("as_of_date") != date(2026, 7, 8) for call in runs.call_args_list)
    conversions = convert.call_count
    assert conversions < 10
    # A second export must read its own inputs; no result escapes the first export.
    export_module.export_read_models(app_method_root, tmp_path / "second")
    assert read.call_count == 2
    assert replay.call_count == 2
    assert prepare.call_count == 2
    assert convert.call_count == 2 * conversions
    assert sum(not call.kwargs for call in runs.call_args_list) == 2
    assert all(call.kwargs.get("as_of_date") != date(2026, 7, 8) for call in runs.call_args_list)
    first = tmp_path / "first/views/security--2331.json"
    second = tmp_path / "second/views/security--2331.json"
    assert json.loads(first.read_text(encoding="utf-8")) == json.loads(
        second.read_text(encoding="utf-8")
    )


def test_export_rereads_and_rejects_a_run_corrupted_after_the_previous_export(
    app_method_root: Path, tmp_path: Path
) -> None:
    export_module.export_read_models(app_method_root, tmp_path / "first")
    database = app_method_root / "stores/screening/runs.sqlite"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE screening_run SET payload=json_set("
            "payload, '$.candidate_discovery.diagnostics.candidate_count', 999) "
            "WHERE asof_date='2026-07-08'"
        )
    output = tmp_path / "second"
    with pytest.raises(ValueError, match="candidate diagnostics differ"):
        export_module.export_read_models(app_method_root, output)
    assert not (output / "views/meta.json").exists()


def test_export_fixes_missing_latest_once_per_export(
    app_method_root: Path, tmp_path: Path, mocker
) -> None:
    from baibai_web.sources import db_sources

    (app_method_root / "stores/screening/runs.sqlite").unlink()
    read = mocker.spy(db_sources, "screening_run_payload")
    for count in (1, 2):
        output = tmp_path / str(count)
        export_module.export_read_models(app_method_root, output)
        assert read.call_count == count
        meta = json.loads((output / "views/meta.json").read_text(encoding="utf-8"))
        assert meta["screening_as_of"] is None


def test_export_builders_do_not_mutate_the_shared_latest_run(
    app_method_root: Path, tmp_path: Path, mocker
) -> None:
    latest = DbScreeningSource(app_method_root / "stores/screening/runs.sqlite").latest_run()
    assert latest is not None
    before = deepcopy(latest)
    read = mocker.patch.object(DbScreeningSource, "latest_run", return_value=latest)

    export_module.export_read_models(app_method_root, tmp_path / "export")

    assert read.call_count == 1
    assert latest == before


def test_export_run_sharing_keeps_exact_dates_and_revision_selection(app_method_root: Path) -> None:
    database = app_method_root / "stores/screening/runs.sqlite"
    store = ScreeningRunStore(database)
    earlier = store.publish_run(
        screening_run_payload(as_of="2026-08-01", run_at="2026-08-01T18:00:00+09:00"),
        rules=load_screening_rules(),
    )
    newer = store.publish_run(
        screening_run_payload(as_of="2026-08-01", run_at="2026-08-01T10:00:00+00:00"),
        rules=load_screening_rules(),
    )
    source = export_module._CachedScreeningSource(database)
    latest = source.latest_run()

    assert latest is not None
    assert latest.run_revision_id == newer.publication_id
    assert source.run_as_of(date(2026, 8, 1)) is latest
    exact = source.run(earlier.publication_id)
    assert exact is not None
    assert exact.run_revision_id == earlier.publication_id
    previous = source.previous_run()
    assert previous is not None
    assert previous.as_of == date(2026, 7, 8)
    assert source.run_as_of(date(2026, 8, 2)) is None


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
    screening_before = json.loads(
        (output / "views/screening_latest.json").read_text(encoding="utf-8")
    )
    _seed_economic_sources(app_method_root)
    assert (
        main(["--output-dir", str(output), "--repo-root", str(app_method_root), "--skip-history"])
        == 0
    )
    assert {
        p.relative_to(output): p.read_bytes() for p in (output / "history").rglob("*.json")
    } == history_before
    screening_after = json.loads(
        (output / "views/screening_latest.json").read_text(encoding="utf-8")
    )
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
