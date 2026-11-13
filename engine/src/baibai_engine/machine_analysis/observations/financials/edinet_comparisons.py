"""同一EDINET文書のIFRS本表に明示された当期・比較期。取得や文書間接続はしない。"""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from pydantic import JsonValue, ValidationError

from baibai_engine.machine_analysis.observations.financials.edinet_comparison_models import (
    REVISION,
    ComparativeMetric,
    ComparativeOperands,
    FilingComparisons,
    NativeAmount,
)
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

_ROLES = {
    ("annual", False): "rol_StatementOfProfitOrLossIFRS",
    ("annual", True): "rol_ConsolidatedStatementOfProfitOrLossIFRS",
    ("interim", True): "rol_CondensedSemiAnnualConsolidatedStatementOfProfitOrLossIFRS",
}
_CONCEPTS: dict[ComparativeMetric, tuple[str, ...]] = {
    "sales": ("RevenueIFRS", "NetSalesIFRS"),
    "operating_profit": ("OperatingProfitLossIFRS",),
    "parent_profit": ("ProfitLossAttributableToOwnersOfParentIFRS",),
}
_NON_IFRS = "comparison_accounting_standard_non_ifrs"


def _finite_amount(value: str) -> bool:
    try:
        return Decimal(value).is_finite()
    except InvalidOperation:
        return False


def _prior_period_matches(
    prior_start: date, prior_end: date, current_start: date, current_end: date
) -> bool:
    return prior_year_dates_match(
        prior_start,
        prior_end,
        current_start,
        current_end,
        length_tolerance_ratio=0.1,
        end_tolerance_days=7,
    )


def reusable_comparison_seed(value: dict[str, JsonValue]) -> FilingComparisons | None:
    """成功または既知の非IFRSだけ。旧/不完全形式や未確定の失敗を補わない。"""
    if value.get("extractor_revision") != REVISION or value.get("reason") not in (None, _NON_IFRS):
        return None
    try:
        seed = FilingComparisons.model_validate(value)
    except ValidationError:
        return None
    if seed.model_dump(mode="json") != value or seed.source_sha256 is None:
        return None
    if seed.reason == _NON_IFRS:
        return (
            seed
            if seed.accounting_standard is None
            and seed.statement_basis is None
            and seed.current_period_start is None
            and seed.current_period_end is None
            and not seed.pairs
            else None
        )
    if (
        seed.accounting_standard != "IFRS"
        or seed.statement_basis is None
        or seed.current_period_start is None
        or seed.current_period_end is None
        or seed.current_period_start > seed.current_period_end
        or seed.current_period_end > seed.source_submit_datetime.date()
        or tuple(pair.metric for pair in seed.pairs)
        != tuple(
            metric
            for metric in _CONCEPTS
            if metric != "parent_profit" or seed.statement_basis == "consolidated"
        )
    ):
        return None
    for pair in seed.pairs:
        complete = (
            pair.current is not None
            and pair.comparative is not None
            and pair.current.amount is not None
            and pair.comparative.amount is not None
        )
        if (pair.reason is None) != complete:
            return None
        for amount in (pair.current, pair.comparative):
            if amount is not None and (
                not amount.locators
                or (amount.amount is not None and not _finite_amount(amount.amount))
                or amount.period_start > amount.period_end
                or amount.period_end > seed.source_submit_datetime.date()
            ):
                return None
        if pair.current is not None and (
            pair.current.period_start != seed.current_period_start
            or pair.current.period_end != seed.current_period_end
        ):
            return None
        if pair.comparative is not None and not _prior_period_matches(
            pair.comparative.period_start,
            pair.comparative.period_end,
            seed.current_period_start,
            seed.current_period_end,
        ):
            return None
        if (
            pair.current is not None
            and pair.comparative is not None
            and pair.current.issuer_id != pair.comparative.issuer_id
        ):
            return None
    return seed


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


def _presentation(content: bytes, kind: str, *, consolidated: bool) -> tuple[str, str, set[str]]:
    expected = _ROLES.get((kind, consolidated))
    if expected is None:
        raise SourceFormatError("comparison_main_statement_unknown")
    links: list[tuple[str, str, set[str]]] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        files = [
            f
            for f in archive.infolist()
            if "/PublicDoc/" in f.filename and f.filename.endswith("_pre.xml")
        ]
        if sum(f.file_size for f in files) > MAX_BUNDLE_BYTES:
            raise SourceFormatError("zip_size_limit")
        for file in files:
            root, _ = xml_root(archive.read(file))
            for link in root:
                role = link.get(f"{{{XLINK}}}role", "")
                if role != "http://disclosure.edinet-fsa.go.jp/role/jpigp/" + expected:
                    continue
                concepts = {
                    loc.get(f"{{{XLINK}}}href", "").partition("#")[2]
                    for loc in link
                    if local_name(loc.tag) == "loc"
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
    expected = (
        () if consolidated else (("ConsolidatedOrNonConsolidatedAxis", "NonConsolidatedMember"),)
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
        if amount is not None and not _finite_amount(amount):
            raise SourceFormatError("comparison_amount_invalid")
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
        instance = read_instance(content, include_labels=False)
        if _dei(instance, "SecurityCodeDEI") != ticker + "0":
            raise SourceFormatError("comparison_security_mismatch")
        standard = _dei(instance, "AccountingStandardsDEI")
        if standard in {"Japan GAAP", "US GAAP"}:
            raise SourceFormatError(_NON_IFRS)
        if standard != "IFRS":
            raise SourceFormatError("comparison_accounting_standard_unsupported")
        scope = _dei(instance, "WhetherConsolidatedFinancialStatementsArePreparedDEI")
        if scope not in {"true", "false"}:
            raise SourceFormatError("comparison_scope_unknown")
        consolidated = scope == "true"
        base = base.model_copy(
            update={
                "accounting_standard": "IFRS",
                "statement_basis": "consolidated" if consolidated else "nonconsolidated",
            }
        )
        current = _context(
            instance, "CurrentYearDuration" if kind == "annual" else "InterimDuration", consolidated
        )
        prior = _context(
            instance,
            "Prior1YearDuration" if kind == "annual" else "Prior1InterimDuration",
            consolidated,
        )
        assert current.period_start is not None
        assert prior.period_start is not None
        if (
            current.issuer_id != prior.issuer_id
            or not _prior_period_matches(
                date.fromisoformat(prior.period_start),
                date.fromisoformat(prior.period_end),
                date.fromisoformat(current.period_start),
                date.fromisoformat(current.period_end),
            )
            or date.fromisoformat(current.period_end) > base.source_submit_datetime.date()
        ):
            raise SourceFormatError("comparison_periods_incomparable")
        presentation = _presentation(content, kind, consolidated=consolidated)
        base = base.model_copy(
            update={
                "current_period_start": date.fromisoformat(current.period_start),
                "current_period_end": date.fromisoformat(current.period_end),
            }
        )
        pairs: list[ComparativeOperands] = []
        for metric in _CONCEPTS:
            if metric == "parent_profit" and not consolidated:
                continue
            try:
                after = _amount(instance, current, metric, presentation)
                before = _amount(instance, prior, metric, presentation)
                pairs.append(
                    ComparativeOperands(
                        metric=metric,
                        current=after,
                        comparative=before,
                        reason=None
                        if after
                        and before
                        and after.amount is not None
                        and before.amount is not None
                        else "comparison_operand_not_observed",
                    )
                )
            except SourceFormatError as exc:
                pairs.append(ComparativeOperands(metric=metric, reason=str(exc)))
        return base.model_copy(update={"pairs": tuple(pairs)})
    except (SourceFormatError, zipfile.BadZipFile) as exc:
        return base.model_copy(update={"reason": str(exc)})
