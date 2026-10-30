"""固定された記事観測を二つの共有モデル入力とsubject関連へ組み立てる。"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Self

from pydantic import model_validator

from baibai_engine.machine_analysis.observations.trends.contracts import (
    SelectedArtifact,
    TrendInputManifest,
    Value,
)
from baibai_engine.machine_analysis.observations.trends.inputs import (
    FixedTrendInputs,
    IndustryInput,
    IndustryScope,
    TrendCoverage,
)
from baibai_engine.machine_analysis.observations.trends.reader import read_trend_source
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only

SELECTION_REVISION = "trend-parser-selection-v1"
SCOPE_REVISION = "industry-scopes-v1"
WINDOW_DAYS = 180


class IndustryPlan(Value):
    definition: IndustryScope
    title_terms: tuple[str, ...]
    related_tickers: tuple[str, ...] = ()
    membership_basis: str | None = None


class TrendGenerationInputs(Value):
    industries: tuple[IndustryInput, ...]
    social: FixedTrendInputs
    industry_links: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @model_validator(mode="after")
    def unique_scopes(self) -> Self:
        scopes = [item.definition.scope_id for item in self.industries]
        if len(set(scopes)) != len(scopes):
            raise ValueError("duplicate industry scope")
        link_scopes = [scope for scope, _ in self.industry_links]
        if len(set(link_scopes)) != len(link_scopes) or set(link_scopes) - set(scopes):
            raise ValueError("industry links refer to an unknown scope")
        return self


# Business-scope links are based on explicit company profiles, not JPX33 alone.
# Information services has no verified ticker membership: read its sector subject independently.
# https://www.medipal.co.jp/company/profile/
# https://www.alfresa.com/ ; https://www.alfresa.com/ir/stock/info/
DEFAULT_INDUSTRIES = (
    IndustryPlan(
        definition=IndustryScope(
            scope_id="jp-pharmaceutical-wholesale",
            label="医薬品・医療用品卸売業",
            taxonomy="Japan Standard Industrial Classification: pharmaceutical wholesale",
            geography="JP",
        ),
        title_terms=("医薬品",),
        related_tickers=("2784", "7459"),
        membership_basis="group pharmaceutical wholesale businesses; official corporate profiles",
    ),
    IndustryPlan(
        definition=IndustryScope(
            scope_id="jp-information-services",
            label="情報サービス業",
            taxonomy="Japan Standard Industrial Classification: information services",
            geography="JP",
        ),
        title_terms=("情報サービス",),
    ),
)


def _report_baselines(selections: tuple[SelectedArtifact, ...]) -> tuple[SelectedArtifact, ...]:
    """同一scopeの公表主張を明示比較元へ固定。事象同一性や状態上書きではない。"""
    result = []
    for item in selections:
        decisions = [
            c for c in item.current.claims if c.domain == "social" and c.status == "decided"
        ]
        if item.baseline is not None or len(decisions) != 1 or decisions[0].occurred_on is None:
            result.append(item)
            continue
        current = decisions[0]
        candidates = [
            (claim.occurred_on, other.current)
            for other in selections
            for claim in other.current.claims
            if claim.domain == "social"
            and claim.status == "decided"
            and claim.scope == current.scope
            and claim.predicate == current.predicate
            and claim.attribution == current.attribution
            and claim.occurred_on is not None
            and current.occurred_on is not None
            and claim.occurred_on < current.occurred_on
        ]
        if candidates:
            last_date = max(day for day, _ in candidates)
            latest = [artifact for day, artifact in candidates if day == last_date]
            if len(latest) == 1:
                item = SelectedArtifact(current=item.current, baseline=latest[0])
        result.append(item)
    return tuple(result)


def read_trend_inputs(
    path: Path,
    *,
    release_id: str,
    release_sha256: str,
    cutoff: datetime,
    as_of: date,
    industries: tuple[IndustryPlan, ...] = DEFAULT_INDUSTRIES,
) -> TrendGenerationInputs:
    """接続を閉じてから純粋値を返す。失敗/未対応も予定scopeを削除しない。"""
    start = as_of - timedelta(days=WINDOW_DAYS)
    with closing(connect_read_only(path)) as conn:
        conn.execute("BEGIN")
        industry_reads = {
            plan.definition.scope_id: read_trend_source(
                conn,
                sources=("tsr_data",),
                cutoff=cutoff,
                start=start,
                as_of=as_of,
                scope_id=plan.definition.scope_id,
                title_terms=plan.title_terms,
            )
            for plan in industries
        }
        social_selections, social_coverage = read_trend_source(
            conn,
            sources=("boj_news",),
            cutoff=cutoff,
            start=start,
            as_of=as_of,
        )
    social_selections = _report_baselines(social_selections)
    social_coverage = social_coverage.model_copy(
        update={
            "reasons": (
                *social_coverage.reasons,
                "initial_semantic_coverage_boj_only_other_sources_searchable",
            ),
        }
    )

    def fixed(
        selections: tuple[SelectedArtifact, ...], coverage: TrendCoverage
    ) -> FixedTrendInputs:
        return FixedTrendInputs(
            manifest=TrendInputManifest(
                release_id=release_id,
                release_sha256=release_sha256,
                cutoff=cutoff,
                selection_revision=SELECTION_REVISION,
                scope_revision=SCOPE_REVISION,
                window_start=start,
                window_end=as_of,
                selected=tuple(item.current.ref for item in selections),
                baselines=tuple(
                    sorted(
                        {item.baseline.ref for item in selections if item.baseline is not None},
                        key=lambda item: item.artifact_id,
                    )
                ),
            ),
            artifacts=selections,
            coverage=coverage,
        )

    def industry_fixed(definition: IndustryScope) -> FixedTrendInputs:
        candidates, coverage = industry_reads[definition.scope_id]
        selected = tuple(
            item
            for item in candidates
            if not item.current.claims
            or any(claim.scope.scope_id == definition.scope_id for claim in item.current.claims)
        )
        if len(selected) != len(candidates):
            coverage = coverage.model_copy(
                update={
                    "state": "partial",
                    "reasons": (*coverage.reasons, "extracted_claims_outside_planned_scope"),
                }
            )
        return fixed(selected, coverage)

    return TrendGenerationInputs(
        industries=tuple(
            IndustryInput(
                definition=plan.definition,
                inputs=industry_fixed(plan.definition),
            )
            for plan in industries
        ),
        social=fixed(social_selections, social_coverage),
        industry_links=tuple(
            (plan.definition.scope_id, plan.related_tickers) for plan in industries
        ),
    )
