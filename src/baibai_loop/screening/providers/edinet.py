from __future__ import annotations

import json
import time
from collections.abc import Mapping
from datetime import date, timedelta
from math import isfinite
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from pydantic import ConfigDict, field_validator
from pydantic.dataclasses import dataclass

from ..schema import TTMQuality, normalize_ticker

EDINET_API_BASE = "https://api.edinet-fsa.go.jp/api/v2"


_MODEL_CONFIG = ConfigDict(strict=True, arbitrary_types_allowed=False)


class EDINETProviderError(RuntimeError):
    """Raised when EDINET access or normalization fails."""


def _validate_finite(value: float | None) -> float | None:
    if value is not None and not isfinite(value):
        raise ValueError("numeric values must be finite")
    return value


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class EdinetMetricRecord:
    ticker: str
    sales_ttm: float | None
    ocf_ttm: float | None
    debt: float | None
    cash: float | None
    ebitda_ttm: float | None
    consolidation_basis: str | None
    ttm_quality_ev_ebitda: TTMQuality
    ttm_quality_p_s: TTMQuality
    ttm_quality_pcfr: TTMQuality

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker_field(cls, value: str) -> str:
        return normalize_ticker(value)

    @field_validator("sales_ttm", "ocf_ttm", "debt", "cash", "ebitda_ttm")
    @classmethod
    def _finite_numeric_fields(cls, value: float | None) -> float | None:
        return _validate_finite(value)


class EDINETProvider:
    """API v2 client. Subscription-Key is always passed as a query parameter."""

    def __init__(
        self, api_key: str, cache_dir: Path, session: requests.Session | None = None
    ) -> None:
        self._api_key = api_key
        self._cache_dir = Path(cache_dir) / "edinet"
        self._session = session or requests.Session()

    def list_documents(self, on_date: date) -> list[dict[str, Any]]:
        cache_path = self._cache_dir / "documents" / f"{on_date.isoformat()}.json"
        if cache_path.exists():
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise EDINETProviderError("cached EDINET document payload must be a list")
            return [dict(item) for item in payload if isinstance(item, Mapping)]

        query = urlencode(
            {
                "date": on_date.isoformat(),
                "type": 2,
                "Subscription-Key": self._api_key,
            }
        )
        url = f"{EDINET_API_BASE}/documents.json?{query}"
        data = self._request_json(url)
        results = data.get("results", [])
        if not isinstance(results, list):
            raise EDINETProviderError("EDINET documents.json returned unexpected results payload")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        return [dict(item) for item in results]

    def load_metric_records(self, asof_date: date) -> dict[str, EdinetMetricRecord]:
        cache_path = self._cache_dir / "metrics" / f"{asof_date.isoformat()}.json"
        if not cache_path.exists():
            return {}
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise EDINETProviderError("cached EDINET metric payload must be a list")
        return {
            record.ticker: record for record in (normalize_metric_record(item) for item in payload)
        }

    def bootstrap_cache(self, start: date, end: date) -> dict[str, int]:
        total = 0
        cursor = start
        while cursor <= end:
            total += len(self.list_documents(cursor))
            cursor += timedelta(days=1)
        return {"documents": total}

    def _request_json(self, url: str) -> dict[str, Any]:
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                response = self._session.get(url, timeout=30)
            except requests.RequestException as exc:
                # 例外文字列には URL がそのまま乗ることが多く、URL に Subscription-Key が
                # 載っている設計制約上、サニタイズしてから新しい例外で投げ直す。
                # `from None` で原因チェーンも切って traceback 経由の漏洩も遮断する。
                message = self._sanitize_secret(str(exc))
                raise EDINETProviderError(
                    f"EDINET request raised: {type(exc).__name__}: {message}"
                ) from None
            if response.status_code < 400:
                payload = response.json()
                if not isinstance(payload, dict):
                    raise EDINETProviderError("EDINET response JSON must be an object")
                return payload
            if response.status_code not in {429, 500, 502, 503, 504}:
                raise EDINETProviderError(
                    f"EDINET request failed with status {response.status_code}"
                )
            # 最終試行後は sleep しない (無駄な 12s を消費するだけなので)。
            if attempt < max_attempts - 1:
                time.sleep(3 * (2**attempt))
        raise EDINETProviderError("EDINET request failed after retries")

    def _sanitize_secret(self, text: str) -> str:
        if self._api_key and self._api_key in text:
            return text.replace(self._api_key, "<redacted>")
        return text


def normalize_metric_record(record: Mapping[str, Any]) -> EdinetMetricRecord:
    # `or` チェーンは 0.0 (cash=0、debt=0 等の正当な値) を falsy 扱いするため、
    # キー存在ベースの coalesce で最初の非 None 値を取る。
    return EdinetMetricRecord(
        ticker=parse_sec_code(_coalesce(record, "secCode", "ticker", "code")),
        sales_ttm=_to_float(_coalesce(record, "sales_ttm", "SalesTTM")),
        ocf_ttm=_to_float(_coalesce(record, "ocf_ttm", "OperatingCashFlowTTM")),
        debt=_to_float(_coalesce(record, "debt", "Debt")),
        cash=_to_float(_coalesce(record, "cash", "Cash")),
        ebitda_ttm=_to_float(_coalesce(record, "ebitda_ttm", "EBITDATTM")),
        consolidation_basis=_coalesce(record, "consolidation_basis", "ConsolidationBasis"),
        ttm_quality_ev_ebitda=_parse_ttm_quality(
            _coalesce(record, "ttm_quality_ev_ebitda", "TTMQualityEvEbitda")
        ),
        ttm_quality_p_s=_parse_ttm_quality(_coalesce(record, "ttm_quality_p_s", "TTMQualityPS")),
        ttm_quality_pcfr=_parse_ttm_quality(
            _coalesce(record, "ttm_quality_pcfr", "TTMQualityPCFR")
        ),
    )


def _coalesce(record: Mapping[str, Any], *keys: str) -> Any:
    """Return first key with a non-None/non-empty value, preserving legitimate zeros."""
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def parse_sec_code(sec_code: Any) -> str:
    raw = str(sec_code or "").strip().upper()
    if len(raw) == 4:
        return normalize_ticker(raw)
    if len(raw) != 5:
        raise EDINETProviderError(f"invalid EDINET secCode: {sec_code!r}")
    if raw[-1] != "0":
        raise EDINETProviderError(f"unsupported EDINET secCode suffix: {sec_code!r}")
    return normalize_ticker(raw[:4])


def _parse_ttm_quality(value: Any) -> TTMQuality:
    raw = str(value or TTMQuality.UNAVAILABLE.value)
    try:
        return TTMQuality(raw)
    except ValueError:
        return TTMQuality.UNAVAILABLE


def _to_float(value: Any) -> float | None:
    if value in (None, "", "-", "null"):
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None
