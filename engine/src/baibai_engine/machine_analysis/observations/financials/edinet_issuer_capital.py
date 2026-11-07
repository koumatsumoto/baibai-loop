"""発行会社の資本注記の表示値。株数分母や現在の資本状態へは補完しない。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Literal
from xml.etree import ElementTree as ET  # nosec B405 -- bounded xml_root only
from zipfile import BadZipFile

from baibai_engine.machine_analysis.observations.financials.edinet_facts.xbrl import (
    XSI,
    Context,
    Instance,
    SourceFormatError,
    local_name,
    read_instance,
    xml_root,
)

_BLOCK = "NotesRegardingIssuedSharesAndTreasurySharesTextBlock"
_NAMESPACE = re.compile(
    r"^\{http://disclosure\.edinet-fsa\.go\.jp/taxonomy/jpcrp/\d{4}-\d{2}-\d{2}/jpcrp_cor\}"
)
_AXIS_NAMESPACE = re.compile(
    r"^\{http://disclosure\.edinet-fsa\.go\.jp/taxonomy/jppfs/\d{4}-\d{2}-\d{2}/jppfs_cor\}"
)
_UNITS: tuple[Literal["株", "千株"], ...] = ("株", "千株")


@dataclass(frozen=True, slots=True)
class IssuerCapitalNote:
    issuer_id: str
    reporting_period_start: date
    reporting_period_end: date
    consolidation_basis: str
    display_unit: Literal["株", "千株"]
    issued_display_count: int
    treasury_display_count: int
    source_element: str
    source_context: str
    source_locator: str
    issued_source_locator: str
    treasury_source_locator: str
    basis: str = "reported_note_display_not_reconciled_to_price_denominator"


@dataclass(frozen=True, slots=True)
class CapitalNoteExtraction:
    observations: tuple[IssuerCapitalNote, ...]
    reasons: tuple[str, ...]


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _basis(context: Context) -> tuple[str, str] | None:
    if not context.dimensions:
        return "consolidated", "当連結会計年度"
    if len(context.dimensions) != 1:
        return None
    axis, member = context.dimensions[0]
    if (
        _AXIS_NAMESPACE.match(axis)
        and _AXIS_NAMESPACE.match(member)
        and axis.rsplit("}", 1)[0] == member.rsplit("}", 1)[0]
        and local_name(axis) == "ConsolidatedOrNonConsolidatedAxis"
        and local_name(member) == "NonConsolidatedMember"
    ):
        return "non_consolidated", "当事業年度"
    return None


def _table_rows(table: ET.Element) -> list[list[str]]:
    rows = []
    for row in table.iter():
        if local_name(row.tag) != "tr":
            continue
        cells = [cell for cell in row if local_name(cell.tag) in {"td", "th"}]
        if len(cells) != 5 or any(
            cell.get("rowspan", "1") != "1" or cell.get("colspan", "1") != "1" for cell in cells
        ):
            raise SourceFormatError("capital_note_table_shape_unsupported")
        rows.append([_normalized("".join(cell.itertext())) for cell in cells])
    if not rows:
        raise SourceFormatError("capital_note_no_rows")
    return rows


def _count(text: str) -> int:
    # Dash/nil/decimals are outside this integer-display contract. Keep the unit:
    # a printed integer in thousands is not an exact integer count in shares.
    if not re.fullmatch(r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)", text):
        raise SourceFormatError("capital_note_count_unavailable")
    return int(text.replace(",", ""))


def _extract_block(
    instance: Instance, element: ET.Element, context: Context, basis: tuple[str, str]
) -> IssuerCapitalNote:
    if element.get(f"{{{XSI}}}nil") in {"true", "1"}:
        raise SourceFormatError("capital_note_nil")
    html, _ = xml_root(("<root>" + (element.text or "") + "</root>").encode())
    tables = [item for item in html.iter() if local_name(item.tag) == "table"]
    # A current-year context can enclose comparative tables. Do not date those
    # tables from the enclosing context, or join values from separate tables.
    if len(tables) != 1 or re.search(
        r"前(?:連結会計年度|事業年度|会計年度|期)", _normalized("".join(html.itertext()))
    ):
        raise SourceFormatError("capital_note_period_layout_unsupported")
    prefix_parts = []
    for node in html.iter():
        if node is tables[0]:
            break
        prefix_parts.append((node.text or "") + (node.tail or ""))
    prefix_parts.extend(
        "".join(node.itertext()) for node in tables[0].iter() if local_name(node.tag) == "caption"
    )
    prefix = _normalized("".join(prefix_parts))
    dates = re.findall(r"([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日", prefix)
    if re.search(r"(?:[0-9]{4}年|[0-9]{4}[-/]|令和|平成|昭和)", prefix):
        try:
            labeled_period = tuple(date(*map(int, parts)).isoformat() for parts in dates)
        except ValueError as error:
            raise SourceFormatError("capital_note_period_label_unsupported") from error
        if labeled_period != (context.period_start, context.period_end):
            raise SourceFormatError("capital_note_period_label_unsupported")
    rows = _table_rows(tables[0])
    consolidation, label = basis
    unit = next(
        (
            unit
            for unit in _UNITS
            if rows[0]
            == [
                "",
                f"{label}期首株式数({unit})",
                f"{label}増加株式数({unit})",
                f"{label}減少株式数({unit})",
                f"{label}末株式数({unit})",
            ]
        ),
        None,
    )
    if unit is None:
        raise SourceFormatError("capital_note_headers_unsupported")
    section: str | None = None
    seen_sections: set[str] = set()
    values: dict[str, tuple[int, int]] = {}
    for index, row in enumerate(rows[1:], 1):
        if row[0] in {"発行済株式", "自己株式"}:
            section = row[0]
            if section in seen_sections or any(row[1:]):
                raise SourceFormatError("capital_note_sections_ambiguous")
            seen_sections.add(section)
        elif re.fullmatch(r"普通株式(?:\(注[0-9,、]*\)[0-9,、]*)?", row[0]):
            if section is None or section in values:
                raise SourceFormatError("capital_note_ordinary_rows_ambiguous")
            values[section] = _count(row[4]), index
        elif row[0] != "合計" or section is None:
            raise SourceFormatError("capital_note_rows_unsupported")
    if set(values) != {"発行済株式", "自己株式"}:
        raise SourceFormatError("capital_note_pair_unavailable")
    issued, issued_row = values["発行済株式"]
    treasury, treasury_row = values["自己株式"]
    locator = f"{instance.filename}#{element.tag}[contextRef={context.context_id}]"
    assert context.period_start is not None
    return IssuerCapitalNote(
        issuer_id=context.issuer_id,
        reporting_period_start=date.fromisoformat(context.period_start),
        reporting_period_end=date.fromisoformat(context.period_end),
        consolidation_basis=consolidation,
        display_unit=unit,
        issued_display_count=issued,
        treasury_display_count=treasury,
        source_element=element.tag,
        source_context=context.context_id,
        source_locator=locator,
        issued_source_locator=f"{locator}/table[0]/row[{issued_row}]/cell[4]",
        treasury_source_locator=f"{locator}/table[0]/row[{treasury_row}]/cell[4]",
    )


def extract_issuer_capital_notes(
    content: bytes, *, edinet_code: str, period_start: date, period_end: date
) -> CapitalNoteExtraction:
    """Read one documented table form, keeping display precision and source scope.

    Notes about trusts, splits and other adjustments are not interpreted. The
    result is neither the legal own-name balance nor a pricing share denominator.
    """
    if period_start > period_end:
        raise ValueError("capital note period is reversed")
    try:
        instance = read_instance(content, include_labels=False)
    except BadZipFile as error:
        raise SourceFormatError("zip_member_invalid") from error
    observations: dict[str, IssuerCapitalNote] = {}
    seen: set[str] = set()
    conflicts: set[str] = set()
    reasons: set[str] = set()
    for element in instance.root:
        if local_name(element.tag) != _BLOCK or not _NAMESPACE.match(element.tag):
            continue
        context = instance.contexts.get(element.get("contextRef", ""))
        if context is None:
            reasons.add("capital_note_context_unavailable")
            continue
        if (context.period_start, context.period_end) != (
            period_start.isoformat(),
            period_end.isoformat(),
        ):
            continue
        basis = _basis(context)
        if context.issuer_id != f"{edinet_code}-000" or basis is None:
            reasons.add("capital_note_scope_unsupported")
            continue
        key = basis[0]
        if key in seen:
            conflicts.add(key)
        seen.add(key)
        try:
            note = _extract_block(instance, element, context, basis)
        except SourceFormatError as error:
            reasons.add(str(error))
            continue
        observations[key] = note
    if conflicts:
        reasons.add("capital_note_multiple_blocks")
    result = tuple(observations[key] for key in sorted(observations) if key not in conflicts)
    if not result and not reasons:
        reasons.add("capital_note_not_observed")
    return CapitalNoteExtraction(result, tuple(sorted(reasons)))
