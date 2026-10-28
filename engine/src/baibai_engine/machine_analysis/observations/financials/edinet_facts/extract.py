"""Pure extraction from a single immutable filing's XBRL ZIP."""

from __future__ import annotations

from baibai_engine.machine_analysis.observations.financials.edinet_facts.debt import debt_facts
from baibai_engine.machine_analysis.observations.financials.edinet_facts.models import ExtractedFacts, filing_identity
from baibai_engine.machine_analysis.observations.financials.edinet_facts.segments import segment_facts
from baibai_engine.machine_analysis.observations.financials.edinet_facts.xbrl import read_instance

# Increment when extraction semantics change. Separate from the existing metrics
# revision: research fact changes must not invalidate screening's financial baseline.
EXTRACTOR_REVISION = "edinet-research-facts-v2"


def extract_facts(*, content: bytes, ticker: str, doc_id: str, submitted: str) -> ExtractedFacts:
    identity = filing_identity(ticker, doc_id, submitted)
    instance = read_instance(content)
    segments, segment_reasons = segment_facts(instance, identity)
    debt, debt_reasons = debt_facts(instance, identity)
    return ExtractedFacts(segments, debt, segment_reasons, debt_reasons)
