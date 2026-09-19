"""Original XBRL download, isolated from the screening metric revision closure."""

from __future__ import annotations

import time
from urllib.parse import urlencode

from .edinet import (
    EDINET_API_BASE,
    EDINETProvider,
    EDINETProviderError,
    EDINETRateLimitError,
    _is_zip_bytes,
    _non_zip_response_status,
    parse_doc_id,
)


class EDINETFactsProvider(EDINETProvider):
    def download_xbrl_zip(self, doc_id: str) -> bytes:
        safe_id = parse_doc_id(doc_id)
        cache = self._cache_dir / "xbrl_zips" / f"{safe_id}.zip"
        if cache.exists():
            content = cache.read_bytes()
            if _is_zip_bytes(content):
                return content
            cache.unlink()
        self._raise_if_cache_only("edinet_xbrl_zip", safe_id)
        query = urlencode(
            {"type": 1, "Subscription-Key": self._require_api_key("download_xbrl_zip")}
        )
        for attempt in range(3):
            content = self._request_bytes(f"{EDINET_API_BASE}/documents/{safe_id}?{query}")
            if _is_zip_bytes(content):
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_bytes(content)
                return content
            if _non_zip_response_status(content) != "429":
                raise EDINETProviderError("EDINET XBRL response is not a ZIP")
            if attempt < 2:
                time.sleep(3 * 2**attempt)
        raise EDINETRateLimitError("EDINET XBRL response was rate limited after retries")
