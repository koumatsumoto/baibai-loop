"""Finite article work, separate from the evictable HTTP validator cache.

The immutable handoff owns the workset. Index coordinates, not copied titles,
keep pending evidence small; each use resolves the original native index again.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from baibai_engine.data_collection.providers.articles.contracts import NativeArticle

FRONTIER_SOURCES = frozenset({"nhk_news", "tsr_data", "tsr_flash"})
FRONTIER_FORMAT = "article-frontier-v1"
FRONTIER_REF_FORMAT = "article-frontier-ref-v1"
# Leave room for validators, selected document evidence and the handoff envelope.
MAX_FRONTIER_BYTES = 1_000_000


def item_key(item: NativeArticle) -> tuple[str, str, str, str]:
    return (
        item.article_id,
        str(item.metadata.get("fetch_url") or item.url),
        item.title,
        str(item.metadata.get("source_item_sha256") or ""),
    )


class ArticleFrontier:
    def __init__(
        self,
        cursor: dict[str, Any],
        resolve: Callable[[dict[str, Any]], tuple[NativeArticle, ...]],
    ) -> None:
        self.resolve = resolve
        self.indexes: list[dict[str, Any]] = []
        self.observed: dict[str, int] = {}
        self.pending: dict[str, list[tuple[int, str]]] = {}
        self.current: dict[str, list[tuple[int, str]]] = {}
        self.new: list[str] = []
        self._new_seen: set[str] = set()
        self.legacy_omitted = 0
        self.legacy_recovery = cursor.get("legacy_recovery")
        self.next_page: str | None = cursor.get("next_page")
        self.previous_keys: set[tuple[str, str, str, str]] = set()
        if cursor.get("format") == FRONTIER_FORMAT:
            self.indexes = list(cursor["indexes"])
            self.observed = dict(cursor["observed"])
            self.legacy_omitted = cursor["legacy_omitted"]
            if cursor.get("capacity_blocked_indexes"):
                raise ValueError("article frontier capacity requires exact recovery")
            for url, refs in cursor["pending"]:
                if url in self.pending:
                    raise ValueError("duplicate article pending URL")
                self.pending[url] = [(int(index), locator) for index, locator in refs]
            if type(self.legacy_omitted) is not int or self.legacy_omitted < 0:
                raise ValueError("invalid historical article omission count")
            for index in self.observed.values():
                self._index(index)
                self.previous_keys.update(item_key(item) for item in resolve(self.indexes[index]))
            for refs in self.pending.values():
                for index, locator in refs:
                    self._index(index)
                    if not isinstance(locator, str):
                        raise ValueError("invalid article native locator")
        else:
            self.legacy_omitted = int(cursor.get("omitted_url_count", 0))
            evidence = cursor.get("pending_evidence", {})
            for url in cursor.get("pending_urls", []):
                link = evidence.get(url)
                refs = []
                if isinstance(link, dict) and isinstance(link.get("stage"), dict):
                    index = self.register(
                        {"stage": link["stage"], "index_receipt_id": link["index_receipt_id"]}
                    )
                    refs = [(index, link["native_locator"])]
                self.pending[url] = refs
        self.initial = not self.observed
        self.capacity_blocked: list[int] = []

    def _index(self, index: int) -> None:
        if type(index) is not int or not 0 <= index < len(self.indexes):
            raise ValueError("article index coordinate outside registry")

    def register(self, reference: dict[str, Any]) -> int:
        key = (reference["stage"]["stage_id"], reference["index_receipt_id"])
        for index, prior in enumerate(self.indexes):
            if (prior["stage"]["stage_id"], prior["index_receipt_id"]) == key:
                return index
        self.indexes.append(reference)
        return len(self.indexes) - 1

    def seed_legacy_index(self, reference: dict[str, Any]) -> None:
        """Recover a superset from retained old indexes, not an invented old URL list."""
        index = self.register(reference)
        self.initial = False
        items = self.resolve(reference)
        self.previous_keys.update(item_key(item) for item in items)
        for item in items:
            url = item_key(item)[1]
            refs = self.pending.setdefault(url, [])
            coordinate = (index, item.locator)
            if coordinate not in refs:
                refs.append(coordinate)

    def observe(
        self, url: str, reference: dict[str, Any], items: tuple[NativeArticle, ...]
    ) -> None:
        index = self.register(reference)
        self.observed[url] = index
        for item in items:
            key = item_key(item)
            refs = self.current.setdefault(key[1], [])
            coordinate = (index, item.locator)
            if coordinate not in refs:
                refs.append(coordinate)
            if key not in self.previous_keys and key[1] not in self._new_seen:
                self.new.append(key[1])
                self._new_seen.add(key[1])

    def queue(self, known: list[str]) -> list[str]:
        # Current evidence supersedes the old locator for the same pending URL.
        for url in self.pending.keys() & self.current.keys():
            self.pending[url] = self.current[url]
        original = dict(self.pending)
        for url in self.new:
            self.pending.setdefault(url, self.current[url])
        if self.encoded_size() > MAX_FRONTIER_BYTES:
            # Preserve older work and the exact indexes containing non-admitted work.
            # The next slot stops before HTTP; a bounded explicit recovery is required.
            self.pending = original
            self.capacity_blocked = list(dict.fromkeys(self.observed.values()))
            return []
        latest = [] if self.initial else self.new[:2]
        oldest = next((url for url in self.pending if url not in latest), None)
        protected = [*latest[:1], *([oldest] if oldest is not None else [])]
        return list(dict.fromkeys([*protected, *known, *latest[1:], *self.pending]))

    def evidence(self, url: str) -> dict[str, Any] | None:
        refs = self.current.get(url) or self.pending.get(url, [])
        matches: list[tuple[dict[str, Any], NativeArticle]] = []
        for index, locator in refs:
            self._index(index)
            reference = self.indexes[index]
            items = [item for item in self.resolve(reference) if item.locator == locator]
            if len(items) != 1 or item_key(items[0])[1] != url:
                return None
            matches.append((reference, items[0]))
        if not matches or len({(item.article_id, item.title) for _, item in matches}) != 1:
            return None
        reference, item = matches[0]
        return {
            **reference,
            "article_id": item.article_id,
            "native_locator": item.locator,
            "title": item.title,
        }

    def complete(self, url: str, *, success: bool) -> None:
        if success:
            self.pending.pop(url, None)
        elif url in self.pending:
            refs = self.pending.pop(url)
            self.pending[url] = refs

    def wire(self) -> dict[str, Any]:
        used = set(self.observed.values()) | set(self.capacity_blocked)
        used.update(index for refs in self.pending.values() for index, _ in refs)
        ordered = sorted(used)
        remap = {old: new for new, old in enumerate(ordered)}
        return {
            "format": FRONTIER_FORMAT,
            "indexes": [self.indexes[index] for index in ordered],
            "observed": {url: remap[index] for url, index in self.observed.items()},
            "pending": [
                [url, [[remap[index], locator] for index, locator in refs]]
                for url, refs in self.pending.items()
            ],
            "next_page": self.next_page,
            "legacy_omitted": self.legacy_omitted,
            "legacy_recovery": self.legacy_recovery,
            "capacity_blocked_indexes": [remap[index] for index in self.capacity_blocked],
        }

    def encoded_size(self) -> int:
        return len(json.dumps(self.wire(), sort_keys=True).encode())


def bind_local_stage(value: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Bind a manifest's self-reference without changing any earlier stage reference."""
    return (
        {**value, "stage": entry}
        if value.get("stage", {}).get("stage_id") == entry["stage_id"]
        else value
    )
