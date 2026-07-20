from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from baibai_app.api.server import create_app
from baibai_app.cli import main
from baibai_engine.appdb.json import canonical_json
from baibai_engine.macro.models import MacroContextDocument
from baibai_engine.macro.service import MacroContextService
from tests.helpers.macro_context import macro_context_payload


def test_api_exposes_read_views_and_spa_fallback(app_records_root: Path) -> None:
    with TestClient(create_app(app_records_root), base_url="http://127.0.0.1") as client:
        health = client.get("/api/health")
        dashboard = client.get("/api/dashboard")
        screening = client.get("/api/screening/latest")
        macro = client.get("/api/macro?as_of=2026-07-19&period=5y&granularity=yearly")
        detail = client.get("/api/securities/2331")

        assert health.status_code == 200
        assert health.json() == {"status": "ok", "root": str(app_records_root.resolve())}
        assert dashboard.status_code == 200
        assert dashboard.json()["total_capital_yen"] == 10_419_500
        assert len(dashboard.json()["open_tasks"]) == 2
        assert screening.status_code == 200
        assert screening.json()["run"]["candidate_count"] == 3
        assert screening.json()["run"]["run_at"] == "2026-07-08T12:00:00+09:00"
        assert macro.status_code == 200
        assert macro.json()["period"] == "5y"
        assert macro.json()["granularity"] == "yearly"
        assert macro.json()["context"] is None
        assert [group["title"] for group in macro.json()["groups"]] == [
            "金利・金融条件",
            "為替・物価",
            "景気・市場",
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
        assert detail.json()["latest_packet"]["permanent_loss_risk_count"] == 7
        fallback = client.get("/securities/2331")
        assert fallback.status_code == 200
        assert "ui/ を build" in fallback.text


def test_macro_api_rejects_unknown_period_and_granularity(app_records_root: Path) -> None:
    with TestClient(create_app(app_records_root), base_url="http://127.0.0.1") as client:
        assert client.get("/api/macro?period=20y").status_code == 422
        assert client.get("/api/macro?granularity=quarterly").status_code == 422


def test_macro_api_renders_eight_section_context_and_series_names(
    app_records_root: Path,
) -> None:
    db_path = app_records_root / "data/app/baibai.sqlite"
    document = MacroContextDocument.model_validate(macro_context_payload())
    MacroContextService(db_path).publish(document, expected_head=None)

    with TestClient(create_app(app_records_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/macro?as_of=2026-07-19")

    assert response.status_code == 200
    body = response.json()
    assert [section["section_id"] for section in body["context"]["sections"]] == [
        "regime_summary",
        "rates_policy",
        "growth_demand",
        "inflation_costs",
        "fx_liquidity",
        "japan_specific",
        "scenarios_connections",
        "monitoring_points",
    ]
    assert body["context"]["sections"][0]["series"] == [
        {
            "series_id": "us.10y",
            "name": "米10Y利回り",
        }
    ]
    assert body["context"]["sections"][6]["scenarios"][0]["case"] == "base"
    assert body["context_history"][0]["context_id"] == document.context_id


def test_macro_api_displays_common_fields_for_sectionless_revision(
    app_records_root: Path,
) -> None:
    db_path = app_records_root / "data/app/baibai.sqlite"
    payload = {
        "schema_version": 2,
        "kind": "macro-context",
        "context_id": "macro-context-2026-07-19-sectionless",
        "as_of": "2026-07-19",
        "valid_until": "2026-08-19",
        "published_at": "2026-07-19T12:00:00+09:00",
        "summary": "共通 field の表示確認",
    }
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO macro_context(
                context_id, as_of, valid_until, published_at, supersedes_id, payload
            ) VALUES (?, ?, ?, ?, NULL, ?)
            """,
            (
                payload["context_id"],
                payload["as_of"],
                payload["valid_until"],
                payload["published_at"],
                canonical_json(payload),
            ),
        )

    with TestClient(create_app(app_records_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/macro?as_of=2026-07-19")

    assert response.status_code == 200
    assert response.json()["context"] == {
        "context_id": payload["context_id"],
        "as_of": payload["as_of"],
        "valid_until": payload["valid_until"],
        "published_at": payload["published_at"],
        "summary": payload["summary"],
        "stale": False,
        "sections": [],
    }


def test_macro_api_preserves_immutable_context_when_series_definition_is_absent(
    app_records_root: Path, mocker
) -> None:
    db_path = app_records_root / "data/app/baibai.sqlite"
    document = MacroContextDocument.model_validate(macro_context_payload())
    MacroContextService(db_path).publish(document, expected_head=None)
    mocker.patch("baibai_app.readmodel.builders.macro_series_names", return_value={})

    with TestClient(create_app(app_records_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/macro?as_of=2026-07-19")

    assert response.status_code == 200
    assert response.json()["context"]["sections"][0]["series"] == [
        {"series_id": "us.10y", "name": "us.10y"}
    ]


def test_api_reads_the_explicit_application_database(app_records_root: Path) -> None:
    default_db = app_records_root / "data/app/baibai.sqlite"
    alternate_db = app_records_root / "alternate.sqlite"
    shutil.copy2(default_db, alternate_db)
    default_db.unlink()

    with TestClient(
        create_app(app_records_root, db_path=alternate_db), base_url="http://127.0.0.1"
    ) as client:
        assert client.get("/api/dashboard").json()["total_capital_yen"] == 10_419_500


def test_api_returns_404_for_unknown_security_and_api_route(app_records_root: Path) -> None:
    with TestClient(create_app(app_records_root), base_url="http://127.0.0.1") as client:
        unknown_security = client.get("/api/securities/0000")
        unknown_api = client.get("/api/unknown")

        assert unknown_security.status_code == 404
        assert unknown_security.json() == {"detail": "unknown ticker"}
        assert unknown_api.status_code == 404
        assert client.post("/api/dashboard").status_code == 405


def test_api_rejects_non_loopback_host(app_records_root: Path) -> None:
    with TestClient(create_app(app_records_root), base_url="http://attacker.example") as client:
        response = client.get("/api/dashboard")

    assert response.status_code == 400


def test_cli_rejects_wrong_root_without_starting_server(tmp_path: Path, mocker) -> None:
    run = mocker.patch("baibai_app.cli.uvicorn.run")

    assert main(["serve", "--root", str(tmp_path)]) == 1
    run.assert_not_called()


def test_cli_binds_uvicorn_to_loopback(app_records_root: Path, mocker) -> None:
    (app_records_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    run = mocker.patch("baibai_app.cli.uvicorn.run")

    assert main(["serve", "--root", str(app_records_root), "--port", "9012"]) == 0

    assert run.call_count == 1
    assert run.call_args.kwargs == {"host": "127.0.0.1", "port": 9012}
