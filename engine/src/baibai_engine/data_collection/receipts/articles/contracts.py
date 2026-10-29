"""Small article handoff contract shared by every article provider.

A receipt identifies one actual GET, including unsuccessful/304 responses. A 200
owns bytes, even if equal to earlier bytes; a 304 names a retained concrete 200
for the identical source/request/representation. Receipt IDs and clocks survive
stage replay. Article identity and semantic event identity are separate.

Writers use retain_receipt(conn, receipt, body=...) in store.py, readers use
read_receipt(conn, receipt_id, receipt_sha256=...) and read_body(conn, BodyRef).
These functions never commit the caller's transaction or fetch the live Web.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


def digest_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        .encode("utf-8")
    ).hexdigest()


class ArticleValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BodyRef(ArticleValue):
    receipt_id: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArticleReceipt(ArticleValue):
    receipt_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    requested_url: str = Field(min_length=1)
    representation: str = "default"
    received_at: AwareDatetime
    status: Literal["ok", "not_modified", "http_error", "transport_error", "budget_exceeded"]
    http_status: int | None = None
    media_type: str | None = None
    encoding: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    retry_after: str | None = None
    cooldown_until: AwareDatetime | None = None
    body_ref: BodyRef | None = None
    error_code: str | None = None

    @property
    def sha256(self) -> str:
        return digest_json(self.model_dump(mode="json"))


class ArticleEvidenceRef(ArticleValue):
    """Fixed citation, not a promise that the whole lineage release survives GC.

    Normal get requires this release. Only explicit exact-evidence may resolve
    these same IDs/digests in another caller-pinned retained release; it reports
    both lineage and lookup identities. Optional projection/artifact must resolve
    to this receipt/body, never to latest. URL remains available to Research.
    """

    lineage_release_id: str
    lineage_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_id: str
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    body_ref: BodyRef
    original_url: str
    observation_id: str | None = None
    observation_sha256: str | None = None
    artifact_id: str | None = None
    artifact_sha256: str | None = None


def utc_day(value: datetime) -> str:
    from datetime import UTC

    return value.astimezone(UTC).date().isoformat()
