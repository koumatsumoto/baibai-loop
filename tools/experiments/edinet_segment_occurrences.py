"""Offline #2692 caller for one observed annual iXDS/JPY format; not a v2/v3 extractor."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from functools import cache
from xml.etree import ElementTree as ET  # nosec B405 -- caller supplies parsed, pinned XML

from tools.experiments.edinet_research_dimensions import (
    Basis,
    ContextEvidence,
    Scopes,
    _qname,
    annual_segment_note_at,
    observe_context,
)

_M = "{http://disclosure.edinet-fsa.go.jp/2013/manifest}"
_I = "{http://www.xbrl.org/2003/instance}"
_IX = "{http://www.xbrl.org/2008/inlineXBRL}"
_ASSETS = "{http://disclosure.edinet-fsa.go.jp/taxonomy/jppfs/2025-11-01/jppfs_cor}Assets"
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
class AnnualReport:
    issuer_id: str
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class NativeContext:
    issuer_id: str
    start: date | None
    end: date
    dimensions: ContextEvidence


def _bounded(manifest: ET.Element, documents: Mapping[str, Part]) -> bool:
    # Check the pre-parsed trees with O(depth) scratch BEFORE constructing any child paths.
    remaining = 50_000
    for tree in (manifest, *(root for root, _ in documents.values())):
        stack = [iter((tree,))]
        while stack:
            node = next(stack[-1], None)
            if node is None:
                stack.pop()
                continue
            remaining -= 1
            if remaining < 0 or len(stack) > 32:
                return False
            if len(node):
                stack.append(iter(node))
    return True


def _native_context(part: Part | None, issuer_id: str) -> NativeContext | None:
    if part is None:
        return None
    node, scopes = part
    children = [c.tag for c in node]
    plain = [_I + "entity", _I + "period"]
    if children not in (plain, [*plain, _I + "scenario"]) or node.attrib.keys() != {"id"}:
        return None
    entity, period = node[:2]
    if (
        any(c.attrib or (c.text or "").strip() for c in node)
        or (node.text or "").strip()
        or any((c.tail or "").strip() for c in node.iter() if c is not node)
        or len(entity) != 1
        or entity[0].tag != _I + "identifier"
        or len(entity[0])
        or entity[0].attrib != {"scheme": "http://disclosure.edinet-fsa.go.jp"}
        or entity[0].text != issuer_id
        or any(c.attrib or len(c) for c in period)
        or any(m.attrib.keys() != {"dimension"} for m in node.findall(f"{_I}scenario/*"))
    ):
        return None
    periods = [c.tag for c in period]
    if periods not in ([_I + "instant"], [_I + "startDate", _I + "endDate"]):
        return None
    raw = [c.text or "" for c in period]
    try:
        dates = [date.fromisoformat(v) for v in raw]
    except ValueError:
        return None
    if raw != [d.isoformat() for d in dates] or dates[0] > dates[-1]:
        return None
    evidence = observe_context(node, scopes)
    if evidence.problem or len(dict(evidence.pairs)) != len(evidence.pairs):
        return None
    return NativeContext(
        issuer_id,
        dates[0] if len(dates) == 2 else None,
        dates[-1],
        ContextEvidence(evidence.context_id, tuple(sorted(evidence.pairs))),
    )


def _index_contexts(parts: list[Part]) -> dict[str, Part | None]:
    indexed: dict[str, Part | None] = {}
    for node, scopes in parts:
        key = node.get("id", "")
        indexed[key] = (node, scopes) if key not in indexed else None
    return indexed


@dataclass(frozen=True, slots=True)
class SegmentAmountOccurrence:
    # The note at this occurrence is not the final consolidation basis.
    note: Basis
    locator: Locator
    context: NativeContext
    note_context: NativeContext
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
    *,
    report: AnnualReport,
) -> dict[Key, SegmentAmountOccurrence]:
    """No I/O. Caller pins one PublicDoc archive; rejects duplicate filenames before parsing.

    Return only a unique (concept, contextRef) in BOTH source forms; never break ties by
    note, value, document ordering, or an arbitrary locator. Return the numeric context
    derived here, not one supplied by the consumer. The manifest, issuer and report dates
    must come from the same fixed filing. A match does not establish DTS membership, public
    availability at the observation time, or final segment/consolidation classification.
    """
    if (
        len(documents) > 33
        or len(candidates) > 128
        or report.start > report.end
        or not re.fullmatch(r"E[0-9]{5}-[0-9]{3}", report.issuer_id)
        or not _bounded(manifest, documents)
    ):
        return {}
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
    native_contexts = [(c, scopes) for c in root.findall(_I + "context")]
    inline_contexts: list[Part] = []
    if len(native_contexts) > 256:
        return {}
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
            if node.tag.startswith("{http://www.xbrl.org/2013/inlineXBRL}"):
                return {}  # A different Inline version could hide a second matching fact.
            if node.tag.startswith(_IX) and "target" in node.attrib:
                return {}  # Do not pool target instances.
            if node.tag == _IX + "header":
                inline_contexts.extend(
                    (c, scopes) for c in node.findall(f"{_IX}resources/{_I}context")
                )
                if len(inline_contexts) > 256:
                    return {}
                ix_units.extend((u, scopes) for u in node.findall(f"{_IX}resources/{_I}unit"))
            if node.tag == _IX + "nonNumeric":
                # Neither a nil note nor an unresolved continuation proves local note scope.
                enclosing_context = (
                    node.get("contextRef", "")
                    if node.attrib.keys() <= {"name", "contextRef", "id", "escape"}
                    and node.get("escape") in {None, "true"}
                    else ""
                )
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
    native, inline = _index_contexts(native_contexts), _index_contexts(inline_contexts)

    @cache
    def bound(ref: str) -> NativeContext | None:
        left = _native_context(native.get(ref), report.issuer_id)
        return (
            left
            if left is not None and left == _native_context(inline.get(ref), report.issuer_id)
            else None
        )

    result = {}
    for key, hit in locations.items():
        fact = source.get(key)
        if hit is None or fact is None:
            continue
        locator, cell, note_ref = hit
        ctx, note_ctx = bound(key[1]), bound(note_ref)
        if (
            (key[0].rsplit("}", 1)[-1], key[1]) in unresolved
            or ctx is None
            or note_ctx is None
            or (ctx.start is None) != (key[0] == _ASSETS)
            or not (
                (ctx.end == report.end and ctx.start in {None, report.start})
                or (report.start - ctx.end).days == 1  # no assumed prior-year duration
            )
            or (note_ctx.start, note_ctx.end) != (report.start, report.end)
            or note_ctx.dimensions.pairs
        ):
            continue
        if fact.get("unitRef") not in units or fact.get("unitRef") != cell.get("unitRef"):
            continue
        if not _amount_matches(fact, cell, documents[locator[0]][1]):
            continue
        note = annual_segment_note_at(documents, locator, concept=key[0], context_id=key[1])
        if note is not None:
            result[key] = SegmentAmountOccurrence(
                note,
                locator,
                ctx,
                note_ctx,
                int(fact.text) if fact.text and fact.text.strip() else None,
            )
    return result
