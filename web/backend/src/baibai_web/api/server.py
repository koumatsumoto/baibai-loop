"""Loopback-only read-only portfolio and task views."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from baibai_engine.storage.paths import APPLICATION_DB_PATH, MARKET_DB_PATH
from baibai_web.readmodel.builders.dashboard import build_dashboard
from baibai_web.readmodel.builders.outcomes import build_outcomes
from baibai_web.readmodel.builders.tasks import build_tasks
from baibai_web.readmodel.models import DashboardView, OutcomesView, TasksView
from baibai_web.sources.db_sources import DbLedgerSource, DbMarketSource, DbTaskSource

PUBLIC_ASSETS = (
    "favicon.ico",
    "apple-touch-icon.png",
    "logo.png",
    "manifest.webmanifest",
    "icon-192.png",
    "icon-512.png",
)


def create_app(root: Path, *, db_path: Path | None = None) -> FastAPI:
    resolved_root = root.resolve()
    application = db_path or resolved_root / APPLICATION_DB_PATH
    market_path = resolved_root / MARKET_DB_PATH
    app = FastAPI(title="Baibai Loop", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/dashboard", response_model=DashboardView)
    def dashboard() -> DashboardView:
        return build_dashboard(
            DbLedgerSource(application, market_path), DbMarketSource(market_path)
        )

    @app.get("/api/tasks", response_model=TasksView)
    def tasks() -> TasksView:
        return build_tasks(
            DbTaskSource(application),
            DbLedgerSource(application, market_path),
            DbMarketSource(market_path),
        )

    @app.get("/api/outcomes", response_model=OutcomesView)
    def outcomes() -> OutcomesView:
        return build_outcomes(application)

    dist = resolved_root / "web/frontend/dist"
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    for filename in PUBLIC_ASSETS:
        app.add_api_route(
            f"/{filename}",
            _public_asset_route(dist, filename),
            include_in_schema=False,
            response_model=None,
        )

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    def spa_fallback(full_path: str) -> FileResponse | PlainTextResponse:
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="unknown API endpoint")
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return PlainTextResponse(
            "web/frontend/ を build してください。API は /api/health で確認できます。"
        )

    return app


def _public_asset_route(dist: Path, filename: str) -> Callable[[], FileResponse]:
    """Serve an explicitly supported Vite public asset without widening the app surface.

    Anything not named in `PUBLIC_ASSETS` falls through to the SPA route and comes back as
    `index.html` with a 200, so an asset the UI links but this list omits is answered with
    a page instead of an image — and nothing reports an error.
    """

    def route() -> FileResponse:
        path = dist / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail="UI asset is not built")
        return FileResponse(path)

    return route
