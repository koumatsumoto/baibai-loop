"""Immutable application-DB storage for research publications."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.holding_review import (
    CanonicalSource,
    HoldingReviewDocument,
    SourceArtifact,
    evaluate_holding_review,
    validate_holding_review_sources,
)
from baibai_engine.research.decision_packet import (
    DecisionPacketDocument,
    DecisionPacketResult,
    IndependentReview,
    evaluate_decision_packet,
)
from baibai_engine.research.holding_review_builder import (
    validate_holding_review_scalars,
    validate_holding_review_scalars_from_db,
)

_REVIEW_REQUIRED = "buy recommendation requires an independent second-pass review"


class ResearchConflictError(ValueError):
    """A publication conflicts with an immutable row or source revision."""


class ResearchValidationError(ValueError):
    """A publication does not satisfy the research domain contract."""


@dataclass(frozen=True, slots=True)
class PacketPublication:
    packet_id: str
    payload: Mapping[str, object]
    supersedes_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewPublication:
    packet_id: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class HoldingReviewPublication:
    holding_review_id: str
    packet_id: str
    payload: Mapping[str, object]
    candidate_packet_id: str | None = None


@dataclass(frozen=True, slots=True)
class ResearchImportResult:
    packets_inserted: int
    packets_unchanged: int
    reviews_inserted: int
    reviews_unchanged: int
    holding_reviews_inserted: int
    holding_reviews_unchanged: int


class ResearchStoreService:
    """Validate revision bindings before writing immutable research rows."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def publish_packet(
        self,
        packet_id: str,
        payload: Mapping[str, object],
        *,
        supersedes_id: str | None = None,
    ) -> DecisionPacketDocument:
        publication = PacketPublication(packet_id, payload, supersedes_id)
        packet, _ = _validate_packet(publication)
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _insert_packet(connection, publication, packet)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return packet

    def publish_packet_with_review(
        self,
        packet_id: str,
        packet_payload: Mapping[str, object],
        review_payload: Mapping[str, object],
        *,
        supersedes_id: str | None = None,
    ) -> tuple[DecisionPacketDocument, IndependentReview]:
        """Atomically publish a packet and its independent review."""
        publication = PacketPublication(packet_id, packet_payload, supersedes_id)
        packet, _ = _validate_packet(publication, allow_review_required=True)
        review = IndependentReview.model_validate(review_payload)
        _require_valid(evaluate_decision_packet(packet, review=review))
        review_publication = ReviewPublication(packet_id, review_payload)
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _insert_packet(connection, publication, packet)
                _insert_review(connection, review_publication, review)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return packet, review

    def publish_review(
        self,
        packet_id: str,
        payload: Mapping[str, object],
    ) -> IndependentReview:
        publication = ReviewPublication(packet_id, payload)
        review = IndependentReview.model_validate(payload)
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _insert_review(connection, publication, review)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return review

    def publish_holding_review(
        self,
        holding_review_id: str,
        packet_id: str,
        payload: Mapping[str, object],
        *,
        root: Path,
        candidate_packet_id: str | None = None,
    ) -> HoldingReviewDocument:
        """Recheck current file sources and DB revision bindings inside the write transaction."""
        publication = HoldingReviewPublication(
            holding_review_id, packet_id, payload, candidate_packet_id
        )
        document = _validate_holding_document(payload)
        canonical_sources = isinstance(document.sources.ledger, CanonicalSource)
        if canonical_sources:
            validate_holding_review_scalars_from_db(document, db_path=self._db_path)
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if canonical_sources:
                    _validate_canonical_holding_sources(connection, publication, document)
                else:
                    validate_holding_review_sources(document, root=root)
                    validate_holding_review_scalars(document, root=root)
                    _validate_holding_source_revisions(connection, publication, document, root=root)
                _insert_holding_review(connection, publication, document)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return document

    def import_publications(
        self,
        *,
        packets: Sequence[PacketPublication],
        reviews: Sequence[ReviewPublication],
        holding_reviews: Sequence[HoldingReviewPublication],
    ) -> ResearchImportResult:
        """Import a legacy domain snapshot in one create-only transaction."""
        _unique("packet_id", [item.packet_id for item in packets])
        _unique("review_id", [str(item.payload.get("review_id", "")) for item in reviews])
        _unique("holding_review_id", [item.holding_review_id for item in holding_reviews])
        validated_packets = [
            (*_validate_packet(item, allow_review_required=True), item) for item in packets
        ]
        validated_reviews = [
            (IndependentReview.model_validate(item.payload), item) for item in reviews
        ]
        validated_holding = [
            (_validate_holding_document(item.payload), item) for item in holding_reviews
        ]
        review_packet_ids = {item.packet_id for item in reviews}
        for _packet, result, publication in validated_packets:
            if (
                result.errors == (_REVIEW_REQUIRED,)
                and publication.packet_id not in review_packet_ids
            ):
                raise ResearchValidationError(
                    f"buy packet has no imported independent review: {publication.packet_id}"
                )
        initialize_database(self._db_path)
        counts = [0, 0, 0, 0, 0, 0]
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for packet, _result, packet_publication in validated_packets:
                    counts[0 if _insert_packet(connection, packet_publication, packet) else 1] += 1
                for review, review_publication in validated_reviews:
                    counts[2 if _insert_review(connection, review_publication, review) else 3] += 1
                for document, holding_publication in validated_holding:
                    counts[
                        4
                        if _insert_holding_review(connection, holding_publication, document)
                        else 5
                    ] += 1
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return ResearchImportResult(*counts)


def _validate_packet(
    publication: PacketPublication,
    *,
    allow_review_required: bool = False,
) -> tuple[DecisionPacketDocument, DecisionPacketResult]:
    if not publication.packet_id.strip():
        raise ResearchValidationError("packet_id must not be empty")
    packet = DecisionPacketDocument.model_validate(publication.payload)
    result = evaluate_decision_packet(packet)
    if result.errors and not (allow_review_required and result.errors == (_REVIEW_REQUIRED,)):
        _require_valid(result)
    return packet, result


def _require_valid(result: DecisionPacketResult) -> None:
    if result.errors:
        raise ResearchValidationError("; ".join(result.errors))


def _validate_holding_document(payload: Mapping[str, object]) -> HoldingReviewDocument:
    document = HoldingReviewDocument.model_validate(payload)
    result = evaluate_holding_review(document)
    if result.errors:
        raise ResearchValidationError("; ".join(result.errors))
    return document


def _insert_packet(
    connection: sqlite3.Connection,
    publication: PacketPublication,
    document: DecisionPacketDocument,
) -> bool:
    payload = canonical_json(publication.payload)
    expected = (
        document.input_snapshot.ticker,
        document.input_snapshot.as_of.isoformat(),
        document.judgment.recommendation,
        document.judgment.proposed_at.isoformat(),
        publication.supersedes_id,
        payload,
    )
    existing = connection.execute(
        """
        SELECT ticker, as_of, recommendation, published_at, supersedes_id, payload
        FROM research_packet WHERE packet_id = ?
        """,
        (publication.packet_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) == expected:
            return False
        raise ResearchConflictError(
            f"packet differs from existing immutable revision: {publication.packet_id}"
        )
    if publication.supersedes_id is not None:
        parent = connection.execute(
            "SELECT ticker FROM research_packet WHERE packet_id = ?",
            (publication.supersedes_id,),
        ).fetchone()
        if parent is None:
            raise ResearchConflictError(f"unknown supersedes packet: {publication.supersedes_id}")
        if str(parent[0]) != document.input_snapshot.ticker:
            raise ResearchConflictError("supersedes packet ticker does not match")
    connection.execute(
        """
        INSERT INTO research_packet (
            packet_id, ticker, as_of, recommendation, published_at, supersedes_id, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (publication.packet_id, *expected),
    )
    return True


def _packet_row(connection: sqlite3.Connection, packet_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT ticker, as_of, payload FROM research_packet WHERE packet_id = ?",
        (packet_id,),
    ).fetchone()
    if row is None:
        raise ResearchConflictError(f"unknown packet revision: {packet_id}")
    return cast(sqlite3.Row, row)


def _insert_review(
    connection: sqlite3.Connection,
    publication: ReviewPublication,
    review: IndependentReview,
) -> bool:
    packet_row = _packet_row(connection, publication.packet_id)
    packet = DecisionPacketDocument.model_validate_json(str(packet_row["payload"]))
    _require_valid(evaluate_decision_packet(packet, review=review))
    payload = canonical_json(publication.payload)
    expected = (publication.packet_id, review.reviewed_at.isoformat(), payload)
    existing = connection.execute(
        "SELECT packet_id, reviewed_at, payload FROM research_review WHERE review_id = ?",
        (review.review_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) == expected:
            return False
        raise ResearchConflictError(
            f"review differs from existing immutable publication: {review.review_id}"
        )
    connection.execute(
        "INSERT INTO research_review(review_id, packet_id, reviewed_at, payload) "
        "VALUES (?, ?, ?, ?)",
        (review.review_id, *expected),
    )
    return True


def _insert_holding_review(
    connection: sqlite3.Connection,
    publication: HoldingReviewPublication,
    document: HoldingReviewDocument,
) -> bool:
    packet = _packet_row(connection, publication.packet_id)
    if document.ticker != str(packet["ticker"]):
        raise ResearchConflictError("holding review ticker does not match packet revision")
    if document.as_of < date.fromisoformat(str(packet["as_of"])):
        raise ResearchConflictError("holding review predates packet revision")
    candidate_source = document.sources.candidate_packet
    if (candidate_source is None) != (publication.candidate_packet_id is None):
        raise ResearchConflictError(
            "candidate_packet_id is required exactly when candidate_packet source exists"
        )
    if publication.candidate_packet_id is not None:
        candidate_packet = _packet_row(connection, publication.candidate_packet_id)
        candidate = document.replacement_comparison.candidate
        if candidate is None or candidate.ticker != str(candidate_packet["ticker"]):
            raise ResearchConflictError(
                "holding review candidate ticker does not match candidate packet revision"
            )
    payload = canonical_json(publication.payload)
    expected = (
        document.ticker,
        document.as_of.isoformat(),
        publication.packet_id,
        publication.candidate_packet_id,
        payload,
    )
    existing = connection.execute(
        """
        SELECT ticker, as_of, packet_id, candidate_packet_id, payload
        FROM holding_review WHERE holding_review_id = ?
        """,
        (publication.holding_review_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) == expected:
            return False
        raise ResearchConflictError(
            "holding review differs from existing immutable publication: "
            f"{publication.holding_review_id}"
        )
    connection.execute(
        """
        INSERT INTO holding_review (
            holding_review_id, ticker, as_of, packet_id, candidate_packet_id, payload
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (publication.holding_review_id, *expected),
    )
    return True


def _validate_holding_source_revisions(
    connection: sqlite3.Connection,
    publication: HoldingReviewPublication,
    document: HoldingReviewDocument,
    *,
    root: Path,
) -> None:
    holding_source = document.sources.holding_packet
    candidate_source = document.sources.candidate_packet
    if not isinstance(holding_source, SourceArtifact) or (
        candidate_source is not None and not isinstance(candidate_source, SourceArtifact)
    ):
        raise ResearchValidationError("legacy holding review sources must be file artifacts")
    bindings = (
        (publication.packet_id, holding_source.ref, "holding"),
        (
            publication.candidate_packet_id,
            None if candidate_source is None else candidate_source.ref,
            "candidate",
        ),
    )
    for packet_id, ref, label in bindings:
        if packet_id is None or ref is None:
            continue
        row = _packet_row(connection, packet_id)
        canonical = DecisionPacketDocument.model_validate_json(str(row["payload"]))
        source_path = Path(ref)
        resolved = source_path if source_path.is_absolute() else root / source_path
        raw = safe_load(resolved.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ResearchValidationError(f"{label} packet source must be a mapping")
        source = DecisionPacketDocument.model_validate(raw)
        if (
            evaluate_decision_packet(canonical).packet_sha256
            != evaluate_decision_packet(source).packet_sha256
        ):
            raise ResearchConflictError(f"{label} source targets a different packet revision")


def _validate_canonical_holding_sources(
    connection: sqlite3.Connection,
    publication: HoldingReviewPublication,
    document: HoldingReviewDocument,
) -> None:
    ledger_source = document.sources.ledger
    packet_source = document.sources.holding_packet
    candidate_source = document.sources.candidate_packet
    if not isinstance(ledger_source, CanonicalSource) or not isinstance(
        packet_source, CanonicalSource
    ):
        raise ResearchConflictError("holding review canonical bindings are incomplete")
    current_head = int(
        connection.execute("SELECT coalesce(max(append_seq), 0) FROM ledger_event").fetchone()[0]
    )
    if ledger_source.entity_id != "portfolio-ledger" or ledger_source.append_head != current_head:
        raise ResearchConflictError("holding review ledger revision changed")
    if packet_source.entity_id != publication.packet_id:
        raise ResearchConflictError("holding review packet revision binding differs")
    if candidate_source is None:
        if publication.candidate_packet_id is not None:
            raise ResearchConflictError("candidate packet revision binding is missing")
    elif not isinstance(candidate_source, CanonicalSource) or (
        candidate_source.entity_id != publication.candidate_packet_id
    ):
        raise ResearchConflictError("candidate packet revision binding differs")


def _unique(label: str, values: Sequence[str]) -> None:
    if any(not value for value in values) or len(values) != len(set(values)):
        raise ResearchConflictError(f"{label} must be non-empty and unique in import input")


__all__ = [
    "HoldingReviewPublication",
    "PacketPublication",
    "ResearchConflictError",
    "ResearchImportResult",
    "ResearchStoreService",
    "ResearchValidationError",
    "ReviewPublication",
]
