"""Offline #2692 caller for one observed annual iXDS/JPY format; not a v2/v3 extractor."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from xml.etree import ElementTree as ET  # nosec B405 -- caller supplies parsed, pinned XML

from tools.experiments.edinet_research_dimensions import (
    Basis,
    Scopes,
    _qname,
    annual_segment_note_at,
)

_M = "{http://disclosure.edinet-fsa.go.jp/2013/manifest}"
_I = "{http://www.xbrl.org/2003/instance}"
_IX = "{http://www.xbrl.org/2008/inlineXBRL}"
_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
_JPY = "{http://www.xbrl.org/2003/iso4217}JPY"
_TRANSFORM = "{http://www.xbrl.org/inlineXBRL/transformation/2011-07-31}numdotdecimal"
_ANNUAL_ROLE = (
    "http://disclosure.edinet-fsa.go.jp/role/jpcrp/"
    "rol_CabinetOfficeOrdinanceOnDisclosureOfCorporateInformationEtcFormNo3AnnualSecuritiesReport"
)
type Part = tuple[ET.Element, Scopes]
type Key = tuple[str, str]
type Locator = tuple[str, tuple[int, ...]]


@dataclass(frozen=True, slots=True)
class SegmentAmountOccurrence:
    # The note at this occurrence is not the final consolidation basis.
    note: Basis
    locator: Locator
    yen: int | None  # None means explicit xsi:nil in BOTH source forms (not zero).


def _yen_units(elements: Iterable[tuple[ET.Element, Scopes]]) -> set[str]:
    seen: set[str] = set()
    verified: set[str] = set()
    for unit, scopes in elements:
        key = unit.get("id", "")
        if key in seen:
            verified.discard(key)
        elif (
            key
            and len(unit) == 1
            and unit[0].tag == _I + "measure"
            and not len(unit[0])
            and _qname(unit[0].text, scopes.get(unit[0], {})) == _JPY
        ):
            verified.add(key)
        seen.add(key)
    return verified


def _amount_matches(source: ET.Element, cell: ET.Element, scopes: Scopes) -> bool:
    """Exactly the signed thousands-of-yen and empty nil forms actually checked."""
    common = {"id", "contextRef", "unitRef", _NIL, "decimals"}
    if (
        len(source)
        or len(cell)
        or source.attrib.keys() - common
        or cell.attrib.keys() - (common | {"name", "format", "scale", "sign"})
    ):
        return False
    if _NIL in source.attrib or _NIL in cell.attrib:
        return (
            source.get(_NIL) == cell.get(_NIL) == "true"
            and source.get("decimals") is None
            and not (cell.attrib.keys() & {"format", "scale", "sign", "decimals"})
            and not (source.text or "").strip()
            and not (cell.text or "").strip()
        )
    raw, displayed = (source.text or "").strip(), (cell.text or "").strip()
    if (
        source.get("decimals") != "-3"
        or cell.get("decimals") != "-3"
        or _qname(cell.get("format"), scopes.get(cell, {})) != _TRANSFORM
        or cell.get("scale") != "3"
        or cell.get("sign") not in {None, "-"}
        or len(displayed) > 40
        or len(raw) > 40
        or re.fullmatch(r"(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)", displayed) is None
    ):
        return False
    magnitude = int(displayed.replace(",", "")) * 1000
    if magnitude == 0 and cell.get("sign") is not None:
        return False  # A negative-zero spelling is not a signed amount observed here.
    return raw == str(-magnitude if cell.get("sign") == "-" else magnitude)


def annual_segment_amount_occurrences(
    manifest: ET.Element,
    documents: Mapping[str, Part],
    candidates: set[Key],
) -> dict[Key, SegmentAmountOccurrence]:
    """No I/O. Caller pins one PublicDoc archive; rejects duplicate filenames before parsing.

    Return only a unique (concept, contextRef) in BOTH source forms; never break ties by
    note, value, document ordering, or an arbitrary locator. Both context references must
    be uniquely declared in the instance and its complete set of Inline files. This does not prove
    the context's entity, period, dimensions, the filing's taxonomy applicability or
    publication time. The enclosing archive's identity/integrity is the caller's boundary.
    """
    items = manifest.findall(f"{_M}tocComposition/{_M}item")
    groups = manifest.findall(f"{_M}list/{_M}instance")
    if manifest.tag != _M + "manifest" or len(items) != 1 or len(groups) != 1:
        return {}
    group, item = groups[0], items[0]
    instance = group.get("preferredFilename", "")
    names = [e.text or "" for e in group]
    if (
        group.get("type") != "PublicDoc"
        or not group.get("id")
        or item.get("ref") != group.get("id")
        or item.get("extrole") != _ANNUAL_ROLE
        or item.get("in") != "presentation"
        or any(e.tag != _M + "ixbrl" or len(e) for e in group)
        or not instance.endswith(".xbrl")
        or not names
        or len(set(names)) != len(names)
        or any(not n.endswith(".htm") for n in names)
        or any("/" in n or "\\" in n for n in (instance, *names))
        or set(documents) != {instance, *names}
    ):
        return {}
    root, scopes = documents[instance]
    if root.tag != _I + "xbrl":
        return {}
    units = _yen_units((u, scopes) for u in root.findall(_I + "unit"))
    instance_contexts = Counter(c.get("id") for c in root.findall(_I + "context"))
    ix_contexts: Counter[str | None] = Counter()
    unresolved = set()
    ix_units: list[tuple[ET.Element, Scopes]] = []
    source: dict[Key, ET.Element | None] = {}
    locations: dict[Key, tuple[Locator, ET.Element, str] | None] = {}
    for child in root:
        key = child.tag, child.get("contextRef", "")
        if key in candidates:
            source[key] = child if key not in source else None
    for name in names:
        page, scopes = documents[name]
        if page.tag != "{http://www.w3.org/1999/xhtml}html":
            return {}
        stack: list[tuple[ET.Element, tuple[int, ...], str]] = [(page, (), "")]
        while stack:
            node, path, enclosing_context = stack.pop()
            if node.tag.startswith(_IX) and "target" in node.attrib:
                return {}  # Do not pool target instances.
            if node.tag == _IX + "header":
                ix_contexts.update(c.get("id") for c in node.findall(f"{_IX}resources/{_I}context"))
                ix_units.extend((u, scopes) for u in node.findall(f"{_IX}resources/{_I}unit"))
            if node.tag == _IX + "nonNumeric":
                enclosing_context = node.get("contextRef", "")
            if node.tag in {_IX + "nonFraction", _IX + "fraction", _IX + "nonNumeric"}:
                lexical, ref = node.get("name", ""), node.get("contextRef", "")
                concept = _qname(lexical, scopes.get(node, {}))
                if concept is None:
                    unresolved.add((lexical.rsplit(":", 1)[-1], ref))
                elif (key := (concept, ref)) in candidates:
                    locations[key] = (
                        ((name, path), node, enclosing_context)
                        if key not in locations and node.tag == _IX + "nonFraction"
                        else None
                    )
            stack.extend((child, (*path, i), enclosing_context) for i, child in enumerate(node))
    units &= _yen_units(ix_units)
    result = {}
    for key, hit in locations.items():
        fact = source.get(key)
        if hit is None or fact is None:
            continue
        locator, cell, note_ref = hit
        if (key[0].rsplit("}", 1)[-1], key[1]) in unresolved or any(
            not ref or ix_contexts[ref] != 1 or instance_contexts[ref] != 1
            for ref in (key[1], note_ref)
        ):
            continue
        if fact.get("unitRef") not in units or fact.get("unitRef") != cell.get("unitRef"):
            continue
        if not _amount_matches(fact, cell, documents[locator[0]][1]):
            continue
        note = annual_segment_note_at(documents, locator, concept=key[0], context_id=key[1])
        if note is not None:
            result[key] = SegmentAmountOccurrence(
                note, locator, int(fact.text) if fact.text and fact.text.strip() else None
            )
    return result
