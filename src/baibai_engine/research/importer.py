"""Legacy research YAML loader used only by the final migration runner."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.holding_review import (
    HoldingReviewDocument,
    SourceArtifact,
    validate_holding_review_sources,
)
from baibai_engine.research.decision_packet import (
    DecisionPacketDocument,
    IndependentReview,
    evaluate_decision_packet,
)
from baibai_engine.research.holding_review_builder import validate_holding_review_scalars

from .store import (
    HoldingReviewPublication,
    PacketPublication,
    ResearchConflictError,
    ResearchImportResult,
    ResearchStoreService,
    ReviewPublication,
)


@dataclass(frozen=True, slots=True)
class _PacketSource:
    path: Path
    payload: Mapping[str, object]
    document: DecisionPacketDocument
    core_sha256: str
    file_sha256: str


def import_research_records(
    research_root: Path,
    position_root: Path,
    *,
    db_path: Path | None = None,
    source_root: Path | None = None,
) -> ResearchImportResult:
    """Import packets, reviews, and holding reviews as one domain transaction."""
    packet_sources = [_load_packet(path) for path in sorted(research_root.rglob("*-decision.yaml"))]
    packet_publications, by_core_hash, by_file_hash = _packet_publications(packet_sources)

    reviews: list[ReviewPublication] = []
    for path in sorted(research_root.rglob("*-decision-review.yaml")):
        payload = _load_mapping(path)
        review = IndependentReview.model_validate(payload)
        packet_id = _unique_hash_binding(
            by_core_hash,
            review.reviewed_packet_sha256,
            label=f"review {review.review_id}",
        )
        reviews.append(ReviewPublication(packet_id=packet_id, payload=payload))

    holding_sources: list[tuple[Path, Mapping[str, object], HoldingReviewDocument]] = []
    validation_root = source_root or Path.cwd()
    for path in sorted(position_root.rglob("*-holding-review.yaml")):
        payload = _load_mapping(path)
        document = HoldingReviewDocument.model_validate(payload)
        validate_holding_review_sources(document, root=validation_root)
        validate_holding_review_scalars(document, root=validation_root)
        holding_sources.append((path, payload, document))
    holding_publications = _holding_publications(
        holding_sources,
        by_file_hash=by_file_hash,
    )
    return ResearchStoreService(db_path).import_publications(
        packets=packet_publications,
        reviews=reviews,
        holding_reviews=holding_publications,
    )


def _load_packet(path: Path) -> _PacketSource:
    payload = _load_mapping(path)
    document = DecisionPacketDocument.model_validate(payload)
    result = evaluate_decision_packet(document)
    if result.errors and result.errors != (
        "buy recommendation requires an independent second-pass review",
    ):
        raise ValueError(f"invalid decision packet {path}: {'; '.join(result.errors)}")
    return _PacketSource(
        path=path,
        payload=payload,
        document=document,
        core_sha256=result.packet_sha256,
        file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _packet_publications(
    sources: list[_PacketSource],
) -> tuple[list[PacketPublication], dict[str, list[str]], dict[str, list[str]]]:
    by_ticker: dict[str, list[_PacketSource]] = defaultdict(list)
    for source in sources:
        by_ticker[source.document.input_snapshot.ticker].append(source)
    publications: list[PacketPublication] = []
    by_core_hash: dict[str, list[str]] = defaultdict(list)
    by_file_hash: dict[str, list[str]] = defaultdict(list)
    for ticker, ticker_sources in sorted(by_ticker.items()):
        previous: str | None = None
        per_day: dict[str, int] = defaultdict(int)
        ordered = sorted(
            ticker_sources,
            key=lambda item: (
                item.document.input_snapshot.as_of,
                item.document.judgment.proposed_at,
                item.path.as_posix(),
            ),
        )
        for source in ordered:
            stamp = source.document.input_snapshot.as_of.strftime("%Y%m%d")
            per_day[stamp] += 1
            packet_id = f"packet-{stamp}-{ticker}-r{per_day[stamp]}"
            publications.append(
                PacketPublication(
                    packet_id=packet_id,
                    payload=source.payload,
                    supersedes_id=previous,
                )
            )
            by_core_hash[source.core_sha256].append(packet_id)
            by_file_hash[source.file_sha256].append(packet_id)
            previous = packet_id
    return publications, by_core_hash, by_file_hash


def _holding_publications(
    sources: list[tuple[Path, Mapping[str, object], HoldingReviewDocument]],
    *,
    by_file_hash: dict[str, list[str]],
) -> list[HoldingReviewPublication]:
    per_day_ticker: dict[tuple[str, str], int] = defaultdict(int)
    publications: list[HoldingReviewPublication] = []
    for path, payload, document in sorted(
        sources,
        key=lambda item: (item[2].as_of, item[2].ticker, item[0].as_posix()),
    ):
        holding_source = document.sources.holding_packet
        candidate_source = document.sources.candidate_packet
        if not isinstance(holding_source, SourceArtifact) or (
            candidate_source is not None and not isinstance(candidate_source, SourceArtifact)
        ):
            raise ValueError(f"legacy holding review has non-file sources: {path}")
        packet_id = _unique_hash_binding(
            by_file_hash,
            holding_source.sha256,
            label=f"holding review {path}",
        )
        candidate_packet_id = None
        if candidate_source is not None:
            candidate_packet_id = _unique_hash_binding(
                by_file_hash,
                candidate_source.sha256,
                label=f"holding review candidate {path}",
            )
        stamp = document.as_of.strftime("%Y%m%d")
        key = (stamp, document.ticker)
        per_day_ticker[key] += 1
        publications.append(
            HoldingReviewPublication(
                holding_review_id=(
                    f"holding-review-{stamp}-{document.ticker}-r{per_day_ticker[key]}"
                ),
                packet_id=packet_id,
                candidate_packet_id=candidate_packet_id,
                payload=payload,
            )
        )
    return publications


def _unique_hash_binding(
    index: dict[str, list[str]],
    digest: str,
    *,
    label: str,
) -> str:
    matches = index.get(digest, [])
    if len(matches) != 1:
        raise ResearchConflictError(
            f"{label} source hash must resolve to exactly one packet revision; "
            f"resolved={len(matches)}"
        )
    return matches[0]


def _load_mapping(path: Path) -> Mapping[str, object]:
    raw = safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"research record root must be a mapping: {path}")
    return raw


__all__ = ["import_research_records"]
