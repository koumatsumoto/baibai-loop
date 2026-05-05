"""Validate trade markdown front matter (records/05-trades/*.md).

Rules enforced (see ``docs/components/trades.md`` for the spec):

- Required front matter fields exist and are well-typed.
- Lifecycle integrity: which fields must be set / null per ``status``
  (``ordered`` / ``open`` / ``closed``).
- ``paper_proxy_position_size_pct`` is consistent with
  ``paper_proxy_position_size_oku / 0.01`` (paper proxy is 1 oku JPY).
- ``real_concentration_pct`` is consistent with
  ``real_order_notional_yen / real_capital_yen * 100`` when real fields are present.
- Optional ``tactical_concentration_pct`` is consistent with
  ``real_order_notional_yen / tactical_capital_yen * 100`` when tactical fields
  are present. Tactical capital represents a temporary deployment budget and
  must not exceed ``real_capital_yen`` when both are set.
- Price-guarded orders record and validate max guarded notional /
  concentration from ``order_price_guard_yen * order_quantity``.
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
_NOTIONAL_YEN_TOLERANCE = 1.0
_REAL_CONCENTRATION_TOLERANCE = 0.5  # absolute tolerance in pct points
_TACTICAL_CONCENTRATION_TOLERANCE = 0.5  # absolute tolerance in pct points
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
    findings.extend(_check_tactical_concentration_consistency(path, front))
    findings.extend(_check_real_concentration_cap(path, front))
    findings.extend(_check_guarded_max_consistency(path, front))
    findings.extend(_check_guarded_max_real_concentration_cap(path, front))
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

    if notional is None and capital is None and pct is None:
        return []

    missing_fields: list[str] = []
    if notional is None:
        missing_fields.append("real_order_notional_yen")
    if capital is None:
        missing_fields.append("real_capital_yen")
    if pct is None:
        missing_fields.append("real_concentration_pct")
    if missing_fields:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.real-concentration-missing-field",
                message="real concentration requires numeric fields: " + ", ".join(missing_fields),
                location="real_concentration_pct",
            )
        ]

    assert notional is not None
    assert capital is not None
    assert pct is not None
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


def _check_tactical_concentration_consistency(
    path: Path, front: Mapping[str, object]
) -> list[ValidationFinding]:
    if (
        front.get("tactical_capital_yen") is None
        and front.get("tactical_concentration_pct") is None
    ):
        return []

    notional = _coerce_number(front.get("real_order_notional_yen"))
    tactical_capital = _coerce_number(front.get("tactical_capital_yen"))
    tactical_pct = _coerce_number(front.get("tactical_concentration_pct"))

    missing_fields: list[str] = []
    if notional is None:
        missing_fields.append("real_order_notional_yen")
    if tactical_capital is None:
        missing_fields.append("tactical_capital_yen")
    if tactical_pct is None:
        missing_fields.append("tactical_concentration_pct")
    if missing_fields:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.tactical-concentration-missing-field",
                message=(
                    "tactical concentration requires numeric fields: " + ", ".join(missing_fields)
                ),
                location="tactical_concentration_pct",
            )
        ]

    assert tactical_capital is not None
    assert tactical_pct is not None
    assert notional is not None
    if tactical_capital <= 0:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.tactical-capital-non-positive",
                message="tactical_capital_yen must be > 0 when tactical_concentration_pct is set",
                location="tactical_capital_yen",
            )
        ]

    findings: list[ValidationFinding] = []
    real_capital = _coerce_number(front.get("real_capital_yen"))
    if real_capital is not None and tactical_capital > real_capital:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.tactical-capital-exceeds-real-capital",
                message=(
                    f"tactical_capital_yen ({tactical_capital}) must not exceed "
                    f"real_capital_yen ({real_capital})"
                ),
                location="tactical_capital_yen",
            )
        )

    expected = notional / tactical_capital * 100.0
    if abs(tactical_pct - expected) > _TACTICAL_CONCENTRATION_TOLERANCE:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.tactical-concentration-mismatch",
                message=(
                    f"tactical_concentration_pct ({tactical_pct}) must equal "
                    f"real_order_notional_yen / tactical_capital_yen * 100 = {expected:.2f} "
                    f"(±{_TACTICAL_CONCENTRATION_TOLERANCE})"
                ),
                location="tactical_concentration_pct",
            )
        )
    return findings


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


def _check_guarded_max_consistency(
    path: Path, front: Mapping[str, object]
) -> list[ValidationFinding]:
    raw_guard_price = front.get("order_price_guard_yen")
    guarded_fields = (
        "guarded_max_notional_yen",
        "guarded_max_real_concentration_pct",
        "guarded_max_tactical_concentration_pct",
    )
    if raw_guard_price is None:
        present_fields = [field for field in guarded_fields if front.get(field) is not None]
        if not present_fields:
            return []
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.guarded-max-without-price-guard",
                message=(
                    "guarded max fields require order_price_guard_yen: " + ", ".join(present_fields)
                ),
                location="order_price_guard_yen",
            )
        ]

    findings: list[ValidationFinding] = []
    guard_price = _coerce_number(raw_guard_price)
    if guard_price is None:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.order-price-guard-invalid-type",
                message="order_price_guard_yen must be a number when set",
                location="order_price_guard_yen",
            )
        ]
    if guard_price <= 0:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.order-price-guard-non-positive",
                message="order_price_guard_yen must be > 0 when set",
                location="order_price_guard_yen",
            )
        )

    quantity = _coerce_integer(front.get("order_quantity"))
    guarded_notional = _coerce_number(front.get("guarded_max_notional_yen"))
    real_capital = _coerce_number(front.get("real_capital_yen"))
    guarded_real_pct = _coerce_number(front.get("guarded_max_real_concentration_pct"))

    missing_fields: list[str] = []
    if quantity is None:
        missing_fields.append("order_quantity")
    if guarded_notional is None:
        missing_fields.append("guarded_max_notional_yen")
    if real_capital is None:
        missing_fields.append("real_capital_yen")
    if guarded_real_pct is None:
        missing_fields.append("guarded_max_real_concentration_pct")
    if missing_fields:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.guarded-max-missing-field",
                message=(
                    "price-guarded orders require numeric fields: " + ", ".join(missing_fields)
                ),
                location="guarded_max_real_concentration_pct",
            )
        )

    if quantity is not None and quantity <= 0:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.order-quantity-non-positive",
                message="order_quantity must be > 0 when order_price_guard_yen is set",
                location="order_quantity",
            )
        )
    if guarded_notional is not None and guarded_notional <= 0:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.guarded-max-notional-non-positive",
                message="guarded_max_notional_yen must be > 0 when order_price_guard_yen is set",
                location="guarded_max_notional_yen",
            )
        )
    if real_capital is not None and real_capital <= 0:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.real-capital-non-positive",
                message="real_capital_yen must be > 0 when guarded max concentration is set",
                location="real_capital_yen",
            )
        )

    if findings:
        return findings

    assert quantity is not None
    assert guarded_notional is not None
    assert real_capital is not None
    assert guarded_real_pct is not None

    expected_notional = guard_price * quantity
    if abs(guarded_notional - expected_notional) > _NOTIONAL_YEN_TOLERANCE:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.guarded-max-notional-mismatch",
                message=(
                    f"guarded_max_notional_yen ({guarded_notional}) must equal "
                    f"order_price_guard_yen * order_quantity = {expected_notional:.0f} "
                    f"(±{_NOTIONAL_YEN_TOLERANCE})"
                ),
                location="guarded_max_notional_yen",
            )
        )

    expected_real_pct = expected_notional / real_capital * 100.0
    if abs(guarded_real_pct - expected_real_pct) > _REAL_CONCENTRATION_TOLERANCE:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.guarded-max-real-concentration-mismatch",
                message=(
                    f"guarded_max_real_concentration_pct ({guarded_real_pct}) must equal "
                    f"guarded max notional / real_capital_yen * 100 = {expected_real_pct:.2f} "
                    f"(±{_REAL_CONCENTRATION_TOLERANCE})"
                ),
                location="guarded_max_real_concentration_pct",
            )
        )

    findings.extend(_check_guarded_max_tactical_concentration(path, front, expected_notional))
    return findings


def _check_guarded_max_tactical_concentration(
    path: Path, front: Mapping[str, object], expected_notional: float
) -> list[ValidationFinding]:
    if (
        front.get("tactical_capital_yen") is None
        and front.get("guarded_max_tactical_concentration_pct") is None
    ):
        return []

    tactical_capital = _coerce_number(front.get("tactical_capital_yen"))
    guarded_tactical_pct = _coerce_number(front.get("guarded_max_tactical_concentration_pct"))

    missing_fields: list[str] = []
    if tactical_capital is None:
        missing_fields.append("tactical_capital_yen")
    if guarded_tactical_pct is None:
        missing_fields.append("guarded_max_tactical_concentration_pct")
    if missing_fields:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.guarded-max-tactical-concentration-missing-field",
                message=(
                    "guarded max tactical concentration requires numeric fields: "
                    + ", ".join(missing_fields)
                ),
                location="guarded_max_tactical_concentration_pct",
            )
        ]

    assert tactical_capital is not None
    assert guarded_tactical_pct is not None
    if tactical_capital <= 0:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.tactical-capital-non-positive",
                message=(
                    "tactical_capital_yen must be > 0 when guarded max tactical "
                    "concentration is set"
                ),
                location="tactical_capital_yen",
            )
        ]

    expected_tactical_pct = expected_notional / tactical_capital * 100.0
    if abs(guarded_tactical_pct - expected_tactical_pct) > _TACTICAL_CONCENTRATION_TOLERANCE:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.guarded-max-tactical-concentration-mismatch",
                message=(
                    f"guarded_max_tactical_concentration_pct ({guarded_tactical_pct}) "
                    "must equal guarded max notional / tactical_capital_yen * 100 = "
                    f"{expected_tactical_pct:.2f} (±{_TACTICAL_CONCENTRATION_TOLERANCE})"
                ),
                location="guarded_max_tactical_concentration_pct",
            )
        ]
    return []


def _check_guarded_max_real_concentration_cap(
    path: Path, front: Mapping[str, object]
) -> list[ValidationFinding]:
    pct = _coerce_number(front.get("guarded_max_real_concentration_pct"))
    if pct is None:
        return []
    if pct >= _REAL_CONCENTRATION_HARD_CAP_PCT:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.guarded-max-real-concentration-hard-cap",
                message=(
                    f"guarded_max_real_concentration_pct {pct} >= hard cap "
                    f"{_REAL_CONCENTRATION_HARD_CAP_PCT}; record overrides "
                    "(type=real_concentration_cap) on the linked research"
                ),
                location="guarded_max_real_concentration_pct",
            )
        ]
    if pct > _REAL_CONCENTRATION_SOFT_CAP_PCT:
        return [
            ValidationFinding(
                severity="warning",
                target=path,
                code="trade.guarded-max-real-concentration-soft-cap",
                message=(
                    f"guarded_max_real_concentration_pct {pct} exceeds soft recommendation "
                    f"{_REAL_CONCENTRATION_SOFT_CAP_PCT} (single-ticker)"
                ),
                location="guarded_max_real_concentration_pct",
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


def _coerce_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


_MISSING: object = object()
