"""FastAPI composition boundary for read-only application views."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from baibai_engine.read_api import (
    APPLICATION_DB_PATH,
    MARKET_DB_PATH,
    screening_run_asof_dates,
    validate_application_store_schema,
    validate_market_store_schema,
)
from baibai_web.readmodel.builders import (
    build_assessment_detail,
    build_daily_delta,
    build_dashboard,
    build_macro,
    build_macro_context_detail,
    build_macro_reading,
    build_meta,
    build_operations_view,
    build_screening,
    build_screening_history_run,
    build_security_detail,
)
from baibai_web.readmodel.models import (
    BargainAssessmentView,
    DailyDeltaView,
    DashboardView,
    MacroContextView,
    MacroReadingView,
    MacroView,
    MetaView,
    OperationsView,
    ScreeningHistoryRunView,
    ScreeningHistoryView,
    ScreeningView,
    SecurityDetailView,
)
from baibai_web.sources.factory import Sources, build_sources, load_macro_groups

# Vite copies `web/frontend/public/` into the build as-is, but this app answers only the files named
# here — one list so a route, its test and the UI's own <link> cannot drift apart.
PUBLIC_ASSETS = (
    "favicon.ico",
    "apple-touch-icon.png",
    "logo.png",
    "manifest.webmanifest",
    "icon-192.png",
    "icon-512.png",
)

_JST = ZoneInfo("Asia/Tokyo")


def create_app(
    root: Path,
    *,
    db_path: Path | None = None,
    runs_db_path: Path | None = None,
) -> FastAPI:
    """Create the local-only read API for one repository root."""

    resolved_root = root.resolve()
    app = FastAPI(
        title="Baibai Loop",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost"],
    )
    app.state.root = resolved_root
    app.state.db_path = db_path
    app.state.runs_db_path = runs_db_path
    # Load once at startup so a broken dashboard config fails app creation, not a request.
    app.state.macro_groups = load_macro_groups(resolved_root)
    validate_application_store_schema((db_path or resolved_root / APPLICATION_DB_PATH).resolve())
    validate_market_store_schema((resolved_root / MARKET_DB_PATH).resolve())

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
            sources.market,
        )

    @app.get("/api/daily-delta", response_model=DailyDeltaView)
    def daily_delta(sources: _SourceDependency) -> DailyDeltaView:
        return build_daily_delta(
            sources.candidates,
            sources.ledger,
            sources.research,
            sources.market,
            sources.macro,
        )

    @app.get("/api/screening/latest", response_model=ScreeningView)
    def screening(sources: _SourceDependency) -> ScreeningView:
        return build_screening(
            sources.candidates,
            sources.ledger,
            sources.research,
            sources.er_level_calibration,
        )

    @app.get("/api/screening/history", response_model=ScreeningHistoryView)
    def screening_history(sources: _SourceDependency) -> ScreeningHistoryView:
        return ScreeningHistoryView(dates=screening_run_asof_dates(sources.runs_db_path))

    @app.get("/api/screening/history/{as_of}", response_model=ScreeningHistoryRunView)
    def screening_history_run(
        as_of: date,
        sources: _SourceDependency,
    ) -> ScreeningHistoryRunView:
        view = build_screening_history_run(
            sources.candidates,
            sources.ledger,
            sources.research,
            as_of=as_of,
        )
        if view is None:
            raise HTTPException(status_code=404, detail="screening history not found")
        return view

    @app.get("/api/assessments/{assessment_id}", response_model=BargainAssessmentView)
    def assessment(assessment_id: str, sources: _SourceDependency) -> BargainAssessmentView:
        view = build_assessment_detail(sources.candidates, assessment_id=assessment_id)
        if view is None:
            raise HTTPException(status_code=404, detail="bargain assessment not found")
        return view

    @app.get("/api/macro", response_model=MacroView)
    def macro(
        sources: _SourceDependency,
        as_of: date | None = None,
        period: Literal["1y", "5y", "10y", "max"] = "1y",
        granularity: Literal["daily", "weekly", "monthly", "yearly"] = "daily",
    ) -> MacroView:
        return build_macro(
            sources.macro,
            as_of=as_of or datetime.now(_JST).date(),
            period=period,
            granularity=granularity,
        )

    @app.get("/api/macro/reading", response_model=MacroReadingView)
    def macro_reading(
        sources: _SourceDependency,
        asof: date | None = None,
    ) -> MacroReadingView:
        view = build_macro_reading(sources.macro, asof=asof or datetime.now(_JST).date())
        if view is None:
            raise HTTPException(status_code=404, detail="macro reading is unavailable")
        return view

    @app.get("/api/macro/context/{context_id}", response_model=MacroContextView)
    def macro_context(
        context_id: str,
        sources: _SourceDependency,
        as_of: date | None = None,
    ) -> MacroContextView:
        try:
            return build_macro_context_detail(
                sources.macro,
                context_id=context_id,
                as_of=as_of or datetime.now(_JST).date(),
            )
        except ValueError as error:
            # Unknown id and not-yet-eligible (future as_of) contexts are both "not found"
            # from the reader's perspective; the message distinguishes them for logs.
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/api/operations", response_model=OperationsView)
    def operations(sources: _SourceDependency) -> OperationsView:
        return build_operations_view(sources.operations)

    @app.get("/api/meta", response_model=MetaView)
    def meta(sources: _SourceDependency) -> MetaView:
        return build_meta(sources.meta)

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
            sources.market,
        )
        if view is None:
            raise HTTPException(status_code=404, detail="unknown ticker")
        return view

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


def _build_sources(request: Request) -> Sources:
    return build_sources(
        request.app.state.root,
        db_path=request.app.state.db_path,
        runs_db_path=request.app.state.runs_db_path,
        macro_groups=request.app.state.macro_groups,
    )


_SourceDependency = Annotated[Sources, Depends(_build_sources)]
