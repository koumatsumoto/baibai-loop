"""Native J-Quants master snapshots and their current security projection."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

from baibai_engine.market.jquants_decode import (
    JQuantsProviderError,
    parse_jquants_code_parts,
    provider_record_payload,
)
from baibai_engine.machine_analysis.observations.stocks.models import SecurityMaster

MASTER_SOURCE: Final = "jquants_master_snapshots"
MASTER_OPERATION: Final = "get_eq_master"
MIN_COMMON_STOCK_MASTER_ROWS: Final = 2500
_REQUIRED_FIELDS: Final = ("Date", "Code", "CoName", "MktNm", "S33Nm", "ProdCat")


@dataclass(frozen=True, slots=True)
class ValidatedMasterSnapshot:
    """Native records for one requested date, including non-common issue classes."""

    requested_asof: date
    rows: tuple[tuple[str, str, str], ...]

    @property
    def persisted_count(self) -> int:
        return len(self.rows)


def master_coverage_key(asof: date) -> str:
    iso = asof.isoformat()
    return f"{MASTER_OPERATION}:{iso}..{iso}"


def master_coverage_count(value: object) -> int | None:
    """Accept only SQLite INTEGER values for an exact native-record count."""
    return value if type(value) is int and value >= 0 else None


def normalize_sector_name(value: Any) -> str:
    """Normalize sector spelling only in the reading projection, never in the raw record."""
    return str(value or "").replace("･", "・")


def normalize_master_record(record: Mapping[str, Any]) -> SecurityMaster | None:
    """Project a primary issue line; product category distinguishes domestic shares.

    ProdCat 011 includes ordinary and preferred domestic shares. The issue suffix
    must also be zero. ETFs and REITs may have suffix zero but are not ordinary shares.
    https://jpx-jquants.com/ja/spec/eq-master/product-category
    """
    ticker, primary_issue = parse_jquants_code_parts(record.get("Code"))
    if not primary_issue:
        return None
    return SecurityMaster(
        ticker=ticker,
        name=str(record.get("CoName") or "").strip(),
        market_segment=str(record.get("MktNm") or "").strip(),
        sector_33=normalize_sector_name(record.get("S33Nm")).strip(),
        is_common_stock=record.get("ProdCat") == "011",
    )


def masters_from_payloads(payloads: Sequence[str]) -> list[SecurityMaster]:
    """Decode stored native records without guessing product type from old derived flags."""
    result = []
    for payload in payloads:
        try:
            record = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise JQuantsProviderError("stored master record is not valid JSON") from exc
        if not isinstance(record, dict) or not _nonempty(record.get("ProdCat")):
            raise JQuantsProviderError("master record has no native product category")
        security = normalize_master_record(record)
        if security is not None:
            result.append(security)
    return sorted(result, key=lambda security: security.ticker)


def validate_master_snapshot(
    records: Sequence[Mapping[str, Any]], requested_asof: date
) -> ValidatedMasterSnapshot:
    """Validate native identities/date before atomically replacing the snapshot."""
    if not records:
        raise JQuantsProviderError("empty J-Quants master response")
    rows: list[tuple[str, str, str]] = []
    seen_codes: set[str] = set()
    common_count = 0
    for index, record in enumerate(records):
        missing = [field for field in _REQUIRED_FIELDS if not _nonempty(record.get(field))]
        if missing:
            raise JQuantsProviderError(
                f"J-Quants master missing required fields at row {index}: {', '.join(missing)}"
            )
        snapshot_date = _parse_snapshot_date(record["Date"], row_index=index)
        if snapshot_date != requested_asof:
            raise JQuantsProviderError(
                "J-Quants master response Date does not match requested as-of: "
                f"requested={requested_asof.isoformat()} response={snapshot_date.isoformat()}"
            )
        code = str(record["Code"]).strip().upper()
        _, primary_issue = parse_jquants_code_parts(code)
        if len(code) != 5:
            raise JQuantsProviderError("native J-Quants master Code must have five characters")
        if code in seen_codes:
            raise JQuantsProviderError(f"duplicate J-Quants master security code: {code}")
        seen_codes.add(code)
        common_count += int(primary_issue and record["ProdCat"] == "011")
        rows.append((snapshot_date.isoformat(), code, provider_record_payload(record)))
    if common_count < MIN_COMMON_STOCK_MASTER_ROWS:
        raise JQuantsProviderError(
            "J-Quants master common-stock population is below the production floor: "
            f"{common_count} < {MIN_COMMON_STOCK_MASTER_ROWS}"
        )
    return ValidatedMasterSnapshot(requested_asof, tuple(rows))


def _nonempty(value: object) -> bool:
    return isinstance(value, (str, date, datetime)) and bool(str(value).strip())


def _parse_snapshot_date(value: object, *, row_index: int) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise JQuantsProviderError(
            f"invalid J-Quants master Date type at row {row_index}: {type(value).__name__}"
        )
    text = value.strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise JQuantsProviderError(
            f"invalid J-Quants master Date at row {row_index}: {value!r}"
        ) from exc
