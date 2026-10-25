"""Database-backed application source implementations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from baibai_engine.read_api import (
    IndicatorGranularity,
    PortfolioSnapshot,
    application_db_updated_at,
    close_change_since,
    current_portfolio,
    indicator_fetch_health,
    indicator_latest_observed_at,
    indicator_series,
    investment_proposal_payload,
    latest_disclosure_dates_after,
    latest_unadjusted_closes,
    list_assessment_review_publications,
    list_investment_proposal_payloads,
    list_operation_sessions,
    list_portfolio_outcome_payloads,
    list_stock_assessment_publications,
    list_task_payloads,
    market_master_snapshot,
    next_earnings_dates,
    portfolio_ledger_document,
    preview_current_proposal_terms,
    previous_business_day,
    price_comparison_basis_is_confirmed,
    reviewed_assessment_projection,
    safe_load,
    screening_run_asof_dates,
    screening_run_payload,
    stock_assessment_publication,
    task_store_exists,
)
from baibai_engine.read_api.external_assessment import (
    ExternalAssessment,
    external_assessment,
    observed_tickers,
)
from baibai_engine.read_api.market_context import (
    MasterSnapshot,
    PreparedMarketContext,
    assessment_payload,
    build_market_snapshot,
    build_security_market_context,
    build_security_market_contexts,
    latest_assessment_payload,
    list_assessment_payloads,
    prepare_market_context_inputs,
    reading_snapshot,
)
from baibai_engine.read_api.company_valuation import VALUATION_HISTORY_SESSIONS
from baibai_web.sources.types import (
    IndicatorGroupConfig,
    IndicatorSeriesConfig,
    ResearchRevision,
    ScreeningRunRecord,
    StockAssessmentDetail,
    TaskRecord,
)


class DbLedgerSource:
    def __init__(self, db_path: Path, market_db_path: Path) -> None:
        self._path = db_path.resolve()
        self._market_path = market_db_path.resolve()

    def exists(self) -> bool:
        return portfolio_ledger_document(self._path) is not None

    def snapshot(self) -> PortfolioSnapshot:
        document = portfolio_ledger_document(self._path)
        if document is None:
            raise ValueError("portfolio ledger has not been initialized")
        return current_portfolio(
            document, sqlite_path=self._market_path, now=datetime.now().astimezone()
        )


class DbMarketPriceSource:
    """Read the latest observed close per ticker from the licensed market store."""

    def __init__(self, market_db_path: Path) -> None:
        self._path = market_db_path.resolve()
        self._master_date: date | None = None
        self._master_identities: Mapping[str, Mapping[str, object]] = {}
        self._master_snapshot: MasterSnapshot | None = None
        self._context_inputs: PreparedMarketContext | None = None
        self._security_contexts: dict[tuple[date, str], dict[str, object]] = {}

    def exists(self) -> bool:
        return self._path.is_file()

    def security_identities(self, *, as_of: date) -> Mapping[str, Mapping[str, object]]:
        # One shared snapshot per request/export, rather than a master query per ticker.
        if self._master_date != as_of:
            snapshot = market_master_snapshot(self._path, as_of=as_of)
            self._master_snapshot = snapshot
            self._master_identities = {} if snapshot is None else snapshot.securities
            self._master_date = as_of
        return self._master_identities

    def prepare_market_context(self, *, as_of: date) -> None:
        """このexportだけで市場短期系列/master/benchmarkを共有する。"""
        self.security_identities(as_of=as_of)
        self._context_inputs = prepare_market_context_inputs(
            sqlite_path=self._path, asof_date=as_of, master=self._master_snapshot
        )

    def market_snapshot(self, *, as_of: date) -> dict[str, object] | None:
        if not self.exists():
            return None
        return build_market_snapshot(
            sqlite_path=self._path, asof_date=as_of, prepared=self._prepared_context(as_of)
        )

    def security_market_context(self, ticker: str, *, as_of: date) -> dict[str, object] | None:
        if not self.exists():
            return None
        if (context := self._security_contexts.get((as_of, ticker))) is not None:
            return context
        return {
            "as_of": as_of.isoformat(),
            **build_security_market_context(
                sqlite_path=self._path,
                ticker=ticker,
                asof_date=as_of,
                self_range_window_bars=VALUATION_HISTORY_SESSIONS,
                prepared=self._prepared_context(as_of),
            ),
        }

    def prepare_security_market_contexts(self, tickers: Sequence[str], *, as_of: date) -> None:
        """現在batchの計算済みcontextだけを置換し、長期barsは保持しない。"""
        self._security_contexts = {
            (as_of, ticker): {"as_of": as_of.isoformat(), **context}
            for ticker, context in build_security_market_contexts(
                sqlite_path=self._path,
                tickers=tickers,
                asof_date=as_of,
                self_range_window_bars=VALUATION_HISTORY_SESSIONS,
                prepared=self._prepared_context(as_of),
            ).items()
        }

    def _prepared_context(self, as_of: date) -> PreparedMarketContext | None:
        return (
            self._context_inputs
            if self._context_inputs is not None and self._context_inputs.asof_date == as_of
            else None
        )

    def previous_business_day(self, day: date) -> date | None:
        return previous_business_day(self._path, day)

    def latest_closes(self, tickers: Sequence[str]) -> Mapping[str, tuple[float, date]]:
        return latest_unadjusted_closes(self._path, tickers)

    def price_comparison_basis_is_confirmed(
        self, ticker: str, *, from_date: date, through_date: date
    ) -> bool:
        return price_comparison_basis_is_confirmed(
            self._path, ticker, from_date=from_date, through_date=through_date
        )

    def close_changes_since(self, tickers: Sequence[str], *, since: date) -> Mapping[str, float]:
        return close_change_since(self._path, tickers, since=since)

    def disclosures_after(
        self, tickers: Sequence[str], *, after: date, through_date: date
    ) -> Mapping[str, date]:
        return latest_disclosure_dates_after(
            self._path, tickers, after=after, through_date=through_date
        )

    def next_earnings_dates(self, tickers: Sequence[str], *, as_of: date) -> Mapping[str, date]:
        return next_earnings_dates(self._path, tickers, asof=as_of)


class DbOperationsSource:
    """Read operation and outcome state for the read-only UI."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path.resolve()

    def operations(self) -> list[dict[str, object]]:
        return list_operation_sessions(self._path)

    def outcomes(self) -> list[dict[str, object]]:
        return list_portfolio_outcome_payloads(self._path)


class DbResearchSource:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path.resolve()

    def revisions(self) -> list[ResearchRevision]:
        # A malformed newer pair is a source error. Never expose an older supported
        # assessment by silently dropping that revision from the current index.
        return [self._revision(item) for item in list_stock_assessment_publications(self._path)]

    def assessment_detail(self, assessment_id: str) -> StockAssessmentDetail | None:
        publication = stock_assessment_publication(self._path, assessment_id=assessment_id)
        if publication is None:
            return None
        reviews = list_assessment_review_publications(self._path, assessment_id=assessment_id)
        if len(reviews) != 1:
            raise ValueError("Stock Assessment requires exactly one independent Review")
        projection = reviewed_assessment_projection(self._path, assessment_id=assessment_id)
        return StockAssessmentDetail(
            revision=self._revision(publication, projection=projection),
            document=dict(_mapping(publication["payload"], label="Stock Assessment")),
            review=dict(_mapping(reviews[0]["payload"], label="Assessment Review")),
            core_sha256=str(publication["core_sha256"]),
            projection=projection,
        )

    def proposals(self) -> list[dict[str, object]]:
        return list_investment_proposal_payloads(self._path)

    def proposal(self, proposal_id: str) -> dict[str, object] | None:
        return investment_proposal_payload(self._path, proposal_id=proposal_id)

    def proposal_preview(
        self, proposal_id: str, *, sqlite_path: Path, now: datetime
    ) -> dict[str, object] | None:
        return preview_current_proposal_terms(
            self._path, proposal_id=proposal_id, sqlite_path=sqlite_path, now=now
        )

    def _revision(
        self,
        publication: dict[str, object],
        *,
        projection: dict[str, object] | None = None,
    ) -> ResearchRevision:
        payload = _mapping(publication["payload"], label="Stock Assessment")
        snapshot = _mapping(payload["input_snapshot"], label="Assessment input snapshot")
        current = (
            projection
            if projection is not None
            else reviewed_assessment_projection(
                self._path, assessment_id=str(publication["assessment_id"])
            )
        )
        return ResearchRevision(
            ticker=str(publication["ticker"]),
            company_name=str(snapshot["company_name"]),
            sector=str(snapshot["sector"]),
            summary=str(
                _mapping(payload["findings"], label="Assessment findings")["value_gap_explanation"]
            ),
            as_of=date.fromisoformat(str(publication["as_of"])),
            published_at=datetime.fromisoformat(str(publication["published_at"])),
            assessment_id=str(publication["assessment_id"]),
            stance=str(current["stance"]),
            pmax_original_yen=_optional_float(current.get("pmax_original_yen")),
            review_id=str(current["review_id"]),
            status=str(current["status"]),
            original_price_as_of=(
                None
                if current["original_price_as_of"] is None
                else date.fromisoformat(str(current["original_price_as_of"]))
            ),
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


def load_indicator_panel_config(path: Path) -> tuple[IndicatorGroupConfig, ...]:
    raw = safe_load(path.read_text(encoding="utf-8"))
    config = _DashboardConfig.model_validate(raw)
    identifiers = [item.id for group in config.groups for item in group.series]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("indicator panel series IDs must be unique")
    return tuple(
        IndicatorGroupConfig(
            title=group.title,
            series=tuple(
                IndicatorSeriesConfig(series_id=item.id, label=item.label) for item in group.series
            ),
        )
        for group in config.groups
    )


class DbMarketContextSource:
    def __init__(
        self,
        app_db_path: Path,
        indicators_db_path: Path,
        groups: tuple[IndicatorGroupConfig, ...],
        reading_rules_path: Path,
    ) -> None:
        self._app_db_path = app_db_path.resolve()
        self._indicators_db_path = indicators_db_path.resolve()
        self._reading_rules_path = reading_rules_path
        self.groups = groups

    def reading(self, *, as_of: date) -> dict[str, object] | None:
        reading = reading_snapshot(
            self._indicators_db_path,
            asof=as_of,
            rules_path=self._reading_rules_path,
        )
        if reading is None:
            return None
        return {"as_of": reading["asof"], **{k: v for k, v in reading.items() if k != "asof"}}

    def fetch_health(self) -> list[dict[str, object]]:
        return indicator_fetch_health(self._indicators_db_path)

    def assessment_by_id(self, *, context_id: str, as_of: date) -> dict[str, object]:
        return assessment_payload(self._app_db_path, context_id=context_id)

    def assessment_history(self) -> list[dict[str, object]]:
        return list_assessment_payloads(self._app_db_path)

    def latest_assessment(self, *, as_of: date, known_at: datetime) -> dict[str, object] | None:
        return latest_assessment_payload(self._app_db_path, as_of=as_of, known_at=known_at)

    def series(
        self,
        series_id: str,
        *,
        start: date | None,
        end: date,
        granularity: IndicatorGranularity,
    ) -> dict[str, object] | None:
        return indicator_series(
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
        screening: DbScreeningSource,
        indicators_db_path: Path,
    ) -> None:
        self._app_db_path = app_db_path.resolve()
        self._screening = screening
        self._indicators_db_path = indicators_db_path.resolve()

    def screening_as_of(self) -> date | None:
        run = self._screening.latest_run()
        return None if run is None else run.as_of

    def indicator_as_of(self) -> date | None:
        return indicator_latest_observed_at(self._indicators_db_path)

    def app_db_updated_at(self) -> datetime | None:
        return application_db_updated_at(self._app_db_path)

    def data_updated_at(self) -> datetime | None:
        """Return the newest timestamp recorded by any UI data store."""

        screening_run = self._screening.latest_run()
        screening_updated_at = None if screening_run is None else screening_run.generated_at
        indicator_observed_at = indicator_latest_observed_at(self._indicators_db_path)
        indicator_updated_at = (
            None
            if indicator_observed_at is None
            else datetime.combine(
                indicator_observed_at, datetime.min.time(), tzinfo=ZoneInfo("Asia/Tokyo")
            )
        )
        return max(
            (
                value
                for value in (
                    self.app_db_updated_at(),
                    screening_updated_at,
                    indicator_updated_at,
                )
                if value is not None
            ),
            default=None,
        )


class DbScreeningSource:
    def __init__(self, runs_db_path: Path) -> None:
        self._runs_path = runs_db_path.resolve()

    def latest_run(self) -> ScreeningRunRecord | None:
        raw = screening_run_payload(self._runs_path)
        return None if raw is None else self._parse_run(raw)

    def run(self, run_revision_id: str) -> ScreeningRunRecord | None:
        raw = screening_run_payload(self._runs_path, run_revision_id=run_revision_id)
        return None if raw is None else self._parse_run(raw)

    def previous_run(self) -> ScreeningRunRecord | None:
        """Return the newest run of the greatest as-of before the latest one.

        A delta needs a stated earlier side. Reading it from the retained run dates
        keeps "no predecessor" (a fresh or pruned store) distinct from "nothing
        changed", which the caller reports as an unavailable section.
        """
        dates = screening_run_asof_dates(self._runs_path, limit=2)
        if len(dates) < 2:
            return None
        return self.run_as_of(dates[1])

    def run_as_of(self, as_of: date) -> ScreeningRunRecord | None:
        raw = screening_run_payload(self._runs_path, as_of_date=as_of)
        return None if raw is None else self._parse_run(raw)

    @staticmethod
    def _parse_run(raw: dict[str, object]) -> ScreeningRunRecord:
        analyses = raw["stock_analyses"]
        if not isinstance(analyses, list) or not all(isinstance(item, dict) for item in analyses):
            raise ValueError("screening stock analyses must be an array of objects")
        header = _mapping(raw["payload"], label="run header")
        summary = _mapping(header["candidate_discovery"], label="candidate discovery")
        return ScreeningRunRecord(
            run_id=str(raw["public_run_id"]),
            as_of=date.fromisoformat(str(raw["as_of_date"])),
            generated_at=datetime.fromisoformat(str(raw["run_at"])),
            universe_size=int(str(raw["universe_size"])),
            run_revision_id=str(raw["run_revision_id"]),
            rules_ref=None if raw.get("rules_ref") is None else str(raw["rules_ref"]),
            screening_rules_hash=(
                None
                if raw.get("screening_rules_hash") is None
                else str(raw["screening_rules_hash"])
            ),
            er_model_version=(
                None if raw.get("er_model_version") is None else str(raw["er_model_version"])
            ),
            candidate_discovery=dict(summary),
            rows=tuple(analyses),
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


class DbExternalAssessmentSource:
    def __init__(self, path: Path) -> None:
        self._path = path.resolve()

    def assessment(self, ticker: str, *, cutoff: datetime) -> ExternalAssessment:
        return external_assessment(self._path, ticker=ticker, cutoff=cutoff, evaluated_at=cutoff)

    def observed_tickers(self, *, cutoff: datetime) -> list[str]:
        return observed_tickers(self._path, cutoff=cutoff)
