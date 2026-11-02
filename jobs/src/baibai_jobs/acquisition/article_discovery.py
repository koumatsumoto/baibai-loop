"""Unfinished link discovery from exact originals, independent of HTTP validators."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from baibai_engine.data_collection.providers.articles.contracts import NativeLink
from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt, digest_json

DISCOVERY_SOURCES = frozenset({"boj_news", "mof_news"})
# v2 is a reader-first storage migration: retain v1 reads permanently. Once
# written, rollbacks must retain this reader; old binaries cannot resume v2.
LEGACY_DISCOVERY_FORMAT = "article-discovery-v1"
DISCOVERY_FORMAT = "article-discovery-v2"
# Keep the exact-stage pointer envelope stable: an old reader follows it and
# rejects the unknown v2 body, rather than treating it as an empty legacy cursor.
DISCOVERY_REF_FORMAT = "article-discovery-ref-v1"
MAX_DISCOVERY_BYTES = 1_000_000
MAX_DISCOVERY_REPLAYS = 8


def expand_cursor(cursor: dict[str, Any]) -> dict[str, Any]:
    """Read v1 inline parents or lossless v2 references before stage binding."""
    if cursor.get("format") != DISCOVERY_FORMAT:
        return deepcopy(cursor)
    parents = cursor["discovery_parents"]
    if any(digest_json(parent) != key for key, parent in parents.items()):
        raise ValueError("article discovery parent hash differs")
    expanded = deepcopy(cursor)
    expanded.pop("discovery_parents")
    expanded["format"] = LEGACY_DISCOVERY_FORMAT
    try:
        expanded["discovery_children"] = {
            url: [{**edge, "parent": deepcopy(parents[edge["parent"]])} for edge in edges]
            for url, edges in cursor["discovery_children"].items()
        }
    except (KeyError, TypeError) as error:
        raise ValueError("article discovery parent reference is missing or invalid") from error
    return expanded


def _compact_children(
    children: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    # A parent includes the exact receipt, stage and index evidence. Hundreds
    # of links repeat that same object. Intern only byte-equivalent metadata;
    # different receipts/versions remain distinct, even when URLs are equal.
    parents: dict[str, Any] = {}
    compact: dict[str, list[dict[str, Any]]] = {}
    for url, edges in children.items():
        compact[url] = []
        for edge in edges:
            key = digest_json(edge["parent"])
            parents[key] = edge["parent"]
            compact[url].append({**edge, "parent": key})
    return parents, compact


def _encoded_size(
    pending: dict[str, Any], children: dict[str, list[dict[str, Any]]], queue: list[str]
) -> int:
    parents, compact = _compact_children(children)
    return len(json.dumps([pending, parents, compact, queue], ensure_ascii=False).encode())


class DiscoveryWork:
    def __init__(self, cursor: dict[str, Any]) -> None:
        cursor = expand_cursor(cursor)
        self.pending: dict[str, dict[str, Any]] = deepcopy(cursor.get("discoveries", {}))
        self.children: dict[str, list[dict[str, Any]]] = deepcopy(
            cursor.get("discovery_children", {})
        )
        self.recoveries: set[str] = set(cursor.get("discovery_recoveries", []))

    def register(
        self,
        receipt: ArticleReceipt,
        stage: dict[str, Any],
        index_evidence: dict[str, Any] | None = None,
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
        # Revalidating an unchanged parent must not push its already-read first
        # children ahead of unfinished work on every run. Preserve the FIFO;
        # newly discovered links still retain their exact evidence at the tail.
        proposed = list(dict.fromkeys([*queue, *(link.url for link in links)]))
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
        size = _encoded_size(remaining, children, proposed)
        if size > MAX_DISCOVERY_BYTES:
            return None
        self.pending = remaining
        self.children = children
        return proposed

    def wire(self, cursor: dict[str, Any]) -> dict[str, Any]:
        parents, children = _compact_children(self.children)
        return {
            **cursor,
            "format": DISCOVERY_FORMAT,
            "discoveries": self.pending,
            "discovery_children": children,
            "discovery_parents": parents,
            "discovery_recoveries": sorted(self.recoveries),
        }

    def encoded_size(self, queue: list[str]) -> int:
        return _encoded_size(self.pending, self.children, queue)
