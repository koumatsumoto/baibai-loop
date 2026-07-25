from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from tools.cloud import export_read_models as export_module
from tools.cloud.export_read_models import main

from baibai_app.api.server import create_app
from baibai_app.readmodel.builders import build_meta
from baibai_app.readmodel.models import (
    DashboardView,
    MachineSelectionView,
    MacroContextView,
    MacroView,
    MetaView,
    OperationsView,
    ScreeningView,
    SecurityDetailView,
)
from baibai_app.sources.db_sources import DbMetaSource
from baibai_engine.appdb.json import canonical_json
from baibai_engine.macro.context.models import MacroContextDocument
from baibai_engine.macro.context.service import MacroContextService
from baibai_engine.macro.indicators.db import (
    ObservationRecord,
    insert_observations,
    open_connection,
)
from baibai_engine.macro.reading.rules import DEFAULT_RULES_PATH as MACRO_READING_RULES_PATH
from baibai_engine.screening.run_store import ScreeningRunReader, ScreeningRunStore
from tests.helpers.macro_context import macro_context_payload

JST = ZoneInfo("Asia/Tokyo")

MACRO_PERIODS = ("1y", "5y", "10y", "max")
MACRO_GRANULARITIES = ("daily", "weekly", "monthly", "yearly")


def _meta_source(root: Path) -> DbMetaSource:
    return DbMetaSource(
        root / "data/app/baibai.sqlite",
        root / "data/screening/runs.sqlite",
        root / "data/indicators/macro.sqlite",
    )


def _seed_macro_observations(root: Path) -> None:
    conn = open_connection(root / "data/indicators/macro.sqlite")
    try:
        insert_observations(
            conn,
            [
                ObservationRecord(
                    series_id="us.10y",
                    observed_at=date(2026, 7, 15),
                    value=4.3,
                    unit="%",
                    source_url="https://example.com/us10y",
                ),
                ObservationRecord(
                    series_id="jp.10y",
                    observed_at=date(2026, 7, 17),
                    value=1.1,
                    unit="%",
                    source_url="https://example.com/jp10y",
                ),
                ObservationRecord(
                    series_id="jp.10y",
                    observed_at=date(2026, 7, 20),
                    value=1.2,
                    unit="%",
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
    MacroContextService(app_method_root / "data/app/baibai.sqlite").publish(
        MacroContextDocument.model_validate(macro_context_payload()), expected_head=None
    )

    view = build_meta(_meta_source(app_method_root), batch="daily")

    assert view.screening_asof == date(2026, 7, 8)
    assert view.macro_asof == date(2026, 7, 17)
    assert view.app_db_updated_at == datetime(2026, 7, 19, 12, 0, tzinfo=JST)
    assert view.data_updated_at == datetime(2026, 7, 19, 12, 0, tzinfo=JST)
    assert view.batch == "daily"
    assert view.generated_at.tzinfo is not None


def test_build_meta_takes_the_latest_judgment_write_across_stores(
    app_method_root: Path,
) -> None:
    # The fixture's newest judgment write without a macro context is a task created
    # on 2026-07-18; its date-only column is read at JST midnight.
    view = build_meta(_meta_source(app_method_root))

    assert view.macro_asof is None
    assert view.app_db_updated_at == datetime(2026, 7, 18, 0, 0, tzinfo=JST)
    assert view.batch is None


def test_build_meta_reflects_a_newly_written_operation_session(
    app_method_root: Path,
) -> None:
    # A judgment write in a table beyond ledger/research/macro must move freshness.
    started_at = datetime(2026, 8, 1, 9, 30, tzinfo=JST)
    with sqlite3.connect(app_method_root / "data/app/baibai.sqlite") as connection:
        connection.execute(
            """
            INSERT INTO operation_session(
                operation_id, session_kind, status, as_of, ticker,
                started_at, completed_at, payload
            ) VALUES (?, 'opportunity', 'active', ?, NULL, ?, NULL, ?)
            """,
            (
                "operation-20260801-opportunity",
                "2026-08-01",
                started_at.isoformat(),
                canonical_json({"kind": "opportunity"}),
            ),
        )

    view = build_meta(_meta_source(app_method_root))

    assert view.app_db_updated_at == started_at
    assert view.data_updated_at == started_at


def test_build_meta_returns_none_for_missing_stores(tmp_path: Path) -> None:
    view = build_meta(_meta_source(tmp_path))

    assert view.screening_asof is None
    assert view.macro_asof is None
    assert view.app_db_updated_at is None
    assert view.data_updated_at is None


def test_export_writes_expected_view_tree(app_method_root: Path, tmp_path: Path) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    runs_db = app_method_root / "data/screening/runs.sqlite"
    run = ScreeningRunReader(runs_db).latest_run()
    assert run is not None
    ScreeningRunStore(runs_db).publish_selection(
        run_revision_id=run.run_revision_id,
        profile="value",
        macro_context_id=None,
        payload={
            "recommendations": [{"ticker": "2331", "er_annual": 0.12}],
            "longlist": [{"ticker": "0001"}],
        },
        created_at=datetime(2026, 7, 8, 4, 0, tzinfo=UTC),
    )
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
        "dashboard.json",
        "macro-reading.json",
        "screening_latest.json",
        "operations.json",
        "meta.json",
        "security--0001.json",
        "security--0002.json",
        "security--2331.json",
    } | {
        f"macro--{period}-{granularity}.json"
        for period in MACRO_PERIODS
        for granularity in MACRO_GRANULARITIES
    }
    assert {item.name for item in views.iterdir()} == expected

    DashboardView.model_validate_json((views / "dashboard.json").read_text(encoding="utf-8"))
    screening = ScreeningView.model_validate_json(
        (views / "screening_latest.json").read_text(encoding="utf-8")
    )
    assert screening.run is not None
    assert screening.run.candidate_count == 3
    assert len(screening.selections) == 1
    OperationsView.model_validate_json((views / "operations.json").read_text(encoding="utf-8"))
    meta = MetaView.model_validate_json((views / "meta.json").read_text(encoding="utf-8"))
    assert meta.batch == "daily"
    assert meta.screening_asof == date(2026, 7, 8)
    for period in MACRO_PERIODS:
        for granularity in MACRO_GRANULARITIES:
            macro = MacroView.model_validate_json(
                (views / f"macro--{period}-{granularity}.json").read_text(encoding="utf-8")
            )
            assert macro.period == period
            assert macro.granularity == granularity
    for name in ("security--0001.json", "security--0002.json", "security--2331.json"):
        detail = SecurityDetailView.model_validate_json((views / name).read_text(encoding="utf-8"))
        assert detail.candidate_row is not None

    select_files = sorted((output_dir / "history/select").iterdir())
    assert [item.name for item in select_files] == ["2026-07-08.json"]
    selection = MachineSelectionView.model_validate_json(
        select_files[0].read_text(encoding="utf-8")
    )
    assert [entry["ticker"] for entry in selection.recommendations] == ["2331"]
    assert [entry["ticker"] for entry in selection.longlist] == ["0001"]

    pool_files = sorted((output_dir / "history/candidate-views").iterdir())
    assert [item.name for item in pool_files] == ["2026-07-01.json", "2026-07-08.json"]
    pool = json.loads(pool_files[0].read_text(encoding="utf-8"))
    assert [candidate["ticker"] for candidate in pool["rows"]] == ["2331", "0001", "0002"]
    assert pool["run"]["asof_date"] == "2026-07-01"
    latest_pool = json.loads(pool_files[1].read_text(encoding="utf-8"))
    assert latest_pool["run"]["source_path"] == run.run_revision_id


def test_export_writes_macro_context_detail_views(app_method_root: Path, tmp_path: Path) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    db_path = app_method_root / "data/app/baibai.sqlite"
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
    # The overview view indexes the same report (summary only, no full sections).
    overview = MacroView.model_validate_json(
        (output_dir / "views/macro--1y-daily.json").read_text(encoding="utf-8")
    )
    assert [report.context_id for report in overview.reports] == [document.context_id]


def test_export_skips_the_reading_view_without_failing_when_its_rules_are_absent(
    app_method_root: Path, tmp_path: Path
) -> None:
    """A rules revision that is not deployed must cost the panel, not the whole export."""

    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (app_method_root / MACRO_READING_RULES_PATH).unlink()
    output_dir = tmp_path / "export"

    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 0
    assert not (output_dir / "views/macro-reading.json").exists()
    assert (output_dir / "views/macro--1y-daily.json").is_file()


def test_exported_views_match_api_responses(app_method_root: Path, tmp_path: Path) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    output_dir = tmp_path / "export"

    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 0

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        api_screening = client.get("/api/screening/latest").json()
        api_macro = client.get("/api/macro?period=1y&granularity=daily").json()
        api_dashboard = client.get("/api/dashboard").json()
    exported_screening = json.loads(
        (output_dir / "views/screening_latest.json").read_text(encoding="utf-8")
    )
    assert exported_screening == api_screening
    exported_macro = json.loads(
        (output_dir / "views/macro--1y-daily.json").read_text(encoding="utf-8")
    )
    assert exported_macro == api_macro
    exported_dashboard = json.loads(
        (output_dir / "views/dashboard.json").read_text(encoding="utf-8")
    )
    del exported_dashboard["generated_at"], api_dashboard["generated_at"]
    assert exported_dashboard == api_dashboard


def test_export_skips_security_view_for_ticker_no_source_knows(
    app_method_root: Path, tmp_path: Path, capsys
) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    payload = {
        "shortlist_id": "shortlist-20260708-value",
        "selection_id": "selection-old",
        "run_revision_id": "run-revision-old",
        "as_of": "2026-07-08",
        "published_at": "2026-07-08T13:00:00+09:00",
        "entries": [{"ticker": "9999", "decision": "rejected", "reason": "決算後に再評価"}],
    }
    with sqlite3.connect(app_method_root / "data/app/baibai.sqlite") as connection:
        connection.execute(
            """
            INSERT INTO shortlist(
                shortlist_id, selection_id, run_revision_id, as_of, published_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload["shortlist_id"],
                payload["selection_id"],
                payload["run_revision_id"],
                payload["as_of"],
                payload["published_at"],
                canonical_json(payload),
            ),
        )
    output_dir = tmp_path / "export"

    assert main(["--output-dir", str(output_dir), "--repo-root", str(app_method_root)]) == 0

    assert not (output_dir / "views/security--9999.json").exists()
    assert (output_dir / "views/security--2331.json").exists()
    assert "9999" in capsys.readouterr().err


def test_export_replaces_views_but_keeps_history(app_method_root: Path, tmp_path: Path) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    output_dir = tmp_path / "export"
    stale_view = output_dir / "views/security--0009.json"
    stale_view.parent.mkdir(parents=True)
    stale_view.write_text("{}", encoding="utf-8")
    kept_history = output_dir / "history/select/2026-01-01.json"
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
