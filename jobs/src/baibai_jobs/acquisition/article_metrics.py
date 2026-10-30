"""Finite article-slot object IO counters; never record payloads or private keys."""

from __future__ import annotations

from pathlib import Path

from baibai_engine.storage.objects.r2 import ObjectStore, RemoteObject


class ArticleObjectMetrics:
    def __init__(self, store: ObjectStore) -> None:
        self.store = store
        self.counts = {
            "metadata_head": 0,
            "metadata_get": 0,
            "metadata_list": 0,
            "input_bytes": 0,
            "raw_download_bytes": 0,
            "private_put": 0,
            "private_upload_bytes": 0,
        }

    def head(self, key: str) -> RemoteObject | None:
        self.counts["metadata_head"] += 1
        return self.store.head(key)

    def get_bytes(self, key: str) -> bytes:
        self.counts["metadata_get"] += 1
        payload = self.store.get_bytes(key)
        self.counts["input_bytes"] += len(payload)
        return payload

    def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None:
        self.store.download_file(key, path, expect_bytes=expect_bytes)
        self.counts["raw_download_bytes"] += path.stat().st_size

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
        content_md5: str,
        content_type: str,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> RemoteObject:
        result = self.store.put_file(
            key,
            path,
            sha256=sha256,
            content_md5=content_md5,
            content_type=content_type,
            if_match=if_match,
            if_none_match=if_none_match,
        )
        self.counts["private_put"] += 1
        self.counts["private_upload_bytes"] += path.stat().st_size
        return result
