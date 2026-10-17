"""FastAPI composition boundary for read-only application views."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime

from baibai_engine.read_api import (
    APPLICATION_DB_PATH,
    MARKET_DB_PATH,
    screening_run_asof_dates,
    validate_application_store_schema,
    validate_market_store_schema,
)
from baibai_web.readmodel.assessment import build_market_context_assessment_detail
from baibai_web.readmodel.builders import (
    build_daily_delta,
    build_dashboard,
    build_meta,
    build_operations_view,
    build_tasks,
)
from baibai_web.readmodel.market_context import build_indicator_series, build_market_context
from baibai_web.readmodel.models import (
    AssessmentPublicationView,
    DailyDeltaView,
    DashboardView,
    IndicatorSeriesView,
    InvestmentProposalPreviewView,
    InvestmentProposalsView,
    InvestmentProposalView,
    MarketContextView,
    MetaView,
    OperationsView,
    ScreeningHistoryRunView,
    ScreeningHistoryView,
    ScreeningView,
    SecurityDetailView,
    StockAssessmentDetailView,
    StockAssessmentsView,
    TasksView,
)
from baibai_web.readmodel.stocks import (
    build_screening,
    build_screening_history_run,
    build_security_detail,
)
from baibai_web.readmodel.stocks.research import (
    PublicationCursorError,
    build_proposal_detail,
    build_proposal_list,
    build_proposal_preview,
    build_stock_assessment_detail,
    build_stock_assessment_list,
)
from baibai_web.sources.factory import Sources, build_sources, load_indicator_groups

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


def _research_source_error(error: ValueError) -> HTTPException:
    return HTTPException(status_code=500, detail={"code": "invalid_source", "message": str(error)})


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
    app.state.indicator_groups = load_indicator_groups(resolved_root)
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
            sources.screening,
            sources.market,
        )

    @app.get("/api/tasks", response_model=TasksView)
    def tasks(sources: _SourceDependency) -> TasksView:
        return build_tasks(
            sources.tasks,
            sources.ledger,
            sources.research,
            sources.screening,
            sources.market,
        )

    @app.get("/api/daily-delta", response_model=DailyDeltaView)
    def daily_delta(sources: _SourceDependency) -> DailyDeltaView:
        return build_daily_delta(
            sources.screening,
            sources.ledger,
            sources.research,
            sources.market,
        )

    @app.get("/api/screening/latest", response_model=ScreeningView)
    def screening(sources: _SourceDependency) -> ScreeningView:
        return build_screening(
            sources.screening,
            sources.ledger,
            sources.research,
            sources.er_level_calibration,
            external_assessment=sources.external_assessment,
            market=sources.market,
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
            sources.screening,
            sources.ledger,
            sources.research,
            as_of=as_of,
        )
        if view is None:
            raise HTTPException(status_code=404, detail="screening history not found")
        return view

    @app.get("/api/stock-assessments", response_model=StockAssessmentsView)
    def stock_assessments(
        sources: _SourceDependency,
        ticker: str | None = None,
        limit: int = Query(default=100, ge=1, le=100),
        cursor: str | None = None,
    ) -> StockAssessmentsView:
        try:
            return build_stock_assessment_list(
                sources.research, ticker=ticker, limit=limit, cursor=cursor
            )
        except PublicationCursorError as error:
            raise HTTPException(
                status_code=400, detail={"code": "invalid_cursor", "message": str(error)}
            ) from error
        except ValueError as error:
            raise _research_source_error(error) from error

    @app.get("/api/stock-assessments/{assessment_id}", response_model=StockAssessmentDetailView)
    def stock_assessment(
        assessment_id: str, sources: _SourceDependency
    ) -> StockAssessmentDetailView:
        try:
            view = build_stock_assessment_detail(sources.research, assessment_id=assessment_id)
        except ValueError as error:
            raise _research_source_error(error) from error
        if view is None:
            raise HTTPException(status_code=404, detail="stock assessment not found")
        return view

    @app.get("/api/investment-proposals", response_model=InvestmentProposalsView)
    def investment_proposals(
        sources: _SourceDependency,
        scope: Literal["discover_new", "specified_stocks", "review_positions"] | None = None,
        ticker: str | None = None,
        limit: int = Query(default=100, ge=1, le=100),
        cursor: str | None = None,
    ) -> InvestmentProposalsView:
        try:
            return build_proposal_list(
                sources.research, scope=scope, ticker=ticker, limit=limit, cursor=cursor
            )
        except PublicationCursorError as error:
            raise HTTPException(
                status_code=400, detail={"code": "invalid_cursor", "message": str(error)}
            ) from error
        except ValueError as error:
            raise _research_source_error(error) from error

    @app.get("/api/investment-proposals/{proposal_id}", response_model=InvestmentProposalView)
    def investment_proposal(proposal_id: str, sources: _SourceDependency) -> InvestmentProposalView:
        try:
            view = build_proposal_detail(sources.research, proposal_id=proposal_id)
        except ValueError as error:
            raise _research_source_error(error) from error
        if view is None:
            raise HTTPException(status_code=404, detail="investment proposal not found")
        return view

    @app.get(
        "/api/investment-proposals/{proposal_id}/current-preview",
        response_model=InvestmentProposalPreviewView,
    )
    def investment_proposal_preview(
        proposal_id: str, sources: _SourceDependency
    ) -> InvestmentProposalPreviewView:
        try:
            view = build_proposal_preview(
                sources.research,
                proposal_id=proposal_id,
                sqlite_path=sources.market_db_path,
                now=datetime.now(_JST),
            )
        except ValueError as error:
            raise _research_source_error(error) from error
        if view is None:
            raise HTTPException(status_code=404, detail="investment proposal not found")
        return view

    @app.get("/api/market-context", response_model=MarketContextView)
    def market_context_overview(
        sources: _SourceDependency,
        as_of: date | None = None,
        known_at: AwareDatetime | None = None,
    ) -> MarketContextView:
        cutoff = known_at or datetime.now(_JST)
        return build_market_context(
            sources.market_context,
            as_of=as_of or cutoff.astimezone(_JST).date(),
            known_at=cutoff,
            market=sources.market,
        )

    @app.get("/api/market-context/indicators/{series_id}", response_model=IndicatorSeriesView)
    def indicator_series_view(
        series_id: str,
        sources: _SourceDependency,
        as_of: date | None = None,
    ) -> IndicatorSeriesView:
        view = build_indicator_series(
            sources.market_context,
            series_id=series_id,
            as_of=as_of or datetime.now(_JST).date(),
        )
        if view is None:
            raise HTTPException(status_code=404, detail="indicator series is unavailable")
        return view

    @app.get(
        "/api/market-context/assessments/{context_id}", response_model=AssessmentPublicationView
    )
    def market_context_assessment(
        context_id: str,
        sources: _SourceDependency,
        as_of: date | None = None,
    ) -> AssessmentPublicationView:
        try:
            return build_market_context_assessment_detail(
                sources.market_context,
                context_id=context_id,
                as_of=as_of or datetime.now(_JST).date(),
            )
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise _research_source_error(error) from error

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
            sources.screening,
            sources.market,
            external_assessment=sources.external_assessment,
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
        indicator_groups=request.app.state.indicator_groups,
    )


_SourceDependency = Annotated[Sources, Depends(_build_sources)]
