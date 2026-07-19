"""Canonical validation and provenance contract for J-Quants master snapshots."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

from baibai_engine.market.jquants import JQuantsProviderError
from baibai_engine.market.sqlite.convert import code_quality, is_common_stock_flag

MASTER_SOURCE: Final = "jquants_master_snapshots"
MASTER_OPERATION: Final = "get_eq_master"
MIN_COMMON_STOCK_MASTER_ROWS: Final = 2500

_LOGGER = logging.getLogger(__name__)
_REQUIRED_FIELDS: Final = ("Date", "Code", "CoName", "MktNm", "S33Nm")


@dataclass(frozen=True, slots=True)
class ValidatedMasterSnapshot:
    """A complete requested-date batch ready for one SQLite transaction."""

    requested_asof: date
    rows: tuple[tuple[str, str, str, str, str, int], ...]
    raw_count: int
    excluded_count: int
    common_stock_count: int

    @property
    def persisted_count(self) -> int:
        return len(self.rows)


def master_coverage_key(asof: date) -> str:
    iso = asof.isoformat()
    return f"{MASTER_OPERATION}:{iso}..{iso}"


def master_coverage_count(value: object) -> int | None:
    """Accept only SQLite INTEGER values for an exact master row count."""
    if type(value) is not int or value < 0:
        return None
    return value


def normalize_sector_name(value: Any) -> str:
    """Normalize the two middle-dot forms emitted for the same TSE sector."""
    return str(value or "").replace("･", "・")


def validate_master_snapshot(
    records: Sequence[Mapping[str, Any]], requested_asof: date
) -> ValidatedMasterSnapshot:
    """Validate a whole response without opening or mutating SQLite.

    A master snapshot is useful only as one complete point-in-time population.
    One malformed persisted row therefore rejects the whole response. The sole
    intentional exclusion is a five-character code with a non-zero suffix,
    which J-Quants uses for a non-common security class.
    """
    if not records:
        raise JQuantsProviderError(
            f"empty J-Quants master response for requested as-of {requested_asof.isoformat()}"
        )

    rows: list[tuple[str, str, str, str, str, int]] = []
    response_dates: set[date] = set()
    seen_tickers: set[str] = set()
    excluded_count = 0
    common_stock_count = 0

    for index, record in enumerate(records):
        missing = [field for field in _REQUIRED_FIELDS if not _nonempty(record.get(field))]
        if missing:
            raise JQuantsProviderError(
                "J-Quants master response has missing/invalid required field(s) "
                f"at row {index}: {', '.join(missing)}"
            )

        snapshot_date = _parse_snapshot_date(record["Date"], row_index=index)
        response_dates.add(snapshot_date)

        raw_code = record["Code"]
        ticker, code_status = code_quality(raw_code)
        if code_status == "excluded":
            code_text = str(raw_code).strip().upper()
            if len(code_text) != 5 or code_text.endswith("0"):
                raise JQuantsProviderError(
                    f"unexpected J-Quants master code exclusion at row {index}: {raw_code!r}"
                )
            excluded_count += 1
            continue
        if code_status != "ok" or ticker is None:
            raise JQuantsProviderError(f"invalid J-Quants master code at row {index}: {raw_code!r}")
        if ticker in seen_tickers:
            raise JQuantsProviderError(
                f"duplicate normalized ticker in J-Quants master response: {ticker}"
            )
        seen_tickers.add(ticker)

        is_common = is_common_stock_flag(record)
        if not is_common:
            raise JQuantsProviderError(
                "J-Quants master common-code row contradicts its security type "
                f"at row {index}: {raw_code!r}"
            )
        common_stock_count += 1
        rows.append(
            (
                snapshot_date.isoformat(),
                ticker,
                str(record["CoName"]).strip(),
                str(record["MktNm"]).strip(),
                normalize_sector_name(record["S33Nm"]).strip(),
                1,
            )
        )

    if len(response_dates) != 1:
        rendered = ", ".join(sorted(value.isoformat() for value in response_dates)) or "<none>"
        raise JQuantsProviderError(
            f"J-Quants master response contains multiple Date values: {rendered}"
        )
    response_date = next(iter(response_dates))
    if response_date != requested_asof:
        raise JQuantsProviderError(
            "J-Quants master response Date does not match requested as-of: "
            f"requested={requested_asof.isoformat()} response={response_date.isoformat()}"
        )
    if common_stock_count < MIN_COMMON_STOCK_MASTER_ROWS:
        raise JQuantsProviderError(
            "J-Quants master common-stock population is below the production floor: "
            f"{common_stock_count} < {MIN_COMMON_STOCK_MASTER_ROWS}"
        )
    if len(records) != len(rows) + excluded_count:
        raise JQuantsProviderError(
            "J-Quants master classification count mismatch: "
            f"raw={len(records)} persisted={len(rows)} excluded={excluded_count}"
        )

    result = ValidatedMasterSnapshot(
        requested_asof=requested_asof,
        rows=tuple(rows),
        raw_count=len(records),
        excluded_count=excluded_count,
        common_stock_count=common_stock_count,
    )
    _LOGGER.info(
        "validated J-Quants master snapshot requested_asof=%s raw=%d persisted=%d "
        "intentional_excluded=%d rejected=0 duplicate=0 common=%d",
        requested_asof.isoformat(),
        result.raw_count,
        result.persisted_count,
        result.excluded_count,
        result.common_stock_count,
    )
    return result


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
