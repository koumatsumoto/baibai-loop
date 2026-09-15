"""Research に固定 origin の L1 GET を供給し、転送超過・世代不整合を止める。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from baibai_engine.market.lake.keys import current_l1_pointer_key, validate_lake_object_key
from baibai_engine.market.lake.models import MAX_LAKE_JSON_BYTES
from baibai_engine.market.lake.objects import mirror_path

from .contract import LIMITS, L1Error, Limits

ORIGIN = "https://baibai-loop.koumatsumoto.workers.dev"


@dataclass
class Gateway:
    token: str = field(repr=False)
    root: Path
    limits: Limits = LIMITS
    gets: int = 0
    transferred_bytes: int = 0
    session: requests.Session = field(default_factory=requests.Session, repr=False)

    def __post_init__(self) -> None:
        # ambient proxy / netrc credentials never change the fixed-origin auth boundary.
        self.session.trust_env = False

    def close(self) -> None:
        self.session.close()

    def uri(self, key: str) -> str:
        raise L1Error("QUERY_REJECTED")

    def read_bytes(self, key: str) -> bytes:
        validate_lake_object_key(key)
        current = key == current_l1_pointer_key()
        manifest = key.startswith(("lake/manifests/releases/l1/", "lake/manifests/datasets/"))
        parquet = key.startswith("lake/l1/canonical/") and key.endswith(".parquet")
        if not (current or (manifest and key.endswith(".json")) or parquet):
            raise L1Error("INVALID_ARGUMENT")
        cached = mirror_path(self.root, key)
        # Immutable manifest bytes are still checked by the existing resolver on every use.
        if manifest and cached.is_file():
            return cached.read_bytes()
        if self.gets >= self.limits.process_gets:
            raise L1Error("TRANSFER_BUDGET_EXCEEDED")
        remaining = self.limits.process_bytes - self.transferred_bytes
        if remaining <= 0:
            raise L1Error("TRANSFER_BUDGET_EXCEEDED")
        cap = min(remaining, self.limits.query_bytes if parquet else MAX_LAKE_JSON_BYTES)
        url = ORIGIN + ("/api/lake/current" if current else "/api/lake/object")
        self.gets += 1
        started = time.monotonic()
        try:
            with self.session.get(
                url,
                params=None if current else {"key": key},
                headers={"Authorization": f"Bearer {self.token}", "Accept-Encoding": "identity"},
                timeout=(5, self.limits.http_seconds),
                stream=True,
                allow_redirects=False,
            ) as response:
                if response.status_code == 404:
                    raise L1Error("RELEASE_UNAVAILABLE")
                if response.status_code != 200:
                    raise L1Error("UPSTREAM_UNAVAILABLE")
                length = response.headers.get("Content-Length")
                if length is not None and int(length) > cap:
                    raise L1Error("TRANSFER_BUDGET_EXCEEDED")
                payload = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    self.transferred_bytes += len(chunk)
                    if len(payload) + len(chunk) > cap:
                        raise L1Error("TRANSFER_BUDGET_EXCEEDED")
                    if time.monotonic() - started > self.limits.http_seconds:
                        raise L1Error("UPSTREAM_UNAVAILABLE")
                    payload.extend(chunk)
        except (requests.RequestException, ValueError):
            raise L1Error("UPSTREAM_UNAVAILABLE") from None
        raw = bytes(payload)
        if manifest:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(raw)
        return raw
