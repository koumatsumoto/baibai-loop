"""Two explicit EDINET debt tables, without free-text or issuer-specific inference."""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from xml.etree import ElementTree as ET  # nosec B405 -- parsed by bounded xml_root

from .models import DebtFact, FactIdentity
from .xbrl import Context, Instance, SourceFormatError, local_name, xml_root

_BLOCKS = {
    "AnnexedConsolidatedDetailedScheduleOfBorrowingsTextBlock": "borrowings",
    "AnnexedDetailedScheduleOfBorrowingsTextBlock": "borrowings",
    "AnnexedConsolidatedDetailedScheduleOfCorporateBondsTextBlock": "bonds",
    "AnnexedDetailedScheduleOfCorporateBondsTextBlock": "bonds",
}
_UNITS = {"百万円": 1_000_000, "千円": 1_000, "円": 1}
_CURRENT_ROWS = {
    "短期借入金": "short_term_borrowings",
    "1年以内に返済予定の長期借入金": "long_term_borrowings",
    "1年内返済予定の長期借入金": "long_term_borrowings",
}


def normalized(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _cells(table: ET.Element) -> list[list[str]]:
    if any(local_name(e.tag) == "table" for child in table for e in child.iter()):
        raise SourceFormatError("debt_nested_table_unsupported")
    result: list[list[str]] = []
    for row in table.iter():
        if local_name(row.tag) != "tr":
            continue
        cells = [e for e in row if local_name(e.tag) in {"td", "th"}]
        if any(e.get("colspan", "1") != "1" or e.get("rowspan", "1") != "1" for e in cells):
            raise SourceFormatError("debt_spanned_table_unsupported")
        result.append([normalized("".join(e.itertext())) for e in cells])
    if not result or not result[0] or any(len(row) != len(result[0]) for row in result):
        raise SourceFormatError("debt_ragged_table_unsupported")
    return result


def _unit(header: str) -> int | None:
    for name, scale in _UNITS.items():
        if header.endswith(f"({name})"):
            return scale
    return None


def _amount(text: str, scale: int) -> float | None:
    if text in {"", "-", "－", "―", "—", "–"}:
        return None
    if not re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", text):
        raise SourceFormatError("debt_amount_unsupported")
    value = float(Decimal(text.replace(",", "")) * scale)
    if value > 1e18:
        raise SourceFormatError("debt_amount_out_of_range")
    return value


def _bucket(header: str) -> tuple[int, int, int] | None:
    scale = _unit(header)
    if scale is None:
        return None
    label = header[: header.rfind("(")]
    if label == "1年以内":
        return 0, 12, scale
    match = re.fullmatch(r"([1-4])年超([2-5])年以内", label)
    if match and int(match[2]) == int(match[1]) + 1:
        return 12 * int(match[1]), 12 * int(match[2]), scale
    return None


def _table_values(
    kind: str, rows: list[list[str]]
) -> list[tuple[str, int, int, float | None, int, int]]:
    header = rows[0]
    result: list[tuple[str, int, int, float | None, int, int]] = []
    current_columns = [
        i for i, cell in enumerate(header) if cell.startswith("当期末残高(") and _unit(cell)
    ]
    if kind == "borrowings" and header[0] == "区分" and len(current_columns) == 1:
        column = current_columns[0]
        scale = _unit(header[column])
        assert scale is not None
        for row_index, row in enumerate(rows[1:], 1):
            category = _CURRENT_ROWS.get(row[0])
            if category:
                result.append((category, 0, 12, _amount(row[column], scale), row_index, column))
        return result
    columns = [(i, _bucket(cell)) for i, cell in enumerate(header)]
    buckets = [(i, bucket) for i, bucket in columns if bucket is not None]
    if not buckets:
        return []
    expected_columns = set(range(1 if kind == "borrowings" else 0, len(header)))
    if {i for i, _ in buckets} != expected_columns:
        raise SourceFormatError("debt_bucket_headers_unsupported")
    if kind == "bonds" and len(rows) != 2:
        raise SourceFormatError("debt_bond_rows_unsupported")
    for row_index, row in enumerate(rows[1:], 1):
        if kind == "borrowings" and row[0] != "長期借入金":
            continue  # Leases are intentionally outside the principal dataset.
        for column, (start, end, scale) in buckets:
            result.append(
                (
                    "bonds" if kind == "bonds" else "long_term_borrowings",
                    start,
                    end,
                    _amount(row[column], scale),
                    row_index,
                    column,
                )
            )
    return result


def _block_rows(
    instance: Instance, identity: FactIdentity, element: ET.Element, context: Context, kind: str
) -> tuple[list[DebtFact], set[str]]:
    html, _ = xml_root(("<root>" + (element.text or "") + "</root>").encode())
    tables = [e for e in html.iter() if local_name(e.tag) == "table"]
    rows: list[DebtFact] = []
    reasons: set[str] = set()
    if not tables:
        reasons.add("debt_no_table")
    for table_index, table in enumerate(tables):
        try:
            values = _table_values(kind, _cells(table))
        except SourceFormatError as exc:
            reasons.add(str(exc))
            continue
        for category, start, end, value, row_index, column in values:
            rows.append(
                DebtFact(
                    **identity.model_dump(),
                    source_element=element.tag,
                    source_context=context.context_id,
                    issuer_id=context.issuer_id,
                    balance_sheet_date=context.period_end,
                    consolidation_basis=context.consolidation_basis,
                    debt_category=category,
                    due_from_months=start,
                    due_to_months=end,
                    principal=value,
                    currency="JPY",
                    source_locator=f"{instance.filename}#{element.tag}[contextRef={context.context_id}]/table[{table_index}]/row[{row_index}]/cell[{column}]",
                )
            )
    return rows, reasons


def debt_facts(
    instance: Instance, identity: FactIdentity
) -> tuple[tuple[DebtFact, ...], tuple[str, ...]]:
    rows: dict[tuple[str, str, str, int, int], DebtFact] = {}
    conflicts: set[tuple[str, str, str, int, int]] = set()
    reasons: set[str] = set()
    for element in instance.root:
        kind = _BLOCKS.get(local_name(element.tag))
        if kind is None or not element.tag.startswith(
            "{http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/"
        ):
            continue
        context = instance.contexts.get(element.get("contextRef", ""))
        if context is None or any(
            local_name(axis) != "ConsolidatedOrNonConsolidatedAxis"
            for axis, _ in context.dimensions
        ):
            reasons.add("debt_context_unsupported")
            continue
        try:
            block, block_reasons = _block_rows(instance, identity, element, context, kind)
        except SourceFormatError as exc:
            reasons.add(str(exc))
            continue
        reasons.update(block_reasons)
        for row in block:
            key = (
                row.balance_sheet_date,
                row.consolidation_basis,
                row.debt_category,
                row.due_from_months,
                row.due_to_months,
            )
            if key in rows:
                conflicts.add(key)
                reasons.add("debt_duplicate_bucket")
            rows[key] = row
    result = tuple(rows[key] for key in sorted(rows) if key not in conflicts)
    if not result:
        reasons.add("debt_supported_facts_not_found")
    return result, tuple(sorted(reasons))
