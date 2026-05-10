from __future__ import annotations

import csv
import io
import re
import time
import zipfile
from collections.abc import Mapping, Sequence
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


TARGET_DOC_TYPE_CODES = frozenset({"120", "130", "140", "150", "160", "170"})
CORRECTION_DOC_TYPE_CODES = frozenset({"130", "150", "170"})
_DOCUMENT_DESCRIPTION_PERIOD_RE = re.compile(
    r"(\d{4}/\d{2}/\d{2})\s*[－~～-]\s*(\d{4}/\d{2}/\d{2})"
)
_DOC_ID_RE = re.compile(r"S[0-9A-Z]{3,32}")


def _validate_finite(value: float | None) -> float | None:
    if value is not None and not isfinite(value):
        raise ValueError("numeric values must be finite")
    return value


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class EdinetMetricRecord:
    ticker: str
    sales_ttm: float | None = None
    ocf_ttm: float | None = None
    debt: float | None = None
    cash: float | None = None
    ebitda_ttm: float | None = None
    consolidation_basis: str | None = None
    ttm_quality_ev_ebitda: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_p_s: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_pcfr: TTMQuality = TTMQuality.UNAVAILABLE
    operating_profit_ttm: float | None = None
    depreciation_and_amortization_ttm: float | None = None
    capex_ttm: float | None = None
    fcf_ttm: float | None = None
    net_cash: float | None = None
    equity: float | None = None
    total_assets: float | None = None
    ttm_quality_fcf: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_net_cash: TTMQuality = TTMQuality.UNAVAILABLE
    source_doc_id: str | None = None
    document_type: str | None = None
    source_submit_datetime: str | None = None
    source_period_start: date | None = None
    source_period_end: date | None = None
    capex_source: str | None = None
    failure_reasons: tuple[str, ...] = ()

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker_field(cls, value: str) -> str:
        return normalize_ticker(value)

    @field_validator(
        "sales_ttm",
        "ocf_ttm",
        "debt",
        "cash",
        "ebitda_ttm",
        "operating_profit_ttm",
        "depreciation_and_amortization_ttm",
        "capex_ttm",
        "fcf_ttm",
        "net_cash",
        "equity",
        "total_assets",
    )
    @classmethod
    def _finite_numeric_fields(cls, value: float | None) -> float | None:
        return _validate_finite(value)

    @field_validator("failure_reasons", mode="before")
    @classmethod
    def _tuple_failure_reasons(
        cls, value: list[str] | tuple[str, ...] | str | None
    ) -> tuple[str, ...]:
        if isinstance(value, str):
            return (value,)
        return tuple(value or ())


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class EdinetDocumentCandidate:
    ticker: str
    doc_id: str
    doc_type_code: str
    submit_datetime: str | None = None
    period_start: date | None = None
    period_end: date | None = None

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker_field(cls, value: str) -> str:
        return normalize_ticker(value)


class EDINETProvider:
    """API v2 client. Subscription-Key is always passed as a query parameter."""

    def __init__(
        self,
        api_key: str | None,
        cache_dir: Path,
        session: requests.Session | None = None,
        *,
        sqlite_path: Path | None = None,
        cache_only: bool = False,
    ) -> None:
        self._api_key = api_key
        self._cache_dir = Path(cache_dir) / "edinet"
        self._session = session or requests.Session()
        self._sqlite_path = Path(sqlite_path) if sqlite_path is not None else None
        self._cache_only = cache_only
        self._zip_cache_dir = self._cache_dir / "csv_zips"

    def list_documents(self, on_date: date) -> list[dict[str, Any]]:
        if self._sqlite_path is not None:
            from ..sqlite_reader import read_edinet_documents

            cached = read_edinet_documents(self._sqlite_path, on_date)
            if cached is not None:
                return cached
        self._raise_if_cache_only("edinet_documents", on_date.isoformat())
        api_key = self._require_api_key("list_documents")
        query = urlencode(
            {
                "date": on_date.isoformat(),
                "type": 2,
                "Subscription-Key": api_key,
            }
        )
        url = f"{EDINET_API_BASE}/documents.json?{query}"
        data = self._request_json(url)
        results = data.get("results", [])
        if not isinstance(results, list):
            raise EDINETProviderError("EDINET documents.json returned unexpected results payload")
        if self._sqlite_path is not None:
            from ..sqlite_cache import store_edinet_documents

            store_edinet_documents(
                self._sqlite_path,
                on_date,
                _coerce_document_items(results, source="EDINET documents.json"),
            )
        return _coerce_document_items(results, source="EDINET documents.json")

    def load_metric_records(self, asof_date: date) -> dict[str, EdinetMetricRecord]:
        if self._sqlite_path is not None:
            from ..sqlite_reader import read_edinet_metrics

            cached = read_edinet_metrics(self._sqlite_path, asof_date)
            if cached is not None:
                return dict(cached)
        if self._cache_only:
            return {}
        return {}

    def bootstrap_cache(self, start: date, end: date) -> dict[str, int]:
        total = 0
        cursor = start
        while cursor <= end:
            total += len(self.list_documents(cursor))
            cursor += timedelta(days=1)
        return {"documents": total}

    def download_csv_zip(self, doc_id: str) -> bytes:
        safe_doc_id = parse_doc_id(doc_id)
        cache_path = self._zip_cache_dir / f"{safe_doc_id}.zip"
        if cache_path.exists():
            return cache_path.read_bytes()
        self._raise_if_cache_only("edinet_csv_zip", safe_doc_id)

        api_key = self._require_api_key("download_csv_zip")
        query = urlencode({"type": 5, "Subscription-Key": api_key})
        url = f"{EDINET_API_BASE}/documents/{safe_doc_id}?{query}"
        content = self._request_bytes(url)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(content)
        return content

    def _require_api_key(self, operation: str) -> str:
        if not self._api_key:
            raise EDINETProviderError(f"EDINET_API_KEY is required for {operation}")
        return self._api_key

    def _raise_if_cache_only(self, source: str, requirement: str) -> None:
        if not self._cache_only:
            return
        sqlite_label = self._sqlite_path.as_posix() if self._sqlite_path is not None else "<none>"
        raise EDINETProviderError(
            f"SQLite cache incomplete for {source} ({requirement}); "
            f"sqlite={sqlite_label}. `screening run` is cache-only: run "
            "`bootstrap-cache --asof` / `extract-edinet-metrics` or repair SQLite "
            "before running screening."
        )

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

    def _request_bytes(self, url: str) -> bytes:
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                response = self._session.get(url, timeout=60)
            except requests.RequestException as exc:
                message = self._sanitize_secret(str(exc))
                raise EDINETProviderError(
                    f"EDINET request raised: {type(exc).__name__}: {message}"
                ) from None
            if response.status_code < 400:
                return bytes(response.content)
            if response.status_code not in {429, 500, 502, 503, 504}:
                raise EDINETProviderError(
                    f"EDINET request failed with status {response.status_code}"
                )
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
        operating_profit_ttm=_to_float(_coalesce(record, "operating_profit_ttm")),
        depreciation_and_amortization_ttm=_to_float(
            _coalesce(record, "depreciation_and_amortization_ttm")
        ),
        capex_ttm=_to_float(_coalesce(record, "capex_ttm")),
        fcf_ttm=_to_float(_coalesce(record, "fcf_ttm")),
        net_cash=_to_float(_coalesce(record, "net_cash")),
        equity=_to_float(_coalesce(record, "equity")),
        total_assets=_to_float(_coalesce(record, "total_assets")),
        ttm_quality_fcf=_parse_ttm_quality(_coalesce(record, "ttm_quality_fcf")),
        ttm_quality_net_cash=_parse_ttm_quality(_coalesce(record, "ttm_quality_net_cash")),
        source_doc_id=_to_str_or_none(_coalesce(record, "source_doc_id")),
        document_type=_to_str_or_none(_coalesce(record, "document_type")),
        source_submit_datetime=_to_str_or_none(_coalesce(record, "source_submit_datetime")),
        source_period_start=_parse_optional_date(_coalesce(record, "source_period_start")),
        source_period_end=_parse_optional_date(_coalesce(record, "source_period_end")),
        capex_source=_to_str_or_none(_coalesce(record, "capex_source")),
        failure_reasons=tuple(_coalesce(record, "failure_reasons") or ()),
    )


def parse_doc_id(value: object) -> str:
    raw = str(value or "").strip().upper()
    if not _DOC_ID_RE.fullmatch(raw):
        raise EDINETProviderError(f"invalid EDINET docID: {_safe_error_value(value)}")
    return raw


def select_document_candidates(
    documents: Sequence[Mapping[str, Any]],
) -> dict[str, EdinetDocumentCandidate]:
    """Select the latest usable EDINET CSV-capable filing per ticker."""
    candidates: dict[str, EdinetDocumentCandidate] = {}
    for document in documents:
        raw_doc_id = _coalesce(document, "docID", "doc_id")
        raw_type = _to_str_or_none(_coalesce(document, "docTypeCode", "doc_type_code"))
        if raw_doc_id is None or raw_type not in TARGET_DOC_TYPE_CODES:
            continue
        if _to_str_or_none(_coalesce(document, "csvFlag", "csv_flag")) != "1":
            continue
        if _to_str_or_none(_coalesce(document, "xbrlFlag", "xbrl_flag")) != "1":
            continue
        if _is_unusable_status(document):
            continue
        doc_id = parse_doc_id(raw_doc_id)
        ticker = parse_sec_code(_coalesce(document, "secCode", "sec_code"))
        period_start, period_end = _document_period(document)
        candidate = EdinetDocumentCandidate(
            ticker=ticker,
            doc_id=doc_id,
            doc_type_code=raw_type,
            submit_datetime=_to_str_or_none(_coalesce(document, "submitDateTime")),
            period_start=period_start,
            period_end=period_end,
        )
        current = candidates.get(ticker)
        if current is None or _document_sort_key(candidate) > _document_sort_key(current):
            candidates[ticker] = candidate
    return candidates


def parse_csv_zip_metric_record(
    *,
    ticker: str,
    doc_id: str,
    doc_type_code: str,
    content: bytes,
    submit_datetime: str | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
) -> EdinetMetricRecord:
    rows = _read_csv_zip_rows(content)
    if not rows:
        return EdinetMetricRecord(
            ticker=ticker,
            source_doc_id=doc_id,
            document_type=doc_type_code,
            source_submit_datetime=submit_datetime,
            source_period_start=period_start,
            source_period_end=period_end,
            failure_reasons=("csv_parse_failed",),
        )

    quality = TTMQuality.EXACT if doc_type_code in {"120", "130"} else TTMQuality.APPROXIMATED
    basis = _detect_consolidation_basis(rows)
    failures: list[str] = []
    if basis == "non_consolidated":
        failures.append("non_consolidated_fallback")

    sales = _single_metric(rows, _TAGS["sales"], basis=basis)
    ocf = _single_metric(rows, _TAGS["ocf"], basis=basis)
    operating_profit = _single_metric(rows, _TAGS["operating_profit"], basis=basis)
    cash = _single_metric(rows, _TAGS["cash"], basis=basis)
    equity = _single_metric(rows, _TAGS["equity"], basis=basis)
    total_assets = _single_metric(rows, _TAGS["total_assets"], basis=basis)
    debt = _debt_metric(rows, basis=basis)
    depreciation = _sum_metric(rows, _TAGS["depreciation"], basis=basis)
    capex = _sum_metric(rows, _TAGS["capex"], basis=basis)
    capex_abs = abs(capex) if capex is not None else None
    ebitda = (
        operating_profit + depreciation
        if operating_profit is not None and depreciation is not None
        else None
    )
    fcf = ocf - capex_abs if ocf is not None and capex_abs is not None else None
    debt_assumed_zero = debt is None and cash is not None
    net_cash = cash - debt if cash is not None and debt is not None else None

    for name, value in (
        ("sales", sales),
        ("ocf", ocf),
        ("cash", cash),
        ("capex", capex_abs),
    ):
        if value is None:
            failures.append(f"tag_not_found:{name}")
    if debt_assumed_zero:
        failures.append("debt_assumed_zero")
    elif debt is None:
        failures.append("tag_not_found:debt")

    return EdinetMetricRecord(
        ticker=ticker,
        sales_ttm=sales,
        ocf_ttm=ocf,
        debt=debt,
        cash=cash,
        ebitda_ttm=ebitda,
        consolidation_basis=basis,
        ttm_quality_ev_ebitda=quality if ebitda is not None else TTMQuality.UNAVAILABLE,
        ttm_quality_p_s=quality if sales is not None else TTMQuality.UNAVAILABLE,
        ttm_quality_pcfr=quality if ocf is not None else TTMQuality.UNAVAILABLE,
        operating_profit_ttm=operating_profit,
        depreciation_and_amortization_ttm=depreciation,
        capex_ttm=capex_abs,
        fcf_ttm=fcf,
        net_cash=net_cash,
        equity=equity,
        total_assets=total_assets,
        ttm_quality_fcf=quality if fcf is not None else TTMQuality.UNAVAILABLE,
        ttm_quality_net_cash=quality if net_cash is not None else TTMQuality.UNAVAILABLE,
        source_doc_id=doc_id,
        document_type=doc_type_code,
        source_submit_datetime=submit_datetime,
        source_period_start=period_start,
        source_period_end=period_end,
        capex_source="purchase_of_fixed_assets" if capex_abs is not None else None,
        failure_reasons=tuple(dict.fromkeys(failures)),
    )


def _coerce_document_items(payload: list[Any], *, source: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise EDINETProviderError(f"{source} contained a non-mapping document item")
        items.append(dict(item))
    return items


def _coalesce(record: Mapping[str, Any], *keys: str) -> Any:
    """Return first key with a non-None/non-empty value, preserving legitimate zeros."""
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def _to_str_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _is_unusable_status(document: Mapping[str, Any]) -> bool:
    legal_status = _to_str_or_none(_coalesce(document, "legalStatus", "legal_status"))
    disclosure_status = _to_str_or_none(
        _coalesce(document, "disclosureStatus", "disclosure_status")
    )
    withdrawal_status = _to_str_or_none(
        _coalesce(document, "withdrawalStatus", "withdrawal_status")
    )
    # EDINET document list uses legalStatus="1", disclosureStatus="0",
    # withdrawalStatus="0" for normal usable filings. Treat unknown/missing
    # status as usable because older cached payloads may not have all fields.
    return (
        legal_status not in (None, "1")
        or disclosure_status not in (None, "0")
        or withdrawal_status not in (None, "0")
    )


def _document_sort_key(
    candidate: EdinetDocumentCandidate,
) -> tuple[int, date, int, date, str, int, str]:
    correction = 1 if candidate.doc_type_code in CORRECTION_DOC_TYPE_CODES else 0
    return (
        1 if candidate.period_end is not None else 0,
        candidate.period_end or date.min,
        1 if candidate.period_start is not None else 0,
        candidate.period_start or date.min,
        candidate.submit_datetime or "",
        correction,
        candidate.doc_id,
    )


def _document_period(document: Mapping[str, Any]) -> tuple[date | None, date | None]:
    start = _parse_optional_date(_coalesce(document, "periodStart", "period_start"))
    end = _parse_optional_date(_coalesce(document, "periodEnd", "period_end"))
    if start is not None and end is not None:
        return start, end
    description = _to_str_or_none(_coalesce(document, "docDescription", "doc_description"))
    fallback_start, fallback_end = _parse_document_description_period(description)
    return start or fallback_start, end or fallback_end


def _parse_document_description_period(value: str | None) -> tuple[date | None, date | None]:
    if not value:
        return None, None
    match = _DOCUMENT_DESCRIPTION_PERIOD_RE.search(value)
    if match is None:
        return None, None
    return (
        _parse_slash_date(match.group(1)),
        _parse_slash_date(match.group(2)),
    )


def _parse_slash_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.replace("/", "-"))
    except ValueError:
        return None


_TAGS: dict[str, tuple[str, ...]] = {
    "sales": ("netsales", "revenuesfromexternalcustomers"),
    "ocf": ("cashflowsfromoperatingactivities", "netcashprovidedbyusedinoperatingactivities"),
    "operating_profit": ("operatingprofit", "operatingincome"),
    "cash": ("cashanddeposits", "cashandcashequivalents"),
    "equity": ("equity", "totalequity", "netassets"),
    "total_assets": ("totalassets", "assets"),
    "debt_total": (
        "interestbearingdebt",
        "interestbearingliabilities",
    ),
    "debt_components": (
        "shorttermborrowings",
        "shorttermloanspayable",
        "currentportionoflongtermborrowings",
        "currentportionoflongtermloanspayable",
        "currentportionofbonds",
        "currentportionofbondspayable",
        "bondspayable",
        "longtermborrowings",
        "longtermloanspayable",
        "leaseobligations",
        "leaseobligationscl",
        "leaseobligationsncl",
    ),
    "depreciation": (
        "depreciationandamortization",
        "depreciationandamortizationopecf",
        "depreciation",
        "amortizationofgoodwill",
        "amortizationofgoodwillopecf",
    ),
    "capex": (
        "purchaseofpropertyplantandequipment",
        "purchaseofpropertyplantandequipmentinvcf",
        "purchaseofintangibleassets",
        "purchaseofintangibleassetsinvcf",
        "paymentsforpurchaseofpropertyplantandequipment",
        "paymentsforpurchaseofintangibleassets",
    ),
}


def _read_csv_zip_rows(content: bytes) -> list[dict[str, str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise EDINETProviderError("zip_invalid") from exc
    rows: list[dict[str, str]] = []
    for name in archive.namelist():
        if not name.lower().endswith(".csv"):
            continue
        with archive.open(name) as handle:
            raw = handle.read()
        text = _decode_csv(raw)
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        rows.extend(dict(row) for row in reader if row)
    return rows


def _decode_csv(content: bytes) -> str:
    for encoding in ("utf-16", "utf-16-le", "utf-8-sig", "cp932"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise EDINETProviderError("csv_parse_failed")


def _detect_consolidation_basis(rows: Sequence[Mapping[str, str]]) -> str:
    if any(_is_consolidated_row(row) for row in rows):
        return "consolidated"
    return "non_consolidated"


def _single_metric(
    rows: Sequence[Mapping[str, str]],
    tags: tuple[str, ...],
    *,
    basis: str,
) -> float | None:
    values = _ranked_metric_values(rows, tags, basis=basis)
    return max(values, key=lambda item: item[0])[1] if values else None


def _sum_metric(
    rows: Sequence[Mapping[str, str]],
    tags: tuple[str, ...],
    *,
    basis: str,
) -> float | None:
    values = _best_metric_values_by_element(rows, tags, basis=basis)
    return sum(values) if values else None


def _debt_metric(rows: Sequence[Mapping[str, str]], *, basis: str) -> float | None:
    total = _single_metric(rows, _TAGS["debt_total"], basis=basis)
    if total is not None:
        return total
    components = _sum_metric(rows, _TAGS["debt_components"], basis=basis)
    if components is not None:
        return components
    if _has_zero_like_metric(rows, (*_TAGS["debt_total"], *_TAGS["debt_components"]), basis=basis):
        return 0.0
    return None


def _best_metric_values_by_element(
    rows: Sequence[Mapping[str, str]],
    tags: tuple[str, ...],
    *,
    basis: str,
) -> list[float]:
    best_by_element: dict[str, tuple[tuple[int, int, str], float]] = {}
    for rank, value, element in _ranked_metric_values(rows, tags, basis=basis):
        current = best_by_element.get(element)
        if current is None or rank > current[0]:
            best_by_element[element] = (rank, value)
    return [value for _, value in best_by_element.values()]


def _ranked_metric_values(
    rows: Sequence[Mapping[str, str]],
    tags: tuple[str, ...],
    *,
    basis: str,
) -> list[tuple[tuple[int, int, str], float, str]]:
    values: list[tuple[tuple[int, int, str], float, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        raw_element = _row_element(row)
        element = _normalize_element(raw_element)
        if not _element_matches(raw_element, tags):
            continue
        context = _row_context(row)
        if basis == "consolidated" and not _is_consolidated_row(row):
            continue
        if basis == "non_consolidated" and _is_consolidated_row(row):
            continue
        value = _to_float(_row_value(row))
        if value is None:
            continue
        key = (element, context, _row_basis(row))
        if key in seen:
            continue
        seen.add(key)
        values.append((_context_rank(context), value, element))
    return values


def _has_zero_like_metric(
    rows: Sequence[Mapping[str, str]],
    tags: tuple[str, ...],
    *,
    basis: str,
) -> bool:
    found = False
    for row in rows:
        raw_element = _row_element(row)
        if not _element_matches(raw_element, tags):
            continue
        if basis == "consolidated" and not _is_consolidated_row(row):
            continue
        if basis == "non_consolidated" and _is_consolidated_row(row):
            continue
        found = True
        if not _is_zero_like(_row_value(row)):
            return False
    return found


def _row_element(row: Mapping[str, str]) -> str:
    return str(_coalesce(row, "要素ID", "element_id", "ElementID", "elementId") or "")


def _row_context(row: Mapping[str, str]) -> str:
    return str(_coalesce(row, "コンテキストID", "context_id", "ContextID", "contextId") or "")


def _row_basis(row: Mapping[str, str]) -> str:
    return str(_coalesce(row, "連結・個別", "consolidation_basis", "ConsolidationBasis") or "")


def _row_value(row: Mapping[str, str]) -> str | None:
    value = _coalesce(row, "値", "value", "Value", "金額")
    return str(value) if value is not None else None


def _is_zero_like(value: str | None) -> bool:
    return str(value or "").strip().replace(",", "") in {"0", "0.0", "-", "－", "―", "–"}


def _normalize_element(value: str) -> str:
    return "".join(char for char in value.lower() if char.isalnum())


def _element_matches(element: str, tags: tuple[str, ...]) -> bool:
    local_name = _normalize_element(element.rsplit(":", 1)[-1])
    return local_name in tags


def _is_consolidated_context(context: str) -> bool:
    lowered = context.lower()
    return "consolidated" in lowered and "nonconsolidated" not in lowered


def _is_consolidated_row(row: Mapping[str, str]) -> bool:
    return "連結" in _row_basis(row) or _is_consolidated_context(_row_context(row))


def _context_rank(context: str) -> tuple[int, int, str]:
    lowered = context.lower()
    current = 0 if "prior" in lowered else 1
    primary = 0 if "_" in context or "member" in lowered else 1
    return (current, primary, context)


def parse_sec_code(sec_code: Any) -> str:
    raw = str(sec_code or "").strip().upper()
    if len(raw) == 4:
        return normalize_ticker(raw)
    if len(raw) != 5:
        raise EDINETProviderError(f"invalid EDINET secCode: {_safe_error_value(sec_code)}")
    if raw[-1] != "0":
        raise EDINETProviderError(
            f"unsupported EDINET secCode suffix: {_safe_error_value(sec_code)}"
        )
    return normalize_ticker(raw[:4])


def _safe_error_value(value: object, *, max_length: int = 80) -> str:
    text = str(value)
    sanitized = "".join(
        char if char.isprintable() and char not in "\r\n\t" else "?" for char in text
    )
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "..."
    return repr(sanitized)


def _parse_ttm_quality(value: Any) -> TTMQuality:
    raw = str(value or TTMQuality.UNAVAILABLE.value)
    try:
        return TTMQuality(raw)
    except ValueError:
        return TTMQuality.UNAVAILABLE


def _parse_optional_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    if value in (None, "", "-", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
