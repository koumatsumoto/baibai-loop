from __future__ import annotations

import hashlib
import io
import json
import re
import time
import zipfile
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from pydantic import ConfigDict, field_validator
from pydantic.dataclasses import dataclass

from baibai_engine.market.edinet_decode import (
    coalesce_field,
    parse_optional_date,
    parse_sec_code,
    safe_error_value,
    text_or_none,
)
from baibai_engine.market.edinet_models import (
    EdinetMetricRecord,
    EDINETProviderError,
    EDINETRateLimitError,
)
from baibai_engine.market.ticker import normalize_ticker

EDINET_API_BASE = "https://api.edinet-fsa.go.jp/api/v2"


_MODEL_CONFIG = ConfigDict(strict=True, arbitrary_types_allowed=False)


TARGET_DOC_TYPE_CODES = frozenset({"120", "130", "140", "150", "160", "170"})
CORRECTION_DOC_TYPE_CODES = frozenset({"130", "150", "170"})
_DOCUMENT_DESCRIPTION_PERIOD_RE = re.compile(
    r"(\d{4}/\d{2}/\d{2})\s*[－~～-]\s*(\d{4}/\d{2}/\d{2})"
)
_DOC_ID_RE = re.compile(r"S[0-9A-Z]{3,32}")


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class EdinetDocumentCandidate:
    ticker: str
    doc_id: str
    doc_type_code: str
    submit_datetime: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    source_document_revision: str | None = None

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker_field(cls, value: str) -> str:
        return normalize_ticker(value)


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class QuarantinedDocumentEvent:
    """An operation row whose origin filing cannot be resolved in the input window."""

    doc_id: str
    target_doc_id: str | None
    event_type: str
    document_type: str | None = None
    ticker: str | None = None


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class DocumentSelection:
    """Selected filings plus operation rows quarantined without an origin filing.

    EDINET lists an edit or withdrawal on the day it happens, so an operation on
    a filing older than the lookback window arrives with its origin absent. Such
    an event cannot safely mutate a candidate. The structured quarantine keeps
    its identity and any usable ticker visible so extraction can fail-close that
    ticker without letting one external inconsistency stop the whole batch.
    """

    candidates: dict[str, EdinetDocumentCandidate]
    quarantined_events: tuple[QuarantinedDocumentEvent, ...] = ()


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

    def list_documents(
        self,
        on_date: date,
        *,
        force_refresh: bool = False,
        is_final: bool = False,
    ) -> list[dict[str, Any]]:
        if self._sqlite_path is not None and not force_refresh:
            from baibai_engine.market.edinet_metrics.store import read_edinet_documents

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
        metadata = data.get("metadata")
        if not isinstance(metadata, Mapping):
            raise EDINETProviderError("EDINET documents.json returned no metadata")
        resultset = metadata.get("resultset")
        if not isinstance(resultset, Mapping):
            raise EDINETProviderError("EDINET documents.json returned no resultset metadata")
        raw_result_count = resultset.get("count")
        if raw_result_count is None:
            raise EDINETProviderError("EDINET documents.json returned invalid resultset count")
        try:
            result_count = int(str(raw_result_count))
        except (TypeError, ValueError) as exc:
            raise EDINETProviderError(
                "EDINET documents.json returned invalid resultset count"
            ) from exc
        if result_count != len(results):
            raise EDINETProviderError(
                "EDINET documents.json resultset count mismatch: "
                f"metadata={result_count} results={len(results)}"
            )
        process_datetime = text_or_none(metadata.get("processDateTime"))
        documents = _coerce_document_items(results, source="EDINET documents.json")
        for document in documents:
            document["doc_date"] = on_date.isoformat()
        if self._sqlite_path is not None:
            from baibai_engine.storage.observations.ingest.edinet import store_edinet_documents

            store_edinet_documents(
                self._sqlite_path,
                on_date,
                documents,
                process_datetime=process_datetime,
                result_count=result_count,
                is_final=is_final,
            )
        return documents

    def load_metric_records(self, asof_date: date) -> dict[str, EdinetMetricRecord]:
        if self._sqlite_path is not None:
            from baibai_engine.market.edinet_metrics.store import read_edinet_metrics

            cached = read_edinet_metrics(self._sqlite_path, asof_date)
            if cached is not None:
                return dict(cached)
        return {}

    def bootstrap_cache(self, start: date, end: date) -> dict[str, int]:
        fetched = self._refresh_document_state(end)
        cursor = start
        while cursor <= end:
            if cursor not in fetched:
                fetched[cursor] = len(self.list_documents(cursor, is_final=cursor < end))
            cursor += timedelta(days=1)
        return {"documents": sum(fetched.values())}

    def refresh_document_state(self, asof_date: date) -> dict[str, int]:
        """Refresh the mutable target day, then finalize every older unresolved day."""
        fetched = self._refresh_document_state(asof_date)
        return {"documents": sum(fetched.values())}

    def backfill_document_identity(self, start: date, end: date) -> dict[str, int]:
        """Re-list every day in the window so stored rows carry submitter and target codes.

        The cached rows are otherwise complete, so this bypasses the cache deliberately
        rather than dropping coverage: a day whose coverage were deleted would read as
        an unobserved day to every consumer until the whole window finished.
        """
        days = 0
        documents = 0
        cursor = start
        while cursor <= end:
            documents += len(self.list_documents(cursor, force_refresh=True, is_final=cursor < end))
            days += 1
            cursor += timedelta(days=1)
        return {"days": days, "documents": documents}

    def _refresh_document_state(self, end: date) -> dict[date, int]:
        fetched = {
            end: len(self.list_documents(end, force_refresh=True, is_final=False)),
        }
        if self._sqlite_path is not None:
            from baibai_engine.market.edinet_metrics.store import (
                read_unfinalized_edinet_document_dates,
            )

            for unresolved in read_unfinalized_edinet_document_dates(self._sqlite_path, before=end):
                fetched[unresolved] = len(
                    self.list_documents(unresolved, force_refresh=True, is_final=True)
                )
        return fetched

    def download_csv_zip(self, doc_id: str) -> bytes:
        safe_doc_id = parse_doc_id(doc_id)
        cache_path = self._zip_cache_dir / f"{safe_doc_id}.zip"
        if cache_path.exists():
            cached = cache_path.read_bytes()
            if _is_zip_bytes(cached):
                return cached
            cache_path.unlink(missing_ok=True)
        self._raise_if_cache_only("edinet_csv_zip", safe_doc_id)

        api_key = self._require_api_key("download_csv_zip")
        query = urlencode({"type": 5, "Subscription-Key": api_key})
        url = f"{EDINET_API_BASE}/documents/{safe_doc_id}?{query}"
        content = self._request_bytes(url)
        if not _is_zip_bytes(content):
            raise EDINETProviderError(
                f"EDINET CSV ZIP response was not a zip: {_non_zip_response_message(content)}"
            )
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
        with closing(self._request_response(url, timeout=30)) as response:
            payload = response.json()
        if not isinstance(payload, dict):
            raise EDINETProviderError("EDINET response JSON must be an object")
        return payload

    def _request_bytes(self, url: str) -> bytes:
        with closing(self._request_response(url, timeout=60)) as response:
            return bytes(response.content)

    def _request_response(self, url: str, *, timeout: int) -> requests.Response:
        """One retry budget for HTTP failures and EDINET's HTTP 200 body 429."""
        max_attempts = 6
        for attempt in range(max_attempts):
            try:
                response = self._session.get(url, timeout=timeout)
            except requests.RequestException as exc:
                if attempt < max_attempts - 1:
                    time.sleep(3 * (2**attempt))
                    continue
                message = self._sanitize_secret(str(exc))
                raise EDINETProviderError(
                    f"EDINET request raised: {type(exc).__name__}: {message}"
                ) from None
            rate_limited = response.status_code == 429 or (
                response.status_code == 200 and _non_zip_response_status(response.content) == "429"
            )
            if response.status_code < 400 and not rate_limited:
                return response
            try:
                if rate_limited and attempt >= max_attempts - 1:
                    raise EDINETRateLimitError("EDINET request rate limited after retries")
                if not rate_limited and response.status_code not in {500, 502, 503, 504}:
                    raise EDINETProviderError(
                        f"EDINET request failed with status {response.status_code}"
                    )
                backoff = 3 * (2**attempt)
                wait = _retry_after_seconds(response, backoff) if rate_limited else backoff
            finally:
                response.close()
            if attempt < max_attempts - 1:
                time.sleep(wait)
        raise EDINETProviderError("EDINET request failed after retries")

    def _sanitize_secret(self, text: str) -> str:
        if self._api_key and self._api_key in text:
            return text.replace(self._api_key, "<redacted>")
        return text


def _retry_after_seconds(response: requests.Response, backoff: int) -> float:
    value = response.headers.get("Retry-After", "").strip()
    if value.isdigit():
        return int(value)
    try:
        deadline = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return backoff
    if deadline.tzinfo is None:
        return backoff
    return max(0.0, (deadline - datetime.now(UTC)).total_seconds())


def parse_doc_id(value: object) -> str:
    raw = str(value or "").strip()
    if not raw.isascii() or not _DOC_ID_RE.fullmatch(raw.upper()):
        raise EDINETProviderError(f"invalid EDINET docID: {safe_error_value(value)}")
    return raw.upper()


def select_document_candidates(
    documents: Sequence[Mapping[str, Any]],
    *,
    origin_start: date | None = None,
) -> DocumentSelection:
    """Select the latest usable EDINET CSV-capable filing per ticker."""
    candidates: dict[str, EdinetDocumentCandidate] = {}
    folded, quarantined = _canonicalize_document_events(documents, origin_start=origin_start)
    for document in folded:
        raw_doc_id = coalesce_field(document, "docID", "doc_id")
        raw_type = text_or_none(coalesce_field(document, "docTypeCode", "doc_type_code"))
        if raw_doc_id is None or raw_type not in TARGET_DOC_TYPE_CODES:
            continue
        if text_or_none(coalesce_field(document, "csvFlag", "csv_flag")) != "1":
            continue
        if text_or_none(coalesce_field(document, "xbrlFlag", "xbrl_flag")) != "1":
            continue
        if _is_unusable_status(document):
            continue
        doc_id = parse_doc_id(raw_doc_id)
        raw_sec_code = coalesce_field(document, "secCode", "sec_code")
        if text_or_none(raw_sec_code) is None:
            continue
        ticker = parse_sec_code(raw_sec_code)
        period_start, period_end = _document_period(document)
        candidate = EdinetDocumentCandidate(
            ticker=ticker,
            doc_id=doc_id,
            doc_type_code=raw_type,
            submit_datetime=text_or_none(coalesce_field(document, "submitDateTime")),
            period_start=period_start,
            period_end=period_end,
            source_document_revision=_source_document_revision(document),
        )
        current = candidates.get(ticker)
        if current is None or _document_sort_key(candidate) > _document_sort_key(current):
            candidates[ticker] = candidate
    return DocumentSelection(candidates=candidates, quarantined_events=quarantined)


def _source_document_revision(document: Mapping[str, Any]) -> str:
    """Identify the canonical EDINET document state that drives extraction."""
    field_aliases = {
        "doc_id": ("docID", "doc_id"),
        "security_code": ("secCode", "sec_code"),
        "document_type_code": ("docTypeCode", "doc_type_code"),
        "csv_flag": ("csvFlag", "csv_flag"),
        "xbrl_flag": ("xbrlFlag", "xbrl_flag"),
        "legal_status": ("legalStatus", "legal_status"),
        "disclosure_status": ("disclosureStatus", "disclosure_status"),
        "withdrawal_status": ("withdrawalStatus", "withdrawal_status"),
        "edit_status": ("docInfoEditStatus", "doc_info_edit_status"),
        "parent_doc_id": ("parentDocID", "parent_doc_id"),
        "operation_datetime": ("opeDateTime", "operation_datetime"),
        "submit_datetime": ("submitDateTime", "submit_datetime"),
        "description": ("docDescription", "description"),
        "period_start": ("periodStart", "period_start"),
        "period_end": ("periodEnd", "period_end"),
        "sequence_number": ("seqNumber", "sequence_number"),
    }
    payload = {
        name: next(
            (
                str(document[alias]).strip()
                for alias in aliases
                if document.get(alias) not in (None, "")
            ),
            None,
        )
        for name, aliases in field_aliases.items()
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonicalize_document_events(
    documents: Sequence[Mapping[str, Any]],
    *,
    origin_start: date | None,
) -> tuple[list[dict[str, Any]], tuple[QuarantinedDocumentEvent, ...]]:
    """Fold EDINET operation rows onto origin filings before ticker selection.

    Returns the folded filings and structured operation rows whose origin is not
    in scope. The latter most often happens when a filing older than the lookback
    window is edited or withdrawn inside it.
    """
    normalized = [dict(document) for document in documents]
    normalized.sort(key=_document_event_sort_key)
    origins: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    for document in normalized:
        _validate_document_statuses(document)
        doc_id_raw = coalesce_field(document, "docID", "doc_id")
        if doc_id_raw is None:
            continue
        doc_id = parse_doc_id(doc_id_raw)
        edit_status = text_or_none(
            coalesce_field(document, "docInfoEditStatus", "doc_info_edit_status")
        )
        withdrawal_status = text_or_none(
            coalesce_field(document, "withdrawalStatus", "withdrawal_status")
        )
        disclosure_status = text_or_none(
            coalesce_field(document, "disclosureStatus", "disclosure_status")
        )
        is_event = edit_status == "1" or withdrawal_status == "1" or disclosure_status in {"1", "3"}
        if is_event:
            events.append(document)
            continue
        origin_date = text_or_none(coalesce_field(document, "doc_date"))
        if origin_start is not None and origin_date is not None:
            try:
                if date.fromisoformat(origin_date) < origin_start:
                    continue
            except ValueError as exc:
                raise EDINETProviderError(
                    f"invalid EDINET origin date: {safe_error_value(origin_date)}"
                ) from exc
        origins[doc_id] = document

    children: dict[str, set[str]] = {}
    for doc_id, origin in origins.items():
        parent = text_or_none(coalesce_field(origin, "parentDocID", "parent_doc_id"))
        if parent:
            children.setdefault(parent, set()).add(doc_id)
    _validate_parent_graph(origins)

    quarantined: set[QuarantinedDocumentEvent] = set()
    withdrawn_by_event: set[str] = set()
    for event in events:
        doc_id = parse_doc_id(coalesce_field(event, "docID", "doc_id"))
        edit_status = text_or_none(
            coalesce_field(event, "docInfoEditStatus", "doc_info_edit_status")
        )
        withdrawal_status = text_or_none(
            coalesce_field(event, "withdrawalStatus", "withdrawal_status")
        )
        disclosure_status = text_or_none(
            coalesce_field(event, "disclosureStatus", "disclosure_status")
        )
        if withdrawal_status == "1":
            parent = text_or_none(coalesce_field(event, "parentDocID", "parent_doc_id"))
            if parent is None or parent not in origins:
                quarantined.add(
                    _quarantined_document_event(
                        event,
                        doc_id=doc_id,
                        target_doc_id=parent,
                        event_type="withdrawal",
                    )
                )
                continue
            withdrawn_by_event.update(
                _tombstone_document_tree(parent, origins=origins, children=children)
            )
            continue
        current = origins.get(doc_id)
        if current is None:
            event_type = "edit" if edit_status == "1" else "disclosure"
            quarantined.add(
                _quarantined_document_event(
                    event,
                    doc_id=doc_id,
                    target_doc_id=doc_id,
                    event_type=event_type,
                )
            )
            continue
        if doc_id in withdrawn_by_event:
            raise EDINETProviderError(f"EDINET event follows withdrawal: {doc_id}")
        # Refreshed origin flags describe current state, not the time of an older
        # immutable operation row. Apply its metadata without reviving a tombstone.
        was_withdrawn = (
            text_or_none(coalesce_field(current, "withdrawalStatus", "withdrawal_status")) == "2"
        )
        origin_date = current.get("doc_date")
        old_parent = text_or_none(coalesce_field(current, "parentDocID", "parent_doc_id"))
        # A metadata-bearing edit replaces optional values, including an explicit
        # empty parent. Sparse cached operation rows have NULL placeholders even
        # for type/time/availability; those do not erase the origin metadata.
        has_edit_metadata = edit_status == "1" and all(
            coalesce_field(event, *names) is not None
            for names in (
                ("docTypeCode", "doc_type_code"),
                ("submitDateTime", "submit_datetime"),
                ("csvFlag", "csv_flag"),
                ("xbrlFlag", "xbrl_flag"),
            )
        )
        current.update(
            event
            if has_edit_metadata
            else {key: value for key, value in event.items() if value is not None}
        )
        new_parent = text_or_none(coalesce_field(current, "parentDocID", "parent_doc_id"))
        if new_parent != old_parent:
            if old_parent is not None:
                children[old_parent].discard(doc_id)
            if new_parent is not None:
                children.setdefault(new_parent, set()).add(doc_id)
        if origin_date is not None:
            current["doc_date"] = origin_date
        if was_withdrawn:
            current["withdrawalStatus"] = "2"
            current["legalStatus"] = "0"
        if edit_status == "1":
            current["docInfoEditStatus"] = "2"
        if disclosure_status == "1":
            current["disclosureStatus"] = "2"
        elif disclosure_status == "3":
            current["disclosureStatus"] = "0"
    # Operation rows can replace parent metadata after the initial graph check.
    _validate_parent_graph(origins)
    return list(origins.values()), tuple(
        sorted(
            quarantined,
            key=lambda item: (
                item.doc_id,
                item.event_type,
                item.target_doc_id or "",
                item.ticker or "",
            ),
        )
    )


def _quarantined_document_event(
    event: Mapping[str, Any],
    *,
    doc_id: str,
    target_doc_id: str | None,
    event_type: str,
) -> QuarantinedDocumentEvent:
    raw_sec_code = coalesce_field(event, "secCode", "sec_code")
    ticker: str | None = None
    if text_or_none(raw_sec_code) is not None:
        try:
            ticker = parse_sec_code(raw_sec_code)
        except EDINETProviderError:
            # The event is already quarantined. An unusable security code reduces
            # the isolation scope to the event itself rather than restoring a
            # batch-wide failure for metadata that cannot identify a ticker.
            ticker = None
    document_type = text_or_none(coalesce_field(event, "docTypeCode", "doc_type_code"))
    if document_type is not None and (
        len(document_type) != 3 or not document_type.isascii() or not document_type.isdigit()
    ):
        document_type = None
    return QuarantinedDocumentEvent(
        doc_id=doc_id,
        target_doc_id=target_doc_id,
        event_type=event_type,
        document_type=document_type,
        ticker=ticker,
    )


def _validate_parent_graph(origins: Mapping[str, Mapping[str, Any]]) -> None:
    """Reject cycles even when no withdrawal event traverses the relation."""
    complete: set[str] = set()
    for start in origins:
        path: set[str] = set()
        current = start
        while current in origins and current not in complete:
            if current in path:
                raise EDINETProviderError(f"EDINET parent relation cycle detected: {current}")
            path.add(current)
            parent = text_or_none(coalesce_field(origins[current], "parentDocID", "parent_doc_id"))
            if parent is None:
                break
            current = parent
        complete.update(path)


def _tombstone_document_tree(
    root: str,
    *,
    origins: dict[str, dict[str, Any]],
    children: Mapping[str, set[str]],
) -> set[str]:
    pending = [root]
    seen: set[str] = set()
    while pending:
        doc_id = pending.pop()
        if doc_id in seen:
            raise EDINETProviderError(f"EDINET parent relation cycle detected: {doc_id}")
        seen.add(doc_id)
        origin = origins.get(doc_id)
        if origin is not None:
            origin["withdrawalStatus"] = "2"
            origin["legalStatus"] = "0"
        pending.extend(children.get(doc_id, ()))
    return seen


def _validate_document_statuses(document: Mapping[str, Any]) -> None:
    allowed = {
        "legal": {None, "0", "1", "2"},
        "withdrawal": {None, "0", "1", "2"},
        "edit": {None, "0", "1", "2"},
        "disclosure": {None, "0", "1", "2", "3"},
    }
    values = {
        "legal": text_or_none(coalesce_field(document, "legalStatus", "legal_status")),
        "withdrawal": text_or_none(
            coalesce_field(document, "withdrawalStatus", "withdrawal_status")
        ),
        "edit": text_or_none(coalesce_field(document, "docInfoEditStatus", "doc_info_edit_status")),
        "disclosure": text_or_none(
            coalesce_field(document, "disclosureStatus", "disclosure_status")
        ),
    }
    for name, value in values.items():
        if value not in allowed[name]:
            raise EDINETProviderError(f"unknown EDINET {name} status: {value!r}")


def _document_event_sort_key(document: Mapping[str, Any]) -> tuple[str, str, int]:
    doc_date = text_or_none(coalesce_field(document, "doc_date")) or ""
    operation = text_or_none(coalesce_field(document, "opeDateTime", "operation_datetime")) or ""
    raw_sequence = coalesce_field(document, "seqNumber", "sequence_number")
    try:
        sequence = int(raw_sequence or 0)
    except (TypeError, ValueError):
        sequence = 0
    return doc_date, operation, sequence


def _coerce_document_items(payload: list[Any], *, source: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise EDINETProviderError(f"{source} contained a non-mapping document item")
        items.append(dict(item))
    return items


def _is_zip_bytes(content: bytes) -> bool:
    return zipfile.is_zipfile(io.BytesIO(content))


def _non_zip_response_status(content: bytes) -> str | None:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    status = payload.get("StatusCode") or payload.get("statusCode") or payload.get("status")
    return str(status) if status not in (None, "") else None


def _non_zip_response_message(content: bytes) -> str:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "zip_invalid"
    if not isinstance(payload, Mapping):
        return "zip_invalid"
    status = _non_zip_response_status(content)
    message = text_or_none(payload.get("message")) or text_or_none(payload.get("Message"))
    if status and message:
        return f"StatusCode={status} message={safe_error_value(message)}"
    if status:
        return f"StatusCode={status}"
    return "zip_invalid"


def _is_unusable_status(document: Mapping[str, Any]) -> bool:
    legal_status = text_or_none(coalesce_field(document, "legalStatus", "legal_status"))
    disclosure_status = text_or_none(
        coalesce_field(document, "disclosureStatus", "disclosure_status")
    )
    withdrawal_status = text_or_none(
        coalesce_field(document, "withdrawalStatus", "withdrawal_status")
    )
    # EDINET document list uses legalStatus="1", disclosureStatus="0",
    # withdrawalStatus="0" for normal usable filings. Treat unknown/missing
    # status as usable because older cached payloads may not have all fields.
    return (
        legal_status not in (None, "1", "2")
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
    start = parse_optional_date(coalesce_field(document, "periodStart", "period_start"))
    end = parse_optional_date(coalesce_field(document, "periodEnd", "period_end"))
    if start is not None and end is not None:
        return start, end
    description = text_or_none(coalesce_field(document, "docDescription", "doc_description"))
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
