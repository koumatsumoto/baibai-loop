"""Resolve each XLink arc using its own labelLink and all matching endpoints."""

import pytest
from tests.helpers.edinet_research_sources import FIXTURES, facts_at_test_clock

from baibai_engine.machine_analysis.observations.financials.edinet_facts.xbrl import (
    SourceFormatError,
)


def _locator(member, endpoint):
    fragment = f"jpcrp030000-asr_E00197-000_{member}ReportableSegmentsMember"
    return (
        f'<link:loc xlink:type="locator" xlink:href="S100YR5P.xsd#{fragment}" '
        f'xlink:label="{endpoint}"/>'
    )


def _label(name, *, lang="ja", role="label"):
    return (
        f'<link:label xml:lang="{lang}" xlink:type="resource" xlink:label="n" '
        f'xlink:role="http://www.xbrl.org/2003/role/{role}">{name}</link:label>'
    )


def _link(source, *endpoints):
    return (
        '<link:labelLink xlink:type="extended" xlink:role="http://www.xbrl.org/2003/role/link">'
        + "".join(endpoints)
        + f'<link:labelArc xlink:type="arc" xlink:from="{source}" xlink:to="n" '
        + 'xlink:arcrole="http://www.xbrl.org/2003/arcrole/concept-label"/></link:labelLink>'
    )


def _with_labels(*links):
    # Keep the retained issuer's instance, schema, monetary facts and members.
    original = (FIXTURES / "S100YR5P_lab.xml").read_text()
    linkbase = (
        '<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">' + "".join(links) + "</link:linkbase>"
    )
    return facts_at_test_clock(replace={original: linkbase})


def _current_profit_names(facts):
    return {
        row.value: row.segment_name
        for row in facts.segments
        if row.segment_kind == "segment"
        and row.period_end == "2026-04-30"
        and row.metric == "segment_profit"
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_endpoint_reused_in_two_links_retains_independent_labels(reverse):
    links = [
        _link("s", _locator("Hotel", "s"), _label("ホテル事業")),
        _link("s", _locator("Housing", "s"), _label("住宅事業")),
    ]
    if reverse:
        links.reverse()
    actual = _with_labels(*links)
    assert _current_profit_names(actual) == {-373000000: "ホテル事業", 3331000000: "住宅事業"}
    assert actual == facts_at_test_clock()


@pytest.mark.parametrize("reverse", [False, True])
def test_arc_reaches_both_locators_with_same_endpoint_in_one_link(reverse):
    locators = [_locator(member, "s") for member in ("Hotel", "Housing")]
    if reverse:
        locators.reverse()
    actual = _with_labels(_link("s", *locators, _label("共通事業")))
    assert _current_profit_names(actual) == {-373000000: "共通事業", 3331000000: "共通事業"}


@pytest.mark.parametrize("reverse", [False, True])
def test_disagreeing_standard_japanese_labels_at_same_endpoint_fail_closed(reverse):
    resources = [_label("ホテル事業"), _label("別事業")]
    if reverse:
        resources.reverse()
    with pytest.raises(SourceFormatError, match=r"^member_label_conflict$"):
        _with_labels(_link("s", _locator("Hotel", "s"), *resources))


def test_identical_labels_at_same_endpoint_and_other_roles_are_not_conflicts():
    actual = _with_labels(
        _link("s", _locator("Housing", "s"), _label("住宅事業")),
        _link(
            "s",
            _locator("Hotel", "s"),
            _label("ホテル事業"),
            _label("Hotel", lang="en"),
            _label("ホテル事業"),
            _label("ホテル事業の詳細", role="verboseLabel"),
        ),
    )
    assert actual == facts_at_test_clock()
