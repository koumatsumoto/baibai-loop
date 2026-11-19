"""Optional fiscal analyst EPS from the reference table, not the four model prices."""

from __future__ import annotations

import math
import re
import unicodedata

from ..models import ConsensusEPS as ConsensusEPS
from ..models import ReferenceEPS as ReferenceEPS
from . import Element, ParseError, compact, number


def _cells(row: Element) -> list[Element]:
    return [c for c in row.children if isinstance(c, Element) and c.tag in {"th", "td"}]


def _estimate(rows: list[list[Element]], label: str, *, fiscal: bool) -> ConsensusEPS | None:
    matches = [cells for cells in rows if cells and compact(cells[0].text).startswith(label)]
    if len(matches) != 1 or len(cells := matches[0]) != 3:
        return None
    heading, value, _ = cells
    if heading.tag != "th" or value.tag != "td" or not value.has_class("td_left_data"):
        return None
    period = None
    if fiscal:
        periods = [s for s in heading.find("span") if s.has_class("thin_str")]
        if len(periods) != 1:
            return None
        period = compact(periods[0].text)
        if not re.fullmatch(r"[1-9][0-9]{3}/(?:0[1-9]|1[0-2])", period):
            return None
    if compact(heading.text) != label + (period or ""):
        return None
    if any(
        n is not value and (n.tag not in {"span", "strong", "b", "em"} or n.attrs)
        for n in value.find()
    ):
        return None  # A locked image or another embedded value is not a numeric EPS cell.
    try:
        yen = number(unicodedata.normalize("NFKC", value.text), unit="yen")
    except ParseError:
        return None
    if yen is not None and not math.isfinite(yen):
        return None
    return ConsensusEPS(provider_label=label, fiscal_end_label=period, eps_jpy_per_share=yen)


def _displayed_sections(page: Element) -> list[Element]:
    """Only copy candidate sections; inert descendants cannot supply or duplicate a value."""
    sections: list[Element] = []
    pending: list[tuple[Element | str, Element | None]] = [(page, None)]
    while pending:
        node, parent = pending.pop()
        if isinstance(node, str):
            if parent is not None:
                parent.children.append(node)
            continue
        if node.tag in {"template", "script", "style"} or "hidden" in node.attrs:
            continue
        is_section = node.tag == "div" and node.has_class("reference_table_left")
        visible = Element(node.tag, node.attrs) if is_section or parent is not None else None
        if visible is not None:
            if parent is not None:
                parent.children.append(visible)
            if is_section:
                sections.append(visible)
        pending.extend((child, visible) for child in reversed(node.children))
    return sections


def reference_eps(page: Element) -> ReferenceEPS | None:
    sections = [
        section
        for section in _displayed_sections(page)
        if [compact(n.text) for n in section.find("div") if n.has_class("table_title")]
        == ["参考EPS"]
    ]
    if len(sections) != 1:
        return None
    section = sections[0]
    tables = section.find("table")
    if len(tables) != 1 or not tables[0].has_class("table_left"):
        return None
    rows = [_cells(row) for row in tables[0].find("tr")]
    if not rows or [compact(cell.text) for cell in rows[0]] != ["", "EPS", "前期比"]:
        return None
    current = _estimate(rows[1:], "今期予想コンセンサス", fiscal=True)
    following = _estimate(rows[1:], "来期予想コンセンサス", fiscal=True)
    if (
        current
        and following
        and current.fiscal_end_label
        and following.fiscal_end_label
        and current.fiscal_end_label >= following.fiscal_end_label
    ):
        current = following = None
    notes = {compact(n.text) for n in section.find("div") if n.has_class("calc_area_comments")}
    return ReferenceEPS(
        current_fy=current,
        next_fy=following,
        rolling_one_year=_estimate(rows[1:], "1年後コンセンサス", fiscal=False),
        annualized_to_12_months=True if "※EPSは12か月換算した値を表示" in notes else None,
        share_basis="shares_at_provider_last_update"
        if "※EPSは最終更新日付時点の株数にて計算した値を表示" in notes
        else None,
    )
