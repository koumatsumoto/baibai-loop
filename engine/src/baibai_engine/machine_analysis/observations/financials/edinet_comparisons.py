"""同一EDINET文書のIFRS本表に明示された当期・比較期。取得や文書間接続はしない。"""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict

from baibai_engine.machine_analysis.observations.financials.edinet_facts.models import (
    filing_identity,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.xbrl import (
    MAX_BUNDLE_BYTES,
    XLINK,
    XSI,
    Context,
    Instance,
    SourceFormatError,
    local_name,
    read_instance,
    xml_root,
)
from baibai_engine.machine_analysis.observations.financials.financial_periods import (
    prior_year_dates_match,
)

REVISION = "edinet-ifrs-statement-comparisons-v1"
type ComparativeMetric = Literal["sales", "operating_profit", "parent_profit"]
_ROLES = {
    "annual": "rol_StatementOfProfitOrLossIFRS",
    "interim": "rol_CondensedSemiAnnualConsolidatedStatementOfProfitOrLossIFRS",
}
_CONCEPTS: dict[ComparativeMetric, tuple[str, ...]] = {
    "sales": ("RevenueIFRS", "NetSalesIFRS"),
    "operating_profit": ("OperatingProfitLossIFRS",),
    "parent_profit": ("ProfitLossAttributableToOwnersOfParentIFRS",),
}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NativeAmount(Contract):
    # Decimal text and source precision are evidence; model floats never replace them.
    amount: str | None
    decimals: str | None
    concept: str
    context_id: str
    issuer_id: str
    period_start: date
    period_end: date
    unit_ref: str
    currency: Literal["JPY"] = "JPY"
    instance_entry: str
    presentation_entry: str
    presentation_role: str
    locators: tuple[str, ...]


class ComparativeOperands(Contract):
    metric: ComparativeMetric
    current: NativeAmount | None = None
    comparative: NativeAmount | None = None
    reason: str | None = None


class FilingComparisons(Contract):
    ticker: str
    source_doc_id: str
    source_submit_datetime: datetime
    document_type: str
    parent_doc_id: str | None = None
    source_sha256: str | None = None
    extractor_revision: str = REVISION
    accounting_standard: Literal["IFRS"] | None = None
    statement_basis: Literal["consolidated", "nonconsolidated"] | None = None
    current_period_start: date | None = None
    current_period_end: date | None = None
    pairs: tuple[ComparativeOperands, ...] = ()
    reason: str | None = None


def _dei(instance: Instance, name: str) -> str:
    values = {
        (element.text or "").strip()
        for element in instance.root
        if element.tag.startswith("{http://disclosure.edinet-fsa.go.jp/taxonomy/jpdei/")
        and local_name(element.tag) == name
    }
    if len(values) != 1:
        raise SourceFormatError("comparison_dei_unknown")
    return values.pop()


def _presentation(content: bytes, kind: str) -> tuple[str, str, set[str]]:
    links: list[tuple[str, str, set[str]]] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        files = [
            f for f in archive.infolist()
            if "/PublicDoc/" in f.filename and f.filename.endswith("_pre.xml")
        ]
        if sum(f.file_size for f in files) > MAX_BUNDLE_BYTES:
            raise SourceFormatError("zip_size_limit")
        for file in files:
            root, _ = xml_root(archive.read(file))
            for link in root:
                role = link.get(f"{{{XLINK}}}role", "")
                if role != "http://disclosure.edinet-fsa.go.jp/role/jpigp/" + _ROLES[kind]:
                    continue
                concepts = {
                    loc.get(f"{{{XLINK}}}href", "").partition("#")[2]
                    for loc in link if local_name(loc.tag) == "loc"
                }
                links.append((file.filename, role, concepts))
    if len(links) != 1:
        raise SourceFormatError("comparison_main_statement_unknown")
    return links[0]


def _context(instance: Instance, name: str, consolidated: bool) -> Context:
    suffix = "" if consolidated else "_NonConsolidatedMember"
    context = instance.contexts.get(name + suffix)
    if context is None or context.period_start is None:
        raise SourceFormatError("comparison_duration_missing")
    dimensions = tuple((local_name(a), local_name(m)) for a, m in context.dimensions)
    expected = () if consolidated else (
        ("ConsolidatedOrNonConsolidatedAxis", "NonConsolidatedMember"),
    )
    if dimensions != expected:
        raise SourceFormatError("comparison_scope_unsupported")
    return context


def _amount(
    instance: Instance,
    context: Context,
    metric: ComparativeMetric,
    presentation: tuple[str, str, set[str]],
) -> NativeAmount | None:
    entry, role, concepts = presentation
    # Keep duplicate occurrences' locators; never choose between contradictory facts.
    matched = [
        (index, element)
        for index, element in enumerate(instance.root)
        if element.tag.startswith("{http://disclosure.edinet-fsa.go.jp/taxonomy/jpigp/")
        and local_name(element.tag) in _CONCEPTS[metric]
        and "jpigp_cor_" + local_name(element.tag) in concepts
        and element.get("contextRef") == context.context_id
    ]
    if not matched:
        return None
    values: set[tuple[str, str | None, str | None, str]] = set()
    for _, element in matched:
        unit = element.get("unitRef", "")
        if instance.units.get(unit) != "JPY":
            raise SourceFormatError("comparison_currency_unsupported")
        nil = element.get(f"{{{XSI}}}nil") in {"true", "1"}
        amount = None if nil else (element.text or "").strip()
        if amount is not None:
            try:
                if not Decimal(amount).is_finite():
                    raise InvalidOperation
            except InvalidOperation as exc:
                raise SourceFormatError("comparison_amount_invalid") from exc
        values.add((element.tag, amount, element.get("decimals"), unit))
    if len(values) != 1:
        raise SourceFormatError("comparison_fact_conflict")
    concept, amount, decimals, unit = values.pop()
    assert context.period_start is not None
    return NativeAmount(
        amount=amount,
        decimals=decimals,
        concept=concept,
        context_id=context.context_id,
        issuer_id=context.issuer_id,
        period_start=date.fromisoformat(context.period_start),
        period_end=date.fromisoformat(context.period_end),
        unit_ref=unit,
        instance_entry=instance.filename,
        presentation_entry=entry,
        presentation_role=role,
        locators=tuple(f"{instance.filename}#child[{index}]" for index, _ in matched),
    )


def extract_comparisons(
    *, content: bytes, ticker: str, doc_id: str, submitted: str, document_type: str
) -> FilingComparisons:
    """Only the demonstrated annual/interim IFRS source contract; no CSV date inference."""
    identity = filing_identity(ticker, doc_id, submitted)
    base = FilingComparisons(
        ticker=ticker,
        source_doc_id=doc_id,
        source_submit_datetime=datetime.fromisoformat(identity.source_submit_datetime),
        document_type=document_type,
        source_sha256=hashlib.sha256(content).hexdigest(),
    )
    try:
        kind = {"120": "annual", "130": "annual", "160": "interim", "170": "interim"}.get(
            document_type
        )
        if kind is None:
            raise SourceFormatError("comparison_document_type_unsupported")
        instance = read_instance(content)
        if _dei(instance, "SecurityCodeDEI") != ticker + "0":
            raise SourceFormatError("comparison_security_mismatch")
        if _dei(instance, "AccountingStandardsDEI") != "IFRS":
            raise SourceFormatError("comparison_accounting_standard_unsupported")
        scope = _dei(instance, "WhetherConsolidatedFinancialStatementsArePreparedDEI")
        if scope not in {"true", "false"}:
            raise SourceFormatError("comparison_scope_unknown")
        consolidated = scope == "true"
        base = base.model_copy(update={
            "accounting_standard": "IFRS",
            "statement_basis": "consolidated" if consolidated else "nonconsolidated",
        })
        current = _context(instance, "CurrentYearDuration" if kind == "annual" else "InterimDuration", consolidated)
        prior = _context(instance, "Prior1YearDuration" if kind == "annual" else "Prior1InterimDuration", consolidated)
        assert current.period_start is not None and prior.period_start is not None
        if current.issuer_id != prior.issuer_id or not prior_year_dates_match(
            date.fromisoformat(prior.period_start), date.fromisoformat(prior.period_end),
            date.fromisoformat(current.period_start), date.fromisoformat(current.period_end),
            length_tolerance_ratio=0.1, end_tolerance_days=7,
        ) or date.fromisoformat(current.period_end) > base.source_submit_datetime.date():
            raise SourceFormatError("comparison_periods_incomparable")
        presentation = _presentation(content, kind)
        base = base.model_copy(update={
            "current_period_start": date.fromisoformat(current.period_start),
            "current_period_end": date.fromisoformat(current.period_end),
        })
        pairs: list[ComparativeOperands] = []
        for metric in _CONCEPTS:
            if metric == "parent_profit" and not consolidated:
                continue
            try:
                after = _amount(instance, current, metric, presentation)
                before = _amount(instance, prior, metric, presentation)
                pairs.append(ComparativeOperands(
                    metric=metric, current=after, comparative=before,
                    reason=None if after and before and after.amount is not None and before.amount is not None else "comparison_operand_not_observed",
                ))
            except SourceFormatError as exc:
                pairs.append(ComparativeOperands(metric=metric, reason=str(exc)))
        return base.model_copy(update={"pairs": tuple(pairs)})
    except (SourceFormatError, zipfile.BadZipFile) as exc:
        return base.model_copy(update={"reason": str(exc)})
