"""保存提案の訂正・人間報告の純粋な契約。取引事実は含まない。"""

from typing import Any, Literal, TypedDict

from pydantic import AwareDatetime

from baibai_engine.investment_judgment.trade_proposal.contracts import (
    SHA256,
    NonEmptyStr,
    _ProposalValue,
)


class ProposalReference(_ProposalValue):
    proposal_id: NonEmptyStr
    draft_sha256: SHA256


class ProposalCorrectionDraft(_ProposalValue):
    correction_id: NonEmptyStr
    erroneous_ref: ProposalReference
    replacement_ref: ProposalReference
    reason: NonEmptyStr


class ProposalCorrection(ProposalCorrectionDraft):
    recorded_at: AwareDatetime


class HumanProposalReportDraft(_ProposalValue):
    report_id: NonEmptyStr
    proposal_ref: ProposalReference
    outcome: Literal["not_purchased"]
    statement: NonEmptyStr
    reported_at: AwareDatetime
    subject_at: AwareDatetime | None = None
    reason_quote: NonEmptyStr | None = None
    context_summary: NonEmptyStr | None = None


class HumanProposalReport(HumanProposalReportDraft):
    recorded_at: AwareDatetime


class ProposalRecords(TypedDict):
    corrections: list[dict[str, Any]]
    human_reports: list[dict[str, Any]]
