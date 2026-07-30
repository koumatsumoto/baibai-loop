"""Database-backed application source implementations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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
    ProviderFailureStreak,
    StoreStats,
    application_db_updated_at,
    application_store_stats,
    bargain_assessment_payload,
    close_change_since,
    latest_disclosure_dates_after,
    latest_shortlist_payload,
    latest_unadjusted_closes,
    list_bargain_assessment_payloads,
    list_holding_review_publications,
    list_macro_context_payloads,
    list_operation_sessions,
    list_portfolio_outcome_payloads,
    list_proposal_payloads,
    list_task_payloads,
    list_thesis_publications,
    list_thesis_review_publications,
    macro_context_payload,
    macro_context_triggers,
    macro_indicator_series,
    macro_latest_observed_at,
    macro_reading_snapshot,
    macro_series_fetch_health,
    never_attempted_series,
    next_earnings_dates,
    portfolio_ledger_document,
    previous_business_day,
    provider_failure_streaks,
    reconcile_portfolio,
    safe_load,
    screening_latest_asof,
    screening_run_asof_dates,
    screening_run_payload,
    screening_selection_payloads,
    store_stats,
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

    def exists(self) -> bool:
        return self._path.is_file()

    def previous_business_day(self, day: date) -> date | None:
        return previous_business_day(self._path, day)

    def latest_closes(self, tickers: Sequence[str]) -> Mapping[str, tuple[float, date]]:
        return latest_unadjusted_closes(self._path, tickers)

    def close_changes_since(self, tickers: Sequence[str], *, since: date) -> Mapping[str, float]:
        return close_change_since(self._path, tickers, since=since)

    def disclosures_after(self, tickers: Sequence[str], *, after: date) -> Mapping[str, date]:
        return latest_disclosure_dates_after(self._path, tickers, after=after)

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
    # Optional display override; when omitted the series registry ``name`` is used
    # so panel and registry do not carry two sources of truth for the same label.
    label: str | None = Field(default=None, min_length=1)


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
        reading_rules_path: Path,
    ) -> None:
        self._app_db_path = app_db_path.resolve()
        self._indicators_db_path = indicators_db_path.resolve()
        self._reading_rules_path = reading_rules_path
        self.groups = groups

    def reading(self, *, asof: date) -> dict[str, object] | None:
        return macro_reading_snapshot(
            self._indicators_db_path, asof=asof, rules_path=self._reading_rules_path
        )

    def fetch_health(self) -> list[dict[str, object]]:
        return macro_series_fetch_health(self._indicators_db_path)

    def context_by_id(self, *, context_id: str, as_of: date) -> dict[str, object]:
        return macro_context_payload(self._app_db_path, context_id=context_id, as_of=as_of)

    def contexts(self) -> list[dict[str, object]]:
        return list_macro_context_payloads(self._app_db_path)

    def context_triggers(self, *, context_id: str, as_of: date) -> dict[str, object] | None:
        return macro_context_triggers(
            self._app_db_path,
            self._indicators_db_path,
            context_id=context_id,
            as_of=as_of,
        )

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


class DbSystemSource:
    """Read the operational state of the machine stores for the system view.

    Separate from :class:`DbMetaSource`: that one dates the data a judgment view
    shows, this one answers whether the pipeline behind those stores is working.
    """

    def __init__(
        self,
        app_db_path: Path,
        runs_db_path: Path,
        indicators_db_path: Path,
        market_db_path: Path,
    ) -> None:
        self._app_db_path = app_db_path.resolve()
        self._runs_db_path = runs_db_path.resolve()
        self._indicators_db_path = indicators_db_path.resolve()
        self._market_db_path = market_db_path.resolve()

    def stores(self) -> list[StoreStats]:
        """Return every store in pipeline order: prices, runs, indicators, judgment."""

        return [
            store_stats("market", self._market_db_path),
            store_stats("runs", self._runs_db_path),
            store_stats("macro", self._indicators_db_path),
            application_store_stats(self._app_db_path),
        ]

    def application_updated_at(self) -> datetime | None:
        return application_db_updated_at(self._app_db_path)

    def macro_asof(self) -> date | None:
        """Date the macro store the same way the judgment views do.

        Reuses the freshness rule (ok status only, retractions hide the date,
        registered series only) so the operations view cannot report a newer
        "latest data" than the header the reader sees on every page.
        """

        return macro_latest_observed_at(self._indicators_db_path)

    def failing_providers(self) -> list[ProviderFailureStreak]:
        return provider_failure_streaks(self._indicators_db_path)

    def never_attempted(self) -> list[str]:
        return never_attempted_series(self._indicators_db_path)

    def fetch_health(self) -> list[dict[str, object]]:
        return macro_series_fetch_health(self._indicators_db_path)


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

    def data_updated_at(self) -> datetime | None:
        """Return the newest timestamp recorded by any UI data store."""

        candidates = screening_run_payload(self._runs_db_path)
        screening_updated_at = (
            None if candidates is None else datetime.fromisoformat(str(candidates["run_at"]))
        )
        macro_observed_at = macro_latest_observed_at(self._indicators_db_path)
        macro_updated_at = (
            None
            if macro_observed_at is None
            else datetime.combine(
                macro_observed_at, datetime.min.time(), tzinfo=ZoneInfo("Asia/Tokyo")
            )
        )
        return max(
            (
                value
                for value in (
                    self.app_db_updated_at(),
                    screening_updated_at,
                    macro_updated_at,
                )
                if value is not None
            ),
            default=None,
        )


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

    def previous_run(self) -> CandidatesRun | None:
        """Return the newest run of the greatest as-of before the latest one.

        A delta needs a stated earlier side. Reading it from the retained run dates
        keeps "no predecessor" (a fresh or pruned store) distinct from "nothing
        changed", which the caller reports as an unavailable section.
        """
        dates = screening_run_asof_dates(self._runs_path, limit=2)
        if len(dates) < 2:
            return None
        return self.run_as_of(dates[1])

    def run_as_of(self, as_of: date) -> CandidatesRun | None:
        raw = screening_run_payload(self._runs_path, as_of_date=as_of)
        return None if raw is None else self._parse_run(raw)

    def selections(self, *, run_revision_id: str | None = None) -> list[dict[str, object]]:
        return screening_selection_payloads(
            self._runs_path,
            run_revision_id=run_revision_id,
        )

    def shortlists(self) -> list[dict[str, object]]:
        latest = latest_shortlist_payload(self._app_path)
        return [] if latest is None else [latest]

    def assessments(self) -> list[dict[str, object]]:
        return list_bargain_assessment_payloads(self._app_path)

    def assessment(self, assessment_id: str) -> dict[str, object] | None:
        return bargain_assessment_payload(self._app_path, assessment_id=assessment_id)

    def proposal_states(self) -> dict[str, str]:
        """Current proposal state by id, so a report can show what moved after publication."""
        return {
            str(item["proposal_id"]): str(item["status"])
            for item in list_proposal_payloads(self._app_path)
        }

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
            run_revision_id=str(raw["run_revision_id"]),
            rules_ref=None if raw.get("rules_ref") is None else str(raw["rules_ref"]),
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
