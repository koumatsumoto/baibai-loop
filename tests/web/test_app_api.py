from __future__ import annotations

import json
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from tests.helpers.assessment_history import historical_context_payload, seed_history

from baibai_engine.market_context.assessment.models import StoredAssessment
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.read_api import MaterializationPreconditionError
from baibai_engine.storage.application.json import canonical_json
from baibai_engine.storage.application.schema import APPLICATION_SCHEMA_VERSION
from baibai_engine.storage.application.write import initialize_database
from baibai_engine.storage.observations.schema import SQLITE_SCHEMA_VERSION, open_connection
from baibai_web.api.server import PUBLIC_ASSETS, create_app
from baibai_web.cli import main


def test_api_exposes_read_views_and_spa_fallback(app_method_root: Path) -> None:
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        health = client.get("/api/health")
        dashboard = client.get("/api/dashboard")
        tasks = client.get("/api/tasks")
        screening = client.get("/api/screening/latest")
        screening_history = client.get("/api/screening/history")
        previous_screening = client.get("/api/screening/history/2026-07-01")
        market_context_response = client.get("/api/market-context?as_of=2026-07-19")
        detail = client.get("/api/securities/2331")

        assert health.status_code == 200
        assert health.json() == {"status": "ok", "root": str(app_method_root.resolve())}
        assert dashboard.status_code == 200
        assert dashboard.json()["total_capital_yen"] is None
        assert "open_tasks" not in dashboard.json()
        assert tasks.status_code == 200
        assert len(tasks.json()["open_tasks"]) == 2
        assert screening.status_code == 200
        assert screening.json()["run"]["analyzed_security_count"] == 3
        assert screening.json()["run"]["generated_at"] == "2026-07-08T12:00:00+09:00"
        assert screening_history.json() == {"dates": ["2026-07-08", "2026-07-01"]}
        assert previous_screening.status_code == 200
        assert previous_screening.json()["run"]["as_of"] == "2026-07-01"
        assert len(previous_screening.json()["rows"]) == 3
        assert previous_screening.json()["rows"][0]["portfolio_state"] == "held"
        assert previous_screening.json()["rows"][0]["has_research"] is True
        assert market_context_response.status_code == 200
        assert market_context_response.json()["reading"]["as_of"] == "2026-07-19"
        assert market_context_response.json()["reports"] == []
        assert [group["title"] for group in market_context_response.json()["groups"]] == [
            "金利・金融政策",
            "インフレ・賃金",
            "景気・雇用",
            "流動性・クレジット・リスク",
            "為替",
            "コモディティ",
            "株式・バリュエーション",
        ]
        indicator_series = {
            series["series_id"]: series
            for group in market_context_response.json()["groups"]
            for series in group["series"]
        }
        assert indicator_series["us.10y"]["tradingview_symbol"] == "TVC:US10Y"
        assert indicator_series["jp.pmi_manufacturing"]["tradingview_symbol"] is None
        assert detail.status_code == 200
        assert detail.json()["ticker"] == "2331"
        assert detail.json()["latest_assessment"]["document"]["permanent_loss_risks"]
        fallback = client.get("/securities/2331")
        assert fallback.status_code == 200
        assert "web/frontend/ を build" in fallback.text


def test_app_serves_every_built_public_asset(app_method_root: Path) -> None:
    dist = app_method_root / "web/frontend/dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    for filename in PUBLIC_ASSETS:
        (dist / filename).write_bytes(filename.encode())

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        responses = {name: client.get(f"/{name}") for name in PUBLIC_ASSETS}

    for filename, response in responses.items():
        assert response.status_code == 200, filename
        # An asset missing from the route list falls through to the SPA route, which answers
        # every path with index.html and a 200 — so the body is what tells them apart.
        assert response.content == filename.encode(), filename


def test_app_returns_404_for_unbuilt_public_assets(app_method_root: Path) -> None:
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        for filename in PUBLIC_ASSETS:
            assert client.get(f"/{filename}").status_code == 404, filename


def test_app_serves_every_public_asset_the_ui_links(app_method_root: Path) -> None:
    """The UI asks for these by URL, so a link the app cannot answer is a broken asset."""

    index = (Path(__file__).resolve().parents[2] / "web/frontend/index.html").read_text(
        encoding="utf-8"
    )
    linked = set(re.findall(r'href="/([\w.-]+\.(?:ico|png|webmanifest))"', index))

    assert linked, "web/frontend/index.html links no public assets — the pattern stopped matching"
    assert linked <= set(PUBLIC_ASSETS)


def test_api_meta_reports_store_freshness(app_method_root: Path) -> None:
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/meta")

    assert response.status_code == 200
    body = response.json()
    assert body["screening_as_of"] == "2026-07-08"
    assert body["indicators_as_of"] is None
    # The latest business-record timestamp is the task creation date, not a DB write time.
    assert datetime.fromisoformat(body["app_db_updated_at"]) == datetime(
        2026, 7, 18, 0, 0, tzinfo=ZoneInfo("Asia/Tokyo")
    )
    assert body["batch"] is None
    assert datetime.fromisoformat(body["data_updated_at"]).tzinfo is not None
    assert datetime.fromisoformat(body["generated_at"]).tzinfo is not None


def test_indicator_series_api_returns_only_the_requested_series(app_method_root: Path) -> None:
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/market-context/indicators/us.10y?as_of=2026-07-19")
        missing = client.get("/api/market-context/indicators/not.registered")

    assert response.status_code == 200
    assert response.json()["series_id"] == "us.10y"
    assert response.json()["points"] == []
    assert missing.status_code == 404


def test_market_context_overview_reports_every_registered_series_without_history(
    app_method_root: Path,
) -> None:
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/market-context?as_of=2026-07-19")

    assert response.status_code == 200
    brief = response.json()
    body = brief["reading"]
    assert body["as_of"] == "2026-07-19"
    assert body["rules_revision"]
    # The fixture store carries no observations, so every series reads as empty rather
    # than as a plausible number.
    assert len(body["series"]) == len(load_definitions().series)
    first = body["series"][0]
    assert first["latest_value"] is None
    assert first["next_print_estimate"] is None
    assert first["print_due_in_days"] is None
    assert first["stale"] is True
    assert first["insufficient_history"] is True
    assert (
        sum(len(series["points"]) for group in brief["groups"] for series in group["series"]) == 0
    )


def test_reading_api_distinguishes_failed_acquisition_from_missing_observations(
    app_method_root: Path,
) -> None:
    """Fetch failure and missing series history are separate facts in the response."""

    store = app_method_root / "stores/market_context/indicators.sqlite"
    with closing(sqlite3.connect(store)) as connection, connection:
        connection.execute(
            """
            INSERT INTO provider_runs(
                run_id, provider, series_id, range_start, range_end,
                started_at, finished_at, status, record_count, error_message
            ) VALUES (
                'run-1', 'fred_csv', 'us.10y', '2026-07-01', '2026-07-19',
                '2026-07-19T00:00:00+00:00', '2026-07-19T00:00:05+00:00', 'failed', 0,
                'provider returned 503'
            )
            """
        )

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        body = client.get("/api/market-context?as_of=2026-07-19").json()["reading"]

    failed = [item for item in body["fetch_health"] if item["status"] != "ok"]
    assert [item["series_id"] for item in failed] == ["us.10y"]
    assert failed[0]["error_message"] == "provider returned 503"
    # Missing observations are stale independently of the recorded acquisition failure.
    series = next(item for item in body["series"] if item["series_id"] == "us.10y")
    assert series["stale"] is True
    assert series["latest_value"] is None


def test_market_context_overview_survives_without_an_indicator_store(
    app_method_root: Path,
) -> None:
    """A missing store must let the page hide the panel, not fail the request handler."""

    (app_method_root / "stores/market_context/indicators.sqlite").unlink()

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/market-context?as_of=2026-07-19")

    assert response.status_code == 200
    assert response.json()["reading"] is None


def test_market_context_api_indexes_published_reports_without_full_sections(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    previous = StoredAssessment.from_payload(
        historical_context_payload(
            context_id="macro-context-2026-07-18-base",
            as_of="2026-07-18",
            published_at="2026-07-18T12:00:00+09:00",
        )
    )
    document = StoredAssessment.from_payload(
        historical_context_payload(
            as_of="2026-07-19",
            published_at="2026-07-20T12:00:00+09:00",
        )
    )
    seed_history(db_path, (previous).document, supersedes=None)
    seed_history(db_path, (document).document, supersedes=previous.context_id)

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/market-context?as_of=2026-07-19")

    assert response.status_code == 200
    body = response.json()
    # The brief keeps a bounded decision excerpt; the full ten sections stay in detail.
    assert body["latest_context"]["context_id"] == document.context_id
    assert body["latest_context"]["as_of"] == "2026-07-19"
    assert body["latest_context"]["published_at"] == "2026-07-20T12:00:00+09:00"
    assert "core" not in body["latest_context"]
    excerpt = body["latest_context"]
    assert excerpt["synthesis"]["dominant_forces"]
    assert len(excerpt["scenarios"]) == 3
    assert excerpt["material_deltas"]
    assert len(excerpt["research_priority_hints"]) == 1
    assert excerpt["bargain_topography"] is not None
    assert len(excerpt["estimate_caveats"]) == 1
    assert len(excerpt["sizing_cautions"]) == 1
    assert [report["context_id"] for report in body["reports"]] == [previous.context_id]


def test_market_context_assessment_detail_renders_core_ten_plus_connection_and_series_names(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    document = StoredAssessment.from_payload(historical_context_payload())
    seed_history(db_path, (document).document, supersedes=None)

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get(
            f"/api/market-context/assessments/{document.context_id}?as_of=2026-07-19"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["context_id"] == document.context_id
    assert [section["section_id"] for section in body["core"]] == [
        "regime_summary",
        "rates_policy",
        "growth_demand",
        "inflation_costs",
        "liquidity_credit",
        "fx",
        "japan",
        "valuation",
        "risk_environment",
        "monitoring",
    ]
    assert body["core"][0]["series"] == [
        {
            "series_id": "us.10y",
            "name": "米10Y利回り",
        }
    ]
    risk_environment = body["core"][8]
    assert risk_environment["risk_environment"]["stance"] == "neutral"
    assert risk_environment["scenarios"][0]["case"] == "base"
    assert risk_environment["scenarios"][0]["probability"] == 0.5
    assert len(risk_environment["scenarios"][0]["scorecard"]) == 2
    assert body["connection"]["section_id"] == "japan_equity_loop"
    assert body["connection"]["research_priority_hints"][0]["applies_to"]
    forces = body["synthesis"]["dominant_forces"]
    assert [force["force_id"] for force in forces] == ["rates-repricing", "fx-extreme"]
    assert forces[0]["series"] == [
        {"series_id": "us.10y", "name": "米10Y利回り"},
        {"series_id": "usd_jpy", "name": "USD/JPY"},
    ]
    assert body["synthesis"]["interactions"][0]["force_ids"] == ["rates-repricing", "fx-extreme"]
    assert body["connection"]["bargain_topography"]["source_ids"]
    assert body["connection"]["estimate_caveats"][0]["affected_component"] == "fv_anchor"


def test_market_context_assessment_detail_serves_a_revision_without_the_strategy_layer(
    app_method_root: Path,
) -> None:
    """A report published before the integrated layer keeps its detail page."""

    db_path = app_method_root / "stores/application/baibai.sqlite"
    initialize_database(db_path)
    legacy = historical_context_payload(strategy_layer=False)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO market_context_assessment (
                context_id, schema_version, as_of, published_at, supersedes_id, payload
            ) VALUES (?, 4, ?, ?, NULL, ?)
            """,
            (legacy["context_id"], legacy["as_of"], legacy["published_at"], json.dumps(legacy)),
        )
        connection.execute(
            "INSERT INTO market_context_assessment_head(singleton, context_id) VALUES (1, ?)",
            (legacy["context_id"],),
        )

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get(
            f"/api/market-context/assessments/{legacy['context_id']}?as_of=2026-07-19"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["synthesis"] is None
    assert body["core"][8]["scenarios"][0]["probability"] is None
    assert body["connection"]["bargain_topography"] is None
    assert body["connection"]["estimate_caveats"] == []


def test_history_id_is_available_independently_of_analysis_date(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    document = StoredAssessment.from_payload(historical_context_payload())
    seed_history(db_path, (document).document, supersedes=None)

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        assert client.get("/api/market-context/assessments/does-not-exist").status_code == 404
        # ID retrieval is historical viewing, not analytical eligibility.
        future = client.get(
            f"/api/market-context/assessments/{document.context_id}?as_of=2026-07-01"
        )
        assert future.status_code == 200
        assert future.json()["context_id"] == document.context_id
        assert future.json()["age_days"] < 0


def test_assessment_history_keeps_an_earlier_contract_visible(
    app_method_root: Path,
) -> None:
    """旧版の履歴も一覧と個別APIから読み出せる。"""

    db_path = app_method_root / "stores/application/baibai.sqlite"
    payload = {
        "schema_version": 2,
        "kind": "macro-context",
        "context_id": "macro-context-2026-07-19-earlier-contract",
        "as_of": "2026-07-19",
        "published_at": "2026-07-19T12:00:00+09:00",
        "summary": "旧契約のrevision",
    }
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO market_context_assessment(
                context_id, schema_version, as_of, published_at, supersedes_id, payload
            ) VALUES (?, 2, ?, ?, NULL, ?)
            """,
            (
                payload["context_id"],
                payload["as_of"],
                payload["published_at"],
                canonical_json(payload),
            ),
        )

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        detail = client.get(
            f"/api/market-context/assessments/{payload['context_id']}?as_of=2026-07-19"
        )
        overview = client.get("/api/market-context?as_of=2026-07-19")

    assert detail.status_code == 200
    assert overview.json()["reports"][0]["context_id"] == payload["context_id"]


def test_market_context_api_preserves_immutable_context_when_series_definition_is_absent(
    app_method_root: Path, mocker
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    document = StoredAssessment.from_payload(historical_context_payload())
    seed_history(db_path, (document).document, supersedes=None)
    mocker.patch("baibai_web.readmodel.assessment.indicator_series_names", return_value={})

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get(
            f"/api/market-context/assessments/{document.context_id}?as_of=2026-07-19"
        )

    assert response.status_code == 200
    assert response.json()["core"][0]["series"] == [{"series_id": "us.10y", "name": "us.10y"}]


def test_api_reads_the_explicit_application_database(app_method_root: Path) -> None:
    default_db = app_method_root / "stores/application/baibai.sqlite"
    alternate_db = app_method_root / "alternate.sqlite"
    shutil.copy2(default_db, alternate_db)
    default_db.unlink()

    with TestClient(
        create_app(app_method_root, db_path=alternate_db), base_url="http://127.0.0.1"
    ) as client:
        assert client.get("/api/dashboard").json()["total_capital_yen"] is None


def test_api_returns_404_for_unknown_security_and_api_route(app_method_root: Path) -> None:
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        unknown_security = client.get("/api/securities/0000")
        unknown_history = client.get("/api/screening/history/2026-06-01")
        unknown_api = client.get("/api/unknown")

        assert unknown_security.status_code == 404
        assert unknown_security.json() == {"detail": "unknown ticker"}
        assert unknown_history.status_code == 404
        assert unknown_api.status_code == 404
        assert client.post("/api/dashboard").status_code == 405


def test_api_rejects_non_loopback_host(app_method_root: Path) -> None:
    with TestClient(create_app(app_method_root), base_url="http://attacker.example") as client:
        response = client.get("/api/dashboard")

    assert response.status_code == 400


def test_api_startup_rejects_an_obsolete_application_store(
    app_method_root: Path, tmp_path: Path
) -> None:
    obsolete = tmp_path / "application-obsolete.sqlite"
    initialize_database(obsolete)
    with closing(sqlite3.connect(obsolete)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION - 1}")

    with pytest.raises(MaterializationPreconditionError, match="obsolete application"):
        create_app(app_method_root, db_path=obsolete)


def test_api_startup_rejects_an_obsolete_market_store(app_method_root: Path) -> None:
    obsolete = app_method_root / "stores/market/market.sqlite"
    open_connection(obsolete).close()
    with closing(sqlite3.connect(obsolete)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 1}")

    with pytest.raises(MaterializationPreconditionError, match="obsolete market"):
        create_app(app_method_root)


def test_cli_rejects_wrong_root_without_starting_server(tmp_path: Path, mocker) -> None:
    run = mocker.patch("baibai_web.cli.uvicorn.run")

    assert main(["serve", "--root", str(tmp_path)]) == 1
    run.assert_not_called()


def test_cli_binds_uvicorn_to_loopback(app_method_root: Path, mocker) -> None:
    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    run = mocker.patch("baibai_web.cli.uvicorn.run")

    assert main(["serve", "--root", str(app_method_root), "--port", "9012"]) == 0

    assert run.call_count == 1
    assert run.call_args.kwargs == {"host": "127.0.0.1", "port": 9012}


@pytest.mark.parametrize(
    "known_at",
    ["2026-07-18T15:00:00+00:00", "2026-07-19T00:00:00+09:00", "2026-07-18T08:00:00-07:00"],
)
@pytest.mark.parametrize("as_of", [None, "2026-07-17"])
def test_market_context_default_date_uses_jst_for_the_same_instant(
    app_method_root: Path, known_at: str, as_of: str | None
) -> None:
    parameters = {"known_at": known_at}
    if as_of is not None:
        parameters["as_of"] = as_of
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/market-context", params=parameters)
    assert response.status_code == 200
    assert response.json()["reading"]["as_of"] == (as_of or "2026-07-19")


def test_market_context_rejects_a_cutoff_without_timezone(app_method_root: Path) -> None:
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/market-context", params={"known_at": "2026-07-19T00:00:00"})
    assert response.status_code == 422


@pytest.mark.parametrize(("field", "value"), [("payload", "[]"), ("as_of", "2026-07-01")])
def test_existing_context_corruption_is_not_reported_as_missing(
    app_method_root: Path, field: str, value: str
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    document = StoredAssessment.from_payload(historical_context_payload())
    seed_history(db_path, document.document, supersedes=None)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            f"UPDATE market_context_assessment SET {field} = ? WHERE context_id = ?",
            (value, document.context_id),
        )
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get(f"/api/market-context/assessments/{document.context_id}")
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "invalid_source"
