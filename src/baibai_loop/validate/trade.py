"""Validate trade markdown front matter (records/05-trades/*.md).

Rules enforced (see ``docs/components/trades.md`` for the spec):

- Required front matter fields exist and are well-typed.
- Lifecycle integrity: which fields must be set / null per ``status``
  (``ordered`` / ``open`` / ``closed``).
- ``paper_proxy_position_size_pct`` is consistent with
  ``paper_proxy_position_size_oku / 0.01`` (paper proxy is 1 oku JPY).
- ``real_concentration_pct`` is consistent with
  ``real_order_notional_yen / real_capital_yen * 100`` when real fields are present.
- Filename matches ``YYYY-MM-DD-<ticker>.md`` and the date equals ``order_date``
  (or ``entry_date`` when ``order_date`` is null).
- ``real_concentration_pct`` exceeding the hard cap from
  ``docs/screening/principles.md §7.2`` requires a corresponding entry in the
  research front matter ``overrides`` array (we surface a warning here; the
  research-side override is validated in ``research.py``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import yaml

from .errors import ValidationFinding

KNOWN_STATUS: tuple[str, ...] = ("ordered", "open", "closed")

REQUIRED_FRONT_MATTER: tuple[str, ...] = (
    "ticker",
    "name",
    "research_ref",
    "status",
    "paper_proxy_position_size_oku",
    "paper_proxy_position_size_pct",
)

_PAPER_PROXY_CAPITAL_OKU = 0.01  # 1 oku JPY = 100 in paper proxy pct denominator
_PAPER_PROXY_PCT_TOLERANCE = 0.05  # absolute tolerance in pct points
_REAL_CONCENTRATION_TOLERANCE = 0.5  # absolute tolerance in pct points
_REAL_CONCENTRATION_HARD_CAP_PCT = 50.0
_REAL_CONCENTRATION_SOFT_CAP_PCT = 25.0

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_TICKER_PATTERN = re.compile(r"^[0-9A-Z]{4}$")
_FILENAME_PATTERN = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-([0-9A-Z]{4})\.md$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def discover_trade_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("*.md")
        if path.is_file() and not path.name.startswith("template")
    )


def validate_trade_file(path: Path) -> list[ValidationFinding]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.io",
                message=f"failed to read file: {exc}",
            )
        ]
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.no-front-matter",
                message="trade markdown must start with YAML front matter",
            )
        ]
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.invalid-yaml",
                message=f"front matter YAML parse failed: {exc}",
            )
        ]
    if not isinstance(loaded, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.front-matter-non-mapping",
                message="trade front matter must be a mapping",
            )
        ]
    front: dict[str, object] = dict(loaded)

    findings: list[ValidationFinding] = []
    findings.extend(_check_required_fields(path, front))
    findings.extend(_check_ticker_field(path, front))
    findings.extend(_check_status_value(path, front))
    findings.extend(_check_lifecycle_fields(path, front))
    findings.extend(_check_paper_proxy_consistency(path, front))
    findings.extend(_check_real_concentration_consistency(path, front))
    findings.extend(_check_real_concentration_cap(path, front))
    findings.extend(_check_filename(path, front))
    return findings


def _check_required_fields(path: Path, front: Mapping[str, object]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for field in REQUIRED_FRONT_MATTER:
        if field not in front:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="trade.required-field-missing",
                    message=f"required front matter field missing: {field}",
                    location=field,
                )
            )
    return findings


def _check_ticker_field(path: Path, front: Mapping[str, object]) -> list[ValidationFinding]:
    ticker = front.get("ticker")
    if not isinstance(ticker, str):
        return []
    if not _TICKER_PATTERN.match(ticker):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.ticker-format",
                message=f"ticker must be 4 alphanumeric uppercase chars (got {ticker!r})",
                location="ticker",
            )
        ]
    return []


def _check_status_value(path: Path, front: Mapping[str, object]) -> list[ValidationFinding]:
    status = front.get("status")
    if status is None:
        return []
    if not isinstance(status, str) or status not in KNOWN_STATUS:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.unknown-status",
                message=f"status must be one of {KNOWN_STATUS} (got {status!r})",
                location="status",
            )
        ]
    return []


def _check_lifecycle_fields(path: Path, front: Mapping[str, object]) -> list[ValidationFinding]:
    status_value = front.get("status")
    if not isinstance(status_value, str):
        return []
    findings: list[ValidationFinding] = []
    match status_value:
        case "ordered":
            findings.extend(_require_non_null(path, front, ("order_date", "expected_fill_at")))
            findings.extend(
                _require_null(
                    path,
                    front,
                    ("entry_date", "entry_price", "exit_date", "exit_price", "pnl_pct"),
                )
            )
        case "open":
            findings.extend(
                _require_non_null(path, front, ("order_date", "entry_date", "entry_price"))
            )
            findings.extend(_require_null(path, front, ("exit_date", "exit_price", "pnl_pct")))
        case "closed":
            findings.extend(
                _require_non_null(
                    path,
                    front,
                    (
                        "order_date",
                        "entry_date",
                        "entry_price",
                        "exit_date",
                        "exit_price",
                        "pnl_pct",
                    ),
                )
            )
        case _:
            # unknown status is reported by _check_status_value; nothing to add here
            pass
    return findings


def _require_non_null(
    path: Path, front: Mapping[str, object], fields: tuple[str, ...]
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for field in fields:
        value = front.get(field, _MISSING)
        if value is _MISSING:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="trade.lifecycle-missing-required",
                    message=f"status requires {field} to be present and non-null",
                    location=field,
                )
            )
        elif value is None:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="trade.lifecycle-null-where-required",
                    message=f"status requires {field} to be non-null",
                    location=field,
                )
            )
    return findings


def _require_null(
    path: Path, front: Mapping[str, object], fields: tuple[str, ...]
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for field in fields:
        if field in front and front[field] is not None:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="trade.lifecycle-non-null-where-prohibited",
                    message=f"status requires {field} to be null (got non-null)",
                    location=field,
                )
            )
    return findings


def _check_paper_proxy_consistency(
    path: Path, front: Mapping[str, object]
) -> list[ValidationFinding]:
    oku = _coerce_number(front.get("paper_proxy_position_size_oku"))
    pct = _coerce_number(front.get("paper_proxy_position_size_pct"))
    if oku is None or pct is None:
        return []
    expected = oku / _PAPER_PROXY_CAPITAL_OKU
    if abs(pct - expected) > _PAPER_PROXY_PCT_TOLERANCE:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.paper-proxy-pct-mismatch",
                message=(
                    f"paper_proxy_position_size_pct ({pct}) must equal "
                    f"paper_proxy_position_size_oku / 0.01 = {expected:.4f} "
                    f"(±{_PAPER_PROXY_PCT_TOLERANCE})"
                ),
                location="paper_proxy_position_size_pct",
            )
        ]
    return []


def _check_real_concentration_consistency(
    path: Path, front: Mapping[str, object]
) -> list[ValidationFinding]:
    notional = _coerce_number(front.get("real_order_notional_yen"))
    capital = _coerce_number(front.get("real_capital_yen"))
    pct = _coerce_number(front.get("real_concentration_pct"))
    if notional is None or capital is None or pct is None:
        return []
    if capital <= 0:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.real-capital-non-positive",
                message="real_capital_yen must be > 0 when real_concentration_pct is set",
                location="real_capital_yen",
            )
        ]
    expected = notional / capital * 100.0
    if abs(pct - expected) > _REAL_CONCENTRATION_TOLERANCE:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.real-concentration-mismatch",
                message=(
                    f"real_concentration_pct ({pct}) must equal "
                    f"real_order_notional_yen / real_capital_yen * 100 = {expected:.2f} "
                    f"(±{_REAL_CONCENTRATION_TOLERANCE})"
                ),
                location="real_concentration_pct",
            )
        ]
    return []


def _check_real_concentration_cap(
    path: Path, front: Mapping[str, object]
) -> list[ValidationFinding]:
    pct = _coerce_number(front.get("real_concentration_pct"))
    if pct is None:
        return []
    if pct >= _REAL_CONCENTRATION_HARD_CAP_PCT:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.real-concentration-hard-cap",
                message=(
                    f"real_concentration_pct {pct} >= hard cap "
                    f"{_REAL_CONCENTRATION_HARD_CAP_PCT}; record overrides "
                    "(type=real_concentration_cap) on the linked research"
                ),
                location="real_concentration_pct",
            )
        ]
    if pct > _REAL_CONCENTRATION_SOFT_CAP_PCT:
        return [
            ValidationFinding(
                severity="warning",
                target=path,
                code="trade.real-concentration-soft-cap",
                message=(
                    f"real_concentration_pct {pct} exceeds soft recommendation "
                    f"{_REAL_CONCENTRATION_SOFT_CAP_PCT} (single-ticker)"
                ),
                location="real_concentration_pct",
            )
        ]
    return []


def _check_filename(path: Path, front: Mapping[str, object]) -> list[ValidationFinding]:
    match = _FILENAME_PATTERN.match(path.name)
    if not match:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.filename-format",
                message="filename must match YYYY-MM-DD-<ticker>.md",
            )
        ]
    file_date_str = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    file_ticker = match.group(4)
    findings: list[ValidationFinding] = []
    ticker = front.get("ticker")
    if isinstance(ticker, str) and ticker != file_ticker:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.filename-ticker-mismatch",
                message=f"filename ticker {file_ticker} != front matter ticker {ticker}",
                location="ticker",
            )
        )
    expected_date = _expected_filename_date(front)
    if expected_date is not None and expected_date != file_date_str:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.filename-date-mismatch",
                message=(
                    f"filename date {file_date_str} must match order_date "
                    f"(or entry_date when no order_date): {expected_date}"
                ),
                location="filename",
            )
        )
    return findings


def _expected_filename_date(front: Mapping[str, object]) -> str | None:
    order_date = _coerce_iso_date(front.get("order_date"))
    if order_date is not None:
        return order_date
    return _coerce_iso_date(front.get("entry_date"))


def _coerce_iso_date(value: object) -> str | None:
    if isinstance(value, str) and _ISO_DATE_RE.match(value):
        return value
    if isinstance(value, date):
        return value.isoformat()
    return None


def _coerce_number(value: object) -> float | None:
    if isinstance(value, bool):  # bool is subclass of int — exclude explicitly
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


_MISSING: object = object()
