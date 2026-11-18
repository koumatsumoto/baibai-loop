"""Direct, attributed share-count observations in retained EDINET holder filings.

A large holder's report is not an issuer attestation or proof of a change date.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from xml.etree.ElementTree import Element  # nosec B405 -- type annotation only
from zipfile import BadZipFile

from baibai_engine.machine_analysis.observations.financials.edinet_facts.xbrl import (
    XBRLI,
    Context,
    SourceFormatError,
    expand_qname,
    local_name,
    read_instance,
)
from baibai_engine.primitives.stocks import SecurityCodeError, parse_security_code_parts
from baibai_engine.primitives.time import JST

_NAMESPACE = re.compile(
    r"^\{http://disclosure\.edinet-fsa\.go\.jp/taxonomy/jplvh/\d{4}-\d{2}-\d{2}/jplvh_cor\}"
)


@dataclass(frozen=True, slots=True)
class HolderReportedCapital:
    ticker: str
    reference_date: date
    submitted_at: datetime
    document_id: str
    total_stocks: int
    original_sha256: str
    source_kind: str = "third_party_holder_report_not_issuer_attestation"


def extract_holder_capital(
    content: bytes, *, document_id: str, submitted_at: datetime, known_at: datetime
) -> HolderReportedCapital | None:
    """Read an unambiguous standard fact without treating it as a corporate action."""
    if submitted_at.utcoffset() is None or known_at.utcoffset() is None:
        raise ValueError("disclosure and knowledge timestamps require timezones")
    if submitted_at > known_at:
        return None
    try:
        instance = read_instance(content, include_labels=False)
    except BadZipFile as error:
        raise SourceFormatError("zip_member_invalid") from error
    share_units = set()
    for unit in instance.root.findall(f"{{{XBRLI}}}unit"):
        measures = unit.findall(f"{{{XBRLI}}}measure")
        if len(measures) != 1 or len(unit) != 1 or not measures[0].text:
            continue
        if expand_qname(measures[0].text, instance.namespaces) == f"{{{XBRLI}}}shares":
            share_units.add(unit.get("id", ""))
    facts: dict[str, list[tuple[Element, Context]]] = {}
    for element in instance.root:
        if not _NAMESPACE.match(element.tag):
            continue
        name = local_name(element.tag)
        if name not in {"SecurityCodeOfIssuer", "TotalNumberOfOutstandingStocksEtc"}:
            continue
        context = instance.contexts.get(element.get("contextRef", ""))
        if context is None or context.dimensions or context.period_start is not None:
            continue
        facts.setdefault(name, []).append((element, context))
    codes = facts.get("SecurityCodeOfIssuer", [])
    totals = facts.get("TotalNumberOfOutstandingStocksEtc", [])
    if len(codes) != 1 or len(totals) != 1:
        return None
    code, code_context = codes[0]
    total, total_context = totals[0]
    try:
        ticker, common_code = parse_security_code_parts(code.text)
    except SecurityCodeError:
        return None
    raw = (total.text or "").strip()
    if not common_code or code_context.period_end != total_context.period_end:
        return None
    reference = date.fromisoformat(total_context.period_end)
    if (
        reference > submitted_at.astimezone(JST).date()
        or not raw.isascii()
        or not raw.isdigit()
        or int(raw) <= 0
    ):
        return None
    if total.get("unitRef") not in share_units or total.get("decimals") not in {"0", "INF"}:
        return None
    return HolderReportedCapital(
        ticker, reference, submitted_at, document_id, int(raw), hashlib.sha256(content).hexdigest()
    )
