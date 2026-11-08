"""不変の提案と訂正・人間報告を読む。現在の入力や適用資格は再評価しない。"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from baibai_engine.investment_judgment.stock_assessment.store import (
    require_assessment_ref,
)
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ProposalConflictError,
    TradeProposal,
    proposal_draft_sha256,
)
from baibai_engine.investment_judgment.trade_proposal.record_contracts import (
    HumanProposalReport,
    ProposalCorrection,
    ProposalRecords,
    ProposalReference,
)


def has_published_proposal_for_operation(connection: sqlite3.Connection, operation_id: str) -> bool:
    """Operationへの参照束縛前も含め、公開済み提案の存在だけを確認する。"""
    return (
        connection.execute(
            "SELECT 1 FROM investment_proposal "
            "WHERE json_extract(payload, '$.research_scope.operation_id') = ? LIMIT 1",
            (operation_id,),
        ).fetchone()
        is not None
    )


def load_investment_proposal(connection: sqlite3.Connection, proposal_id: str) -> TradeProposal:
    """本文/hash/index/正確な評価pairを検証する。現在の資格やprogressは読まない。"""

    row = connection.execute(
        "SELECT as_of,prepared_at,published_at,request_scope,payload,draft_sha256 "
        "FROM investment_proposal WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise FileNotFoundError(f"investment proposal is unavailable: {proposal_id}")
    proposal = TradeProposal.model_validate_json(str(row[4]))
    draft_hash = proposal_draft_sha256(proposal)
    if (
        proposal.proposal_id != proposal_id
        or proposal.as_of.isoformat() != str(row[0])
        or proposal.prepared_at != datetime.fromisoformat(str(row[1]))
        or proposal.published_at != datetime.fromisoformat(str(row[2]))
        or proposal.request_scope != str(row[3])
        or draft_hash != str(row[5])
        or draft_hash != proposal.review.draft_sha256
    ):
        raise ProposalConflictError("stored proposal identity/hash differs from its payload")
    actual_items = connection.execute(
        "SELECT item_index,ticker,action,assessment_id FROM investment_proposal_item "
        "WHERE proposal_id=? ORDER BY item_index",
        (proposal_id,),
    ).fetchall()
    expected_items = [
        (
            index,
            item.ticker,
            item.action,
            None if item.assessment_ref is None else item.assessment_ref.assessment_id,
        )
        for index, item in enumerate(proposal.items)
    ]
    if [tuple(item) for item in actual_items] != expected_items:
        raise ProposalConflictError("stored proposal item index differs from its payload")
    for item in proposal.items:
        if item.assessment_ref is not None:
            pair = require_assessment_ref(connection, item.assessment_ref)
            if pair.document.input_snapshot.ticker != item.ticker:
                raise ProposalConflictError("stored proposal assessment ticker differs")
    for position in proposal.inputs.positions:
        if position.original_assessment_ref is not None:
            pair = require_assessment_ref(connection, position.original_assessment_ref)
            if pair.document.input_snapshot.ticker != position.ticker:
                raise ProposalConflictError("stored original assessment ticker differs")
    binding = connection.execute(
        "SELECT reference,ticker,side,origin,proposal_id,source_id,source_digest "
        "FROM position_decision_reference WHERE proposal_id=?",
        (proposal_id,),
    ).fetchall()
    expected_bindings = [
        (proposal_id, item.ticker, "buy", "proposal", proposal_id, proposal_id, draft_hash)
        for item in proposal.items
        if item.action == "buy" and item.terms is not None
    ]
    if [tuple(item) for item in binding] != expected_bindings:
        raise ProposalConflictError("stored proposal buy binding differs from its concrete item")
    return proposal


def load_latest_investment_proposal(connection: sqlite3.Connection) -> TradeProposal | None:
    """全scopeから最大の(as_of, aware published_at, ID)を読み、破損版を飛ばさない。"""

    # Remove only erroneous records from chronological selection. A correction does
    # not pin its replacement over a later, unrelated valid proposal.
    excluded = {r.erroneous_ref.proposal_id for r in load_corrections(connection)}
    rows = connection.execute(
        "SELECT proposal_id,published_at,as_of FROM investment_proposal"
    ).fetchall()
    rows = [row for row in rows if row[0] not in excluded]
    latest_day = max((row[2] for row in rows), default=None)
    rows = [row for row in rows if row[2] == latest_day]
    headers: list[tuple[datetime, str]] = []
    for row in rows:
        try:
            instant = datetime.fromisoformat(str(row[1]))
            if instant.utcoffset() is None:
                raise ValueError("stored publication time must include a timezone")
        except ValueError as error:
            raise ProposalConflictError("invalid stored proposal publication header") from error
        headers.append((instant, str(row[0])))
    # Compare aware instants in Python: textual offsets and SQLite julianday
    # respectively lose instant ordering and sub-millisecond precision.
    if not headers:
        return None
    return load_investment_proposal(connection, max(headers)[1])


def _require_ref(connection: sqlite3.Connection, reference: ProposalReference) -> TradeProposal:
    proposal = load_investment_proposal(connection, reference.proposal_id)
    if proposal_draft_sha256(proposal) != reference.draft_sha256:
        raise ProposalConflictError("proposal record exact reference differs")
    return proposal


def _rank(proposal: TradeProposal) -> tuple[date, datetime, str]:
    return proposal.as_of, proposal.published_at, proposal.proposal_id


def load_corrections(connection: sqlite3.Connection) -> tuple[ProposalCorrection, ...]:
    rows = connection.execute(
        "SELECT correction_id,erroneous_proposal_id,replacement_proposal_id,recorded_at,payload "
        "FROM proposal_correction ORDER BY correction_id"
    ).fetchall()
    records = []
    for row in rows:
        record = ProposalCorrection.model_validate_json(row[4])
        if (
            record.correction_id != row[0]
            or record.erroneous_ref.proposal_id != row[1]
            or record.replacement_ref.proposal_id != row[2]
            or record.recorded_at != datetime.fromisoformat(row[3])
        ):
            raise ProposalConflictError("stored proposal correction header differs")
        erroneous = _require_ref(connection, record.erroneous_ref)
        replacement = _require_ref(connection, record.replacement_ref)
        if _rank(replacement) >= _rank(erroneous) or record.recorded_at < erroneous.published_at:
            raise ProposalConflictError("correction must reference an earlier saved proposal")
        records.append(record)
    erroneous_ids = [record.erroneous_ref.proposal_id for record in records]
    replacement_ids = {record.replacement_ref.proposal_id for record in records}
    if len(set(erroneous_ids)) != len(erroneous_ids) or set(erroneous_ids) & replacement_ids:
        raise ProposalConflictError("duplicate or chained proposal correction")
    return tuple(records)


def proposal_records(connection: sqlite3.Connection, proposal_id: str) -> ProposalRecords:
    """Exact reads keep erroneous originals readable, with separate record provenance."""
    corrections = load_corrections(connection)
    reports = []
    for row in connection.execute(
        "SELECT report_id,recorded_at,payload FROM proposal_human_report WHERE proposal_id=?",
        (proposal_id,),
    ):
        report = HumanProposalReport.model_validate_json(row[2])
        proposal = _require_ref(connection, report.proposal_ref)
        if (
            report.report_id != row[0]
            or report.proposal_ref.proposal_id != proposal_id
            or report.recorded_at != datetime.fromisoformat(row[1])
            or not proposal.published_at <= report.reported_at <= report.recorded_at
        ):
            raise ProposalConflictError("stored human report identity or time differs")
        reports.append(report)
    return {
        "corrections": [
            record.model_dump(mode="json")
            for record in corrections
            if proposal_id in (record.erroneous_ref.proposal_id, record.replacement_ref.proposal_id)
        ],
        "human_reports": [
            report.model_dump(mode="json")
            for report in sorted(reports, key=lambda report: (report.recorded_at, report.report_id))
        ],
    }
