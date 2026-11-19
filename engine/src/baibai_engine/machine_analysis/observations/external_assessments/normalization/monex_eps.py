"""Optional fiscal analyst EPS from the reference table, not the four model prices."""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Literal

from pydantic import FiniteFloat

from ..models import FrozenModel
from . import Element, ParseError, compact, number


class ConsensusEPS(FrozenModel):
    provider_label: str
    fiscal_end_label: str | None
    eps_jpy_per_share: FiniteFloat | None
    analyst_count: int | None = None
    provider_published_at_utc: None = None


class ReferenceEPS(FrozenModel):
    current_fy: ConsensusEPS | None
    next_fy: ConsensusEPS | None
    rolling_one_year: ConsensusEPS | None
    annualized_to_12_months: Literal[True] | None
    share_basis: Literal["shares_at_provider_last_update"] | None


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


def reference_eps(page: Element) -> ReferenceEPS | None:
    sections = [
        section
        for section in page.find("div")
        if section.has_class("reference_table_left")
        and [compact(n.text) for n in section.find("div") if n.has_class("table_title")]
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
