"""Supply engine-owned facts and writes that let batch produce and stop loop outputs."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from baibai_engine.external_assessment.acquisition.cli import (
    main as external_assessment_acquisition_main,
)
from baibai_engine.external_assessment.acquisition.collection import (
    PARSE_FAILURE_REASONS as external_assessment_parse_failure_reasons,
)
from baibai_engine.external_assessment.acquisition.collection import (
    CollectionOutcome as ExternalAssessmentCollectionOutcome,
)
from baibai_engine.external_assessment.acquisition.collection import (
    CollectionResult as ExternalAssessmentCollectionResult,
)
from baibai_engine.external_assessment.acquisition.collection import (
    HostRateLimiter as ExternalAssessmentHostRateLimiter,
)
from baibai_engine.external_assessment.acquisition.collection import (
    ReceivedHTML as ExternalAssessmentReceivedHTML,
)
from baibai_engine.external_assessment.acquisition.collection import (
    Source as ExternalAssessmentSource,
)
from baibai_engine.external_assessment.acquisition.collection import (
    collect_pair as external_assessment_collect_pair,
)
from baibai_engine.external_assessment.acquisition.collection import (
    collector_binding as external_assessment_collector_binding,
)
from baibai_engine.external_assessment.acquisition.collection import (
    load_staging as external_assessment_load_staging,
)
from baibai_engine.external_assessment.acquisition.collection import (
    save_staging as external_assessment_save_staging,
)
from baibai_engine.external_assessment.acquisition.universe import (
    calendar_master_asof as external_assessment_calendar_master_asof,
)
from baibai_engine.external_assessment.acquisition.universe import (
    frozen_lake_universe as external_assessment_frozen_lake_universe,
)
from baibai_engine.external_assessment.acquisition.universe import (
    frozen_universe as external_assessment_frozen_universe,
)
from baibai_engine.external_assessment.acquisition.verification import (
    history_digests as external_assessment_history_digests,
)
from baibai_engine.external_assessment.acquisition.verification import (
    verify_observations as external_assessment_verify_observations,
)
from baibai_engine.external_assessment.acquisition.verification import (
    verify_release_history as external_assessment_verify_release_history,
)
from baibai_engine.external_assessment.observations.identity import (
    ANALYST_RATING_DATASET as EXTERNAL_ASSESSMENT_ANALYST_RATING_DATASET,
)
from baibai_engine.external_assessment.observations.identity import (
    MODEL_VALUATION_DATASET as EXTERNAL_ASSESSMENT_MODEL_VALUATION_DATASET,
)
from baibai_engine.external_assessment.observations.identity import (
    QUANTS_SCORE_DATASET as EXTERNAL_ASSESSMENT_QUANTS_SCORE_DATASET,
)
from baibai_engine.external_assessment.observations.models import (
    AnalystRatingObservation as ExternalAssessmentAnalystRatingObservation,
)
from baibai_engine.external_assessment.observations.models import (
    ModelValuationObservation as ExternalAssessmentModelValuationObservation,
)
from baibai_engine.external_assessment.observations.models import (
    Observation as ExternalAssessmentObservation,
)
from baibai_engine.external_assessment.observations.models import (
    QuantsScoreObservation as ExternalAssessmentQuantsScoreObservation,
)
from baibai_engine.external_assessment.store import (
    save_observations as external_assessment_save_observations,
)
from baibai_engine.external_assessment.tradingview.accepted import (
    accept_observations as tradingview_accept_observations,
)
from baibai_engine.external_assessment.tradingview.accepted import (
    load_observations as tradingview_load_observations,
)
from baibai_engine.external_assessment.tradingview.accepted import (
    verify_observations as tradingview_verify_observations,
)
from baibai_engine.external_assessment.tradingview.collector import (
    preflight_snapshot as tradingview_preflight,
)
from baibai_engine.external_assessment.tradingview.collector import (
    validate_time as tradingview_validate_time,
)
from baibai_engine.external_assessment.tradingview.diagnostics import (
    failure_line as tradingview_failure_line,
)
from baibai_engine.external_assessment.tradingview.diagnostics import (
    safe_progress as tradingview_safe_progress,
)
from baibai_engine.foundation.repository_layout import (
    APPLICATION_DB_PATH,
    CALIBRATION_DIR,
    INDICATORS_DB_PATH,
    MARKET_DB_PATH,
    RUNS_DB_PATH,
    StoreLayoutError,
    reject_noncanonical_store_paths,
    repository_root_error,
)
from baibai_engine.market_context.assessment.models import (
    ASSESSMENT_SCHEMA_VERSION,
    ASSESSMENT_STALE_DAYS,
    StoredAssessment,
    require_aware,
)
from baibai_engine.market_context.assessment.projection import (
    assessment_projection,
    assessment_series_ids,
    legacy_context_projection,
)
from baibai_engine.market_context.assessment.query import latest_assessment_payload
from baibai_engine.market_context.indicators.cli import parse_refresh_failure_count
from baibai_engine.market_context.indicators.db import (
    DEFAULT_DB_PATH as DEFAULT_INDICATORS_DB_PATH,
)
from baibai_engine.market_context.indicators.db import (
    SQLITE_SCHEMA_VERSION as MACRO_SCHEMA_VERSION,
)
from baibai_engine.market_context.indicators.db import (
    IndicatorsSchemaError,
)
from baibai_engine.market_context.indicators.db import (
    open_connection as open_indicator_store,
)
from baibai_engine.market_context.indicators.db import (
    validate_current_schema as validate_indicator_schema,
)
from baibai_engine.market_context.indicators.definitions import (
    IndicatorDefinitions,
    load_definitions,
)
from baibai_engine.market_context.indicators.service import (
    DEFAULT_LATEST_LOOKBACK_DAYS,
    LATEST_FETCH_LOOKBACK_DAYS,
)
from baibai_engine.research.triage.contracts import (
    ResearchTriage,
    ResearchTriageEntry,
    ReviewSetEntrySnapshot,
    StoredResearchTriage,
)
from baibai_engine.research.triage.query import (
    latest_research_triage_id,
    research_triage_payloads_for_review_set,
)
from baibai_engine.research.triage.service import ResearchTriageConflictError, ResearchTriageService
from baibai_engine.screening.discovery.contracts import PublishedReviewSet
from baibai_engine.screening.run_store import ReviewSetPublication, ScreeningRunReader
from baibai_engine.screening.run_store.schema import RUN_STORE_SCHEMA_VERSION
from baibai_engine.storage.application.paths import database_path
from baibai_engine.storage.application.read import connect_read_only
from baibai_engine.storage.application.schema import APPLICATION_SCHEMA_VERSION
from baibai_engine.storage.lake.datasets import LAKE_DATASETS
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.hydrate import hydrate_market_store
from baibai_engine.storage.lake.identity import verified_git_commit as lake_verified_git_commit
from baibai_engine.storage.lake.keys import current_l1_pointer_key as lake_current_l1_pointer_key
from baibai_engine.storage.lake.keys import dataset_manifest_key as lake_dataset_manifest_key
from baibai_engine.storage.lake.keys import release_manifest_key as lake_release_manifest_key
from baibai_engine.storage.lake.models import DatasetManifest as LakeDatasetManifest
from baibai_engine.storage.lake.models import ReleaseManifest as LakeReleaseManifest
from baibai_engine.storage.lake.models import SQLiteSnapshotSourceRef as LakeSQLiteSnapshotSourceRef
from baibai_engine.storage.lake.models import canonical_lake_model_bytes, load_lake_model_json
from baibai_engine.storage.lake.models import (
    validate_release_policy as validate_lake_release_policy,
)
from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.lake.objects import mirror_path as lake_mirror_path
from baibai_engine.storage.lake.reader import FixedRelease as LakeFixedRelease
from baibai_engine.storage.lake.reader import LakeReadError, resolve_release
from baibai_engine.storage.lake.release import L1ReleasePointer
from baibai_engine.storage.lake.release import create_l1_release as create_lake_l1_release
from baibai_engine.storage.lake.writer import LakeBuildError, LakeBuildReport, export_lake_legacy
from baibai_engine.storage.observations.lake_origin import (
    LakeStoreOrigin,
    LakeStoreOriginError,
    advance_lake_store_origin,
    read_lake_store_origin,
)
from baibai_engine.storage.observations.schema import SQLITE_SCHEMA_VERSION as MARKET_SCHEMA_VERSION
from baibai_engine.storage.observations.schema import SQLiteSchemaError as MarketSchemaError
from baibai_engine.storage.observations.schema import open_connection as open_market_store
from baibai_engine.storage.observations.schema import (
    validate_current_schema as validate_market_schema,
)
from baibai_engine.storage.observations.snapshot import create_snapshot as create_market_snapshot


class DailyTriageDecision(BaseModel):
    """Strict judgment-only boundary between the local model and engine writer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ticker: str = Field(pattern=r"^[0-9A-Z]{4}$")
    verdict: Literal["research", "skip"]
    priority: int | None = Field(ge=1)
    rationale: str = Field(min_length=1, max_length=1200)
    research_question: str | None = Field(max_length=600)
    key_risk: str | None = Field(max_length=600)

    @model_validator(mode="after")
    def _decision_shape(self) -> DailyTriageDecision:
        if self.verdict == "research" and (
            self.priority is None or not self.research_question or not self.key_risk
        ):
            raise ValueError("research requires priority, research_question, and key_risk")
        if self.verdict == "skip" and (
            self.priority is not None
            or self.research_question is not None
            or self.key_risk is not None
        ):
            raise ValueError("skip forbids priority, research_question, and key_risk")
        return self


@dataclass(frozen=True, slots=True)
class DailyAnalysisContext:
    """Current machine and canonical inputs resolved before a model can run."""

    review_set: PublishedReviewSet
    existing_triage: ResearchTriage | StoredResearchTriage | None
    market_context_assessment: StoredAssessment | None
    known_at: datetime


def validated_review_set(publication: ReviewSetPublication) -> PublishedReviewSet:
    review_set = PublishedReviewSet.model_validate(publication.payload)
    if review_set.review_set_id != publication.review_set_id:
        raise ValueError("Review Set reader returned a different identity")
    if review_set.run_revision_id != publication.run_revision_id:
        raise ValueError("Review Set reader returned a different run binding")
    if review_set.as_of.isoformat() != publication.as_of_date:
        raise ValueError("Review Set reader returned a different as-of binding")
    if review_set.created_at != datetime.fromisoformat(publication.created_at):
        raise ValueError("Review Set reader returned a different publication time")
    return review_set


def load_daily_analysis_context(
    as_of: date,
    *,
    known_at: datetime,
    app_db_path: Path | None = None,
    runs_db_path: Path | None = None,
) -> DailyAnalysisContext | None:
    """Resolve the latest canonical Review Set for one exact as-of date."""

    publication = ScreeningRunReader(runs_db_path).latest_review_set(as_of_date=as_of.isoformat())
    if publication is None:
        return None
    return _analysis_context(publication, app_db_path=app_db_path, known_at=known_at)


def load_latest_analysis_context(
    *,
    known_at: datetime,
    app_db_path: Path | None = None,
    runs_db_path: Path | None = None,
) -> DailyAnalysisContext:
    """Resolve the canonical latest publication only when the latest run is complete."""

    reader = ScreeningRunReader(runs_db_path)
    publication = reader.resolve_review_set()
    if publication is None:
        raise ValueError("latest Review Set is unavailable")
    latest_run = reader.latest_run()
    if latest_run is None:
        raise ValueError("latest Screening Run is unavailable")
    if publication.run_revision_id != latest_run.run_revision_id:
        raise ValueError("latest Review Set does not match the latest Screening Run revision")
    return _analysis_context(publication, app_db_path=app_db_path, known_at=known_at)


def _analysis_context(
    publication: ReviewSetPublication, *, app_db_path: Path | None, known_at: datetime
) -> DailyAnalysisContext:
    review_set = validated_review_set(publication)
    app_path = database_path(app_db_path)
    exact_triages = research_triage_payloads_for_review_set(app_path, review_set.review_set_id)
    existing_triage: ResearchTriage | StoredResearchTriage | None = None
    if exact_triages:
        existing_triage = StoredResearchTriage.from_payload(exact_triages[0])
        if existing_triage.run_revision_id != review_set.run_revision_id:
            raise ValueError("canonical Research Triage run binding differs from the Review Set")
    macro_payload = latest_assessment_payload(app_path, as_of=review_set.as_of, known_at=known_at)
    market_context_assessment = (
        None if macro_payload is None else StoredAssessment.from_payload(macro_payload)
    )
    return DailyAnalysisContext(
        review_set=review_set,
        existing_triage=existing_triage,
        market_context_assessment=market_context_assessment,
        known_at=known_at,
    )


def publish_daily_research_triage(
    review_set_id: str,
    decisions: Sequence[DailyTriageDecision | Mapping[str, object]],
    *,
    market_context_assessment_id: str | None,
    app_db_path: Path | None = None,
    runs_db_path: Path | None = None,
    published_at: datetime,
    known_at: datetime | None = None,
) -> StoredResearchTriage:
    """Build a canonical Triage from strict judgments and delegate every invariant."""

    publication = ScreeningRunReader(runs_db_path).get_review_set(review_set_id)
    if publication is None:
        raise ValueError(f"source Review Set is unavailable: {review_set_id}")
    review_set = validated_review_set(publication)
    if review_set.review_set_id != review_set_id:
        raise ValueError("Review Set reader returned a different identity")
    validated = tuple(
        item if isinstance(item, DailyTriageDecision) else DailyTriageDecision.model_validate(item)
        for item in decisions
    )
    by_ticker = {item.ticker: item for item in validated}
    expected_tickers = [entry.ticker for entry in review_set.entries]
    if len(by_ticker) != len(validated) or set(by_ticker) != set(expected_tickers):
        raise ValueError("AI decisions must contain every Review Set ticker exactly once")
    app_path = database_path(app_db_path)
    requested_decisions = {
        item.ticker: (
            item.verdict,
            item.priority,
            item.rationale,
            item.research_question,
            item.key_risk,
        )
        for item in validated
    }
    existing = _matching_daily_triage(
        app_path,
        review_set,
        requested_decisions,
        market_context_assessment_id=market_context_assessment_id,
    )
    if existing is not None:
        return existing
    entries: list[ResearchTriageEntry] = []
    for source in review_set.entries:
        decision = by_ticker[source.ticker]
        entries.append(
            ResearchTriageEntry(
                ticker=source.ticker,
                decision=decision.verdict,
                priority=decision.priority,
                rationale=decision.rationale,
                research_question=decision.research_question,
                key_risk=decision.key_risk,
                candidate_snapshot=ReviewSetEntrySnapshot(
                    name=source.name,
                    sector_33=source.sector_33,
                    nominations=source.nominations,
                    analysis=source.analysis,
                ),
            )
        )
    identifier_digest = sha256(review_set.review_set_id.encode()).hexdigest()[:16]
    triage = ResearchTriage(
        schema_version=4,
        kind="research_triage",
        research_triage_id=(
            f"research-triage-{review_set.as_of.strftime('%Y%m%d')}-{identifier_digest}"
        ),
        review_set_id=review_set.review_set_id,
        run_revision_id=review_set.run_revision_id,
        as_of=review_set.as_of,
        published_at=published_at,
        market_context_assessment_id=market_context_assessment_id,
        expected_prior_research_triage_id=latest_research_triage_id(app_db_path),
        screening_rules_hash=review_set.screening_rules_hash,
        candidate_discovery_method=review_set.method,
        triage_contract_id="research-triage-v4",
        entries=tuple(entries),
    )
    try:
        published = ResearchTriageService(app_db_path).publish(
            triage, review_set=review_set, known_at=known_at
        )
        return StoredResearchTriage.from_payload(published.model_dump(mode="json"))
    except (OSError, ResearchTriageConflictError, sqlite3.Error):
        # A commit response can be ambiguous. Reconcile only this exact Review Set once;
        # every unrelated head/binding conflict still fails closed.
        reconciled = _matching_daily_triage(
            app_path,
            review_set,
            requested_decisions,
            market_context_assessment_id=market_context_assessment_id,
        )
        if reconciled is not None:
            return reconciled
        raise


def _matching_daily_triage(
    app_path: Path,
    review_set: PublishedReviewSet,
    requested_decisions: Mapping[
        str, tuple[Literal["research", "skip"], int | None, str, str | None, str | None]
    ],
    *,
    market_context_assessment_id: str | None,
) -> StoredResearchTriage | None:
    payloads = research_triage_payloads_for_review_set(app_path, review_set.review_set_id)
    if not payloads:
        return None
    existing = StoredResearchTriage.from_payload(payloads[0])
    existing_decisions = {
        entry.ticker: (
            entry.decision,
            entry.priority,
            entry.rationale,
            entry.research_question,
            entry.key_risk,
        )
        for entry in existing.entries
    }
    if (
        existing.run_revision_id == review_set.run_revision_id
        and existing.market_context_assessment_id == market_context_assessment_id
        and existing_decisions == requested_decisions
    ):
        return existing
    raise ResearchTriageConflictError(
        "canonical Research Triage already differs for the exact Review Set"
    )


__all__ = [
    "APPLICATION_DB_PATH",
    "APPLICATION_SCHEMA_VERSION",
    "ASSESSMENT_SCHEMA_VERSION",
    "ASSESSMENT_STALE_DAYS",
    "CALIBRATION_DIR",
    "DEFAULT_INDICATORS_DB_PATH",
    "DEFAULT_LATEST_LOOKBACK_DAYS",
    "EXTERNAL_ASSESSMENT_ANALYST_RATING_DATASET",
    "EXTERNAL_ASSESSMENT_MODEL_VALUATION_DATASET",
    "EXTERNAL_ASSESSMENT_QUANTS_SCORE_DATASET",
    "INDICATORS_DB_PATH",
    "LAKE_DATASETS",
    "LATEST_FETCH_LOOKBACK_DAYS",
    "MACRO_SCHEMA_VERSION",
    "MARKET_DB_PATH",
    "MARKET_SCHEMA_VERSION",
    "RUNS_DB_PATH",
    "RUN_STORE_SCHEMA_VERSION",
    "DailyAnalysisContext",
    "DailyTriageDecision",
    "ExternalAssessmentAnalystRatingObservation",
    "ExternalAssessmentCollectionOutcome",
    "ExternalAssessmentCollectionResult",
    "ExternalAssessmentHostRateLimiter",
    "ExternalAssessmentModelValuationObservation",
    "ExternalAssessmentObservation",
    "ExternalAssessmentQuantsScoreObservation",
    "ExternalAssessmentReceivedHTML",
    "ExternalAssessmentSource",
    "IndicatorDefinitions",
    "IndicatorsSchemaError",
    "L1ReleasePointer",
    "LakeBuildError",
    "LakeBuildReport",
    "LakeDatasetManifest",
    "LakeFixedRelease",
    "LakeObjectCache",
    "LakeReadError",
    "LakeReleaseManifest",
    "LakeSQLiteSnapshotSourceRef",
    "LakeStoreOrigin",
    "LakeStoreOriginError",
    "LocalMirrorSource",
    "MarketSchemaError",
    "PublishedReviewSet",
    "ResearchTriage",
    "ReviewSetEntrySnapshot",
    "StoreLayoutError",
    "StoredAssessment",
    "StoredResearchTriage",
    "advance_lake_store_origin",
    "assessment_projection",
    "assessment_series_ids",
    "canonical_lake_model_bytes",
    "connect_read_only",
    "create_lake_l1_release",
    "create_market_snapshot",
    "database_path",
    "export_lake_legacy",
    "external_assessment_acquisition_main",
    "external_assessment_calendar_master_asof",
    "external_assessment_collect_pair",
    "external_assessment_collector_binding",
    "external_assessment_frozen_lake_universe",
    "external_assessment_frozen_universe",
    "external_assessment_history_digests",
    "external_assessment_load_staging",
    "external_assessment_parse_failure_reasons",
    "external_assessment_save_observations",
    "external_assessment_save_staging",
    "external_assessment_verify_observations",
    "external_assessment_verify_release_history",
    "hydrate_market_store",
    "lake_current_l1_pointer_key",
    "lake_dataset_manifest_key",
    "lake_mirror_path",
    "lake_release_manifest_key",
    "lake_session",
    "lake_verified_git_commit",
    "legacy_context_projection",
    "load_daily_analysis_context",
    "load_definitions",
    "load_lake_model_json",
    "load_latest_analysis_context",
    "open_indicator_store",
    "open_market_store",
    "parse_refresh_failure_count",
    "publish_daily_research_triage",
    "read_lake_store_origin",
    "reject_noncanonical_store_paths",
    "repository_root_error",
    "require_aware",
    "resolve_release",
    "tradingview_accept_observations",
    "tradingview_failure_line",
    "tradingview_load_observations",
    "tradingview_preflight",
    "tradingview_safe_progress",
    "tradingview_validate_time",
    "tradingview_verify_observations",
    "validate_indicator_schema",
    "validate_lake_release_policy",
    "validate_market_schema",
    "validated_review_set",
]
