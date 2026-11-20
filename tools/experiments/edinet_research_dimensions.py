"""#2692: 未接続の研究用判定。対象は保持試料にある2版・8数値概念・2連結明細のみ。

入力はXMLのprefixではなく展開済みQName。提出者が正規軸上に記した独自メンバーは
観測できるが、その事業所属をDTSで検証したとはみなさない。未知の帰属は数値にしない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from xml.etree import ElementTree as ET  # nosec B405 -- only inspect already-parsed elements

Basis = Literal["consolidated", "non_consolidated"]
Kind = Literal["issuer_reported", "issuer_unverified", "total", "other", "reconciliation"]

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


_I = "{http://www.xbrl.org/2003/instance}"
_DI = "{http://xbrl.org/2006/xbrldi}"
_XS = "{http://www.w3.org/2001/XMLSchema}"
_DOMAIN_TYPE = "{http://www.xbrl.org/dtr/type/non-numeric}domainItemType"
Scopes = Mapping[ET.Element, Mapping[str, str]]


def _qname(raw: str | None, ns: Mapping[str, str]) -> str | None:
    parts = (raw or "").strip().split(":")
    if len(parts) == 1:
        parts.insert(0, "")
    if len(parts) != 2 or not parts[1] or not (uri := ns.get(parts[0])):
        return None
    return "{" + uri + "}" + parts[1]


def annual_segment_note_at(
    parts: Mapping[str, tuple[ET.Element, Scopes]],
    locator: tuple[str, tuple[int, ...]] | None,
    *,
    concept: str,
    context_id: str,
) -> Basis | None:
    """Check a single occurrence in an already pinned filing, not a matching value.

    The caller supplies a known (archive member, child-index path); do not choose
    among equal facts. Only the two directly enclosing annual notes verified with
    2025-11-01 filings are supported. This does not read a ZIP or match normalized amounts.
    """
    if locator is None or not context_id or _FACTS.get(concept) != ("2025-11-01", "segment"):
        return None
    part, path = locator
    if not part or not path or part not in parts:
        return None
    node, scopes = parts[part]
    ix = "{http://www.xbrl.org/2008/inlineXBRL}"
    xhtml = "{http://www.w3.org/1999/xhtml}"
    if node.tag != xhtml + "html":
        return None
    crp = _ns("jpcrp", "2025-11-01")
    notes: dict[str, Basis] = {
        crp + "NotesSegmentInformationEtcConsolidatedFinancialStatementsTextBlock": "consolidated",
        crp + "NotesSegmentInformationEtcFinancialStatementsTextBlock": "non_consolidated",
    }
    basis = None
    for index in path:
        if not node.tag.startswith(xhtml):
            if basis is not None or node.tag != ix + "nonNumeric" or not node.get("contextRef"):
                return None
            name = _qname(node.get("name"), scopes.get(node, {}))
            if name is None or (basis := notes.get(name)) is None:
                return None
        if not 0 <= index < len(node):
            return None
        node = node[index]
    if (
        basis is not None
        and node.tag == ix + "nonFraction"
        and node.get("contextRef") == context_id
        and _qname(node.get("name"), scopes.get(node, {})) == concept
    ):
        return basis
    return None


@dataclass(frozen=True, slots=True)
class ContextEvidence:
    context_id: str
    pairs: tuple[tuple[str, str], ...] = ()
    problem: str | None = None


def observe_context(node: ET.Element, scopes: Scopes) -> ContextEvidence:
    """Preserve context ID and reject unsupported placement before flattening."""
    context_id = node.get("id", "")
    scenarios = node.findall(_I + "scenario")
    direct = node.findall(f"{_I}scenario/{_DI}explicitMember")
    if (
        node.tag != _I + "context"
        or not context_id
        or len(scenarios) > 1
        or node.find(f"{_I}entity/{_I}segment") is not None
        or set(direct) != set(node.iter(_DI + "explicitMember"))
        or node.find(".//" + _DI + "typedMember") is not None
        or (scenarios and (list(scenarios[0]) != direct or (scenarios[0].text or "").strip()))
        or any(len(m) or (m.tail or "").strip() for m in direct)
    ):
        return ContextEvidence(context_id, problem="context_placement_unsupported")
    pairs = []
    for member in direct:
        ns = scopes.get(member, {})
        axis, key = _qname(member.get("dimension"), ns), _qname(member.text, ns)
        if axis is None or key is None:
            return ContextEvidence(context_id, problem="dimension_qname_unresolved")
        pairs.append((axis, key))
    return ContextEvidence(context_id, tuple(pairs))


@dataclass(frozen=True, slots=True)
class IssuerDeclaration:
    data_type: str | None
    group: str | None
    label: str | None


def issuer_declarations(
    schema: ET.Element, scopes: Scopes, labels: Mapping[str, str]
) -> dict[str, IssuerDeclaration]:
    """Record the filing's declarations; do not guess a missing prefix or fetch a DTS."""
    namespace = schema.get("targetNamespace")
    if schema.tag != _XS + "schema" or not namespace:
        return {}
    result = {}
    for declaration in schema.findall(_XS + "element"):
        if not (name := declaration.get("name")):
            continue
        key = "{" + namespace + "}" + name
        if key in result:
            result[key] = IssuerDeclaration(None, None, labels.get(key))
            continue
        ns = scopes.get(declaration, {})
        result[key] = IssuerDeclaration(
            _qname(declaration.get("type"), ns),
            _qname(declaration.get("substitutionGroup"), ns),
            labels.get(key),
        )
    return result


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
    context: ContextEvidence | None,
    *,
    issuer_members: Mapping[str, IssuerDeclaration],
    verified_annual_segment_note: Basis | None = None,
) -> Attribution:
    """Classify a finite candidate; no I/O, value conversion or v2 fallback.

    Inputs retain placement and the issuer declaration's scoped type and group.
    The optional note scope is
    independent evidence of the applicable annual segment note at this particular
    source occurrence, not a value match, context ID or filing-wide flag. Without it we do
    not apply the consolidation default to a segment observation.
    """
    profile = _FACTS.get(concept)
    if profile is None:
        namespace = concept.partition("}")[0] + "}"
        reason = "concept_unverified" if namespace in _STANDARD_NS else "taxonomy_unverified"
        return Attribution(None, reason)
    if context is None:
        return Attribution(None, "context_unavailable")
    if context.problem:
        return Attribution(None, context.problem)
    dimensions = context.pairs
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
            declaration = issuer_members[key]
            label = declaration.label or None
            if declaration.data_type not in {None, _DOMAIN_TYPE} or declaration.group not in {
                None,
                _I + "item",
            }:
                return Attribution(None, "issuer_declaration_not_member")
            if declaration.data_type is None or declaration.group is None:
                unverified = Segment(
                    key, "issuer_unverified", label, "issuer" if label else "missing"
                )
                return Attribution(None, "issuer_declaration_unresolved", unverified)
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
