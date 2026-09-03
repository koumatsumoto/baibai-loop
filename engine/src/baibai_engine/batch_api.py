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

from baibai_engine.appdb.paths import database_path
from baibai_engine.appdb.read import connect_read_only
from baibai_engine.appdb.schema import APPLICATION_SCHEMA_VERSION
from baibai_engine.foundation.repository_layout import (
    APPLICATION_DB_PATH,
    CALIBRATION_DIR,
    MACRO_DB_PATH,
    MARKET_DB_PATH,
    RUNS_DB_PATH,
    StoreLayoutError,
    reject_noncanonical_store_paths,
    repository_root_error,
)
from baibai_engine.macro.context.models import (
    MACRO_CONTEXT_SCHEMA_VERSION,
    MACRO_CONTEXT_STALE_DAYS,
    MacroContextDocument,
    cited_series_ids,
    scorecard_series_ids,
)
from baibai_engine.macro.indicators.cli import parse_refresh_failure_count
from baibai_engine.macro.indicators.db import (
    DEFAULT_DB_PATH as DEFAULT_MACRO_DB_PATH,
)
from baibai_engine.macro.indicators.db import (
    SQLITE_SCHEMA_VERSION as MACRO_SCHEMA_VERSION,
)
from baibai_engine.macro.indicators.db import (
    IndicatorsSchemaError,
)
from baibai_engine.macro.indicators.db import (
    open_connection as open_macro_store,
)
from baibai_engine.macro.indicators.db import (
    validate_current_schema as validate_macro_schema,
)
from baibai_engine.macro.indicators.definitions import IndicatorDefinitions, load_definitions
from baibai_engine.macro.indicators.service import (
    DEFAULT_LATEST_LOOKBACK_DAYS,
    LATEST_FETCH_LOOKBACK_DAYS,
)
from baibai_engine.market.lake.datasets import LAKE_DATASETS
from baibai_engine.market.lake.identity import verified_git_commit as lake_verified_git_commit
from baibai_engine.market.lake.keys import (
    current_l1_pointer_key as lake_current_l1_pointer_key,
)
from baibai_engine.market.lake.keys import dataset_manifest_key as lake_dataset_manifest_key
from baibai_engine.market.lake.keys import release_manifest_key as lake_release_manifest_key
from baibai_engine.market.lake.models import DatasetManifest as LakeDatasetManifest
from baibai_engine.market.lake.models import ReleaseManifest as LakeReleaseManifest
from baibai_engine.market.lake.models import SQLiteSnapshotSourceRef as LakeSQLiteSnapshotSourceRef
from baibai_engine.market.lake.models import (
    canonical_lake_model_bytes,
    load_lake_model_json,
)
from baibai_engine.market.lake.models import validate_release_policy as validate_lake_release_policy
from baibai_engine.market.lake.objects import LocalMirrorSource
from baibai_engine.market.lake.objects import mirror_path as lake_mirror_path
from baibai_engine.market.lake.reader import FixedRelease as LakeFixedRelease
from baibai_engine.market.lake.reader import LakeReadError, resolve_release
from baibai_engine.market.lake.release import L1ReleasePointer
from baibai_engine.market.lake.release import create_l1_release as create_lake_l1_release
from baibai_engine.market.lake.writer import (
    LakeBuildError,
    LakeBuildReport,
    export_lake_legacy,
)
from baibai_engine.market.sqlite import open_connection as open_market_store
from baibai_engine.market.sqlite.lake_origin import (
    LakeStoreOrigin,
    LakeStoreOriginError,
    advance_lake_store_origin,
    read_lake_store_origin,
)
from baibai_engine.market.sqlite.schema import (
    SQLITE_SCHEMA_VERSION as MARKET_SCHEMA_VERSION,
)
from baibai_engine.market.sqlite.schema import (
    SQLiteSchemaError as MarketSchemaError,
)
from baibai_engine.market.sqlite.schema import (
    validate_current_schema as validate_market_schema,
)
from baibai_engine.market.sqlite.snapshot import create_snapshot as create_market_snapshot
from baibai_engine.read_api.macro import latest_macro_context_payload
from baibai_engine.read_api.research_triage import research_triage_payloads_for_review_set
from baibai_engine.screening.discovery.review_set import PublishedReviewSet
from baibai_engine.screening.research_triage import (
    ResearchTriage,
    ResearchTriageCandidateSnapshot,
    ResearchTriageConflictError,
    ResearchTriageEntry,
    ResearchTriageService,
    latest_research_triage_id,
)
from baibai_engine.screening.run_store import ReviewSetPublication, ScreeningRunReader
from baibai_engine.screening.run_store.schema import RUN_STORE_SCHEMA_VERSION


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
    existing_triage: ResearchTriage | None
    macro_context: MacroContextDocument | None


def _validated_review_set(publication: ReviewSetPublication) -> PublishedReviewSet:
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
    app_db_path: Path | None = None,
    runs_db_path: Path | None = None,
) -> DailyAnalysisContext | None:
    """Resolve the latest canonical Review Set for one exact as-of date."""

    publication = ScreeningRunReader(runs_db_path).latest_review_set(as_of_date=as_of.isoformat())
    if publication is None:
        return None
    review_set = _validated_review_set(publication)
    app_path = database_path(app_db_path)
    exact_triages = research_triage_payloads_for_review_set(app_path, review_set.review_set_id)
    existing_triage: ResearchTriage | None = None
    if exact_triages:
        existing_triage = ResearchTriage.model_validate(exact_triages[0])
        if existing_triage.run_revision_id != review_set.run_revision_id:
            raise ValueError("canonical Research Triage run binding differs from the Review Set")
    macro_payload = latest_macro_context_payload(app_path, as_of=review_set.as_of)
    macro_context = (
        None if macro_payload is None else MacroContextDocument.model_validate(macro_payload)
    )
    return DailyAnalysisContext(
        review_set=review_set,
        existing_triage=existing_triage,
        macro_context=macro_context,
    )


def publish_daily_research_triage(
    review_set_id: str,
    decisions: Sequence[DailyTriageDecision | Mapping[str, object]],
    *,
    macro_context_id: str | None,
    app_db_path: Path | None = None,
    runs_db_path: Path | None = None,
    published_at: datetime,
) -> ResearchTriage:
    """Build a canonical Triage from strict judgments and delegate every invariant."""

    publication = ScreeningRunReader(runs_db_path).get_review_set(review_set_id)
    if publication is None:
        raise ValueError(f"source Review Set is unavailable: {review_set_id}")
    review_set = _validated_review_set(publication)
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
        macro_context_id=macro_context_id,
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
                candidate_snapshot=ResearchTriageCandidateSnapshot(
                    name=source.name,
                    sector_33=source.sector_33,
                    nominations=source.nominations,
                    analysis=source.analysis,
                ),
            )
        )
    identifier_digest = sha256(review_set.review_set_id.encode()).hexdigest()[:16]
    triage = ResearchTriage(
        schema_version=3,
        kind="research_triage",
        research_triage_id=(
            f"research-triage-{review_set.as_of.strftime('%Y%m%d')}-{identifier_digest}"
        ),
        review_set_id=review_set.review_set_id,
        run_revision_id=review_set.run_revision_id,
        as_of=review_set.as_of,
        published_at=published_at,
        macro_context_id=macro_context_id,
        expected_prior_research_triage_id=latest_research_triage_id(app_db_path),
        screening_rules_hash=review_set.screening_rules_hash,
        candidate_discovery_method=review_set.method,
        triage_contract_id="research-triage-v3",
        entries=tuple(entries),
    )
    try:
        return ResearchTriageService(app_db_path).publish(triage, review_set=review_set)
    except (OSError, ResearchTriageConflictError, sqlite3.Error):
        # A commit response can be ambiguous. Reconcile only this exact Review Set once;
        # every unrelated head/binding conflict still fails closed.
        reconciled = _matching_daily_triage(
            app_path,
            review_set,
            requested_decisions,
            macro_context_id=macro_context_id,
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
    macro_context_id: str | None,
) -> ResearchTriage | None:
    payloads = research_triage_payloads_for_review_set(app_path, review_set.review_set_id)
    if not payloads:
        return None
    existing = ResearchTriage.model_validate(payloads[0])
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
        and existing.macro_context_id == macro_context_id
        and existing_decisions == requested_decisions
    ):
        return existing
    raise ResearchTriageConflictError(
        "canonical Research Triage already differs for the exact Review Set"
    )


__all__ = [
    "APPLICATION_DB_PATH",
    "APPLICATION_SCHEMA_VERSION",
    "CALIBRATION_DIR",
    "DEFAULT_LATEST_LOOKBACK_DAYS",
    "DEFAULT_MACRO_DB_PATH",
    "LAKE_DATASETS",
    "LATEST_FETCH_LOOKBACK_DAYS",
    "MACRO_CONTEXT_SCHEMA_VERSION",
    "MACRO_CONTEXT_STALE_DAYS",
    "MACRO_DB_PATH",
    "MACRO_SCHEMA_VERSION",
    "MARKET_DB_PATH",
    "MARKET_SCHEMA_VERSION",
    "RUNS_DB_PATH",
    "RUN_STORE_SCHEMA_VERSION",
    "DailyAnalysisContext",
    "DailyTriageDecision",
    "IndicatorDefinitions",
    "IndicatorsSchemaError",
    "L1ReleasePointer",
    "LakeBuildError",
    "LakeBuildReport",
    "LakeDatasetManifest",
    "LakeFixedRelease",
    "LakeReadError",
    "LakeReleaseManifest",
    "LakeSQLiteSnapshotSourceRef",
    "LakeStoreOrigin",
    "LakeStoreOriginError",
    "LocalMirrorSource",
    "MacroContextDocument",
    "MarketSchemaError",
    "ResearchTriageCandidateSnapshot",
    "StoreLayoutError",
    "advance_lake_store_origin",
    "canonical_lake_model_bytes",
    "cited_series_ids",
    "connect_read_only",
    "create_lake_l1_release",
    "create_market_snapshot",
    "database_path",
    "export_lake_legacy",
    "lake_current_l1_pointer_key",
    "lake_dataset_manifest_key",
    "lake_mirror_path",
    "lake_release_manifest_key",
    "lake_verified_git_commit",
    "load_daily_analysis_context",
    "load_definitions",
    "load_lake_model_json",
    "open_macro_store",
    "open_market_store",
    "parse_refresh_failure_count",
    "publish_daily_research_triage",
    "read_lake_store_origin",
    "reject_noncanonical_store_paths",
    "repository_root_error",
    "resolve_release",
    "scorecard_series_ids",
    "validate_lake_release_policy",
    "validate_macro_schema",
    "validate_market_schema",
]
