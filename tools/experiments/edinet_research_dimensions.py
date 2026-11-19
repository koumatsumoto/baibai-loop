"""#2692: 未接続の研究用判定。対象は保持試料にある2版・8数値概念・2連結明細のみ。

入力はXMLのprefixではなく展開済みQName。提出者が正規軸上に記した独自メンバーは
観測できるが、その事業所属をDTSで検証したとはみなさない。未知の帰属は数値にしない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

Basis = Literal["consolidated", "non_consolidated"]
Kind = Literal["issuer_reported", "total", "other", "reconciliation"]

_ROOT = "http://disclosure.edinet-fsa.go.jp/taxonomy/"
_VERSIONS = ("2024-11-01", "2025-11-01")
_SEGMENT_CONCEPTS = {
    "jpcrp": ("RevenuesFromExternalCustomers",),
    "jppfs": ("Assets", "NetSales", "OperatingIncome", "OrdinaryIncome"),
    "jpigp": ("RevenueFromExternalCustomersIFRS", "RevenueIFRS", "OperatingProfitLossIFRS"),
}
_DEBT_BLOCKS = (
    "AnnexedConsolidatedDetailedScheduleOfBorrowingsTextBlock",
    "AnnexedConsolidatedDetailedScheduleOfCorporateBondsTextBlock",
)
_SPECIAL: dict[str, tuple[Kind, str]] = {
    "ReportableSegmentsMember": ("total", "報告セグメント"),
    "TotalOfReportableSegmentsAndOthersMember": ("total", "事業セグメント合計"),
    "ReconcilingItemsMember": ("reconciliation", "調整項目"),
    "OtherReportableSegmentsMember": ("other", "その他"),
    (
        "OperatingSegmentsNotIncludedInReportableSegments"
        "AndOtherRevenueGeneratingBusinessActivitiesMember"
    ): ("other", "その他"),
}


def _ns(family: str, version: str) -> str:
    return "{" + f"{_ROOT}{family}/{version}/{family}_cor" + "}"


_STANDARD_NS = {_ns(family, version) for family in _SEGMENT_CONCEPTS for version in _VERSIONS}
_FACTS = {
    _ns(family, version) + concept: (version, "segment")
    for family, concepts in _SEGMENT_CONCEPTS.items()
    for version in _VERSIONS
    for concept in concepts
} | {
    _ns("jpcrp", version) + block: (version, "debt")
    for version in _VERSIONS
    for block in _DEBT_BLOCKS
}


@dataclass(frozen=True, slots=True)
class Segment:
    key: str
    kind: Kind
    label: str | None
    label_source: Literal["issuer", "standard", "missing"]


@dataclass(frozen=True, slots=True)
class Attribution:
    # A reported member can survive an unproven basis. No basis => no classified fact.
    basis: Basis | None
    evidence_or_reason: str
    member: Segment | None = None


def classify(
    concept: str,
    dimensions: tuple[tuple[str, str], ...] | None,
    *,
    issuer_members: Mapping[str, str | None],
    verified_annual_segment_note: Basis | None = None,
) -> Attribution:
    """Classify a finite candidate; no I/O, value conversion or v2 fallback.

    issuer_members keys are full QNames declared in this filing's own XSD; values
    are Japanese standard-role labels, or None. The optional note scope is
    independent evidence of the applicable annual segment note, not a context ID,
    issuer name or inference from the absence of dimensions. Without it we do
    not apply the consolidation default to a segment observation.
    """
    profile = _FACTS.get(concept)
    if profile is None:
        namespace = concept.partition("}")[0] + "}"
        reason = "concept_unverified" if namespace in _STANDARD_NS else "taxonomy_unverified"
        return Attribution(None, reason)
    if dimensions is None:
        return Attribution(None, "context_unavailable")
    version, family = profile
    jpcrp, jppfs = _ns("jpcrp", version), _ns("jppfs", version)
    segment_axis = jpcrp + "OperatingSegmentsAxis"
    basis_axis = jppfs + "ConsolidatedOrNonConsolidatedAxis"
    pairs = dict(dimensions)
    if len(pairs) != len(dimensions):
        return Attribution(None, "repeated_dimension")
    allowed = {basis_axis, segment_axis} if family == "segment" else {basis_axis}
    if pairs.keys() - allowed:
        return Attribution(None, "dimension_unsupported")

    member = None
    if family == "segment":
        key = pairs.get(segment_axis)
        if key is None:
            return Attribution(None, "no_explicit_segment")
        if key == jpcrp + "EntityTotalMember":
            return Attribution(None, "explicit_dimension_default")
        special = _SPECIAL.get(key.removeprefix(jpcrp)) if key.startswith(jpcrp) else None
        if special:
            member = Segment(key, *special, "standard")
        elif key.startswith("{") and not key.startswith("{" + _ROOT) and key in issuer_members:
            label = issuer_members[key] or None
            member = Segment(key, "issuer_reported", label, "issuer" if label else "missing")
        else:
            return Attribution(None, "segment_member_unverified")

    basis_key = pairs.get(basis_axis)
    if basis_key == jppfs + "ConsolidatedMember":
        return Attribution(None, "explicit_dimension_default", member)
    if basis_key is not None:
        if basis_key != jppfs + "NonConsolidatedMember":
            return Attribution(None, "basis_member_unverified", member)
        if family == "debt" or verified_annual_segment_note == "consolidated":
            return Attribution(None, "basis_contradicts_source", member)
        return Attribution("non_consolidated", "explicit_pair", member)
    if family == "debt":
        return Attribution("consolidated", "consolidated_annex_concept")
    if verified_annual_segment_note == "consolidated":
        return Attribution("consolidated", "applicable_note_default", member)
    if verified_annual_segment_note == "non_consolidated":
        return Attribution(None, "explicit_non_consolidated_missing", member)
    return Attribution(None, "segment_note_scope_unverified", member)
