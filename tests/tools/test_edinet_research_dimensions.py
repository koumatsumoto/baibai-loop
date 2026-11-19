"""#2692 ローカル設計対照。提出時刻はsource fixture helperの試験時計で、実提出時刻ではない。"""

import hashlib
import io
import json
import zipfile
from collections import Counter
from dataclasses import replace

import pytest
from tests.helpers.edinet_research_sources import facts_at_test_clock, source_zip
from tools.experiments.edinet_research_dimensions import Attribution, Segment, classify

from baibai_engine.machine_analysis.observations.financials.edinet_facts.debt import debt_facts
from baibai_engine.machine_analysis.observations.financials.edinet_facts.extract import (
    EXTRACTOR_REVISION,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.models import (
    filing_identity,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.xbrl import (
    read_instance,
    xml_root,
)

# Captured from unmodified v2 at b2e12514, not independent accounting evidence.
_BASELINE = {
    "S100XTM0": (30, 0, 12, 12, "940c646b1fa1a3feaa18b909f985b2d97ea5d8f72e14e4d134a9dbdbfe6056d9"),
    "S100YCUF": (
        40,
        11,
        16,
        16,
        "3a1cd326a7853df65fee0867eec1b5a0ea2b482e39c1416e5bdad052e8f67b41",
    ),
    "S100YJ24": (32, 2, 16, 16, "ca4f300765655b846f59a2a0ae8f6c33bb02a4572a29423385a2343d223a6616"),
    "S100YR5P": (
        40,
        11,
        16,
        16,
        "f15dd87343fa49ac334dfe23217da96bc7bc0550469a5c4790551af6d60bd31c",
    ),
}


def _source(doc, replacements=None):
    content = source_zip(doc, replacements)
    instance = read_instance(content)
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        xsd, _ = xml_root(z.read(f"XBRL/PublicDoc/{doc}.xsd"))
    namespace = xsd.attrib["targetNamespace"]
    qnames = {
        "{" + namespace + "}" + element.attrib["name"]
        for element in xsd.findall("{http://www.w3.org/2001/XMLSchema}element")
    }
    # A declaration is not a definition link proving membership in a dimension.
    return instance, {key: instance.labels.get(key) for key in qnames}


@pytest.mark.parametrize("doc", _BASELINE)
def test_original_filing_rows_and_unqualified_scope(doc):
    v2 = facts_at_test_clock(doc)
    groups = [[row.model_dump() for row in group] for group in (v2.segments, v2.debt)]
    payload = json.dumps(
        [*groups, v2.segment_reasons, v2.debt_reasons],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    n_seg, n_debt, n_issuer, n_label_changes, digest = _BASELINE[doc]
    assert EXTRACTOR_REVISION == "edinet-research-facts-v2"
    assert (len(v2.segments), len(v2.debt), hashlib.sha256(payload).hexdigest()) == (
        n_seg,
        n_debt,
        digest,
    )
    instance, issuers = _source(doc)
    counts = Counter()
    for row in v2.segments:
        dimensions = instance.contexts[row.source_context].dimensions
        actual = classify(row.source_element, dimensions, issuer_members=issuers)
        # All four excerpts omit the issuer definition linkbase. Do not manufacture note scope.
        assert actual.basis is None
        assert actual.evidence_or_reason == "segment_note_scope_unverified"
        member = actual.member
        assert member is not None
        assert member.key == row.segment_key
        if member.kind == "issuer_reported":
            assert member.label_source == "issuer"
            assert member.label == row.segment_name
        else:
            assert member.label_source == "standard"
            assert member.kind == row.segment_kind
        counts[member.kind] += 1
        counts["display_label_changes"] += member.label != row.segment_name
        # Conditional control, not evidence of the note in the truncated filing.
        assert classify(
            row.source_element,
            dimensions,
            issuer_members=issuers,
            verified_annual_segment_note="consolidated",
        ) == Attribution("consolidated", "applicable_note_default", member)
    assert counts["issuer_reported"] == n_issuer
    assert counts["display_label_changes"] == n_label_changes
    for row in v2.debt:
        assert classify(
            row.source_element,
            instance.contexts[row.source_context].dimensions,
            issuer_members=issuers,
        ) == Attribution("consolidated", "consolidated_annex_concept")


def _housing(replacements=None):
    instance, issuers = _source("S100YR5P", replacements)
    facts = facts_at_test_clock("S100YR5P", replacements)
    return [
        (r, instance.contexts[r.source_context].dimensions, issuers)
        for r in facts.segments
        if r.segment_name == "住宅事業"
    ]


@pytest.mark.parametrize(
    ("local", "v2_kind", "v2_basis"),
    [
        ("NonConsolidatedMember", "segment", "non_consolidated"),
        ("ReportableSegmentsMember", "total", "consolidated"),
    ],
)
def test_same_local_issuer_member_reproduces_v2_but_never_becomes_standard(
    local, v2_kind, v2_basis
):
    rows = _housing({"HousingReportableSegmentsMember": local})
    assert len(rows) == 8
    for row, dims, issuers in rows:
        assert (row.segment_kind, row.consolidation_basis) == (v2_kind, v2_basis)
        observed = classify(row.source_element, dims, issuer_members=issuers)
        assert observed == Attribution(
            None,
            "segment_note_scope_unverified",
            Segment(row.segment_key, "issuer_reported", "住宅事業", "issuer"),
        )


def test_renamed_prefix_is_equivalent_and_same_local_issuer_axis_is_not():
    original = _housing()
    aliased = _housing({"xmlns:jpcrp_cor=": "xmlns:alias=", "jpcrp_cor:": "alias:"})
    assert aliased == original
    modified_axis = {
        "jpcrp_cor:OperatingSegmentsAxis": "jpcrp030000-asr_E00197-000:OperatingSegmentsAxis"
    }
    instance, issuers = _source("S100YR5P", modified_axis)
    v2 = facts_at_test_clock("S100YR5P", modified_axis)
    assert len(v2.segments) == 40
    assert v2.debt == facts_at_test_clock("S100YR5P").debt
    for row in v2.segments:
        assert classify(
            row.source_element,
            instance.contexts[row.source_context].dimensions,
            issuer_members=issuers,
        ) == Attribution(None, "dimension_unsupported")
    # The same archive's unrelated debt contexts remain usable.
    for row in v2.debt:
        assert (
            classify(
                row.source_element,
                instance.contexts[row.source_context].dimensions,
                issuer_members=issuers,
            ).basis
            == "consolidated"
        )


@pytest.mark.parametrize("version", ["2024-11-01", "2025-11-01"])
def test_synthetic_note_applicability_and_ambiguous_dimensions(version):
    root = "http://disclosure.edinet-fsa.go.jp/taxonomy/"
    crp, pfs = ("{" + f"{root}{name}/{version}/{name}_cor" + "}" for name in ("jpcrp", "jppfs"))
    sa, ca = crp + "OperatingSegmentsAxis", pfs + "ConsolidatedOrNonConsolidatedAxis"
    # Synthetic attestations test conditional logic; none comes from the four filing excerpts.
    seg = ((sa, crp + "ReconcilingItemsMember"),)
    single = ((ca, pfs + "NonConsolidatedMember"),)
    item = pfs + "NetSales"
    member = Segment(crp + "ReconcilingItemsMember", "reconciliation", "調整項目", "standard")

    def evaluate(dims, scope=None):
        return classify(item, dims, issuer_members={}, verified_annual_segment_note=scope)

    assert evaluate(seg, "consolidated") == Attribution(
        "consolidated", "applicable_note_default", member
    )
    assert evaluate(seg + single, "non_consolidated") == Attribution(
        "non_consolidated", "explicit_pair", member
    )
    assert (
        evaluate(seg, "non_consolidated").evidence_or_reason == "explicit_non_consolidated_missing"
    )
    assert evaluate(seg + single, "consolidated").basis is None
    for dims, reason in (
        (None, "context_unavailable"),
        ((), "no_explicit_segment"),
        (seg + seg, "repeated_dimension"),
        ((*seg, (ca, "{urn:unrelated}NonConsolidatedMember")), "basis_member_unverified"),
        ((*seg, (ca, pfs + "ConsolidatedMember")), "explicit_dimension_default"),
        (
            (*seg, ("{urn:other}ConsolidatedOrNonConsolidatedAxis", pfs + "NonConsolidatedMember")),
            "dimension_unsupported",
        ),
        (((sa, pfs + "NonConsolidatedMember"),), "segment_member_unverified"),
        (((sa, crp + "TotalOfReportableSegmentsMember"),), "segment_member_unverified"),
        (((sa, crp + "EntityTotalMember"),), "explicit_dimension_default"),
        (((sa, "{urn:undeclared}Sales"),), "segment_member_unverified"),
    ):
        decision = evaluate(dims)
        assert decision.evidence_or_reason == reason
        assert decision.basis is None
    other_year = "2025-11-01" if version == "2024-11-01" else "2024-11-01"
    assert (
        evaluate(
            ((sa, crp.replace(version, other_year) + "ReconcilingItemsMember"),)
        ).evidence_or_reason
        == "segment_member_unverified"
    )
    assert (
        evaluate(((sa.replace(version, other_year), member.key),)).evidence_or_reason
        == "dimension_unsupported"
    )
    for local, kind, label in (
        ("ReportableSegmentsMember", "total", "報告セグメント"),
        ("TotalOfReportableSegmentsAndOthersMember", "total", "事業セグメント合計"),
        ("OtherReportableSegmentsMember", "other", "その他"),
        (
            "OperatingSegmentsNotIncludedInReportableSegmentsAndOtherRevenueGeneratingBusinessActivitiesMember",
            "other",
            "その他",
        ),
    ):
        key = crp + local
        assert evaluate(((sa, key),)).member == Segment(key, kind, label, "standard")
    for concept, reason in (
        (item.replace(version, "2030-11-01"), "taxonomy_unverified"),
        (crp + "NetSales", "concept_unverified"),
        (
            crp + "AnnexedDetailedScheduleOfBorrowingsFinancialStatementsTextBlock",
            "concept_unverified",
        ),
    ):
        assert classify(concept, seg, issuer_members={}) == Attribution(None, reason)
    debt = crp + "AnnexedConsolidatedDetailedScheduleOfBorrowingsTextBlock"
    assert classify(debt, single, issuer_members={}) == Attribution(
        None, "basis_contradicts_source"
    )
    assert classify(debt, ((ca, "{urn:u}Unknown"),), issuer_members={}) == Attribution(
        None, "basis_member_unverified"
    )
    unlabeled = classify(
        item,
        ((sa, "{urn:this-issuer}OtherReportableSegmentsMember"),),
        issuer_members={"{urn:this-issuer}OtherReportableSegmentsMember": None},
    )
    assert unlabeled.member == Segment(
        "{urn:this-issuer}OtherReportableSegmentsMember", "issuer_reported", None, "missing"
    )


@pytest.mark.parametrize(
    ("use_standard_axis", "old_basis", "reason"),
    [
        (False, "non_consolidated", "dimension_unsupported"),
        (True, "consolidated", "basis_member_unverified"),
    ],
)
def test_postparse_maturity_control_cannot_inherit_v2_basis(use_standard_axis, old_basis, reason):
    v2 = facts_at_test_clock()
    instance, issuers = _source("S100YR5P")
    standard = "{http://disclosure.edinet-fsa.go.jp/taxonomy/jppfs/2025-11-01/jppfs_cor}"
    axis = (standard if use_standard_axis else "{urn:issuer}") + "ConsolidatedOrNonConsolidatedAxis"
    key = "{urn:issuer}" + ("Unknown" if use_standard_axis else "NonConsolidatedMember")
    targeted = {row.source_context for row in v2.debt}
    contexts = {
        k: replace(v, dimensions=((axis, key),)) if k in targeted else v
        for k, v in instance.contexts.items()
    }
    modified = replace(instance, contexts=contexts)
    identity = filing_identity("1873", "S100YR5P", v2.debt[0].source_submit_datetime)
    old, _ = debt_facts(modified, identity)

    # Same source amounts, dates and locators; only the synthetic attribution changed.
    def comparable(rows):
        return {r.source_locator: r.model_dump(exclude={"consolidation_basis"}) for r in rows}

    assert len(old) == 11
    assert comparable(old) == comparable(v2.debt)
    assert {r.consolidation_basis for r in old} == {old_basis}
    for r in old:
        assert classify(
            r.source_element, contexts[r.source_context].dimensions, issuer_members=issuers
        ) == Attribution(None, reason)
