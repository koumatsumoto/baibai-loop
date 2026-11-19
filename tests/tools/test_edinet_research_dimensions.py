"""#2692 ローカル設計対照。提出時刻はsource fixture helperの試験時計で、実提出時刻ではない。"""

import hashlib
import io
import json
import zipfile
from collections import Counter
from dataclasses import replace
from xml.etree import ElementTree as ET

import pytest
from tests.helpers.edinet_research_sources import facts_at_test_clock, source_zip
from tools.experiments.edinet_research_dimensions import (
    Attribution,
    ContextEvidence,
    IssuerDeclaration,
    Segment,
    classify,
    consolidated_note_at,
    issuer_declarations,
    observe_context,
)

from baibai_engine.machine_analysis.observations.financials.edinet_facts.debt import debt_facts
from baibai_engine.machine_analysis.observations.financials.edinet_facts.extract import (
    EXTRACTOR_REVISION,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.models import (
    filing_identity,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.xbrl import (
    read_instance,
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


_DTR = "http://www.xbrl.org/dtr/type/non-numeric"
_I = "http://www.xbrl.org/2003/instance"
_DI = "http://xbrl.org/2006/xbrldi"
# The real excerpt lost xmlns:nonnum. Use a different, bound prefix ONLY as a synthetic positive.
_BOUND_TYPE = {
    "<xs:schema ": f'<xs:schema xmlns:nn="{_DTR}" ',
    "nonnum:domainItemType": "nn:domainItemType",
}


def _scoped(raw):
    # Only the small versioned fixtures and locally constructed controls enter this helper.
    parser = ET.iterparse(io.BytesIO(raw), events=("start-ns", "start", "end"))
    scopes, new, stack = {}, {}, [{}]
    for event, obj in parser:
        if event == "start-ns":
            prefix, uri = obj
            new[prefix] = uri
        elif event == "start":
            stack.append(stack[-1] | new if new else stack[-1])
            scopes[obj] = stack[-1]
            new = {}
        else:
            stack.pop()
    return parser.root, scopes


def _source(doc, replacements=None):
    content = source_zip(doc, replacements)
    labels = read_instance(content).labels
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        xsd, xs_scopes = _scoped(z.read(f"XBRL/PublicDoc/{doc}.xsd"))
        xml, scopes = _scoped(z.read(f"XBRL/PublicDoc/{doc}.xbrl"))
    return {
        c.get("id"): observe_context(c, scopes) for c in xml.findall("{" + _I + "}context")
    }, issuer_declarations(xsd, xs_scopes, labels)


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
    contexts, issuers = _source(doc)
    counts = Counter()
    for row in v2.segments:
        dimensions = contexts[row.source_context]
        actual = classify(row.source_element, dimensions, issuer_members=issuers)
        # All four excerpts omit the issuer definition linkbase. Do not manufacture note scope.
        assert actual.basis is None
        member = actual.member
        assert member is not None
        assert member.key == row.segment_key
        if member.kind == "issuer_unverified":
            assert actual.evidence_or_reason == "issuer_declaration_unresolved"
            assert member.label_source == "issuer"
            assert member.label == row.segment_name
        else:
            assert actual.evidence_or_reason == "segment_note_scope_unverified"
            assert member.label_source == "standard"
            assert member.kind == row.segment_kind
        counts[member.kind] += 1
        counts["display_label_changes"] += member.label != row.segment_name
        # Conditional control, not evidence of the note in the truncated filing.
        conditional = classify(
            row.source_element,
            dimensions,
            issuer_members=issuers,
            verified_annual_segment_note="consolidated",
        )
        assert conditional == (
            actual
            if member.kind == "issuer_unverified"
            else Attribution("consolidated", "applicable_note_default", member)
        )
        assert dimensions.context_id == row.source_context
    assert counts["issuer_unverified"] == n_issuer
    assert counts["issuer_reported"] == 0
    assert counts["display_label_changes"] == n_label_changes
    for row in v2.debt:
        assert classify(
            row.source_element,
            contexts[row.source_context],
            issuer_members=issuers,
        ) == Attribution("consolidated", "consolidated_annex_concept")


def _housing(replacements=None):
    contexts, issuers = _source("S100YR5P", replacements)
    facts = facts_at_test_clock("S100YR5P", replacements)
    return [
        (r, contexts[r.source_context], issuers)
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
    rows = _housing({**_BOUND_TYPE, "HousingReportableSegmentsMember": local})
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
    contexts, issuers = _source("S100YR5P", modified_axis)
    v2 = facts_at_test_clock("S100YR5P", modified_axis)
    assert len(v2.segments) == 40
    assert v2.debt == facts_at_test_clock("S100YR5P").debt
    for row in v2.segments:
        assert classify(
            row.source_element,
            contexts[row.source_context],
            issuer_members=issuers,
        ) == Attribution(None, "dimension_unsupported")
    # The same archive's unrelated debt contexts remain usable.
    for row in v2.debt:
        assert (
            classify(
                row.source_element,
                contexts[row.source_context],
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
        obs = ContextEvidence("synthetic", dims) if dims is not None else None
        return classify(item, obs, issuer_members={}, verified_annual_segment_note=scope)

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
    synthetic = ContextEvidence("synthetic", seg)
    for concept, reason in (
        (item.replace(version, "2030-11-01"), "taxonomy_unverified"),
        (crp + "NetSales", "concept_unverified"),
        (
            crp + "AnnexedDetailedScheduleOfBorrowingsFinancialStatementsTextBlock",
            "concept_unverified",
        ),
    ):
        assert classify(concept, synthetic, issuer_members={}) == Attribution(None, reason)
    debt = crp + "AnnexedConsolidatedDetailedScheduleOfBorrowingsTextBlock"
    assert classify(debt, ContextEvidence("synthetic", single), issuer_members={}) == Attribution(
        None, "basis_contradicts_source"
    )
    assert classify(
        debt, ContextEvidence("synthetic", ((ca, "{urn:u}Unknown"),)), issuer_members={}
    ) == Attribution(None, "basis_member_unverified")
    unlabeled = classify(
        item,
        ContextEvidence("synthetic", ((sa, "{urn:this-issuer}OtherReportableSegmentsMember"),)),
        issuer_members={
            "{urn:this-issuer}OtherReportableSegmentsMember": IssuerDeclaration(
                "{" + _DTR + "}domainItemType", "{" + _I + "}item", None
            )
        },
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
    _, issuers = _source("S100YR5P")
    instance = read_instance(source_zip("S100YR5P"))
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
            r.source_element,
            ContextEvidence(r.source_context, contexts[r.source_context].dimensions),
            issuer_members=issuers,
        ) == Attribution(None, reason)


@pytest.mark.parametrize("under", ["entity_segment", "scenario_child"])
def test_unsupported_placement_is_not_absence_even_for_a_consolidated_annex(under):
    doc = "S100YR5P"
    row = facts_at_test_clock(doc).debt[0]
    with zipfile.ZipFile(io.BytesIO(source_zip(doc))) as z:
        xml, scopes = _scoped(z.read(f"XBRL/PublicDoc/{doc}.xbrl"))
    ctx = next(c for c in xml.findall("{" + _I + "}context") if c.get("id") == row.source_context)
    # Same actual annex: its original context has no explicit dimensions.
    valid = observe_context(ctx, scopes)
    assert classify(row.source_element, valid, issuer_members={}).basis == "consolidated"
    donor = next(xml.iter("{" + _DI + "}explicitMember"))
    if under == "entity_segment":
        entity = ctx.find("{" + _I + "}entity")
        target = ET.SubElement(entity, "{" + _I + "}segment")
    else:
        target = ET.SubElement(ET.SubElement(ctx, "{" + _I + "}scenario"), "{urn:other}container")
    target.append(donor)
    invalid = observe_context(ctx, scopes)
    assert invalid.context_id == row.source_context
    assert classify(row.source_element, invalid, issuer_members={}) == Attribution(
        None, "context_placement_unsupported"
    )


def test_moving_an_explicit_segment_from_scenario_to_entity_rejects_only_that_context():
    doc = "S100YR5P"
    v2 = facts_at_test_clock(doc)
    row = next(r for r in v2.segments if r.segment_name == "住宅事業")
    with zipfile.ZipFile(io.BytesIO(source_zip(doc, _BOUND_TYPE))) as z:
        xml, scopes = _scoped(z.read(f"XBRL/PublicDoc/{doc}.xbrl"))
    _, issuers = _source(doc, _BOUND_TYPE)
    contexts = {x.get("id"): x for x in xml.findall("{" + _I + "}context")}
    ctx = contexts[row.source_context]
    scenario = ctx.find("{" + _I + "}scenario")
    member = scenario.find("{" + _DI + "}explicitMember")
    saved = observe_context(ctx, scopes)
    assert (
        classify(
            row.source_element,
            saved,
            issuer_members=issuers,
            verified_annual_segment_note="consolidated",
        ).basis
        == "consolidated"
    )
    ctx.remove(scenario)
    ET.SubElement(ctx.find("{" + _I + "}entity"), "{" + _I + "}segment").append(member)
    scoped = observe_context(ctx, scopes)
    assert scoped.context_id == saved.context_id
    assert classify(
        row.source_element,
        scoped,
        issuer_members=issuers,
        verified_annual_segment_note="consolidated",
    ) == Attribution(None, "context_placement_unsupported")
    ann = v2.debt[0]
    assert (
        classify(
            ann.source_element,
            observe_context(contexts[ann.source_context], scopes),
            issuer_members=issuers,
        ).basis
        == "consolidated"
    )


def test_issuer_declaration_scopes_do_not_leak_to_a_sibling_or_turn_an_axis_into_a_member():
    doc = "S100YR5P"
    v2 = facts_at_test_clock(doc)
    housing = next(r for r in v2.segments if r.segment_name == "住宅事業")
    hotel = next(r for r in v2.segments if r.segment_name == "ホテル事業")
    # Only the hotel element gets a local binding for the lexical type in the excerpt.
    hotel_id = hotel.segment_key.split("}")[1]
    scoped_binding = {
        f'<xs:element id="jpcrp030000-asr_E00197-000_{hotel_id}"': f'<xs:element xmlns:nonnum="{_DTR}" id="jpcrp030000-asr_E00197-000_{hotel_id}"'
    }
    contexts, issuers = _source(doc, scoped_binding)

    def decision(row, declarations):
        return classify(
            row.source_element,
            contexts[row.source_context],
            issuer_members=declarations,
            verified_annual_segment_note="consolidated",
        )

    assert decision(housing, issuers).member == Segment(
        housing.segment_key, "issuer_unverified", "住宅事業", "issuer"
    )
    assert decision(hotel, issuers).member == Segment(
        hotel.segment_key, "issuer_reported", "ホテル事業", "issuer"
    )
    # A synthetic complete declaration works with an arbitrary prefix; changing only
    # Housing's declared type/group cannot promote it via its unchanged label.
    with zipfile.ZipFile(io.BytesIO(source_zip(doc, _BOUND_TYPE))) as z:
        xsd, scopes = _scoped(z.read(f"XBRL/PublicDoc/{doc}.xsd"))
    labels = {housing.segment_key: "住宅事業", hotel.segment_key: "ホテル事業"}
    decl = next(d for d in xsd if d.get("name") == housing.segment_key.split("}")[1])
    baseline = issuer_declarations(xsd, scopes, labels)
    assert decision(housing, baseline).basis == "consolidated"
    for attribute, wrong_value in (
        ("type", "xbrli:stringItemType"),
        ("type", "wrong:domainItemType"),
        ("substitutionGroup", "dim:dimensionItem"),
    ):
        correct = decl.get(attribute)
        decl.set(attribute, wrong_value)
        scopes[decl] = {
            **scopes[decl],
            "dim": "http://xbrl.org/2005/xbrldt",
            "wrong": "urn:not-dtr",
        }
        changed = issuer_declarations(xsd, scopes, labels)
        assert decision(housing, changed) == Attribution(None, "issuer_declaration_not_member")
        assert decision(hotel, changed) == decision(hotel, baseline)
        decl.set(attribute, correct)


def test_equal_inline_facts_do_not_share_their_parent_note():
    # Four synthetic pairs only. No filing, original amounts, list or external I/O.
    base = "http://disclosure.edinet-fsa.go.jp/taxonomy/"
    pfs = base + "jppfs/2025-11-01/jppfs_cor"
    crp = base + "jpcrp/2025-11-01/jpcrp_cor"
    note = "NotesSegmentInformationEtcConsolidatedFinancialStatementsTextBlock"
    keys = [(item, ctx) for ctx in ("A", "B") for item in ("NetSales", "OperatingIncome")]
    cells = "".join(
        f'<i:nonFraction name="p:{item}" contextRef="{ctx}" unitRef="JPY" decimals="0">'
        f"{100 + index}</i:nonFraction>"
        for index, (item, ctx) in enumerate(keys)
    )

    def page(section, end, note_ns=crp):
        return _scoped(
            (
                f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:n="{note_ns}" xmlns:p="{pfs}"'
                f' xmlns:i="http://www.xbrl.org/2008/inlineXBRL"><body>{section}<div>{cells}</div>'
                f"{end}</body></html>"
            ).encode()
        )

    inside = page(f'<i:nonNumeric name="n:{note}" contextRef="A">', "</i:nonNumeric>")
    outside = page("<section>", "</section>")
    parts = {"a.htm": outside, "b.htm": inside}
    synthetic_axis = (f"{{{crp}}}OperatingSegmentsAxis", f"{{{crp}}}ReportableSegmentsMember")
    for i, (item, ctx) in enumerate(keys):
        inner, outer = (part[0][0][0][0][i] for part in (inside, outside))
        assert inner.attrib == outer.attrib
        assert inner.text == outer.text
        concept = f"{{{pfs}}}{item}"
        path = (0, 0, 0, i)
        for filename, expected in (("a.htm", None), ("b.htm", "consolidated")):
            scope = consolidated_note_at(parts, (filename, path), concept=concept, context_id=ctx)
            assert scope == expected
            # The real duplicate pattern has NO segment axis. Even a true note is insufficient.
            assert classify(
                concept, ContextEvidence(ctx), issuer_members={}, verified_annual_segment_note=scope
            ) == Attribution(None, "no_explicit_segment")
            # Add a segment axis ONLY as a sensitivity control: borrowing the inner note would
            # now give the outer occurrence a false positive.
            candidate = classify(
                concept,
                ContextEvidence(ctx, (synthetic_axis,)),
                issuer_members={},
                verified_annual_segment_note=scope,
            )
            assert candidate.basis == expected

    sample = f"{{{pfs}}}NetSales"
    positive = ("b.htm", (0, 0, 0, 0))
    assert consolidated_note_at(parts, None, concept=sample, context_id="A") is None
    for ambiguous in (("missing.htm", positive[1]), ("b.htm", (0, 0, 0, -1)), ("b.htm", (0, 0))):
        assert consolidated_note_at(parts, ambiguous, concept=sample, context_id="A") is None
    assert consolidated_note_at(parts, positive, concept=sample, context_id="B") is None
    parts["b.htm"] = page(
        f'<i:nonNumeric name="n:{note}" contextRef="A">', "</i:nonNumeric>", "urn:homonym"
    )
    assert consolidated_note_at(parts, positive, concept=sample, context_id="A") is None
