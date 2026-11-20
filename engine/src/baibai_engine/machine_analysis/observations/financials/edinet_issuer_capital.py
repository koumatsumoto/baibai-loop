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
    treasury_display_count: int | None
    source_element: str
    source_context: str
    source_locator: str
    issued_source_locator: str
    treasury_source_locator: str
    basis: str = "reported_note_display_not_reconciled_to_price_denominator"
    treasury_evidence: Literal["numeric_cell", "section_not_applicable"] = "numeric_cell"


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


def _issued_remarks(middle: list[tuple[int, ET.Element, str]], increase: str) -> bool:
    if not middle:
        return True
    # Only the observed explanations of this table's increase may intervene.
    # An unclassified paragraph can change period or switch to a correction.
    if any(local_name(node.tag) != "p" for _, node, _ in middle):
        return False
    number = _count(increase)
    if number <= 0:
        return False
    notes = [wording for _, _, wording in middle]
    return notes in (
        [
            "(変動事由の概要)",
            f"普通株式の株式数の増加{increase}株は、新株予約権の行使による増加であります。",
        ],
        [
            (
                "(注)発行済株式の総数の増加は、"
                f"譲渡制限付株式報酬としての新株式発行{increase}株による増加分であります。"
            )
        ],
    )


def _separate_treasury_section(
    html: ET.Element, table: ET.Element, rows: list[list[str]], label: str
) -> tuple[int, str]:
    """Read the issued cell and the treasury section's wording, never an inferred zero."""
    if (
        len(rows) != 2
        or rows[0] != ["株式の種類", f"{label}期首", "増加", "減少", f"{label}末"]
        or rows[1][0] != "普通株式(株)"
    ):
        raise SourceFormatError("capital_note_headers_unsupported")
    structural_tags = {
        "root",
        "p",
        "h4",
        "h5",
        "div",
        "table",
        "colgroup",
        "col",
        "thead",
        "tbody",
        "tr",
        "td",
        "th",
        "caption",
    }
    if any(local_name(node.tag) not in structural_tags for node in html.iter()):
        raise SourceFormatError("capital_note_sections_ambiguous")
    children = list(html)
    parts = [(node, _normalized("".join(node.itertext()))) for node in children]
    content = [(i, node, value) for i, (node, value) in enumerate(parts) if value]
    if (
        len(content) < 4
        or _normalized(html.text or "")
        or any(_normalized(node.tail or "") for node in children)
    ):
        raise SourceFormatError("capital_note_sections_ambiguous")
    _, issued_heading, issued_title = content[0]
    _, table_container, _ = content[1]
    _, treasury_heading, treasury_title = content[-2]
    index, paragraph, statement = content[-1]
    if (
        local_name(issued_heading.tag) not in {"p", "h4", "h5"}
        or not re.fullmatch(r"1[.]?発行済株式に関する事項", issued_title)
        or table not in table_container.iter()
        or _normalized("".join(table_container.itertext()))
        != _normalized("".join(table.itertext()))
        or not re.fullmatch(r"2[.]?自己株式に関する事項", treasury_title)
        or local_name(treasury_heading.tag) not in {"p", "h4", "h5"}
        or not _issued_remarks(content[2:-2], rows[1][2])
        or local_name(paragraph.tag) != "p"
        or statement != "該当事項はありません。"
    ):
        raise SourceFormatError("capital_note_sections_ambiguous")
    return _count(rows[1][4]), f"/block_child[{index}]"


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
    treasury: int | None
    if unit is None:
        issued, treasury_path = _separate_treasury_section(html, tables[0], rows, label)
        unit = "株"
        issued_row = 1
        treasury = None
    else:
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
        treasury_path = f"/table[0]/row[{treasury_row}]/cell[4]"
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
        treasury_source_locator=f"{locator}{treasury_path}",
        treasury_evidence="section_not_applicable" if treasury is None else "numeric_cell",
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
