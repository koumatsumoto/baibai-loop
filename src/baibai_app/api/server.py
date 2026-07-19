"""FastAPI composition boundary for read-only application views."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from baibai_app.readmodel.builders import (
    build_dashboard,
    build_macro,
    build_screening,
    build_security_detail,
)
from baibai_app.readmodel.models import DashboardView, MacroView, ScreeningView, SecurityDetailView
from baibai_app.sources.db_sources import (
    DbCandidatesSource,
    DbMacroSource,
    DbTaskSource,
    load_macro_dashboard_config,
)
from baibai_app.sources.yaml_sources import (
    YamlLedgerSource,
    YamlResearchSource,
)

_JST = ZoneInfo("Asia/Tokyo")


@dataclass(frozen=True, slots=True)
class _Sources:
    ledger: YamlLedgerSource
    research: YamlResearchSource
    tasks: DbTaskSource
    candidates: DbCandidatesSource
    macro: DbMacroSource


def create_app(root: Path) -> FastAPI:
    """Create the local-only read API for one repository root."""

    resolved_root = root.resolve()
    app = FastAPI(
        title="Baibai-Loop cockpit",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost"],
    )
    app.state.root = resolved_root
    app.state.macro_groups = load_macro_dashboard_config(
        resolved_root / "records/_config/macro-dashboard.yaml"
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "root": str(resolved_root)}

    @app.get("/api/dashboard", response_model=DashboardView)
    def dashboard(sources: _SourceDependency) -> DashboardView:
        return build_dashboard(
            sources.ledger,
            sources.research,
            sources.tasks,
            sources.candidates,
        )

    @app.get("/api/screening/latest", response_model=ScreeningView)
    def screening(
        sources: _SourceDependency,
        run_revision_id: str | None = None,
    ) -> ScreeningView:
        return build_screening(
            sources.candidates,
            sources.ledger,
            sources.research,
            run_revision_id=run_revision_id,
        )

    @app.get("/api/macro", response_model=MacroView)
    def macro(
        sources: _SourceDependency,
        as_of: date | None = None,
    ) -> MacroView:
        return build_macro(sources.macro, as_of=as_of or datetime.now(_JST).date())

    @app.get("/api/securities/{ticker}", response_model=SecurityDetailView)
    def security_detail(
        ticker: str,
        sources: _SourceDependency,
    ) -> SecurityDetailView:
        view = build_security_detail(
            ticker,
            sources.ledger,
            sources.research,
            sources.candidates,
        )
        if view is None:
            raise HTTPException(status_code=404, detail="unknown ticker")
        return view

    dist = resolved_root / "ui/dist"
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    def spa_fallback(full_path: str) -> FileResponse | PlainTextResponse:
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="unknown API endpoint")
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return PlainTextResponse("ui/ を build してください。API は /api/health で確認できます。")

    return app


def _build_sources(request: Request) -> _Sources:
    root: Path = request.app.state.root
    return _Sources(
        ledger=YamlLedgerSource(root),
        research=YamlResearchSource(root),
        tasks=DbTaskSource(root / "data/app/baibai.sqlite"),
        candidates=DbCandidatesSource(
            root / "data/screening/runs.sqlite",
            root / "data/app/baibai.sqlite",
        ),
        macro=DbMacroSource(
            root / "data/app/baibai.sqlite",
            root / "data/indicators/macro.sqlite",
            request.app.state.macro_groups,
        ),
    )


_SourceDependency = Annotated[_Sources, Depends(_build_sources)]
