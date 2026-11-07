"""namespace検査と同じparseのtreeを使い、XMLの拒否条件と順序を保つ。"""

from xml.etree import ElementTree as ET

import pytest

from baibai_engine.machine_analysis.observations.financials.edinet_facts import xbrl


def test_namespace_scan_returns_its_completed_tree_without_parsing_again(monkeypatch):
    parsed = []
    original = ET.iterparse

    def parse(*args, **kwargs):
        result = original(*args, **kwargs)
        parsed.append(result)
        return result

    def no_second_parse(*args, **kwargs):
        pytest.fail("namespace scan already built the complete XML tree")

    monkeypatch.setattr(ET, "iterparse", parse)
    monkeypatch.setattr(ET, "fromstring", no_second_parse)
    root, namespaces = xbrl.xml_root(b'<r xmlns:p="urn:fact"><p:fact>12</p:fact></r>')
    assert len(parsed) == 1
    assert root is parsed[0].root
    assert [child.tag for child in root] == ["{urn:fact}fact"]
    assert root[0].text == "12"
    assert namespaces == {"p": "urn:fact"}


@pytest.mark.parametrize(
    ("content", "namespaces"),
    [
        (b"<r/>", {}),
        (b'<?xml version="1.0" encoding="UTF-8"?><r>text &amp; &#x65E5;</r>', {}),
        (b"\xef\xbb\xbf<r>\xe6\x97\xa5\xe6\x9c\xac</r>", {}),
        (b'<?xml version="1.0" encoding="ISO-8859-1"?><r>\xc3\xa9</r>', {}),
        (
            b'<r xmlns="urn:default" xmlns:p="urn:fact"><p:a/><b/></r>',
            {"": "urn:default", "p": "urn:fact"},
        ),
        (b'<r xmlns:p="urn:same"><a xmlns:p="urn:same"><p:b/></a></r>', {"p": "urn:same"}),
        (
            b'<!--before--><?start yes?><r a="1">head<!--inside--><a/>tail<?middle no?><b/></r><!--after-->',
            {},
        ),
        (
            (
                b'<r xmlns:x="urn:xbrl" xmlns:f="urn:fact"><f:amount contextRef="current" unitRef="JPY">12</f:amount>'
                b'<x:context id="current"><x:entity>issuer</x:entity></x:context><x:unit id="JPY">JPY</x:unit>'
                b'<f:amount contextRef="prior" unitRef="JPY">10</f:amount><x:context id="prior"/></r>'
            ),
            {"x": "urn:xbrl", "f": "urn:fact"},
        ),
    ],
)
def test_single_parse_preserves_tree_text_attributes_and_child_order(content, namespaces):
    expected = ET.fromstring(content.decode("utf-8-sig"))
    actual, actual_namespaces = xbrl.xml_root(content)
    assert ET.tostring(actual) == ET.tostring(expected)
    assert actual_namespaces == namespaces


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"", "xml_invalid"),
        (b" \n", "xml_invalid"),
        (b"<r>", "xml_invalid"),
        (b"<r/><after/>", "xml_invalid"),
        (b"<r>&unknown;</r>", "xml_invalid"),
        (b"\xff", "xml_invalid"),
        (b"<r>\x00</r>", "xml_encoding_unsupported"),
        (
            '<!DOCTYPE r [<!ENTITY a "expanded">]><r>&a;</r>'.encode("utf-16-le"),
            "xml_encoding_unsupported",
        ),
        (
            '<!DOCTYPE r [<!ENTITY a "expanded">]><r>&a;</r>'.encode("utf-16-be"),
            "xml_encoding_unsupported",
        ),
        (b'<!DOCTYPE r [<!ENTITY a "expanded">]><r>&a;</r>', "xml_dtd_forbidden"),
        (b'<!DOCTYPE r SYSTEM "https://example.invalid/external.dtd"><r/>', "xml_dtd_forbidden"),
        (b'<!ENTITY a SYSTEM "file:///never-read"><r/>', "xml_dtd_forbidden"),
        (b"<! doctype r><r/>", "xml_dtd_forbidden"),
        (
            b'<r xmlns:p="urn:first"><a xmlns:p="urn:second"/></r>',
            "namespace_rebinding_unsupported",
        ),
        (b'<r xmlns="urn:first"><a xmlns="urn:second"/></r>', "namespace_rebinding_unsupported"),
    ],
)
def test_single_parse_preserves_xml_rejection_reasons(content, reason):
    with pytest.raises(xbrl.SourceFormatError, match="^" + reason + "$"):
        xbrl.xml_root(content)


def test_single_parse_keeps_xml_byte_limit_before_decoding(monkeypatch):
    content = b"<r>" + b" " * 57 + b"</r>"
    monkeypatch.setattr(xbrl, "MAX_XML_BYTES", len(content))
    assert xbrl.xml_root(content)[0].tag == "r"
    with pytest.raises(xbrl.SourceFormatError, match=r"^xml_size_limit$"):
        xbrl.xml_root(content + b"\xff")
