"""Read bounded EDINET instance/label bytes without fetching taxonomy resources."""

from __future__ import annotations

import io
import math
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET  # nosec B405 -- DTD/entities rejected in xml_root

XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"
XLINK = "http://www.w3.org/1999/xlink"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
MAX_XML_BYTES = 32 * 1024 * 1024
MAX_BUNDLE_BYTES = 96 * 1024 * 1024


class SourceFormatError(ValueError):
    """The supported source structure cannot establish an unambiguous fact."""


def local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def xml_root(content: bytes) -> tuple[ET.Element, dict[str, str]]:
    if len(content) > MAX_XML_BYTES:
        raise SourceFormatError("xml_size_limit")
    try:
        text = content.decode("utf-8-sig")
        if "\x00" in text:
            raise SourceFormatError("xml_encoding_unsupported")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
            raise SourceFormatError("xml_dtd_forbidden")
        namespaces: dict[str, str] = {}
        for _, (prefix, uri) in ET.iterparse(io.StringIO(text), events=("start-ns",)):  # nosec B314
            if prefix in namespaces and namespaces[prefix] != uri:
                raise SourceFormatError("namespace_rebinding_unsupported")
            namespaces[prefix] = uri
        return ET.fromstring(text), namespaces  # nosec B314 -- no DTD or entities
    except (UnicodeError, ET.ParseError) as exc:
        raise SourceFormatError("xml_invalid") from exc


def expand_qname(value: str, namespaces: dict[str, str]) -> str:
    prefix, separator, name = value.partition(":")
    if not separator or prefix not in namespaces:
        raise SourceFormatError("qname_namespace_missing")
    return "{" + namespaces[prefix] + "}" + name


@dataclass(frozen=True)
class Context:
    context_id: str
    issuer_id: str
    period_start: str | None
    period_end: str
    dimensions: tuple[tuple[str, str], ...]

    @property
    def consolidation_basis(self) -> str:
        # EDINET's consolidation axis defaults to consolidated. Explicit non-consolidated
        # members must never be pooled with that default, including segment contexts.
        return (
            "non_consolidated"
            if any(local_name(member) == "NonConsolidatedMember" for _, member in self.dimensions)
            else "consolidated"
        )


@dataclass(frozen=True)
class Instance:
    filename: str
    root: ET.Element
    contexts: dict[str, Context]
    units: dict[str, str]
    labels: dict[str, str]


def read_instance(content: bytes) -> Instance:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise SourceFormatError("zip_invalid") from exc
    with archive:
        selected = [
            info
            for info in archive.infolist()
            if "/PublicDoc/" in info.filename
            and info.filename.endswith((".xbrl", ".xsd", "_lab.xml"))
        ]
        if sum(info.file_size for info in selected) > MAX_BUNDLE_BYTES:
            raise SourceFormatError("zip_size_limit")
        instances = [info for info in selected if info.filename.endswith(".xbrl")]
        if len(instances) != 1:
            raise SourceFormatError("instance_count_unsupported")
        documents = {info.filename: xml_root(archive.read(info)) for info in selected}
    filename = instances[0].filename
    root, namespaces = documents[filename]
    contexts: dict[str, Context] = {}
    for element in root.findall(f"{{{XBRLI}}}context"):
        identifier = element.findtext(f"{{{XBRLI}}}entity/{{{XBRLI}}}identifier")
        period = element.find(f"{{{XBRLI}}}period")
        context_id = element.get("id", "")
        if not identifier or period is None or not context_id:
            raise SourceFormatError("context_identity_missing")
        if element.find(f".//{{{XBRLDI}}}typedMember") is not None:
            continue
        start = period.findtext(f"{{{XBRLI}}}startDate")
        end = period.findtext(f"{{{XBRLI}}}endDate") or period.findtext(f"{{{XBRLI}}}instant")
        if not end:
            continue
        try:
            date.fromisoformat(end)
            if start and date.fromisoformat(start) > date.fromisoformat(end):
                raise ValueError("period reversed")
        except ValueError as exc:
            raise SourceFormatError("context_period_invalid") from exc
        dimensions = tuple(
            sorted(
                (
                    expand_qname(member.get("dimension", ""), namespaces),
                    expand_qname(member.text or "", namespaces),
                )
                for member in element.findall(f".//{{{XBRLDI}}}explicitMember")
            )
        )
        context = Context(context_id, identifier, start, end, dimensions)
        if context_id in contexts and contexts[context_id] != context:
            raise SourceFormatError("context_conflict")
        contexts[context_id] = context
    units: dict[str, str] = {}
    for unit in root.findall(f"{{{XBRLI}}}unit"):
        measures = unit.findall(f"{{{XBRLI}}}measure")
        if len(measures) == 1 and len(unit) == 1:
            measure = expand_qname(measures[0].text or "", namespaces)
            if measure.startswith("{http://www.xbrl.org/2003/iso4217}"):
                units[unit.get("id", "")] = local_name(measure)
    return Instance(filename, root, contexts, units, _labels(documents))


def _labels(documents: dict[str, tuple[ET.Element, dict[str, str]]]) -> dict[str, str]:
    identities: dict[tuple[str, str], str] = {}
    for path, (root, _) in documents.items():
        if not path.endswith(".xsd"):
            continue
        namespace = root.get("targetNamespace", "")
        for element in root:
            if local_name(element.tag) == "element" and element.get("id") and element.get("name"):
                identities[(PurePosixPath(path).name, element.attrib["id"])] = (
                    "{" + namespace + "}" + element.attrib["name"]
                )
    result: dict[str, str] = {}
    for path, (root, _) in documents.items():
        if not path.endswith("_lab.xml"):
            continue
        locators: dict[str, str] = {}
        labels: dict[str, str] = {}
        for element in root.iter():
            label = element.get(f"{{{XLINK}}}label", "")
            if local_name(element.tag) == "loc":
                filename, _, fragment = element.get(f"{{{XLINK}}}href", "").partition("#")
                identity = identities.get((PurePosixPath(filename).name, fragment))
                if identity:
                    locators[label] = identity
            elif (
                local_name(element.tag) == "label"
                and element.get("{http://www.w3.org/XML/1998/namespace}lang") == "ja"
                and element.get(f"{{{XLINK}}}role") == "http://www.xbrl.org/2003/role/label"
            ):
                labels[label] = "".join(element.itertext()).strip()
        for arc in root.iter():
            if local_name(arc.tag) != "labelArc":
                continue
            identity = locators.get(arc.get(f"{{{XLINK}}}from", ""))
            label_text = labels.get(arc.get(f"{{{XLINK}}}to", ""))
            if identity and label_text:
                if identity in result and result[identity] != label_text:
                    raise SourceFormatError("member_label_conflict")
                result[identity] = label_text
    return result


def numeric_fact(element: ET.Element) -> float | None:
    if element.get(f"{{{XSI}}}nil") in {"true", "1"}:
        return None
    text = (element.text or "").strip()
    if not text:
        return None
    try:
        value = Decimal(text)
        result = float(value)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise SourceFormatError("numeric_fact_invalid") from exc
    if not math.isfinite(result):
        raise SourceFormatError("numeric_fact_nonfinite")
    return result
