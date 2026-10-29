"""Append-only article projection/artifact persistence. No implicit latest selection."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from baibai_engine.data_collection.receipts.articles.contracts import utc_day
from baibai_engine.data_collection.receipts.articles.store import (
    ArticleStoreError,
    read_body,
    read_receipt,
)
from baibai_engine.machine_analysis.observations.articles.contracts import (
    ArticleExtractionArtifact,
    ArticleObservation,
    ArticleProjectionRun,
)


def save_projection(
    conn: sqlite3.Connection,
    run: ArticleProjectionRun,
    observations: Sequence[ArticleObservation],
) -> None:
    receipt = read_receipt(conn, run.receipt_id, receipt_sha256=run.receipt_sha256)
    if tuple(item.observation_id for item in observations) != run.observation_ids:
        raise ArticleStoreError("projection run differs from its observation IDs")
    if (run.status == "parsed") != bool(observations):
        raise ArticleStoreError("only parsed projection runs contain observations")
    rows = []
    for item in observations:
        if (
            item.receipt_id != receipt.receipt_id
            or item.receipt_sha256 != receipt.sha256
            or item.body_ref != receipt.body_ref
            or item.source != receipt.source
            or item.parser_revision != run.parser_revision
        ):
            raise ArticleStoreError("observation differs from source receipt/projection")
        read_body(conn, item.body_ref)
        rows.append(
            (
                item.observation_id,
                item.article_id,
                item.receipt_id,
                utc_day(receipt.received_at),
                item.sha256,
                item.model_dump_json(),
            )
        )
    # A failed write must not leave only half of a native projection in a caller transaction.
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT article_projection")
    try:
        for row in rows:
            previous = conn.execute(
                "SELECT observation_sha256 FROM article_observations WHERE observation_id=?",
                (row[0],),
            ).fetchone()
            if previous is not None and previous[0] != row[4]:
                raise ArticleStoreError("observation identity collision")
            conn.execute("INSERT OR IGNORE INTO article_observations VALUES (?,?,?,?,?,?)", row)
        conn.execute(
            "INSERT OR IGNORE INTO article_projection_runs VALUES (?,?,?,?)",
            (run.run_id, run.receipt_id, utc_day(run.generated_at), run.model_dump_json()),
        )
        conn.execute("RELEASE article_projection")
    except BaseException:
        conn.execute("ROLLBACK TO article_projection")
        conn.execute("RELEASE article_projection")
        raise


def read_observations(
    conn: sqlite3.Connection, observation_ids: Sequence[str]
) -> tuple[ArticleObservation, ...]:
    result = []
    for identity in observation_ids:
        row = conn.execute(
            "SELECT observation_sha256,observation_json FROM article_observations "
            "WHERE observation_id=?",
            (identity,),
        ).fetchone()
        if row is None:
            raise ArticleStoreError("article observation is not retained")
        item = ArticleObservation.model_validate_json(row[1])
        if item.observation_id != identity or item.sha256 != row[0]:
            raise ArticleStoreError("article observation digest mismatch")
        result.append(item)
    return tuple(result)


def save_artifact(conn: sqlite3.Connection, artifact: ArticleExtractionArtifact) -> None:
    observation = read_observations(conn, [artifact.observation_id])[0]
    if (
        observation.sha256 != artifact.observation_sha256
        or observation.text_sha256 != artifact.text_sha256
        or observation.text_revision != artifact.text_revision
    ):
        raise ArticleStoreError("extraction text coordinates differ from observation")
    previous = conn.execute(
        "SELECT artifact_sha256 FROM article_extraction_artifacts WHERE artifact_id=?",
        (artifact.artifact_id,),
    ).fetchone()
    if previous is not None and previous[0] != artifact.sha256:
        raise ArticleStoreError("extraction artifact identity collision")
    conn.execute(
        "INSERT OR IGNORE INTO article_extraction_artifacts VALUES (?,?,?,?,?)",
        (
            artifact.artifact_id,
            artifact.observation_id,
            utc_day(artifact.generated_at),
            artifact.sha256,
            artifact.model_dump_json(),
        ),
    )


def read_artifacts(
    conn: sqlite3.Connection, artifact_ids: Sequence[str]
) -> tuple[ArticleExtractionArtifact, ...]:
    result = []
    for identity in artifact_ids:
        row = conn.execute(
            "SELECT artifact_sha256,artifact_json FROM article_extraction_artifacts "
            "WHERE artifact_id=?",
            (identity,),
        ).fetchone()
        if row is None:
            raise ArticleStoreError("article extraction artifact is not retained")
        item = ArticleExtractionArtifact.model_validate_json(row[1])
        if item.artifact_id != identity or item.sha256 != row[0]:
            raise ArticleStoreError("article extraction artifact digest mismatch")
        result.append(item)
    return tuple(result)
