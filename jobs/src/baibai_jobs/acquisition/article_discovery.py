"""Unfinished link discovery from exact originals, independent of HTTP validators."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from baibai_engine.data_collection.providers.articles.contracts import NativeLink
from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt, digest_json

DISCOVERY_SOURCES = frozenset({"boj_news", "mof_news"})
DISCOVERY_FORMAT = "article-discovery-v1"
DISCOVERY_REF_FORMAT = "article-discovery-ref-v1"
MAX_DISCOVERY_BYTES = 1_000_000
MAX_DISCOVERY_REPLAYS = 8


class DiscoveryWork:
    def __init__(self, cursor: dict[str, Any]) -> None:
        self.pending: dict[str, dict[str, Any]] = deepcopy(cursor.get("discoveries", {}))
        self.children: dict[str, list[dict[str, Any]]] = deepcopy(
            cursor.get("discovery_children", {})
        )

    def register(
        self, receipt: ArticleReceipt, stage: dict[str, Any], index_evidence: dict[str, Any] | None = None
    ) -> str:
        if receipt.body_ref is None:
            raise ValueError("discovery requires a retained original")
        identity = digest_json(
            {
                "source": receipt.source,
                "url": receipt.requested_url,
                "body_ref": receipt.body_ref.model_dump(),
                "encoding": receipt.encoding,
            }
        )
        self.pending.setdefault(
            identity,
            {
                "receipt_id": receipt.receipt_id,
                "receipt_sha256": receipt.sha256,
                "url": receipt.requested_url,
                "body_ref": receipt.body_ref.model_dump(),
                "encoding": receipt.encoding or "utf-8",
                "stage": stage,
                "index_evidence": index_evidence,
                "attempted_revision": None,
            },
        )
        return identity

    def unsupported(self, identity: str, revision: str) -> None:
        self.pending[identity]["attempted_revision"] = revision

    def admit(
        self, identity: str, links: tuple[NativeLink, ...], queue: list[str], *, revision: str
    ) -> list[str] | None:
        """Commit parent completion and child work together, or retain the parent.

        The caller persists this whole cursor in one immutable stage/CAS. Capacity
        is never implemented by truncating the unprocessed tail.
        """
        parent = self.pending[identity]
        children = deepcopy(self.children)
        proposed = list(dict.fromkeys([*(link.url for link in links), *queue]))
        for link in links:
            edge = {
                "parent": parent,
                "locator": link.locator,
                "discovery_revision": revision,
            }
            edges = children.setdefault(link.url, [])
            if edge not in edges:
                edges.append(edge)
        remaining = {key: value for key, value in self.pending.items() if key != identity}
        size = len(json.dumps([remaining, children, proposed], ensure_ascii=False).encode())
        if size > MAX_DISCOVERY_BYTES:
            return None
        self.pending = remaining
        self.children = children
        return proposed

    def wire(self, cursor: dict[str, Any]) -> dict[str, Any]:
        return {
            **cursor,
            "format": DISCOVERY_FORMAT,
            "discoveries": self.pending,
            "discovery_children": self.children,
        }

    def encoded_size(self, queue: list[str]) -> int:
        return len(json.dumps([self.pending, self.children, queue], ensure_ascii=False).encode())
