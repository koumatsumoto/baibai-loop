"""seal内の有限読取。本文再取得も生成中のlazy SQLも行わない。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

from baibai_engine.machine_analysis.observations.articles.contracts import ArticleObservation
from baibai_engine.machine_analysis.observations.articles.store import (
    read_artifacts,
    read_observations,
)
from baibai_engine.machine_analysis.observations.trends.contracts import (
    ExtractionArtifact,
    SelectedArtifact,
    TrendInputManifest,
    digest,
)
from baibai_engine.machine_analysis.observations.trends.extraction import (
    decode_artifact,
    matching_artifacts,
)
from baibai_engine.machine_analysis.observations.trends.inputs import (
    FixedTrendInputs,
    IndustryInput,
    IndustryScope,
    TrendCoverage,
    TrendGenerationInputs,
)
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only

SELECTION_REVISION = "trend-parser-selection-v1"
SCOPE_REVISION = "industry-scopes-v1"
# Business-scope links, not an assertion of identical businesses or direct peers.
# Unknown memberships stay unlinked; JPX33 alone is never sufficient.
# https://www.medipal.co.jp/company/profile/
# https://www.alfresa.com/ ; https://www.alfresa.com/ir/stock/info/
DEFAULT_INDUSTRIES = (
    IndustryScope(
        scope_id="jp-pharmaceutical-wholesale",
        label="医薬品・医療用品卸売業",
        taxonomy="Japan Standard Industrial Classification: pharmaceutical wholesale",
        geography="JP",
        related_tickers=("2784", "7459"),
        membership_basis="group pharmaceutical wholesale businesses; official corporate profiles",
    ),
    IndustryScope(
        scope_id="jp-information-services",
        label="情報サービス業",
        taxonomy="Japan Standard Industrial Classification: information services",
        geography="JP",
    ),
)
MAX_ARTICLES = 16
WINDOW_DAYS = 180


def _choose(
    conn: sqlite3.Connection,
    observation: ArticleObservation,
    *,
    cutoff: datetime,
) -> tuple[ExtractionArtifact | None, str | None]:
    candidates = matching_artifacts(conn, observation, cutoff=cutoff)
    if not candidates:
        return None, "extraction_pending"
    # Ignore generation clocks when comparing semantic results. Different results
    # under the same method remain unresolved; receive order cannot adjudicate.
    meanings = {
        digest(
            {
                "status": item.status,
                "payload": {key: value for key, value in item.payload.items() if key != "evidence"},
            }
        )
        for item in candidates
    }
    if len(meanings) != 1:
        return None, "competing_extraction_artifacts"
    chosen = min(candidates, key=lambda item: item.artifact_id)
    original = read_observations(conn, (chosen.observation_id,))[0]
    return decode_artifact(chosen, original), None


def _read_source(
    conn: sqlite3.Connection,
    *,
    sources: tuple[str, ...],
    cutoff: datetime,
    start: date,
    title_terms: tuple[str, ...] = (),
) -> tuple[tuple[SelectedArtifact, ...], TrendCoverage]:
    source_placeholders = ",".join("?" for _ in sources)
    receipt_rows = conn.execute(
        "SELECT source, json_extract(receipt_json,'$.status'), count(*) "
        f"FROM article_source_receipts WHERE source IN ({source_placeholders}) "  # nosec B608
        "AND received_on>=? AND julianday(json_extract(receipt_json,'$.received_at'))<=julianday(?) "
        "GROUP BY source, json_extract(receipt_json,'$.status') ORDER BY 1,2",
        (*sources, start.isoformat(), cutoff.isoformat()),
    ).fetchall()
    latest = conn.execute(
        "SELECT json_extract(receipt_json,'$.received_at') FROM article_source_receipts "
        f"WHERE source IN ({source_placeholders}) "  # nosec B608
        "AND received_on>=? AND julianday(json_extract(receipt_json,'$.received_at'))<=julianday(?) "
        "ORDER BY julianday(json_extract(receipt_json,'$.received_at')) DESC LIMIT 1",
        (*sources, start.isoformat(), cutoff.isoformat()),
    ).fetchone()
    title_filter = ""
    if title_terms:
        title_filter = (
            " AND ("
            + " OR ".join("json_extract(o.observation_json,'$.title') LIKE ?" for _ in title_terms)
            + ")"
        )
    # Latest native representation per article, keeping all distinct article IDs.
    # Late replay preserves the receipt's original clock; it cannot become latest.
    rows = conn.execute(
        "WITH ranked AS (SELECT o.observation_id, o.article_id, o.received_on, "
        "row_number() OVER (PARTITION BY o.article_id ORDER BY "
        "julianday(json_extract(r.receipt_json,'$.received_at')) DESC, o.observation_id) AS rank "
        "FROM article_observations o JOIN article_source_receipts r ON r.receipt_id=o.receipt_id "
        f"WHERE r.source IN ({source_placeholders}) "  # nosec B608: placeholders only
        "AND julianday(json_extract(r.receipt_json,'$.received_at'))<=julianday(?) "
        "AND o.received_on>=?" + title_filter + ") SELECT observation_id FROM ranked WHERE rank=1 "
        "ORDER BY received_on DESC, article_id LIMIT ?",
        (
            *sources,
            cutoff.isoformat(),
            start.isoformat(),
            *(f"%{term}%" for term in title_terms),
            MAX_ARTICLES + 1,
        ),
    ).fetchall()
    selections = []
    reasons: list[str] = []
    for observation in read_observations(conn, [row[0] for row in rows[:MAX_ARTICLES]]):
        current, reason = _choose(conn, observation, cutoff=cutoff)
        if reason is not None:
            reasons.append(f"{reason}:{observation.observation_id}")
            continue
        if current is None:
            raise ValueError("selected extraction unexpectedly absent")
        # Read an explicit previous representation of this article, not inferred
        # event identity. Distinct BOJ decisions can coexist without auto-merging.
        earlier = conn.execute(
            "SELECT a.artifact_id FROM article_extraction_artifacts a "
            "JOIN article_observations o ON o.observation_id=a.observation_id "
            "JOIN article_source_receipts r ON r.receipt_id=o.receipt_id "
            "WHERE o.article_id=? "
            "AND json_extract(a.artifact_json,'$.payload.schema')='trend-claims-v1' "
            "AND json_extract(a.artifact_json,'$.payload.article_version_id')<>? "
            "AND julianday(json_extract(r.receipt_json,'$.received_at'))"
            "<julianday(?) AND julianday(json_extract(a.artifact_json,'$.generated_at'))<=julianday(?) "
            "ORDER BY julianday(json_extract(r.receipt_json,'$.received_at')) DESC, a.artifact_id LIMIT 1",
            (
                observation.article_id,
                observation.article_version_id,
                current.evidence.received_at.isoformat(),
                cutoff.isoformat(),
            ),
        ).fetchone()
        baseline = None
        if earlier:
            saved_baseline = read_artifacts(conn, [earlier[0]])[0]
            baseline = decode_artifact(
                saved_baseline,
                read_observations(conn, (saved_baseline.observation_id,))[0],
            )
        selections.append(SelectedArtifact(current=current, baseline=baseline))
    selections.sort(key=lambda item: item.current.ref.artifact_id)
    omitted = max(0, len(rows) - MAX_ARTICLES)
    if omitted:
        reasons.append("article_limit_reached_count_is_lower_bound")
    state = "available"
    if not rows:
        state = "partial" if receipt_rows else "not_collected"
        reasons.append("no_matching_family_articles_in_receipt_window")
    elif reasons:
        state = "partial" if selections else "extraction_pending"
    elif any(item.current.status in ("failed", "pending", "unsupported") for item in selections):
        state = "partial"
    return tuple(selections), TrendCoverage.model_validate(
        {
            "state": state,
            "reasons": reasons,
            "omitted_artifacts": omitted,
            "continuation": "articles.list:same_manifest:after=null" if omitted else None,
            "source_states": tuple(
                f"{source}:{status}:receipts={count}" for source, status, count in receipt_rows
            ),
            "latest_receipt_at": None if latest is None else latest[0],
        }
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
    industries: tuple[IndustryScope, ...] = DEFAULT_INDUSTRIES,
) -> TrendGenerationInputs:
    """接続を閉じてから純粋値を返す。失敗/未対応も予定scopeを削除しない。"""
    start = as_of - timedelta(days=WINDOW_DAYS)
    with closing(connect_read_only(path)) as conn:
        conn.execute("BEGIN")
        industry_selections, industry_coverage = _read_source(
            conn,
            sources=("tsr_data",),
            cutoff=cutoff,
            start=start,
            title_terms=("医薬品", "情報サービス"),
        )
        social_selections, social_coverage = _read_source(
            conn,
            sources=("boj_news",),
            cutoff=cutoff,
            start=start,
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

    return TrendGenerationInputs(
        industries=tuple(
            IndustryInput(
                definition=definition,
                inputs=fixed(
                    tuple(
                        item
                        for item in industry_selections
                        if not item.current.claims
                        or any(
                            claim.scope.scope_id == definition.scope_id
                            for claim in item.current.claims
                        )
                    ),
                    industry_coverage,
                ),
            )
            for definition in industries
        ),
        social=fixed(social_selections, social_coverage),
    )
