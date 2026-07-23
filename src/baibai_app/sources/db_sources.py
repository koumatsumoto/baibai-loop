"""Database-backed application source implementations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from baibai_app.sources.types import (
    CandidatesRun,
    HoldingReviewSummary,
    MacroGroupConfig,
    MacroSeriesConfig,
    ResearchRevision,
    ScenarioSummary,
    TaskRecord,
    ThesisDetail,
)
from baibai_engine.read_api import (
    MacroGranularity,
    PortfolioSnapshot,
    application_db_updated_at,
    latest_macro_context_payload,
    latest_shortlist_payload,
    latest_unadjusted_closes,
    list_holding_review_publications,
    list_macro_context_payloads,
    list_operation_sessions,
    list_portfolio_outcome_payloads,
    list_proposal_payloads,
    list_task_payloads,
    list_thesis_publications,
    list_thesis_review_publications,
    macro_indicator_series,
    macro_latest_observed_at,
    next_earnings_dates,
    portfolio_ledger_document,
    reconcile_portfolio,
    safe_load,
    screening_latest_asof,
    screening_run_payload,
    screening_selection_payloads,
    task_store_exists,
    thesis_publication,
)


class DbLedgerSource:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path.resolve()

    def exists(self) -> bool:
        return portfolio_ledger_document(self._path) is not None

    def snapshot(self) -> PortfolioSnapshot:
        document = portfolio_ledger_document(self._path)
        if document is None:
            raise ValueError("portfolio ledger has not been initialized")
        return reconcile_portfolio(document)


class DbMarketPriceSource:
    """Read the latest observed close per ticker from the licensed market store."""

    def __init__(self, market_db_path: Path) -> None:
        self._path = market_db_path.resolve()

    def latest_closes(self, tickers: Sequence[str]) -> Mapping[str, tuple[float, date]]:
        return latest_unadjusted_closes(self._path, tickers)

    def next_earnings_dates(self, tickers: Sequence[str], *, asof: date) -> Mapping[str, date]:
        return next_earnings_dates(self._path, tickers, asof=asof)


class DbOperationsSource:
    """Read proposal, operation, and outcome state for the Baibai App."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path.resolve()

    def operations(self) -> list[dict[str, object]]:
        return list_operation_sessions(self._path)

    def proposals(self) -> list[dict[str, object]]:
        return list_proposal_payloads(self._path)

    def outcomes(self) -> list[dict[str, object]]:
        return list_portfolio_outcome_payloads(self._path)


class DbResearchSource:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path.resolve()
        self._load_errors: list[str] = []

    def revisions(self) -> list[ResearchRevision]:
        reviews = {
            str(item["thesis_id"]): str(item["review_id"])
            for item in list_thesis_review_publications(self._path)
        }
        result: list[ResearchRevision] = []
        errors: list[str] = []
        for publication in list_thesis_publications(self._path):
            thesis_id = str(publication["thesis_id"])
            try:
                result.append(self._revision(publication, review_id=reviews.get(thesis_id)))
            except (KeyError, TypeError, ValueError):
                errors.append(thesis_id)
        self._load_errors = errors
        return result

    def thesis_detail(self, thesis_id: str) -> ThesisDetail:
        publication = thesis_publication(self._path, thesis_id=thesis_id)
        if publication is None:
            raise ValueError(f"unknown thesis_id: {thesis_id}")
        reviews = list_thesis_review_publications(self._path, thesis_id=thesis_id)
        revision = self._revision(
            publication,
            review_id=None if not reviews else str(reviews[0]["review_id"]),
        )
        payload = _mapping(publication["payload"], label="research thesis")
        estimates = _mapping(payload["estimates"], label="thesis estimates")
        judgment = _mapping(payload["judgment"], label="thesis judgment")
        risks = _mapping_list(payload["permanent_loss_risks"], label="permanent loss risks")
        scenarios = _mapping_list(estimates["scenarios"], label="research scenarios")
        return ThesisDetail(
            revision=revision,
            entry_price_basis_yen=_optional_float(estimates.get("entry_price_basis_yen")),
            required_5y_base_cagr_pct=_optional_float(estimates.get("required_5y_base_cagr_pct")),
            permanent_loss_risk_count=len(risks),
            scenarios=tuple(
                ScenarioSummary(
                    name=str(item["name"]),
                    horizon_years=int(str(item["horizon_years"])),
                )
                for item in scenarios
            ),
            permanent_loss_conclusion=_optional_text(judgment.get("permanent_loss_conclusion")),
            strongest_countercase=_optional_text(judgment.get("strongest_countercase")),
            sizing_action=_optional_text(judgment.get("sizing_action")),
        )

    def holding_reviews(self, *, ticker: str | None = None) -> list[HoldingReviewSummary]:
        result: list[HoldingReviewSummary] = []
        for publication in list_holding_review_publications(self._path, ticker=ticker):
            payload = _mapping(publication["payload"], label="holding review")
            result.append(
                HoldingReviewSummary(
                    holding_review_id=str(publication["holding_review_id"]),
                    ticker=str(publication["ticker"]),
                    as_of=date.fromisoformat(str(publication["as_of"])),
                    thesis_id=str(publication["thesis_id"]),
                    candidate_thesis_id=_optional_text(publication.get("candidate_thesis_id")),
                    action=str(payload["action"]),
                    note=_optional_text(payload.get("note")),
                )
            )
        return result

    def load_errors(self) -> list[str]:
        return list(self._load_errors)

    @staticmethod
    def _revision(
        publication: dict[str, object],
        *,
        review_id: str | None,
    ) -> ResearchRevision:
        payload = _mapping(publication["payload"], label="research thesis")
        snapshot = _mapping(payload["input_snapshot"], label="thesis input snapshot")
        estimates = _mapping(payload["estimates"], label="thesis estimates")
        judgment = _mapping(payload["judgment"], label="thesis judgment")
        return ResearchRevision(
            ticker=str(publication["ticker"]),
            company_name=str(snapshot["company_name"]),
            sector=str(snapshot["sector"]),
            as_of=date.fromisoformat(str(publication["as_of"])),
            thesis_id=str(publication["thesis_id"]),
            recommendation=str(publication["recommendation"]),
            confidence=_optional_text(judgment.get("confidence")),
            current_fair_value_yen=_optional_float(estimates.get("current_fair_value_yen")),
            model_version=_optional_text(estimates.get("model_version")),
            review_id=review_id,
        )


class DbTaskSource:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path.resolve()

    def exists(self) -> bool:
        return task_store_exists(self._path)

    def list_tasks(self) -> list[TaskRecord]:
        return [self._parse_task(payload) for payload in list_task_payloads(self._path)]

    @staticmethod
    def _parse_task(raw: dict[str, object]) -> TaskRecord:
        related_refs = raw.get("related_refs", [])
        if not isinstance(related_refs, list):
            raise ValueError("task related_refs must be an array")
        return TaskRecord(
            task_id=str(raw["task_id"]),
            title=str(raw["title"]),
            kind=str(raw["kind"]),
            status=str(raw["status"]),
            ticker=_optional_text(raw.get("ticker")),
            due_date=date.fromisoformat(str(raw["due_date"])),
            event_label=_optional_text(raw.get("event_label")),
            event_date=_optional_date(raw.get("event_date")),
            body_md=_optional_text(raw.get("body_md")),
            related_refs=tuple(str(item) for item in related_refs),
            created_at=date.fromisoformat(str(raw["created_at"])),
            closed_at=_optional_date(raw.get("closed_at")),
        )


class _SeriesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)


class _GroupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)
    series: tuple[_SeriesConfig, ...] = Field(min_length=1)


class _DashboardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    groups: tuple[_GroupConfig, ...] = Field(min_length=1)


def load_macro_panel_config(path: Path) -> tuple[MacroGroupConfig, ...]:
    raw = safe_load(path.read_text(encoding="utf-8"))
    config = _DashboardConfig.model_validate(raw)
    identifiers = [item.id for group in config.groups for item in group.series]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("macro panel series IDs must be unique")
    return tuple(
        MacroGroupConfig(
            title=group.title,
            series=tuple(
                MacroSeriesConfig(series_id=item.id, label=item.label) for item in group.series
            ),
        )
        for group in config.groups
    )


class DbMacroSource:
    def __init__(
        self,
        app_db_path: Path,
        indicators_db_path: Path,
        groups: tuple[MacroGroupConfig, ...],
    ) -> None:
        self._app_db_path = app_db_path.resolve()
        self._indicators_db_path = indicators_db_path.resolve()
        self.groups = groups

    def context(self, *, as_of: date) -> dict[str, object] | None:
        return latest_macro_context_payload(self._app_db_path, as_of=as_of)

    def contexts(self) -> list[dict[str, object]]:
        return list_macro_context_payloads(self._app_db_path)

    def series(
        self,
        series_id: str,
        *,
        start: date | None,
        end: date,
        granularity: MacroGranularity,
    ) -> dict[str, object] | None:
        return macro_indicator_series(
            self._indicators_db_path,
            series_id=series_id,
            start=start,
            end=end,
            granularity=granularity,
            limit=None,
        )


class DbMetaSource:
    """Read per-store freshness for the meta view."""

    def __init__(
        self,
        app_db_path: Path,
        runs_db_path: Path,
        indicators_db_path: Path,
    ) -> None:
        self._app_db_path = app_db_path.resolve()
        self._runs_db_path = runs_db_path.resolve()
        self._indicators_db_path = indicators_db_path.resolve()

    def screening_asof(self) -> date | None:
        return screening_latest_asof(self._runs_db_path)

    def macro_asof(self) -> date | None:
        return macro_latest_observed_at(self._indicators_db_path)

    def app_db_updated_at(self) -> datetime | None:
        return application_db_updated_at(self._app_db_path)


class DbCandidatesSource:
    def __init__(self, runs_db_path: Path, app_db_path: Path) -> None:
        self._runs_path = runs_db_path.resolve()
        self._app_path = app_db_path.resolve()

    def latest_run(self) -> CandidatesRun | None:
        raw = screening_run_payload(self._runs_path)
        return None if raw is None else self._parse_run(raw)

    def run(self, run_revision_id: str) -> CandidatesRun | None:
        raw = screening_run_payload(self._runs_path, run_revision_id=run_revision_id)
        return None if raw is None else self._parse_run(raw)

    def selections(self, *, run_revision_id: str | None = None) -> list[dict[str, object]]:
        return screening_selection_payloads(
            self._runs_path,
            run_revision_id=run_revision_id,
        )

    def shortlists(self) -> list[dict[str, object]]:
        latest = latest_shortlist_payload(self._app_path)
        return [] if latest is None else [latest]

    @staticmethod
    def _parse_run(raw: dict[str, object]) -> CandidatesRun:
        candidates = raw["candidates"]
        if not isinstance(candidates, list) or not all(
            isinstance(item, dict) for item in candidates
        ):
            raise ValueError("screening candidates must be an array of objects")
        return CandidatesRun(
            run_id=str(raw["public_run_id"]),
            run_date=date.fromisoformat(str(raw["run_date"])),
            asof_date=date.fromisoformat(str(raw["as_of_date"])),
            run_at=datetime.fromisoformat(str(raw["run_at"])),
            universe_size=int(str(raw["universe_size"])),
            source_path=str(raw["run_revision_id"]),
            application_git_commit=_optional_text(raw.get("application_git_commit")),
            rows=tuple(candidates),
        )


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_date(value: object) -> date | None:
    return None if value is None else date.fromisoformat(str(value))


def _optional_float(value: object) -> float | None:
    return None if value is None else float(str(value))


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _mapping_list(value: object, *, label: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ValueError(f"{label} must be an array of objects")
    return list(value)
