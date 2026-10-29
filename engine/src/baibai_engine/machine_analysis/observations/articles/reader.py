"""Pinned article list/get and explicit cross-release exact-evidence resolution.

No current-pointer lookup, Web fallback, or promise of old release retention.
Only the passed FixedRelease is queried. Raw bodies remain private, and are only
returned by explicit get. List is bounded, stable and has a same-release cursor.
"""

from __future__ import annotations

import gzip
import hashlib
from dataclasses import dataclass
from typing import Any

from baibai_engine.data_collection.receipts.articles.contracts import (
    ArticleEvidenceRef,
    ArticleReceipt,
)
from baibai_engine.data_collection.receipts.articles.snapshot_contracts import ARTICLE_RECEIPTS
from baibai_engine.data_collection.receipts.articles.store import ArticleStoreError
from baibai_engine.machine_analysis.observations.articles.contracts import (
    ArticleExtractionArtifact,
    ArticleObservation,
)
from baibai_engine.machine_analysis.observations.articles.snapshot_contracts import (
    ARTICLE_EXTRACTION_ARTIFACTS,
    ARTICLE_OBSERVATIONS,
)
from baibai_engine.storage.snapshots.contracts import LakeDataset
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.objects import LakeObjectCache
from baibai_engine.storage.snapshots.reader import (
    FixedRelease,
    accepted_dataset,
    materialize_partitions,
    selected_partitions,
)


@dataclass(frozen=True)
class ArticleEvidence:
    reference: ArticleEvidenceRef
    lookup_release_id: str
    lookup_manifest_sha256: str
    receipt: ArticleReceipt
    body: bytes
    observation: ArticleObservation | None
    artifact: ArticleExtractionArtifact | None


@dataclass(frozen=True)
class FixedArticleReader:
    release: FixedRelease
    cache: LakeObjectCache

    def _rows(
        self,
        dataset: LakeDataset,
        *,
        columns: tuple[str, ...],
        identity: str | None = None,
        after: str | None = None,
        limit: int = 1,
    ) -> list[tuple[Any, ...]]:
        if dataset.name not in self.release.dataset_manifests:
            return []
        accepted_dataset(self.release, dataset)
        paths = materialize_partitions(
            self.cache,
            dataset=dataset,
            partitions=selected_partitions(self.release, dataset.name),
        )
        if not paths:
            return []
        # All SQL identifiers come only from owner constants, never user input.
        names = {column.name for column in dataset.columns}
        if not set(columns) <= names:
            raise ArticleStoreError("unknown article snapshot column")
        key = dataset.primary_key[0]
        parameters: dict[str, Any] = {"files": [str(path) for path in paths], "limit": limit}
        predicate = "TRUE"
        if identity is not None:
            predicate = f'"{key}" = $identity'
            parameters["identity"] = identity
        elif after is not None:
            predicate = f'"{key}" > $after'
            parameters["after"] = after
        projection = ",".join(f'"{name}"' for name in columns)
        with lake_session() as session:
            return session.connection.execute(
                f"SELECT {projection} FROM read_parquet($files, union_by_name=false) "  # nosec B608
                f'WHERE {predicate} ORDER BY "{key}" LIMIT $limit',
                parameters,
            ).fetchall()

    def receipt(self, receipt_id: str, digest: str | None = None) -> ArticleReceipt:
        rows = self._rows(
            ARTICLE_RECEIPTS, columns=("receipt_sha256", "receipt_json"), identity=receipt_id
        )
        if not rows:
            raise ArticleStoreError("article receipt absent from fixed release")
        receipt = ArticleReceipt.model_validate_json(rows[0][1])
        if receipt.receipt_id != receipt_id or receipt.sha256 != rows[0][0]:
            raise ArticleStoreError("article receipt identity mismatch")
        if digest is not None and receipt.sha256 != digest:
            raise ArticleStoreError("article receipt differs from exact descriptor")
        return receipt

    def list(self, *, after: str | None = None, limit: int = 20) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise ValueError("article list limit must be between 1 and 200")
        rows = self._rows(
            ARTICLE_OBSERVATIONS,
            columns=("observation_id", "observation_sha256", "observation_json"),
            after=after,
            limit=limit + 1,
        )
        items = []
        for identity, digest, raw in rows[:limit]:
            observation = ArticleObservation.model_validate_json(raw)
            if observation.observation_id != identity or observation.sha256 != digest:
                raise ArticleStoreError("article observation identity mismatch")
            reference = ArticleEvidenceRef(
                lineage_release_id=self.release.release_id,
                lineage_manifest_sha256=self.release.manifest_sha256,
                receipt_id=observation.receipt_id,
                receipt_sha256=observation.receipt_sha256,
                body_ref=observation.body_ref,
                original_url=observation.original_url,
                observation_id=identity,
                observation_sha256=digest,
            )
            items.append(
                {
                    "reference": reference.model_dump(mode="json"),
                    "article_id": observation.article_id,
                    "source": observation.source,
                    "content_scope": observation.content_scope,
                    "published": observation.published.model_dump(mode="json"),
                }
            )
        return {
            "release_id": self.release.release_id,
            "manifest_sha256": self.release.manifest_sha256,
            "items": items,
            "has_more": len(rows) > limit,
            "next_after": rows[limit - 1][0] if len(rows) > limit else None,
        }

    def get(self, reference: ArticleEvidenceRef) -> ArticleEvidence:
        if (reference.lineage_release_id, reference.lineage_manifest_sha256) != (
            self.release.release_id,
            self.release.manifest_sha256,
        ):
            raise ArticleStoreError("normal get requires the descriptor's original fixed release")
        return self.exact_evidence(reference)

    def exact_evidence(self, reference: ArticleEvidenceRef) -> ArticleEvidence:
        """Resolve only the exact cited bytes in this explicitly selected lookup release."""
        receipt = self.receipt(reference.receipt_id, reference.receipt_sha256)
        if receipt.body_ref != reference.body_ref:
            raise ArticleStoreError("receipt body differs from exact descriptor")
        baseline = self.receipt(reference.body_ref.receipt_id)
        if baseline.status != "ok" or baseline.body_ref != reference.body_ref:
            raise ArticleStoreError("article body does not name a concrete 200")
        if (baseline.source, baseline.requested_url, baseline.representation) != (
            receipt.source,
            receipt.requested_url,
            receipt.representation,
        ):
            raise ArticleStoreError("304 body belongs to a different representation")
        rows = self._rows(ARTICLE_RECEIPTS, columns=("body_gzip",), identity=baseline.receipt_id)
        if not rows or rows[0][0] is None:
            raise ArticleStoreError("article body is absent from lookup release")
        body = gzip.decompress(rows[0][0])
        if hashlib.sha256(body).hexdigest() != reference.body_ref.sha256:
            raise ArticleStoreError("article body digest mismatch")
        observation = None
        artifact = None
        if reference.observation_id is not None:
            rows = self._rows(
                ARTICLE_OBSERVATIONS,
                columns=("observation_json",),
                identity=reference.observation_id,
            )
            if not rows:
                raise ArticleStoreError("exact observation is absent")
            observation = ArticleObservation.model_validate_json(rows[0][0])
            if (
                observation.observation_id != reference.observation_id
                or observation.sha256 != reference.observation_sha256
                or observation.receipt_id != receipt.receipt_id
                or observation.receipt_sha256 != receipt.sha256
                or observation.body_ref != reference.body_ref
                or observation.original_url != reference.original_url
            ):
                raise ArticleStoreError("exact observation differs from descriptor")
        elif reference.original_url != receipt.requested_url:
            raise ArticleStoreError("raw-only descriptor URL differs from receipt")
        if reference.artifact_id is not None:
            rows = self._rows(
                ARTICLE_EXTRACTION_ARTIFACTS,
                columns=("artifact_json",),
                identity=reference.artifact_id,
            )
            if not rows or observation is None:
                raise ArticleStoreError("exact artifact/observation is absent")
            artifact = ArticleExtractionArtifact.model_validate_json(rows[0][0])
            if (
                artifact.artifact_id != reference.artifact_id
                or artifact.sha256 != reference.artifact_sha256
                or artifact.observation_id != observation.observation_id
                or artifact.observation_sha256 != observation.sha256
                or artifact.text_sha256 != observation.text_sha256
                or artifact.text_revision != observation.text_revision
            ):
                raise ArticleStoreError("exact extraction artifact differs from descriptor")
        return ArticleEvidence(
            reference,
            self.release.release_id,
            self.release.manifest_sha256,
            receipt,
            body,
            observation,
            artifact,
        )
