from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from baibai_loop.app.api.server import create_app
from baibai_loop.app.cli import main


def test_api_exposes_read_views_and_spa_fallback(app_records_root: Path) -> None:
    with TestClient(create_app(app_records_root), base_url="http://127.0.0.1") as client:
        health = client.get("/api/health")
        dashboard = client.get("/api/dashboard")
        screening = client.get("/api/screening/latest")
        detail = client.get("/api/securities/2331")

        assert health.status_code == 200
        assert health.json() == {"status": "ok", "root": str(app_records_root.resolve())}
        assert dashboard.status_code == 200
        assert dashboard.json()["total_capital_yen"] == 10_419_500
        assert len(dashboard.json()["open_tasks"]) == 2
        assert screening.status_code == 200
        assert screening.json()["run"]["candidate_count"] == 3
        assert detail.status_code == 200
        assert detail.json()["ticker"] == "2331"
        assert detail.json()["latest_packet"]["permanent_loss_risk_count"] == 7
        fallback = client.get("/securities/2331")
        assert fallback.status_code == 200
        assert "ui/ を build" in fallback.text


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
    run = mocker.patch("baibai_loop.app.cli.uvicorn.run")

    assert main(["serve", "--root", str(tmp_path)]) == 1
    run.assert_not_called()


def test_cli_binds_uvicorn_to_loopback(app_records_root: Path, mocker) -> None:
    (app_records_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    run = mocker.patch("baibai_loop.app.cli.uvicorn.run")

    assert main(["serve", "--root", str(app_records_root), "--port", "9012"]) == 0

    assert run.call_count == 1
    assert run.call_args.kwargs == {"host": "127.0.0.1", "port": 9012}
