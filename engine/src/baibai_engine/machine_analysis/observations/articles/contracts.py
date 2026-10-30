"""Persistence seam for native article projection and semantic extraction owners.

The model worker owns semantic claim schemas/parsers. This module only fixes
storage identity and text coordinates. save_projection(conn, run, observations)
and save_artifact(conn, artifact) in store.py append without implicit selection.
read_observations/read_artifacts accept explicit IDs; selection policy and input
manifests belong to the consuming owner. All calls use caller-owned transactions.

An observation's article_id is provider-local identity, not an inferred event.
observation_id hashes all fields (including receipt and parser revision). The
text hash is UTF-8 SHA256; spans are zero-based Python Unicode codepoint offsets,
half-open, into exactly this text. PDF page mappings may live in native_metadata.
An extraction artifact hashes its entire envelope, including payload, method,
revision, model/prompt/config identifiers and generation clock. Re-extraction
never overwrites an old artifact. Status lives in the artifact once extraction
was attempted; projection runs describe native parsing only.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from baibai_engine.data_collection.receipts.articles.contracts import (
    ArticleValue,
    BodyRef,
    digest_json,
)


class ArticleTime(ArticleValue):
    raw: str | None = None
    value: str | None = None
    precision: Literal["unknown", "date", "minute", "second"] = "unknown"
    timezone_origin: str | None = None


class ArticleObservation(ArticleValue):
    article_id: str
    source: str
    receipt_id: str
    receipt_sha256: str
    body_ref: BodyRef
    native_locator: str
    original_url: str
    canonical_url: str | None = None
    title: str
    published: ArticleTime = ArticleTime()
    updated: ArticleTime = ArticleTime()
    content_scope: Literal["headline", "summary", "full_text", "partial_text", "unavailable"]
    missing_parts: tuple[str, ...] = ()
    parser_revision: str
    text_revision: str
    text: str = Field(repr=False)
    native_metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def article_version_id(self) -> str:
        """Native content version separate from GET identity and HTML decoration.

        A reorder of an index or same-value 200/304 is a new receipt, not a new
        article content version. Parser/text revisions remain explicit; semantic
        extraction revision belongs to its separate artifact.
        """
        return "article-version-" + digest_json(
            self.model_dump(
                mode="json",
                exclude={
                    "receipt_id",
                    "receipt_sha256",
                    "body_ref",
                    "native_locator",
                },
            )
        )

    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def observation_id(self) -> str:
        return "article-observation-" + self.sha256

    @property
    def sha256(self) -> str:
        return digest_json(self.model_dump(mode="json"))


class ArticleProjectionRun(ArticleValue):
    receipt_id: str
    receipt_sha256: str
    parser_revision: str
    generated_at: AwareDatetime
    status: Literal["parsed", "empty", "parse_failed", "unsupported", "pending"]
    observation_ids: tuple[str, ...] = ()
    error_code: str | None = None

    @property
    def run_id(self) -> str:
        return "article-projection-" + digest_json(self.model_dump(mode="json"))


class ArticleExtractionArtifact(ArticleValue):
    observation_id: str
    observation_sha256: str
    text_sha256: str
    text_revision: str
    coordinate_system: Literal["unicode_codepoint_half_open"] = "unicode_codepoint_half_open"
    generated_at: AwareDatetime
    method: Literal["parser", "assistant"]
    extractor_revision: str
    status: Literal["extracted", "empty", "failed", "unsupported", "pending"]
    model_id: str | None = None
    prompt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    config_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    error_code: str | None = None

    @model_validator(mode="after")
    def assistant_identity(self) -> ArticleExtractionArtifact:
        if self.method == "assistant" and not (
            self.model_id and self.prompt_sha256 and self.config_sha256
        ):
            raise ValueError(
                "assistant extraction requires fixed model, prompt and config identities"
            )
        return self

    @property
    def artifact_id(self) -> str:
        return "article-extraction-" + self.sha256

    @property
    def sha256(self) -> str:
        return digest_json(self.model_dump(mode="json"))
