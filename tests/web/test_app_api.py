from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from tests.helpers.macro_context import macro_context_payload

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.schema import APPLICATION_SCHEMA_VERSION
from baibai_engine.appdb.write import initialize_database
from baibai_engine.macro.context.models import MacroContextDocument
from baibai_engine.macro.context.service import MacroContextService
from baibai_engine.macro.indicators.definitions import load_definitions
from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION, open_connection
from baibai_engine.read_api import MaterializationPreconditionError
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
        macro = client.get("/api/macro?as_of=2026-07-19")
        detail = client.get("/api/securities/2331")

        assert health.status_code == 200
        assert health.json() == {"status": "ok", "root": str(app_method_root.resolve())}
        assert dashboard.status_code == 200
        assert dashboard.json()["total_capital_yen"] == 10_419_500
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
        assert macro.status_code == 200
        assert macro.json()["reading"]["asof"] == "2026-07-19"
        assert macro.json()["reports"] == []
        assert [group["title"] for group in macro.json()["groups"]] == [
            "金利・金融政策",
            "インフレ・賃金",
            "景気・雇用",
            "流動性・クレジット・リスク",
            "為替",
            "コモディティ",
            "株式・バリュエーション",
        ]
        macro_series = {
            series["series_id"]: series
            for group in macro.json()["groups"]
            for series in group["series"]
        }
        assert macro_series["us.10y"]["tradingview_symbol"] == "TVC:US10Y"
        assert macro_series["jp.pmi_manufacturing"]["tradingview_symbol"] is None
        assert detail.status_code == 200
        assert detail.json()["ticker"] == "2331"
        assert detail.json()["latest_thesis"]["permanent_loss_risk_count"] == 7
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
    assert body["screening_asof"] == "2026-07-08"
    assert body["macro_asof"] is None
    # Newest judgment write in the fixture is a task created on 2026-07-18 (JST date).
    assert datetime.fromisoformat(body["app_db_updated_at"]) == datetime(
        2026, 7, 18, 0, 0, tzinfo=ZoneInfo("Asia/Tokyo")
    )
    assert body["batch"] is None
    assert datetime.fromisoformat(body["data_updated_at"]).tzinfo is not None
    assert datetime.fromisoformat(body["generated_at"]).tzinfo is not None


def test_macro_series_api_returns_only_the_requested_series(app_method_root: Path) -> None:
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/macro/series/us.10y?as_of=2026-07-19")
        missing = client.get("/api/macro/series/not.registered")

    assert response.status_code == 200
    assert response.json()["series_id"] == "us.10y"
    assert response.json()["points"] == []
    assert missing.status_code == 404


def test_macro_brief_reports_every_registered_series_without_history(app_method_root: Path) -> None:
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/macro?as_of=2026-07-19")

    assert response.status_code == 200
    brief = response.json()
    body = brief["reading"]
    assert body["asof"] == "2026-07-19"
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


def test_macro_reading_api_surfaces_a_failed_acquisition_before_it_turns_stale(
    app_method_root: Path,
) -> None:
    """A silent provider must be visible immediately, not after the staleness threshold.

    The store keeps the previous observations, so the reading of a monthly series looks
    healthy for weeks after its source stops answering; the last run status is the only
    signal that arrives on the day it breaks.
    """

    store = app_method_root / "stores/macro/macro.sqlite"
    with sqlite3.connect(store) as connection:
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
        body = client.get("/api/macro?as_of=2026-07-19").json()["reading"]

    failed = [item for item in body["fetch_health"] if item["status"] != "ok"]
    assert [item["series_id"] for item in failed] == ["us.10y"]
    assert failed[0]["error_message"] == "provider returned 503"
    # The same series is not stale-flagged by the reading: it has no observations at all
    # in the fixture, which is a different fact than "the last fetch failed".
    assert body["series"]


def test_macro_brief_survives_without_an_indicator_store(
    app_method_root: Path,
) -> None:
    """A missing store must let the page hide the panel, not fail the request handler."""

    (app_method_root / "stores/macro/macro.sqlite").unlink()

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/macro?as_of=2026-07-19")

    assert response.status_code == 200
    assert response.json()["reading"] is None


def test_macro_api_indexes_published_reports_without_full_sections(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    previous = MacroContextDocument.model_validate(
        macro_context_payload(
            context_id="macro-context-2026-07-18-base",
            as_of="2026-07-18",
            published_at="2026-07-18T12:00:00+09:00",
        )
    )
    document = MacroContextDocument.model_validate(
        macro_context_payload(
            as_of="2026-07-19",
            published_at="2026-07-20T12:00:00+09:00",
        )
    )
    MacroContextService(db_path).publish(previous, expected_head=None)
    MacroContextService(db_path).publish(document, expected_head=previous.context_id)

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/macro?as_of=2026-07-19")

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


def test_macro_context_detail_renders_core_ten_plus_connection_and_series_names(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    document = MacroContextDocument.model_validate(macro_context_payload())
    MacroContextService(db_path).publish(document, expected_head=None)

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get(f"/api/macro/context/{document.context_id}?as_of=2026-07-19")

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


def test_macro_context_detail_serves_a_revision_without_the_strategy_layer(
    app_method_root: Path,
) -> None:
    """A report published before the integrated layer keeps its detail page."""

    db_path = app_method_root / "stores/application/baibai.sqlite"
    initialize_database(db_path)
    legacy = macro_context_payload(strategy_layer=False)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO macro_context (
                context_id, schema_version, as_of, published_at, supersedes_id, payload
            ) VALUES (?, 4, ?, ?, NULL, ?)
            """,
            (legacy["context_id"], legacy["as_of"], legacy["published_at"], json.dumps(legacy)),
        )
        connection.execute(
            "INSERT INTO macro_context_head(singleton, context_id) VALUES (1, ?)",
            (legacy["context_id"],),
        )

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get(f"/api/macro/context/{legacy['context_id']}?as_of=2026-07-19")

    assert response.status_code == 200
    body = response.json()
    assert body["synthesis"] is None
    assert body["core"][8]["scenarios"][0]["probability"] is None
    assert body["connection"]["bargain_topography"] is None
    assert body["connection"]["estimate_caveats"] == []


def test_macro_context_detail_404_for_unknown_and_future_context(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    document = MacroContextDocument.model_validate(macro_context_payload())
    MacroContextService(db_path).publish(document, expected_head=None)

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        assert client.get("/api/macro/context/does-not-exist").status_code == 404
        # The context as_of is 2026-07-19; asking before it makes it not-yet-eligible.
        future = client.get(f"/api/macro/context/{document.context_id}?as_of=2026-07-01")
        assert future.status_code == 404


def test_macro_api_does_not_serve_a_revision_from_an_earlier_contract(
    app_method_root: Path,
) -> None:
    """Older revisions stay in the table as a log; the UI is never asked to render them."""

    db_path = app_method_root / "stores/application/baibai.sqlite"
    payload = {
        "schema_version": 2,
        "kind": "macro-context",
        "context_id": "macro-context-2026-07-19-earlier-contract",
        "as_of": "2026-07-19",
        "published_at": "2026-07-19T12:00:00+09:00",
        "summary": "旧契約のrevision",
    }
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO macro_context(
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
        detail = client.get(f"/api/macro/context/{payload['context_id']}?as_of=2026-07-19")
        overview = client.get("/api/macro?as_of=2026-07-19")

    assert detail.status_code == 404
    assert overview.json()["reports"] == []


def test_macro_api_preserves_immutable_context_when_series_definition_is_absent(
    app_method_root: Path, mocker
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    document = MacroContextDocument.model_validate(macro_context_payload())
    MacroContextService(db_path).publish(document, expected_head=None)
    mocker.patch("baibai_web.readmodel.macro.macro_series_names", return_value={})

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get(f"/api/macro/context/{document.context_id}?as_of=2026-07-19")

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
        assert client.get("/api/dashboard").json()["total_capital_yen"] == 10_419_500


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
    obsolete = tmp_path / "application-v16.sqlite"
    initialize_database(obsolete)
    with sqlite3.connect(obsolete) as connection:
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION - 1}")

    with pytest.raises(MaterializationPreconditionError, match="obsolete application"):
        create_app(app_method_root, db_path=obsolete)


def test_api_startup_rejects_an_obsolete_market_store(app_method_root: Path) -> None:
    obsolete = app_method_root / "stores/market/market.sqlite"
    open_connection(obsolete).close()
    with sqlite3.connect(obsolete) as connection:
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
