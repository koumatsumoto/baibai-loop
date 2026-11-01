"""JPX保存PDFのIFRS損益表。表示額の精度を残し、XBRL値を偽造しない。"""

from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from calendar import monthrange
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Literal
from urllib.parse import urlparse

from pydantic import AwareDatetime, BaseModel, ConfigDict
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from baibai_engine.machine_analysis.observations.financials.financial_periods import (
    is_annual_period,
    prior_year_dates_match,
)
from baibai_engine.primitives.dates import add_months_clamped
from baibai_engine.primitives.time import JST

REVISION = "pdf-ifrs-display-statements-v2"
type Metric = Literal["sales", "operating_profit", "parent_profit"]
_ROWS: dict[Metric, str] = {
    "sales": "売上高",
    "operating_profit": "営業利益",
    "parent_profit": "親会社の所有者",
}
_DATE = r"(\d{4})年(\d{1,2})月(\d{1,2})日"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DisplayPair(Contract):
    metric: Metric
    current: int
    comparative: int
    current_text: str
    comparative_text: str
    pdf_page: int
    table: str
    row: str
    current_column: Literal[2] = 2
    comparative_column: Literal[1] = 1
    headline_crosscheck_page: Literal[1] = 1


class PdfStatement(Contract):
    ticker: str
    disclosure_number: str
    source_url: str
    source_sha256: str
    bundle_id: str
    resource_name: str
    received_at: AwareDatetime
    published_on: date | None = None
    publication_precision: Literal["date_only"] = "date_only"
    extractor_revision: str = REVISION
    accounting_standard: Literal["IFRS"] | None = None
    statement_basis: Literal["consolidated"] | None = None
    unit: Literal["JPY_million"] | None = None
    rounding: Literal["truncate_below_unit"] | None = None
    precision_kind: Literal["published_display_not_unrounded"] = "published_display_not_unrounded"
    current_start: date | None = None
    current_end: date | None = None
    comparative_start: date | None = None
    comparative_end: date | None = None
    comparative_basis_page: int | None = None
    comparative_basis_text: str | None = None
    pairs: tuple[DisplayPair, ...] = ()
    reason: str | None = None


class DisplayArithmetic(Contract):
    metric: Metric
    amount: int
    unit: Literal["JPY_million"] = "JPY_million"
    precision_kind: Literal["derived_from_truncated_display_amounts"] = (
        "derived_from_truncated_display_amounts"
    )
    formula: Literal["current - comparative", "annual + current - comparative"]
    # Pointers resolve into the retained statements in this same supplement.
    current_sha256: str
    annual_sha256: str | None = None
    period_start: date | None = None
    period_end: date


class PdfSupplement(Contract):
    revision: str = REVISION
    read_semantics: Literal["retained_document_vintages_not_canonical_earnings"] = (
        "retained_document_vintages_not_canonical_earnings"
    )
    correction_coverage: Literal["retained_inputs_only_not_exhaustive"] = (
        "retained_inputs_only_not_exhaustive"
    )
    statements: tuple[PdfStatement, ...]
    comparisons: tuple[DisplayArithmetic, ...] = ()
    ttm: tuple[DisplayArithmetic, ...] = ()
    unresolved: tuple[str, ...] = ()


def disclosure_identity(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    match = re.fullmatch(r"/disc/([0-9A-Z]{4})0/1401(\d{14})\.pdf", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www2.jpx.co.jp"
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        raise ValueError("unsupported JPX financial PDF URL")
    return match[1], match[2]


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _date(parts: tuple[str, str, str]) -> date:
    return date(*(int(value) for value in parts))


def extract_pages(base: PdfStatement, pages: Sequence[str]) -> PdfStatement:
    """Supported two-column IFRS PL with a matching six-metric headline table.

    No OCR, expected-value substitution, filename publication date, or generic
    table guessing. Unsupported layouts return an explicit missing observation.
    """
    try:
        _require(bool(pages), "pdf_pages_missing")
        normalized = [unicodedata.normalize("NFKC", page) for page in pages]
        first = normalized[0]
        compact = _compact(first)
        _require("決算短信〔IFRS〕(連結)" in compact, "pdf_accounting_basis_unsupported")
        _require(f"コード番号{base.ticker}" in compact, "pdf_issuer_mismatch")
        _require("百万円未満切捨て" in compact, "pdf_precision_unknown")
        title = first.split("上場会社名", 1)[0]
        dates = re.findall(_DATE, title)
        _require(len(dates) == 1, "pdf_publication_date_unknown")
        published = _date(dates[0])
        _require(
            published <= base.received_at.astimezone(JST).date(), "pdf_publication_after_receipt"
        )
        candidates = [
            (number, page)
            for number, page in enumerate(normalized, 1)
            if any(
                label in _compact(page)
                for label in ("(連結損益計算書)", "(要約四半期連結損益計算書)")
            )
        ]
        _require(len(candidates) == 1, "pdf_statement_missing_or_ambiguous")
        number, table = candidates[0]
        _require("(単位:百万円)" in _compact(table), "pdf_statement_unit_unknown")
        # pypdf's native text order is each column's start/end, not visual row order.
        dates = re.findall(_DATE, table)
        _require(len(dates) == 4, "pdf_period_headers_unknown")
        prior_start, prior_end, current_start, current_end = map(_date, dates)
        _require(
            prior_start <= prior_end < current_start <= current_end <= published,
            "pdf_period_headers_invalid",
        )
        _require(
            prior_year_dates_match(
                prior_start,
                prior_end,
                current_start,
                current_end,
                length_tolerance_ratio=0.03,
                end_tolerance_days=7,
            ),
            "pdf_comparative_period_mismatch",
        )
        # Native labels establish left prior/right current, not numerical date sorting.
        _require(
            re.search(r"前(?:第1四半期連結累計期間|連結会計年度)", table) is not None
            and re.search(r"当(?:第1四半期連結累計期間|連結会計年度)", table) is not None,
            "pdf_column_roles_unsupported",
        )
        basis = [
            (index, match[0])
            for index, page in enumerate(normalized, 1)
            if (
                match := re.search(
                    r"前(?:第1四半期連結累計期間|連結会計年度)の数値についても、"
                    r"IFRS(?:会計基準)?ベースに組み替えて比較分析",
                    _compact(page),
                )
            )
            is not None
        ]
        _require(bool(basis), "pdf_comparative_basis_not_explicit")
        heading = first.split("基本的1株当たり", 1)[0]
        headline = []
        for line in heading.splitlines():
            tokens = line.split()
            if len(tokens) == 13 and re.fullmatch(r"\d{4}年\d+月期(?:第1四半期)?", tokens[0]):
                headline.append(tokens)
        _require(len(headline) == 2, "pdf_headline_columns_unknown")
        _require(headline[0][0] > headline[1][0], "pdf_headline_order_unknown")
        for row, start, end in (
            (headline[0], current_start, current_end),
            (headline[1], prior_start, prior_end),
        ):
            header = re.fullmatch(r"(\d{4})年(\d+)月期(第1四半期)?", row[0])
            assert header is not None
            year, month = int(header[1]), int(header[2])
            fiscal_end = date(year, month, monthrange(year, month)[1])
            fiscal_start = add_months_clamped(fiscal_end, -12) + timedelta(days=1)
            expected_end = (
                add_months_clamped(fiscal_start, 3) - timedelta(days=1) if header[3] else fiscal_end
            )
            _require(start == fiscal_start and end == expected_end, "pdf_headline_period_conflict")
        pairs = []
        for metric, label in _ROWS.items():
            matched = re.findall(
                r"^\s*" + re.escape(label) + r"\s+([△-]?[\d,]+)\s+([△-]?[\d,]+)\s*$",
                table,
                re.MULTILINE,
            )
            _require(len(matched) == 1, "pdf_metric_missing_or_ambiguous:" + metric)
            prior_text, current_text = matched[0]
            prior, current = (int(value.replace(",", "").replace("△", "-")) for value in matched[0])
            column = {"sales": 1, "operating_profit": 3, "parent_profit": 9}[metric]
            _require(
                [int(row[column].replace(",", "").replace("△", "-")) for row in headline]
                == [current, prior],
                "pdf_headline_statement_conflict:" + metric,
            )
            pairs.append(
                DisplayPair(
                    metric=metric,
                    current=current,
                    comparative=prior,
                    current_text=current_text,
                    comparative_text=prior_text,
                    pdf_page=number,
                    table="consolidated_statement_of_profit_or_loss",
                    row=label,
                )
            )
        return base.model_copy(
            update={
                "published_on": published,
                "current_start": current_start,
                "current_end": current_end,
                "comparative_start": prior_start,
                "comparative_end": prior_end,
                "pairs": tuple(pairs),
                "comparative_basis_page": basis[0][0],
                "comparative_basis_text": basis[0][1],
                "accounting_standard": "IFRS",
                "statement_basis": "consolidated",
                "unit": "JPY_million",
                "rounding": "truncate_below_unit",
            }
        )
    except (ValueError, IndexError) as error:
        return base.model_copy(update={"reason": str(error), "pairs": ()})


def extract_pdf(
    *,
    content: bytes,
    source_url: str,
    received_at: datetime,
    bundle_id: str,
    resource_name: str,
) -> PdfStatement:
    ticker, disclosure = disclosure_identity(source_url)
    base = PdfStatement(
        ticker=ticker,
        disclosure_number=disclosure,
        source_url=source_url,
        source_sha256=hashlib.sha256(content).hexdigest(),
        received_at=received_at,
        bundle_id=bundle_id,
        resource_name=resource_name,
    )
    if len(content) > 16 * 1024 * 1024 or not content.startswith(b"%PDF-"):
        return base.model_copy(update={"reason": "pdf_format_or_size_unsupported"})
    try:
        reader = PdfReader(io.BytesIO(content))
        if len(reader.pages) > 80:
            return base.model_copy(update={"reason": "pdf_page_limit"})
        return extract_pages(base, [page.extract_text() for page in reader.pages])
    except (PdfReadError, ValueError, KeyError) as error:
        return base.model_copy(update={"reason": "pdf_read_failed:" + type(error).__name__})


def build_pdf_supplement(
    statements: Sequence[PdfStatement],
    *,
    as_of: date,
) -> PdfSupplement | None:
    # Repeated identical receipts are retained in L1; arithmetic uses one byte vintage.
    selected = {
        (item.disclosure_number, item.source_sha256): item
        for item in statements
        if item.published_on is None or item.published_on <= as_of
    }
    if not selected:
        return None
    records = tuple(
        sorted(selected.values(), key=lambda x: (x.published_on or date.min, x.source_sha256))
    )
    _require(len({item.ticker for item in records}) == 1, "pdf_supplement_mixed_issuer")
    comparisons, ttm, unresolved = [], [], []
    for item in records:
        if item.reason or item.current_end is None or item.current_start is None:
            unresolved.append(item.disclosure_number + ":" + (item.reason or "period_missing"))
            continue
        for pair in item.pairs:
            comparisons.append(
                DisplayArithmetic(
                    metric=pair.metric,
                    amount=pair.current - pair.comparative,
                    formula="current - comparative",
                    current_sha256=item.source_sha256,
                    period_start=item.current_start,
                    period_end=item.current_end,
                )
            )
        if is_annual_period(item.current_start, item.current_end):
            continue
        annuals = [
            annual
            for annual in records
            if annual.reason is None
            and annual.current_start == item.comparative_start
            and annual.current_end is not None
            and annual.current_end + timedelta(days=1) == item.current_start
            and annual.current_start is not None
            and is_annual_period(annual.current_start, annual.current_end)
            and annual.published_on is not None
            and item.published_on is not None
            and annual.published_on <= item.published_on
            and annual.accounting_standard == item.accounting_standard == "IFRS"
            and annual.statement_basis == item.statement_basis == "consolidated"
            and annual.unit == item.unit == "JPY_million"
            and annual.rounding == item.rounding == "truncate_below_unit"
        ]
        if len(annuals) != 1 or item.comparative_end is None:
            unresolved.append(item.disclosure_number + ":annual_operand_missing_or_ambiguous")
            continue
        annual = annuals[0]
        start = item.comparative_end + timedelta(days=1)
        if not is_annual_period(start, item.current_end):
            unresolved.append(item.disclosure_number + ":ttm_period_not_annual")
            continue
        amounts = {pair.metric: pair.current for pair in annual.pairs}
        for pair in item.pairs:
            if pair.metric in amounts:
                ttm.append(
                    DisplayArithmetic(
                        metric=pair.metric,
                        amount=amounts[pair.metric] + pair.current - pair.comparative,
                        formula="annual + current - comparative",
                        current_sha256=item.source_sha256,
                        annual_sha256=annual.source_sha256,
                        period_start=start,
                        period_end=item.current_end,
                    )
                )
    return PdfSupplement(
        statements=records,
        comparisons=tuple(comparisons),
        ttm=tuple(ttm),
        unresolved=tuple(unresolved),
    )
