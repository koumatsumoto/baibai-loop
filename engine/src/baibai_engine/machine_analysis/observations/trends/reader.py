"""seal内の有限読取。本文再取得も生成中のlazy SQLも行わない。"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from baibai_engine.machine_analysis.observations.articles.contracts import ArticleObservation
from baibai_engine.machine_analysis.observations.articles.store import (
    read_artifacts,
    read_observations,
)
from baibai_engine.machine_analysis.observations.trends.contracts import (
    ExtractionArtifact,
    SelectedArtifact,
    digest,
)
from baibai_engine.machine_analysis.observations.trends.extraction import matching_artifacts
from baibai_engine.machine_analysis.observations.trends.inputs import TrendCoverage
from baibai_engine.machine_analysis.observations.trends.validation import decode_artifact

MAX_ARTICLES = 16


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


def _latest_document(
    conn: sqlite3.Connection,
    article_id: str,
    *,
    cutoff: datetime,
    start: date,
    as_of: date,
) -> ArticleObservation | None:
    row = conn.execute(
        "SELECT o.observation_id FROM article_observations o "
        "JOIN article_source_receipts r ON r.receipt_id=o.receipt_id "
        "WHERE o.article_id=? AND o.received_on>=? "
        "AND json_extract(o.observation_json,'$.content_scope') IN ('full_text','partial_text') "
        "AND (json_extract(o.observation_json,'$.published.value') IS NULL "
        "OR date(json_extract(o.observation_json,'$.published.value'))<=?) "
        "AND julianday(json_extract(r.receipt_json,'$.received_at'))<=julianday(?) "
        "ORDER BY julianday(json_extract(r.receipt_json,'$.received_at')) DESC, "
        "o.observation_id DESC LIMIT 1",
        (article_id, start.isoformat(), as_of.isoformat(), cutoff.isoformat()),
    ).fetchone()
    return None if row is None else read_observations(conn, (row[0],))[0]


def _index_change(
    conn: sqlite3.Connection,
    index: ArticleObservation,
    *,
    cutoff: datetime,
) -> bool | None:
    """native項目の明示改訂だけを見る。以前のindexがなければ未知。"""
    current = index.native_metadata.get("source_item_sha256")
    if not isinstance(current, str):
        return None
    row = conn.execute(
        "SELECT json_extract(o.observation_json,'$.native_metadata.source_item_sha256') "
        "FROM article_observations o JOIN article_source_receipts r ON r.receipt_id=o.receipt_id "
        "WHERE o.article_id=? AND o.observation_id<>? "
        "AND json_extract(o.observation_json,'$.content_scope') IN ('headline','summary') "
        "AND julianday(json_extract(r.receipt_json,'$.received_at'))<julianday(?) "
        "AND julianday(json_extract(r.receipt_json,'$.received_at'))<=julianday(?) "
        "ORDER BY julianday(json_extract(r.receipt_json,'$.received_at')) DESC LIMIT 1",
        (index.article_id, index.observation_id, _receipt_time(conn, index), cutoff.isoformat()),
    ).fetchone()
    return None if row is None or not isinstance(row[0], str) else current != row[0]


def _receipt_time(conn: sqlite3.Connection, observation: ArticleObservation) -> str:
    return conn.execute(
        "SELECT json_extract(receipt_json,'$.received_at') FROM article_source_receipts "
        "WHERE receipt_id=?",
        (observation.receipt_id,),
    ).fetchone()[0]


def _index_publication(
    conn: sqlite3.Connection, article_id: str, *, cutoff: datetime, as_of: date
) -> date | None:
    row = conn.execute(
        "SELECT json_extract(o.observation_json,'$.published.value') "
        "FROM article_observations o JOIN article_source_receipts r ON r.receipt_id=o.receipt_id "
        "WHERE o.article_id=? "
        "AND json_extract(o.observation_json,'$.content_scope') IN ('headline','summary') "
        "AND json_extract(o.observation_json,'$.published.value') IS NOT NULL "
        "AND date(json_extract(o.observation_json,'$.published.value'))<=? "
        "AND julianday(json_extract(r.receipt_json,'$.received_at'))<=julianday(?) "
        "ORDER BY julianday(json_extract(r.receipt_json,'$.received_at')) DESC LIMIT 1",
        (article_id, as_of.isoformat(), cutoff.isoformat()),
    ).fetchone()
    return None if row is None else date.fromisoformat(row[0][:10])


def read_trend_source(
    conn: sqlite3.Connection,
    *,
    sources: tuple[str, ...],
    cutoff: datetime,
    start: date,
    as_of: date,
    title_terms: tuple[str, ...] = (),
    scope_id: str | None = None,
) -> tuple[tuple[SelectedArtifact, ...], TrendCoverage]:
    source_placeholders = ",".join("?" for _ in sources)
    receipt_rows = conn.execute(
        "SELECT source, json_extract(receipt_json,'$.status'), count(*) "
        f"FROM article_source_receipts WHERE source IN ({source_placeholders}) "  # nosec B608
        "AND received_on>=? "
        "AND julianday(json_extract(receipt_json,'$.received_at'))<=julianday(?) "
        "GROUP BY source, json_extract(receipt_json,'$.status') ORDER BY 1,2",
        (*sources, start.isoformat(), cutoff.isoformat()),
    ).fetchall()
    latest = conn.execute(
        "SELECT json_extract(receipt_json,'$.received_at') FROM article_source_receipts "
        f"WHERE source IN ({source_placeholders}) "  # nosec B608
        "AND received_on>=? "
        "AND julianday(json_extract(receipt_json,'$.received_at'))<=julianday(?) "
        "ORDER BY julianday(json_extract(receipt_json,'$.received_at')) DESC LIMIT 1",
        (*sources, start.isoformat(), cutoff.isoformat()),
    ).fetchone()
    future_publications = conn.execute(
        "SELECT count(DISTINCT o.article_id) FROM article_observations o "
        "JOIN article_source_receipts r ON r.receipt_id=o.receipt_id "
        f"WHERE r.source IN ({source_placeholders}) "  # nosec B608: constants only
        "AND o.received_on>=? "
        "AND julianday(json_extract(r.receipt_json,'$.received_at'))<=julianday(?) "
        "AND date(json_extract(o.observation_json,'$.published.value'))>?",
        (*sources, start.isoformat(), cutoff.isoformat(), as_of.isoformat()),
    ).fetchone()[0]
    family_filter = ""
    family_parameters: tuple[str, ...] = ()
    if title_terms:
        family_filter = (
            " AND ("
            + " OR ".join(
                "json_extract(candidate.observation_json,'$.title') LIKE ?" for _ in title_terms
            )
            + " OR EXISTS (SELECT 1 FROM article_extraction_artifacts a "
            "JOIN json_each(a.artifact_json,'$.payload.claims') c "
            "WHERE a.observation_id=candidate.observation_id "
            "AND json_extract(a.artifact_json,'$.payload.family')='tsr_performance' "
            "AND json_extract(c.value,'$.scope.scope_id')=?))"
        )
        family_parameters = (*tuple(f"%{term}%" for term in title_terms), scope_id or "")
    if sources == ("boj_news",):
        # A policy PDF can have an empty title. This only admits a candidate;
        # the saved parser still determines whether a decision was extracted.
        family_filter = (
            " AND (json_extract(candidate.observation_json,'$.title') LIKE ? "
            "OR candidate.article_id LIKE ? OR EXISTS ("
            "SELECT 1 FROM article_extraction_artifacts a "
            "WHERE a.observation_id=candidate.observation_id "
            "AND json_extract(a.artifact_json,'$.payload.family')=?))"
        )
        family_parameters = (
            "%金融市場調節方針%",
            "%/mopo/mpmdeci/mpr_%",
            "boj_policy_decision",
        )
    # Eligibility belongs to the article, not one title-bearing representation.
    # Rank every retained representation of that article after filtering the
    # finite source/window and the saved family evidence. Unrelated recent RSS
    # entries cannot consume the display cap before a policy document is seen.
    rows = conn.execute(
        "WITH eligible AS (SELECT DISTINCT candidate.article_id "
        "FROM article_observations candidate "
        "JOIN article_source_receipts cr ON cr.receipt_id=candidate.receipt_id "
        f"WHERE cr.source IN ({source_placeholders}) "  # nosec B608: constants only
        "AND candidate.received_on>=? "
        "AND julianday(json_extract(cr.receipt_json,'$.received_at'))<=julianday(?)"
        " AND (json_extract(candidate.observation_json,'$.published.value') IS NULL "
        "OR date(json_extract(candidate.observation_json,'$.published.value'))<=?)"
        + family_filter
        + "), ranked AS ("
        "SELECT o.observation_id, o.article_id, o.received_on, "
        "row_number() OVER (PARTITION BY o.article_id ORDER BY "
        "julianday(json_extract(r.receipt_json,'$.received_at')) DESC, o.observation_id) AS rank "
        "FROM article_observations o JOIN article_source_receipts r ON r.receipt_id=o.receipt_id "
        f"WHERE r.source IN ({source_placeholders}) "  # nosec B608: constants only
        "AND julianday(json_extract(r.receipt_json,'$.received_at'))<=julianday(?) "
        "AND (json_extract(o.observation_json,'$.published.value') IS NULL "
        "OR date(json_extract(o.observation_json,'$.published.value'))<=?) "
        "AND o.received_on>=? AND o.article_id IN (SELECT article_id FROM eligible)) "
        "SELECT observation_id FROM ranked WHERE rank=1 "
        "ORDER BY received_on DESC, article_id LIMIT ?",
        (
            *sources,
            start.isoformat(),
            cutoff.isoformat(),
            as_of.isoformat(),
            *family_parameters,
            *sources,
            cutoff.isoformat(),
            as_of.isoformat(),
            start.isoformat(),
            MAX_ARTICLES + 1,
        ),
    ).fetchall()
    selections = []
    reasons: list[str] = []
    if future_publications:
        reasons.append(f"future_publications_excluded:{future_publications}")
    for latest_observation in read_observations(conn, [row[0] for row in rows[:MAX_ARTICLES]]):
        observation = latest_observation
        document = _latest_document(
            conn,
            observation.article_id,
            cutoff=cutoff,
            start=start,
            as_of=as_of,
        )
        if document is not None and document.observation_id != observation.observation_id:
            changed = _index_change(conn, observation, cutoff=cutoff)
            if changed is True:
                reasons.append(f"index_changed_document_unverified:{observation.observation_id}")
            else:
                observation = document
                if changed is None:
                    reasons.append(
                        f"newer_index_revision_unknown:{latest_observation.observation_id}"
                    )
        current, reason = _choose(conn, observation, cutoff=cutoff)
        if reason is not None:
            reasons.append(f"{reason}:{observation.observation_id}")
            if document is not None and document != observation:
                last_known, _ = _choose(conn, document, cutoff=cutoff)
                if last_known is not None:
                    reasons.append(f"last_known_artifact:{last_known.ref.artifact_id}")
            continue
        if current is None:
            raise ValueError("selected extraction unexpectedly absent")
        published = current.evidence.published_date
        if published is None and current.evidence.published_at is not None:
            published = current.evidence.published_at.date()
        claim_dates = [c.occurred_on for c in current.claims if c.occurred_on is not None]
        if (
            published is None
            and not claim_dates
            and current.evidence.received_at.date() > as_of
            and observation.content_scope in ("full_text", "partial_text")
        ):
            reasons.append(f"document_version_after_as_of_unverified:{observation.observation_id}")
            continue
        if published is None:
            published = _index_publication(
                conn,
                observation.article_id,
                cutoff=cutoff,
                as_of=as_of,
            )
        if published is None:
            if claim_dates:
                published = min(claim_dates)  # BOJ heading, an attributed decision date.
        if published is not None and published > as_of:
            reasons.append(f"publication_after_as_of:{observation.observation_id}")
            continue
        if published is None:
            reasons.append(f"publication_date_unknown:{observation.observation_id}")
            if current.evidence.received_at.date() > as_of:
                continue  # A late receipt cannot establish historical availability.
        # Read an explicit previous representation of this article, not inferred
        # event identity. Distinct BOJ decisions can coexist without auto-merging.
        earlier = conn.execute(
            "SELECT a.artifact_id FROM article_extraction_artifacts a "
            "JOIN article_observations o ON o.observation_id=a.observation_id "
            "JOIN article_source_receipts r ON r.receipt_id=o.receipt_id "
            "WHERE o.article_id=? "
            "AND json_extract(o.observation_json,'$.content_scope') "
            "IN ('full_text','partial_text') "
            "AND json_extract(a.artifact_json,'$.payload.schema')='trend-claims-v1' "
            "AND json_extract(a.artifact_json,'$.payload.article_version_id')<>? "
            "AND julianday(json_extract(r.receipt_json,'$.received_at'))"
            "<julianday(?) "
            "AND julianday(json_extract(a.artifact_json,'$.generated_at'))<=julianday(?) "
            "ORDER BY julianday(json_extract(r.receipt_json,'$.received_at')) DESC, "
            "a.artifact_id LIMIT 1",
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
