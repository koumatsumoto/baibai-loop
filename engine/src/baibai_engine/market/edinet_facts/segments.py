"""Segment observations selected by explicit axes and a finite set of concepts."""

from __future__ import annotations

from .models import FactIdentity, SegmentFact
from .xbrl import Instance, SourceFormatError, local_name, numeric_fact

# Only EDINET standard concepts are normalized. Issuer extension names that merely
# resemble them do not establish the same accounting meaning.
_CONCEPTS = {
    "RevenuesFromExternalCustomers": ("external_sales", None),
    "RevenueFromExternalCustomersIFRS": ("external_sales", None),
    "NetSales": ("total_sales", None),
    "OperatingRevenue1": ("total_sales", None),
    "OperatingRevenue2": ("total_sales", None),
    "RevenueIFRS": ("total_sales", None),
    "OperatingIncome": ("segment_profit", "operating"),
    "OrdinaryIncome": ("segment_profit", "ordinary"),
    "OrdinaryIncomeBNK": ("segment_profit", "ordinary"),
    "IncomeBeforeIncomeTaxes": ("segment_profit", "pretax"),
    "ProfitLoss": ("segment_profit", "net"),
    "OperatingProfitLossIFRS": ("segment_profit", "operating"),
    "ProfitLossBeforeTaxIFRS": ("segment_profit", "pretax"),
    "SegmentProfitLossIFRS": ("segment_profit", "issuer_defined"),
    "Assets": ("assets", None),
    "AssetsIFRS": ("assets", None),
}
_SPECIAL_MEMBERS = {
    "ReportableSegmentsMember": ("total", "報告セグメント合計"),
    (
        "OperatingSegmentsNotIncludedInReportableSegments"
        "AndOtherRevenueGeneratingBusinessActivitiesMember"
    ): (
        "other",
        "その他",
    ),
    "OtherReportableSegmentsMember": ("other", "その他"),
    "TotalOfReportableSegmentsMember": ("total", "報告セグメント合計"),
    "TotalOfReportableSegmentsAndOthersMember": ("total", "報告セグメント及びその他合計"),
    "ReconciliationMember": ("reconciliation", "調整額"),
    "ReconcilingItemsMember": ("reconciliation", "調整額"),
}


def segment_facts(
    instance: Instance, identity: FactIdentity
) -> tuple[tuple[SegmentFact, ...], tuple[str, ...]]:
    rows: dict[tuple[str, str], SegmentFact] = {}
    conflicts: set[tuple[str, str]] = set()
    reasons: set[str] = set()
    for element in instance.root:
        concept = _CONCEPTS.get(local_name(element.tag))
        if concept is None or not element.tag.startswith(
            "{http://disclosure.edinet-fsa.go.jp/taxonomy/"
        ):
            continue
        context = instance.contexts.get(element.get("contextRef", ""))
        if context is None:
            continue
        segments = [
            (axis, member)
            for axis, member in context.dimensions
            if local_name(axis) == "OperatingSegmentsAxis"
        ]
        if len(segments) != 1:
            continue
        if any(
            local_name(axis) not in {"OperatingSegmentsAxis", "ConsolidatedOrNonConsolidatedAxis"}
            for axis, _ in context.dimensions
        ):
            reasons.add("segment_dimensions_unsupported")
            continue
        axis, member = segments[0]
        special = _SPECIAL_MEMBERS.get(local_name(member))
        kind = special[0] if special else "segment"
        name = instance.labels.get(member) or (special[1] if special else None)
        if name is None:
            reasons.add("member_label_missing")
        currency = instance.units.get(element.get("unitRef", ""))
        if currency is None:
            reasons.add("segment_currency_unsupported")
            continue
        metric, profit_basis = concept
        if (metric == "assets") != (context.period_start is None):
            reasons.add("segment_period_type_mismatch")
            continue
        key = (context.context_id, element.tag)
        try:
            value = numeric_fact(element)
        except SourceFormatError:
            reasons.add("segment_numeric_invalid")
            conflicts.add(key)
            continue
        row = SegmentFact(
            **identity.model_dump(),
            source_element=element.tag,
            source_context=context.context_id,
            issuer_id=context.issuer_id,
            period_start=context.period_start,
            period_end=context.period_end,
            consolidation_basis=context.consolidation_basis,
            segment_axis=axis,
            segment_key=member,
            segment_name=name,
            segment_kind=kind,
            metric=metric,
            profit_basis=profit_basis,
            value=value,
            currency=currency,
            source_locator=f"{instance.filename}#{element.tag}[contextRef={context.context_id}]",
        )
        if key in rows and rows[key] != row:
            reasons.add("segment_duplicate_conflict")
            conflicts.add(key)
        rows[key] = row
    result = tuple(rows[key] for key in sorted(rows) if key not in conflicts)
    if not result:
        reasons.add("segment_numeric_facts_not_found")
    return result, tuple(sorted(reasons))
