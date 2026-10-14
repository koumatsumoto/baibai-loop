"""Research用の原典XBRL取得。metric抽出と別の意味revisionで管理する。"""

from __future__ import annotations

from urllib.parse import urlencode

from baibai_engine.market.providers.edinet import (
    EDINET_API_BASE,
    EDINETProvider,
    EDINETProviderError,
    _is_zip_bytes,
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
        content = self._request_bytes(f"{EDINET_API_BASE}/documents/{safe_id}?{query}")
        if not _is_zip_bytes(content):
            raise EDINETProviderError("EDINET XBRL response is not a ZIP")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(content)
        return content
